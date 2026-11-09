"""判定层：给一个视频定「有没有广告、在哪切、能不能自动切」。

## 判定流程

```
probe ──▶ 文件坏了？ ──▶ SKIP
   │
   ▼
取片头指纹（走缓存）
   │
   ├─▶ 命中负样本？ ──▶ NONE（人工说过这里没广告）
   │
   ├─▶ 命中某个模板？
   │      ├─▶ 裁剪点 = 模板长度（人划出来的边界）
   │      ├─▶ 匹配长度只用来判断「是不是这支广告」
   │      ├─▶ 裁剪点处的画面差异用来佐证（硬切 = 可信）
   │      └─▶ 按置信度分档：CUT / REVIEW / NONE
   │
   └─▶ 没命中任何模板 ──▶ NONE，并进入「未知片头聚类」
```

## 为什么裁剪点不信「画面突变」

实测族A（ABF-094 / ACHJ-038 / AV-174 等十几个视频共享的片头）是一段
**两分钟级的连续推广片**，内部有五六个硬切：26.7s、30.3s、33.7s、
57.4s、104.8s……

任何「找最大突变点」「找第一个突变点」的启发式，都会切在广告**中间**。
实测族B（CLT-013C / DVDMS-930）倒是干净的一刀（27 秒处硬切），
但拿启发式去赌，遇到族A 就废了。

所以：**边界只信人划出来的模板长度**，启发式一律不参与定边界。
帧间差异曲线只用来**佐证** —— 裁剪点那里有硬切，说明模板记对了；
没有硬切，说明这条模板可能记错了，降到人工确认档。
"""
from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import cache, ffmpeg, fingerprint
from .models import AdBoundary, AdPlan, Cluster, Detection, TemplateBank, TemplateStatus, Verdict
from .templates import is_negative

# 视频扩展名。只认视频 —— 图片（jpg）和字幕不参与广告判定
VIDEO_EXTENSIONS = {
    ".mp4", ".mpg", ".mpeg", ".avi", ".wmv", ".mov", ".mkv", ".flv",
    ".ts", ".m4v", ".rmvb", ".webm", ".vob",
}

# 默认只看片头这么多秒。
#
# **为什么是 300 而不是 180**：实测「站方推广片」是一段**多卡片轮播**，
# 每张卡片 4~8 秒，从手机画面、二维码、到「票选前10大 最红女优」，
# 实测各变体的真实长度在 **190 ~ 235 秒**之间（早先按 180 秒的窗口
# 看拼图，只看到广告的前半段，把边界误判成 134~166 秒 ——
# 结果是切在广告中间，剪完还剩半段广告）。
#
# 窗口必须装得下**最长**的模板（还要留几帧的偏移余量），
# 否则判定层会认为「模板装不下」而不敢下刀。
DEFAULT_HEAD_SECONDS = 300.0

# 置信度分档
AUTO_CUT_THRESHOLD = 0.80
"""≥ 这个值且模板已被人工确认 —— 直接裁。"""
REVIEW_THRESHOLD = 0.55
"""≥ 这个值 —— 疑似有广告，进人工确认队列。"""

# 聚类时比对的公共前缀帧数
CLUSTER_PREFIX_FRAMES = 8


def _is_fork(name: str) -> bool:
    """是不是资源叉垃圾（不是真视频）。

    exFAT 这类非 HFS+ 卷上，macOS 会给每个文件留一个 ``._`` 前缀的
    资源叉副本。实测 P7000Z 上每个视频旁边都跟着一个 4 KB 的
    ``._ABF-094.mp4`` —— **扩展名还是 .mp4**，会被当成视频扫进来。

    它不是视频（4096 字节的垃圾），但文件名以点开头，扫到了也读不出
    东西，只会把「跳过」计数搞脏、拖慢扫描。
    """
    return name.startswith("._") or name in (".DS_Store", "Thumbs.db")


def _is_temp(name: str) -> bool:
    """是不是裁剪过程留下的临时文件。

    临时文件是 ``xxx.adcut-tmp.mp4`` —— 扩展名还是视频扩展名。
    裁剪过程中如果同时跑扫描，它会被当成一个待处理的视频，
    轻则白扫一遍（几百 MB 的垃圾），重则对着半成品动手。
    """
    return ".adcut-tmp" in name


