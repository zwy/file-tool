"""感知指纹：把一段视频压成可比较的数字序列。

## 用的是 dHash（difference hash）

对每一帧做两件事：

1. **缩到 9×8 灰度**，逐行比较相邻像素的大小，得到 64 位 —— 即 dHash。
   相同画面的 dHash 几乎一致；画面变一点点（重新编码、轻微压缩）
   只会有几个比特翻转。
2. **算与前一帧的平均绝对差（MAD）** —— 这条曲线在「广告切进正片」的
   那一帧会有一个尖峰，是**独立于模板**的边界证据。

为什么是 dHash 而不是精确哈希：实测同族的 ABF-094 与 ACHJ-038，
逐像素 md5 **第一帧就不同**（各自重新编码过），但 dHash 下连续 45 秒
全部匹配；跨族的视频匹配率是 0.00。精确哈希在这个场景下完全不可用。

## 为什么不用 PIL / numpy

帧数据由 ffmpeg 以 ``rawvideo`` 管道直接吐字节，用 ``bytes`` 就能切。
dHash 的 64 次像素比较、MAD 的一次求和，在纯 Python 里对「每视频 180 帧」
的量级完全够用 —— 实测单视频 1.5~7 秒，瓶颈在 ffmpeg 解码而不在这里。
少两个依赖，也少一处版本冲突。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from . import ffmpeg

# dHash 的标准尺寸：宽 9（比较 8 次相邻像素）、高 8
DHASH_W = 9
DHASH_H = 8

# 汉明距离容差。重新编码过的同一素材，dHash 通常差 0~3 位；
# 留到 6 是给「缩放/加台标」留的余量。实测同族 ≤6、跨族 ≫6，间隔很大。
DEFAULT_TOLERANCE = 6

# 平均亮度差容差（0~255 灰度）。
#
# **为什么光靠 dHash 不够**：dHash 只比较相邻像素的大小关系，
# 纯色画面的所有相邻像素都一样 → 哈希恒为 0。于是「纯红片头」和
# 「纯蓝片头」的 dHash 完全相同，匹配不上任何区别 —— 而实拍广告里
# 纯色画面（色块过场、纯色卡）并不罕见。
#
# 补一个平均亮度就能把这个洞堵上：红和蓝的平均亮度差得很远，
# 而重新编码不会明显改变平均亮度（实测同族差异 ≲ 3）。
BRIGHTNESS_TOLERANCE = 12.0

# 连续匹配至少要有这么多帧才算数，否则一张静止黑帧就能"匹配"任何模板
MIN_PREFIX_FRAMES = 3


@dataclass(frozen=True)
class FrameSignature:
    """一段视频的指纹。

    ``hashes[i]`` 是第 i 帧的 dHash；``mad[i]`` 是第 i 帧与前一帧的
    平均绝对差（``mad[0]`` 恒为 0）；``brightness[i]`` 是第 i 帧的
    平均亮度。``fps`` 决定帧号对应的时间。
    """

    hashes: List[int]
    mad: List[float]
    brightness: List[float] = field(default_factory=list)
    fps: float = 1.0

    def __len__(self) -> int:
        return len(self.hashes)

    @property
    def seconds(self) -> float:
        return len(self.hashes) / self.fps if self.fps else 0.0

    def segment(self, start_frame: int, end_frame: int) -> "FrameSignature":
        """截取一段（闭区间 start，含 end）。"""
        start = max(0, start_frame)
        end = min(len(self.hashes) - 1, end_frame)
        if end < start:
            return FrameSignature(hashes=[], mad=[], brightness=[], fps=self.fps)
        return FrameSignature(
            hashes=self.hashes[start:end + 1],
            mad=self.mad[start:end + 1],
            brightness=self.brightness[start:end + 1] if self.brightness else [],
            fps=self.fps,
        )


@dataclass(frozen=True)
class MatchResult:
    """一次模板匹配的量化结果。"""

    matched: bool
    offset: int = 0
    """模板在视频里的起始帧号（广告未必严格从第 0 帧开始）。"""
    prefix_len: int = 0
    """从模板起点开始**连续**匹配上的帧数 —— 它天然就是广告长度。"""
    coverage: float = 0.0
    """整条模板被命中的比例（不要求连续）。"""
    template_len: int = 0
    score: float = 0.0
    """综合得分 0~1，用于与阈值比较。"""

    @property
    def ad_seconds(self) -> float:
        """广告时长（秒）：偏移 + 连续匹配长度。"""
        return self.offset + self.prefix_len


def _pool_gray(frame: bytes, size: int) -> List[float]:
    """把 size×size 的灰度帧按比例池化到 DHASH_H×DHASH_W 的均值网格。"""
    row_edges = [(i * size) // DHASH_H for i in range(DHASH_H + 1)]
    col_edges = [(j * size) // DHASH_W for j in range(DHASH_W + 1)]
    grid: List[float] = []
    for r in range(DHASH_H):
        r0, r1 = row_edges[r], row_edges[r + 1]
        for c in range(DHASH_W):
            c0, c1 = col_edges[c], col_edges[c + 1]
            if r1 <= r0 or c1 <= c0:
                grid.append(0.0)
                continue
            total = 0
            count = 0
            for y in range(r0, r1):
                base = y * size
                for x in range(c0, c1):
                    total += frame[base + x]
                    count += 1
            grid.append(total / count if count else 0.0)
    return grid


def dhash(frame: bytes, size: int = ffmpeg.FRAME_SIZE) -> int:
    """单帧的 64 位 dHash。

    逐行比较相邻像素：左边更暗记 1。灰度图上任何整体亮度变化
    （加台标、换码率）几乎不会改变这些比较结果 —— 这正是我们要的稳定性。
    """
    grid = _pool_gray(frame, size)
    bits = 0
    for r in range(DHASH_H):
        base = r * DHASH_W
        for c in range(DHASH_W - 1):
            bits = (bits << 1) | (1 if grid[base + c] < grid[base + c + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    """两个哈希相差几位。"""
    return (a ^ b).bit_count()


def signature_from_raw(
    raw: bytes,
    fps: float = 1.0,
    size: int = ffmpeg.FRAME_SIZE,
) -> FrameSignature:
    """把 ``rawvideo`` 字节流转成指纹序列。"""
    frame_bytes = size * size
    count = len(raw) // frame_bytes
    hashes: List[int] = []
    brightness: List[float] = []
    prev: bytes = b""
    mad: List[float] = []
    for i in range(count):
        frame = raw[i * frame_bytes:(i + 1) * frame_bytes]
        hashes.append(dhash(frame, size))
        brightness.append(sum(frame) / frame_bytes)
        if prev:
            diff = sum(abs(a - b) for a, b in zip(frame, prev))
            mad.append(diff / frame_bytes)
        else:
            mad.append(0.0)
        prev = frame
    return FrameSignature(hashes=hashes, mad=mad, brightness=brightness, fps=fps)


def signature_of(
    path,
    seconds: float,
    fps: float = 1.0,
    size: int = ffmpeg.FRAME_SIZE,
) -> FrameSignature:
    """取片头的指纹。"""
    raw = ffmpeg.extract_gray_frames(path, seconds, fps=fps, size=size)
    return signature_from_raw(raw, fps=fps, size=size)


def segment_signature_of(
    path,
    start: float,
    seconds: float,
    fps: float = 1.0,
    size: int = ffmpeg.FRAME_SIZE,
) -> FrameSignature:
    """取任意区间的指纹（学习模板时用）。"""
    raw = ffmpeg.extract_segment_frames(path, start, seconds, fps=fps, size=size)
    return signature_from_raw(raw, fps=fps, size=size)


def match(
    template: Sequence[int],
    video: Sequence[int],
    tolerance: int = DEFAULT_TOLERANCE,
    max_offset: int = 3,
    template_brightness: Optional[Sequence[float]] = None,
    video_brightness: Optional[Sequence[float]] = None,
) -> MatchResult:
    """把一条模板指纹与视频片头做比对。

    模板假设出现在视频**开头**，但允许有 1~2 帧的错位（有些片头前面
    挂着半秒的黑场或站点 logo），所以在 ``max_offset`` 范围内试几个
    起点，取最好的那个。

    两个指标：

    - ``prefix_len``：从起点开始**连续**匹配上的帧数。它同时是
      「广告有多长」和「这段内容确实是这支广告」的证据。
    - ``coverage``：整条模板被命中的比例（允许中间断开）。
      用来区分「同一支广告」和「同一支广告的变体（换了几帧）」。

    给了亮度序列时，两帧要匹配**同时**满足：dHash 汉明距离 ≤ 容差
    **且**平均亮度差 ≤ :data:`BRIGHTNESS_TOLERANCE`。纯色画面的
    dHash 恒为 0，只靠它分不开「纯红」和「纯蓝」。
    """
    if not template or not video:
        return MatchResult(matched=False, template_len=len(template))

    use_brightness = bool(template_brightness) and bool(video_brightness)
    best = MatchResult(matched=False, template_len=len(template))
    limit = min(len(template), len(video))

    for offset in range(0, min(max_offset + 1, len(video))):
        usable = min(limit, len(video) - offset)
        if usable <= 0:
            continue

        prefix = 0
        matched = 0
        for j in range(usable):
            if not _frames_match(
                template[j], video[offset + j], tolerance,
                template_brightness[j] if use_brightness else None,
                video_brightness[offset + j] if use_brightness else None,
            ):
                continue
            matched += 1
            if j == prefix:
                prefix += 1

        coverage = matched / len(template)
        candidate = MatchResult(
            matched=False,
            offset=offset,
            prefix_len=prefix,
            coverage=coverage,
            template_len=len(template),
        )
        if _better(candidate, best):
            best = candidate

    best = MatchResult(
        matched=best.prefix_len >= MIN_PREFIX_FRAMES,
        offset=best.offset,
        prefix_len=best.prefix_len,
        coverage=best.coverage,
        template_len=best.template_len,
        score=score_of(best),
    )
    return best


def _frames_match(
    template_hash: int,
    video_hash: int,
    tolerance: int,
    template_brightness: Optional[float],
    video_brightness: Optional[float],
) -> bool:
    """两帧是否算同一画面。"""
    if hamming(template_hash, video_hash) > tolerance:
        return False
    if template_brightness is not None and video_brightness is not None:
        if abs(template_brightness - video_brightness) > BRIGHTNESS_TOLERANCE:
            return False
    return True


def _better(candidate: MatchResult, current: MatchResult) -> bool:
    """比较两次匹配谁更好：先看连续长度，再看覆盖率。"""
    if candidate.prefix_len != current.prefix_len:
        return candidate.prefix_len > current.prefix_len
    return candidate.coverage > current.coverage


def score_of(result: MatchResult) -> float:
    """把匹配结果折算成 0~1 的置信分。

    **按连续匹配的绝对帧数给分，而不是按比例。** 匹配上 27 帧就是
    匹配上 27 秒广告 —— 这是强证据，不管模板本身有多长。模板是 45 帧
    而这个视频只匹配了 27 帧（因为这支视频的广告确实短一些），
    仍然该给高分；而"匹配 3 帧"和"匹配 40 帧"绝不该同分。

    分档：

    - 整条模板都匹配上（P ≥ L）→ 0.95，直接可裁
    - 只匹配上一部分 → 0.45 + 0.5×比例，比例 0.6 得 0.75（进人工确认档）
    """
    if result.template_len <= 0 or result.prefix_len <= 0:
        return 0.0
    ratio = result.prefix_len / result.template_len
    if ratio >= 1.0:
        return 0.95
    return round(0.45 + 0.5 * ratio, 4)


def mad_spikes(
    mad: Sequence[float],
    threshold: float = 12.0,
) -> List[int]:
    """在帧间差异曲线里找「画面突变」的帧号。

    阈值 12 是按 8 bit 灰度（0~255）定的：广告切进正片通常有 20~60
    的突变，而同一支广告内部的镜头切换在 5~15 之间。

    **注意**：这个函数只用来「看」，不用来定边界 —— 实测族A 的片头
    内部就有五六个硬切，拿突变点当边界会切在广告中间。
    边界只认模板长度（人划的）。
    """
    spikes: List[int] = []
    for i in range(1, len(mad)):
        if mad[i] < threshold:
            continue
        left = mad[i - 1]
        right = mad[i + 1] if i + 1 < len(mad) else 0.0
        if mad[i] >= left and mad[i] >= right:
            spikes.append(i)
    return spikes
