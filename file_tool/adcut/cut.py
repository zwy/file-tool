"""裁剪执行：临时文件 → 校验 → 原子替换。

## 为什么要这么绕

磁盘只剩 168 GiB 而素材有几百 GB，**不能另存一份再删**。
但直接 ``ffmpeg -i 原文件 -c copy 输出文件`` 再删原片，中途失败
（磁盘满、播放器占用）就会两头都没了。

所以顺序必须是：

1. 裁到**同目录下的临时文件**（同卷 → rename 是原子的）
2. 校验临时文件（时长、流完整）
3. 校验通过才 ``os.replace`` 顶掉原片

任何一步失败：临时文件删掉，原片原封不动。

## 拷贝还是重编码

这是**耗时差一个数量级**的选择：

- **流拷贝**（``-c copy``）：不重新编码，画质无损，一个 10 GB 的片子几秒钟。
  代价是**只能切在关键帧上**。
- **重编码**：能切到任意帧，但要重新编 2~4 小时的 1080p，一个片子几十分钟。

实测这些片子的关键帧间隔是 8.34 秒（部分文件 2 秒）。关键帧落在
裁剪点附近时走拷贝，差太远才重编码 —— 而且差太远时**优先找裁剪点
之后的关键帧**（把广告切干净），而不是之前那个（会留一截广告尾巴）。

实测还发现一个好消息：**广告→正片的切换点经常正好是关键帧**
（CAWD-660CX 的 10.81 秒既是画面硬切也是关键帧）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from . import ffmpeg
from .models import CutOutcome

# 临时文件的名字里带这个标记，扫描时要躲开它
TEMP_MARK = ".adcut-tmp"


class CutError(RuntimeError):
    """裁剪失败。"""


def _container_policy(ext: str) -> Tuple[str, List[str], List[str]]:
    """按容器决定「拷贝参数」和「重编码参数」。

    返回 ``(容器族, copy_args, reencode_args)``。
    ``-movflags +faststart`` 只有 mp4 家族认，其它容器加了会直接报错。
    """
    ext = ext.lower()
    if ext in {".mp4", ".m4v", ".mov"}:
        return (
            "mp4",
            ["-c", "copy", "-movflags", "+faststart"],
            ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
        )
    if ext == ".mkv":
        return (
            "matroska",
            ["-c", "copy"],
            ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
             "-c:a", "aac", "-b:a", "192k"],
        )
    if ext == ".wmv":
        return (
            "asf",
            ["-c", "copy"],
            ["-c:v", "wmv2", "-c:a", "wmav2", "-b:a", "192k"],
        )
    if ext in {".avi"}:
        return (
            "avi",
            ["-c", "copy"],
            ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "mp3", "-b:a", "192k"],
        )
    if ext in {".mpg", ".mpeg", ".vob"}:
        return (
            "mpegts",
            ["-c", "copy"],
            ["-c:v", "mpeg2video", "-c:a", "mp2", "-b:a", "192k"],
        )
    if ext == ".flv":
        return (
            "flv",
            ["-c", "copy"],
            ["-c:v", "flv", "-c:a", "mp3", "-b:a", "192k"],
        )
    if ext == ".ts":
        return (
            "mpegts",
            ["-c", "copy"],
            ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
             "-c:a", "aac", "-b:a", "192k"],
        )
    # 未知容器：只敢拷贝
    return ("unknown", ["-c", "copy"], ["-c:v", "libx264", "-crf", "18", "-c:a", "aac"])


def plan_cut_point(
    cut_at: float,
    keyframes: List[float],
    max_snap: float = 2.0,
) -> Tuple[float, str, float]:
    """决定实际从哪儿切。

    返回 ``(实际切割秒数, 模式, 吸附偏移)``。模式是 ``copy`` 或 ``reencode``。

    优先级：

    1. 切割点**之后**最近的关键帧（距离 ≤ max_snap）—— 广告切得干干净净
    2. 切割点**之前**最近的关键帧（距离 ≤ max_snap）—— 会留一截广告尾巴，
       但比重编码快几十倍
    3. 都不满足 → 重编码，切在精确位置
    """
    if cut_at <= 0:
        raise CutError("裁剪点必须大于 0")

    after = [k for k in keyframes if k >= cut_at - 0.05]
    before = [k for k in keyframes if k < cut_at - 0.05]

    if after:
        target = after[0]
        if target - cut_at <= max_snap:
            return target, "copy", target - cut_at
    if before:
        target = before[-1]
        if cut_at - target <= max_snap:
            return target, "copy", target - cut_at
    return cut_at, "reencode", 0.0


def _temp_path(path: Path) -> Path:
    """临时文件放在原文件旁边 —— 必须同卷，rename 才是原子的。"""
    return path.with_name(f"{path.stem}{TEMP_MARK}{path.suffix}")


def _run_cut(
    source: Path,
    target: Path,
    cut_at: float,
    mode: str,
    copy_args: List[str],
    reencode_args: List[str],
    timeout: int = 7200,
) -> None:
    exe = ffmpeg.require_executable("ffmpeg")
    args = [
        exe, "-v", "error", "-y",
        "-ss", f"{cut_at:.3f}",
        "-i", str(source),
    ]
    if mode == "copy":
        args += ["-map", "0", "-dn", *copy_args]
    else:
        args += ["-map", "0:v", "-map", "0:a?", "-map", "0:s?", *reencode_args]
    args += [str(target)]

    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CutError(f"裁剪超时（{timeout}s）：{source}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        tail = "\n".join(err.splitlines()[-6:])
        raise CutError(f"ffmpeg 裁剪失败：{source}\n{tail}")


def _verify(
    target: Path,
    original_duration: float,
    cut_at: float,
    expect_audio: bool,
    tolerance: float = 2.5,
) -> str:
    """校验裁出来的文件是否完整。不合格就抛异常（保留原片）。"""
    if not target.exists():
        raise CutError("裁剪产物不存在")
    size = target.stat().st_size
    if size < 1024:
        raise CutError(f"裁剪产物只有 {size} 字节，明显不完整")

    info = ffmpeg.probe(target)
    if not info.has_video:
        raise CutError("裁剪产物没有视频轨")
    if expect_audio and not info.has_audio:
        raise CutError("原片有音频，裁剪产物却没有音频轨")
    if info.duration <= 0:
        raise CutError("裁剪产物读不出时长")

    expected = original_duration - cut_at
    # 容差：2.5 秒 + 原片的 1%（长片子的关键帧吸附会差几秒）
    allowed = tolerance + abs(expected) * 0.01
    delta = abs(info.duration - expected)
    if delta > allowed:
        raise CutError(
            f"裁剪产物时长异常：期望 {expected:.1f}s，实际 {info.duration:.1f}s"
            f"（差 {delta:.1f}s，超过容差 {allowed:.1f}s）"
        )
    return info.duration


def cut_video(
    path: Path,
    cut_at: float,
    max_snap: float = 2.0,
    accurate: bool = False,
    keep_original: bool = False,
    timeout: int = 7200,
) -> CutOutcome:
    """裁掉视频开头 ``cut_at`` 秒。

    ``accurate=True`` 强制重编码，切在精确帧上 —— 慢，但边界完全准。
    默认先试流拷贝（关键帧对齐），差太多才退回重编码。
    """
    outcome = CutOutcome(path=str(path), cut_at=cut_at)

    if not path.exists():
        outcome.reason = "文件不存在"
        return outcome

    info = ffmpeg.probe(path)
    if info.broken:
        outcome.reason = "文件读不出时长或没有视频轨"
        return outcome
    if cut_at >= info.duration - 1.0:
        outcome.reason = f"裁剪点 {cut_at:.1f}s 不合理（片长只有 {info.duration:.1f}s）"
        return outcome

    _family, copy_args, reencode_args = _container_policy(path.suffix)

    if accurate:
        real_cut, mode = cut_at, "reencode"
    else:
        try:
            keyframes = ffmpeg.keyframes(path, seconds=min(cut_at + 60.0, 600.0))
        except ffmpeg.FFmpegError:
            keyframes = []
        real_cut, mode, snap = plan_cut_point(cut_at, keyframes, max_snap=max_snap)
        outcome.snapped = round(snap, 3)

    outcome.mode = mode
    outcome.cut_at = round(real_cut, 3)
    outcome.before = info.duration

    temp = _temp_path(path)
    if temp.exists():
        try:
            temp.unlink()
        except OSError:
            pass

    try:
        _run_cut(path, temp, real_cut, mode, copy_args, reencode_args, timeout=timeout)
    except CutError as exc:
        _safe_unlink(temp)
        outcome.reason = str(exc)
        return outcome

    try:
        after = _verify(temp, info.duration, real_cut, expect_audio=info.has_audio)
    except (CutError, ffmpeg.FFmpegError) as exc:
        _safe_unlink(temp)
        outcome.reason = f"校验未通过，原片保留：{exc}"
        return outcome

    # ---- 顶替原片 ----
    try:
        shutil.copymode(path, temp)
    except OSError:
        pass  # 权限拿不到就算了，内容对就行
    try:
        if keep_original:
            backup = path.with_name(f"{path.stem}{TEMP_MARK}.orig{path.suffix}")
            if backup.exists():
                _safe_unlink(backup)
            os.replace(path, backup)
            os.replace(temp, path)
        else:
            os.replace(temp, path)
    except OSError as exc:
        _safe_unlink(temp)
        outcome.reason = f"替换失败，原片保留：{exc}"
        return outcome

    outcome.ok = True
    outcome.after = after
    outcome.saved_bytes = max(0, info.size - path.stat().st_size)
    outcome.reason = (
        f"已裁掉 {real_cut:.2f}s（{mode}）"
        + (f"，吸附偏移 {outcome.snapped:+.2f}s" if outcome.snapped else "")
    )
    return outcome


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def find_temps(root: Path, recursive: bool = True) -> List[Path]:
    """找出遗留的临时文件（上次裁剪中途失败留下的）。"""
    found: List[Path] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in filenames:
                if TEMP_MARK in name:
                    found.append(Path(dirpath) / name)
    else:
        for item in sorted(root.iterdir()):
            if item.is_file() and TEMP_MARK in item.name:
                found.append(item)
    return found


def contact_sheet(
    path: Path,
    at: float,
    span: float = 60.0,
    interval: float = 3.0,
    columns: int = 5,
    rows: int = 4,
    out: Optional[Path] = None,
) -> Path:
    """生成一张「时间轴拼图」，用来肉眼确认广告在哪结束。

    人工划刀时最耗时的是「拖进度条找边界」。拼图把候选区间切成
    一格格小图，一眼就能看出「广告卡」和「正片」的分界在哪一格。

    ffmpeg 这套构建**没有 drawtext**，烧不了时间戳，所以时间靠格数推算：
    第 1 格是 ``start``，之后每格 ``interval`` 秒。返回值里带上起点，
    调用方负责把「格号 ↔ 时间」的对照表打出来。
    """
    exe = ffmpeg.require_executable("ffmpeg")
    start = max(0.0, at - span / 2.0)
    frames = max(1, columns * rows)
    duration = frames * interval
    target = out or Path(f"{path.stem}-sheet.png")

    cmd = [
        exe, "-v", "error", "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(path),
        "-vf",
        f"fps=1/{interval},scale=320:-1,tile={columns}x{rows}",
        "-frames:v", "1",
        str(target),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise CutError(f"生成拼图超时：{path}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        tail = "\n".join(err.splitlines()[-4:])
        raise CutError(f"生成拼图失败：{path}\n{tail}")
    return target