def find_videos(root: Path, recursive: bool = True) -> List[Path]:
    """列出目录下的视频文件。"""
    found: List[Path] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                if _is_fork(name) or _is_temp(name):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    found.append(Path(dirpath) / name)
    else:
        for item in sorted(root.iterdir()):
            if item.is_file() and not _is_fork(item.name) and not _is_temp(item.name) \
                    and item.suffix.lower() in VIDEO_EXTENSIONS:
                found.append(item)
    return found


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _manual_boundary(
    boundaries: Optional[Dict[str, AdBoundary]],
    label: str,
) -> Optional[AdBoundary]:
    """按相对路径查人工核验边界。

    先精确匹配；扫的是子目录时（相对路径少了几级）退化成后缀匹配，
    但**只在唯一命中时才算数** —— 同名文件在库里不止一份时宁可不匹配。
    """
    if not boundaries:
        return None
    item = boundaries.get(label)
    if item is not None:
        return item
    suffix = "/" + label
    hits = [b for path, b in boundaries.items() if path.endswith(suffix)]
    if len(hits) == 1:
        return hits[0]
    return None


def detect_video(
    path: Path,
    bank: TemplateBank,
    root: Optional[Path] = None,
    head_seconds: float = DEFAULT_HEAD_SECONDS,
    tolerance: int = fingerprint.DEFAULT_TOLERANCE,
    fps: float = 1.0,
    size: int = ffmpeg.FRAME_SIZE,
    use_cache: bool = True,
    boundaries: Optional[Dict[str, AdBoundary]] = None,
) -> Detection:
    """判定单个视频。

    ``boundaries`` 是**人工核验过的逐文件边界**（相对路径 -> 记录）。
    命中表里有记录的文件时，裁剪点用表里的值，不用模板长度 ——
    模板记的是「一支广告有多长」，而站方推广片的卡片数每支片子都不一样。
    """
    label = _relative(path, root) if root else str(path)
    manual = _manual_boundary(boundaries, label)

    try:
        info = ffmpeg.probe(path)
    except ffmpeg.FFmpegError as exc:
        return Detection(path=label, verdict=Verdict.SKIP, reason=str(exc)[:200])

    if info.broken:
        return Detection(
            path=label,
            duration=info.duration,
            size=info.size,
            verdict=Verdict.SKIP,
            reason="文件读不出时长或没有视频轨（损坏或为空）",
        )

    try:
        if use_cache:
            signature = cache.get_or_compute(path, head_seconds, fps=fps, size=size)
        else:
            raw = ffmpeg.extract_gray_frames(path, head_seconds, fps=fps, size=size)
            signature = fingerprint.signature_from_raw(raw, fps=fps, size=size)
            cache.store(path, signature, seconds=head_seconds, size=size)
    except ffmpeg.FFmpegError as exc:
        return Detection(
            path=label, duration=info.duration, size=info.size,
            verdict=Verdict.SKIP, reason=str(exc)[:200],
        )

    if not signature.hashes:
        return Detection(
            path=label, duration=info.duration, size=info.size,
            verdict=Verdict.SKIP, reason="取不到片头帧",
        )

    negative = is_negative(
        bank, signature.hashes, tolerance=tolerance, brightness=signature.brightness,
    )
    if negative is not None:
        return Detection(
            path=label, duration=info.duration, size=info.size,
            verdict=Verdict.NONE,
            reason=f"命中负样本 {negative.id}（人工确认过这里没有广告）",
        )

    # ---- 模板匹配 ----
    #
    # 挑哪条模板，优先级：**整条匹配上 > 人工确认过 > 匹配得长 > 覆盖率高**。
    #
    # 「人工确认过」这一条是必要的：模板长度现在是**边界**，
    # 一条被人眼量过的模板和一个机器猜的模板都整条匹配上时，
    # 该信人眼那条 —— 机器猜的边界可能把片名卡也切进去。
    # （实测 AKID-103 整条匹配上了 227 秒的人工模板，
    #   也整条匹配上了 230 秒的机器模板 —— 按「匹配得长」会挑错。）
    best_id = ""
    best_label = ""
    best_result = fingerprint.MatchResult(matched=False)
    best_status = None
    for template in bank.templates:
        result = fingerprint.match(
            template.hashes, signature.hashes, tolerance=tolerance,
            template_brightness=template.brightness or None,
            video_brightness=signature.brightness or None,
        )
        if best_id == "" or _better_template(result, template.status, best_result, best_status):
            best_id = template.id
            best_label = template.label
            best_result = result
            best_status = template.status

    if not best_id or best_result.prefix_len < fingerprint.MIN_PREFIX_FRAMES:
        static_ratio = _static_ratio(signature.mad)
        # 这里**不**按人工边界裁。
        #
        # 人工边界记的是「原始文件里站方广告到哪为止」，
        # 只有在片头**还认得出这支广告**时才适用。
        # 要是连模板都匹配不上（广告已经不在片头了 —— 比如这支已经裁过了），
        # 还照着边界再裁一次，就是**把正片又砍掉一截**。
        #
        # 实测踩过：全量裁剪之后重跑一次，3 支被裁了两遍
        # （SVDVD-739 多砍 290 秒、DVMM-012 多砍 318 秒、STSK-108A 多砍 208 秒）。
        # 根因就是这条「模板失配也按人工边界裁」的兜底规则。
        #
        # 边界表里的 71 支全都靠模板匹配识别（最小 prefix = 5 帧），
        # 所以去掉这条兜底不影响任何一支的判定 —— 只是让工具变得幂等。
        note = ""
        if static_ratio >= 0.6:
            note = (
                f"开头 {static_ratio * 100:.0f}% 是几乎静止的画面，"
                f"可能是还没认识的片头卡 —— 用 sheet 看一眼确认"
            )
        return Detection(
            path=label, duration=info.duration, size=info.size,
            verdict=Verdict.NONE,
            confidence=0.0,
            static_ratio=round(static_ratio, 3),
            reason="片头没有匹配任何已知广告模板",
            note=note,
        )

    # ---- 裁剪点 ----
    #
    # 裁剪点取**模板的长度**，不是从匹配长度倒推的。
    #
    # 原因：实测族A 的片头是一段两分钟级的连续推广片，里面有五六个
    # 硬切（26.7s / 30.3s / 33.7s / 57.4s / 104.8s …）。片头内部就有
    # 大量画面突变，任何「找最大突变点」的启发式都会切在广告中间。
    #
    # 模板的长度是**人划出来的**，是唯一可信的边界。匹配长度只用来
    # 回答另一个问题：「这个视频的开头是不是这支广告」。
    template_len = best_result.template_len
    # 广告不一定从第 0 帧开始（前面可能挂着站点 logo、黑场）。
    # ``match()`` 里的 offset 就是模板起点在视频里的位置 ——
    # 裁剪点必须把这段偏移也算上，否则会切早几秒，把广告尾巴留在片头。
    offset = best_result.offset
    if offset + template_len > len(signature.hashes):
        # 片头窗口不够长，装不下整条模板 —— 不能判
        # 但人工核验过边界的文件不受窗口限制：边界是量出来的，不是猜的。
        if manual is not None:
            return Detection(
                path=label, duration=info.duration, size=info.size,
                verdict=Verdict.CUT,
                template_id=best_id,
                template_label=best_label,
                matched_frames=best_result.prefix_len,
                template_frames=template_len,
                coverage=round(best_result.coverage, 4),
                cut_at=round(manual.cut_at, 3),
                confidence=1.0,
                reason=(
                    f"匹配 {best_label or best_id}，片头窗口（{head_seconds:.0f}s）"
                    f"装不下模板；采用人工核验边界 {manual.cut_at:.0f}s"
                ),
            )
        return Detection(
            path=label, duration=info.duration, size=info.size,
            verdict=Verdict.REVIEW,
            template_id=best_id,
            template_label=best_label,
            matched_frames=best_result.prefix_len,
            template_frames=template_len,
            coverage=round(best_result.coverage, 4),
            reason=(
                f"匹配 {best_label or best_id}，但片头窗口（{head_seconds:.0f}s）"
                f"装不下 {template_len / fps:.1f}s 的模板，无法确定裁剪点"
            ),
        )

    cut_frame = offset + template_len
    cut_at = cut_frame / fps if fps else 0.0
    mad_value = signature.mad[cut_frame] if 0 <= cut_frame < len(signature.mad) else 0.0

    confidence, incomplete = compute_confidence(
        best_result, mad_value, best_status, template_len,
    )

    if confidence >= AUTO_CUT_THRESHOLD:
        verdict = Verdict.CUT
    elif confidence >= REVIEW_THRESHOLD:
        verdict = Verdict.REVIEW
    else:
        verdict = Verdict.NONE

    reason = (
        f"匹配 {best_label or best_id}：连续 {best_result.prefix_len}/{template_len} 帧"
        f"（{best_result.prefix_len / fps:.1f}s）"
    )
    if offset:
        reason += f"，广告从第 {offset} 秒开始"

    # ---- 人工核验边界优先 ----
    #
    # 模板长度是「一支广告有多长」，但站方推广片是多卡片轮播，
    # 每支片子插的卡片数不一样 —— 同一条模板下的成员实测从 291 到
    # 348 秒都有。按模板长度裁，短的要留下几十秒广告尾巴。
    #
    # 人工核验过的边界直接覆盖模板长度的推断值，并且不再受
    # 「硬切 / 无硬切」这个启发式的影响 —— 边界是量出来的。
    if manual is not None:
        old_cut = cut_at
        cut_at = manual.cut_at
        cut_frame = int(round(cut_at * fps)) if fps else cut_frame
        mad_value = signature.mad[cut_frame] if 0 <= cut_frame < len(signature.mad) else 0.0
        verdict = Verdict.CUT
        confidence = 1.0
        reason = (
            f"匹配 {best_label or best_id}（连续 {best_result.prefix_len}/{template_len} 帧）；"
            f"采用人工核验边界 {cut_at:.0f}s"
            + (f"（模板长度推断为 {old_cut:.0f}s）" if abs(old_cut - cut_at) >= 1 else "")
        )
        if manual.note:
            reason += f"；{manual.note}"
        incomplete = False
    elif mad_value >= 12.0:
        reason += f"，裁剪点处画面差异 {mad_value:.0f}（硬切，可信）"
    else:
        reason += f"，裁剪点处画面差异仅 {mad_value:.0f}（无明显硬切，需人工确认）"
    if incomplete:
        reason += (
            f"；模板只对上了前 {best_result.prefix_len / fps:.0f}s，"
            f"这段视频的广告可能更长或更短，不能自动切"
        )

    detection = Detection(
        path=label,
        duration=info.duration,
        size=info.size,
        verdict=verdict,
        template_id=best_id,
        template_label=best_label,
        cut_at=round(cut_at, 3),
        confidence=round(confidence, 4),
        matched_frames=best_result.prefix_len,
        template_frames=template_len,
        coverage=round(best_result.coverage, 4),
        static_ratio=round(_static_ratio(signature.mad), 3),
        boundary_delta=0.0,
        mad_value=round(mad_value, 2),
        reason=reason,
    )
    detection.note = _subtitle_note(path)
    return detection


def _confirmed(status: Optional[TemplateStatus]) -> bool:
    """这条模板是不是被人眼确认过。"""
    return status is not None and status.value == "confirmed"


def _better_template(
    candidate: fingerprint.MatchResult,
    candidate_status: Optional[TemplateStatus],
    current: fingerprint.MatchResult,
    current_status: Optional[TemplateStatus],
) -> bool:
    """挑模板的优先级：整条匹配 > 人工确认 > 匹配得长 > 覆盖率高。

    为什么要「人工确认」排在「匹配得长」前面：模板长度就是**裁剪点**。
    一条被人眼量过边界的模板，和一条机器猜的模板，都整条匹配上同一个视频时，
    该信人眼那条 —— 机器猜的边界可能把片名卡也切进去。
    """
    candidate_full = candidate.prefix_len >= candidate.template_len > 0
    current_full = current.prefix_len >= current.template_len > 0
    if candidate_full != current_full:
        return candidate_full
    if _confirmed(candidate_status) != _confirmed(current_status):
        return _confirmed(candidate_status)
    if candidate.prefix_len != current.prefix_len:
        return candidate.prefix_len > current.prefix_len
    return candidate.coverage > current.coverage


def compute_confidence(
    result: fingerprint.MatchResult,
    mad_value: float,
    status: Optional[TemplateStatus] = None,
    template_len: int = 0,
) -> Tuple[float, bool]:
    """算出置信分，并说明这次匹配是不是「不完整」。

    返回 ``(置信分, 是否匹配不完整)``。

    ## 三道修正

    1. **裁剪点处的画面突变**：硬切（MAD ≥ 12）加 0.03 —— 独立佐证，
       说明模板记对了；几乎没变化（MAD < 4）减 0.12 —— 广告可能还没结束。

    2. **模板没被人工确认过**：封顶到人工确认档。机器自己认出来的模板，
       第一次见到不足以让后续视频自动开刀。

    3. **匹配不完整**（``prefix_len < template_len``）：封顶到人工确认档。
       裁剪点取的是**模板长度**，而模板的后半段在这个视频里没被验证过，
       只有两种可能：这支视频的广告比模板短（切过头，把正片也削了），
       或者比模板长（切不到底，广告还留着）。

       这条是实测踩出来的：公式 ``0.45 + 0.5×比例`` 给 105/160 这种
       比例算出 0.78，叠上硬切的 +0.03 正好 0.81，越过自动裁剪的线 ——
       而这几个视频的实际广告长度根本不是 160 秒，切下去会削掉
       最多 55 秒正片。**分数公式区分不出这两种情况，必须单独封顶。**
    """
    confidence = result.score
    if mad_value >= 12.0:
        # 边界处确实有画面硬切 —— 独立佐证
        confidence = min(0.99, confidence + 0.03)
    elif mad_value < 4.0:
        # 边界处几乎没变化 —— 广告可能还没结束，或者模板记错了
        confidence = max(0.0, confidence - 0.12)

    if status is not None and status.value != "confirmed":
        confidence = min(confidence, AUTO_CUT_THRESHOLD - 0.01)

    incomplete = template_len > 0 and result.prefix_len < template_len
    if incomplete:
        confidence = min(confidence, AUTO_CUT_THRESHOLD - 0.01)

    return confidence, incomplete


def _static_ratio(mad: Sequence[float], threshold: float = 2.0) -> float:
    """片头里「几乎不动」的画面占比。

    真实内容一直在动；片头卡（黑底字幕卡、作品介绍卡、logo 板）
    是静止的。所以这个比值能把「可能藏着广告但没认出来」的视频
    从一堆「无广告」里挑出来 —— 人工不用逐个拖进度条。

    阈值 2.0（8 bit 灰度）：相邻两帧平均差不到 2 就是肉眼看不出来的静止。
    """
    if len(mad) <= 1:
        return 1.0
    still = sum(1 for value in mad[1:] if value < threshold)
    return still / (len(mad) - 1)


def _subtitle_note(path: Path) -> str:
    """检查旁边有没有外挂字幕 —— 裁完视频字幕时间轴就对不上了。

    目前**不自动偏移**（R 库实测没有外挂字幕），但必须提醒：
    视频前 30 秒被切掉，字幕还按原来的时间轴走，内容会整体错位。
    """
    try:
        siblings = [
            item for item in path.parent.iterdir()
            if item.is_file()
            and item.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
            and item.stem == path.stem
        ]
    except OSError:
        return ""
    if siblings:
        names = ", ".join(item.name for item in siblings[:3])
        return f"存在外挂字幕（{names}），裁剪后字幕时间轴会错位，需要手动偏移"
    return ""


def scan_tree(
    root: Path,
    bank: TemplateBank,
    head_seconds: float = DEFAULT_HEAD_SECONDS,
    tolerance: int = fingerprint.DEFAULT_TOLERANCE,
    jobs: int = 4,
    limit: int = 0,
    recursive: bool = True,
    use_cache: bool = True,
    on_progress=None,
    boundaries: Optional[Dict[str, AdBoundary]] = None,
) -> AdPlan:
    """扫描整个目录，返回完整计划。

    ``boundaries`` 是人工核验过的逐文件边界（相对路径 -> 记录），
    命中时用它替代模板长度算出的裁剪点。
    """
    videos = find_videos(root, recursive=recursive)
    if limit:
        videos = videos[:limit]

    plan = AdPlan(root=str(root))

    def work(path: Path) -> Detection:
        return detect_video(
            path, bank, root=root, head_seconds=head_seconds,
            tolerance=tolerance, use_cache=use_cache, boundaries=boundaries,
        )

    workers = max(1, min(jobs, 8))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, path): path for path in videos}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            done += 1
            try:
                plan.detections.append(future.result())
            except Exception as exc:  # 单个文件出错不该让整批失败
                plan.errors.append(f"{_relative(path, root)}：{exc}")
            if on_progress:
                on_progress(done, len(videos), path)

    plan.detections.sort(key=lambda d: d.path)
    return plan


def cluster_unknown(
    plan: AdPlan,
    prefix_frames: int = CLUSTER_PREFIX_FRAMES,
    tolerance: int = fingerprint.DEFAULT_TOLERANCE,
    signatures: Optional[Dict[str, List[int]]] = None,
) -> List[Cluster]:
    """把「还没完全认出来」的视频按片头相似度聚成组。

    这是**把人工量从「视频数」压到「广告种类数」**的关键：
    同组的视频共享同一段片头，人工只要给其中一个划一刀，
    整组就都能自动处理了。

    候选分两类，**用不同的信号聚**：

    ``REVIEW``（匹配上了模板但只对上一部分）
        按 **``(模板, 匹配长度)``** 分组。匹配长度就是**变体的分叉点** ——
        实战中最常见的情况：族A 的推广片有多个版本，前 30 帧（或前 57 帧）
        一样，后面各自分叉，而分叉点正好落在推广片内部的镜头切换上
        （26.7s / 30.3s / 33.7s / 57.4s）。同一个分叉点 = 同一个变体，
        划一刀就够。

    ``NONE``（完全没匹配上任何模板）
        按片头**公共前缀**聚类。分组规则是「**加入公共前缀最长的那一组**」
        而不是「第一个能匹配上的组」—— 一个长变体同时匹配短组和长组，
        只有进最长那一组才对。

    注意这里**不能**按 ``note`` 过滤：``NONE`` 的 note 是「开头几乎静止，
    可能是没认识的片头卡」—— 这类视频恰恰是最该被聚出来的。
    """
    clusters: List[Cluster] = []

    # ---- REVIEW：按变体分叉点分组 ----
    review_groups: Dict[Tuple[str, int], List[str]] = {}
    for detection in plan.detections:
        if detection.verdict is not Verdict.REVIEW:
            continue
        key = (detection.template_id, detection.matched_frames)
        review_groups.setdefault(key, []).append(detection.path)

    for (template_id, frames), members in sorted(review_groups.items()):
        if len(members) < 2:
            continue
        clusters.append(Cluster(
            id="",
            members=sorted(members),
            frames=frames,
            representative=sorted(members)[0],
            note=f"模板 {template_id} 的变体（前 {frames} 帧相同，之后分叉）",
        ))

    # ---- NONE：按公共前缀聚类 ----
    #
    # 代表列表和分组列表必须**一一对应**。之前这两份列表是分开的
    # （``clusters`` 里还塞着前面 REVIEW 的组），结果 ``representatives``
    # 的下标和 ``clusters`` 的下标对不上，成员被挂进了错误的组 ——
    # 29 个毫无关系的视频被凑成了一组。
    if signatures:
        candidates = [
            d for d in plan.detections
            if d.verdict is Verdict.NONE
        ]
        none_clusters: List[Cluster] = []
        none_reps: List[List[int]] = []
        for detection in sorted(candidates, key=lambda d: d.path):
            hashes = signatures.get(detection.path)
            if not hashes or len(hashes) < prefix_frames:
                continue
            head = hashes[:prefix_frames]

            best_index = -1
            best_key = (0, 0)
            for index, rep in enumerate(none_reps):
                result = fingerprint.match(rep, head, tolerance=tolerance)
                if result.prefix_len < prefix_frames:
                    continue
                key = (result.prefix_len, len(rep))
                if key > best_key:
                    best_key = key
                    best_index = index

            if best_index >= 0:
                none_clusters[best_index].members.append(detection.path)
                continue

            none_clusters.append(Cluster(
                id="",
                members=[detection.path],
                frames=prefix_frames,
                representative=detection.path,
            ))
            none_reps.append(head)

        clusters.extend(none_clusters)

    # 只保留有 2 个以上成员的组 —— 单例不值得人工花时间
    result = [c for c in clusters if c.size >= 2]
    for number, cluster in enumerate(result, start=1):
        cluster.id = f"clu-{number:03d}"
        cluster.members.sort()
    return result


def collect_signatures(
    paths: Sequence[Path],
    root: Path,
    head_seconds: float = DEFAULT_HEAD_SECONDS,
    fps: float = 1.0,
    size: int = ffmpeg.FRAME_SIZE,
) -> Dict[str, List[int]]:
    """批量取指纹（供聚类用）。"""
    result: Dict[str, List[int]] = {}
    for path in paths:
        try:
            signature = cache.get_or_compute(path, head_seconds, fps=fps, size=size)
        except ffmpeg.FFmpegError:
            continue
        if signature.hashes:
            result[_relative(path, root)] = list(signature.hashes)
    return result
