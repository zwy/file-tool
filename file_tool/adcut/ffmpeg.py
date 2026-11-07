"""ffmpeg / ffprobe 调用封装。

这个模块只做三件事，但每件都有坑：

1. **找得到可执行文件**
   macOS 上 ffmpeg 常常在 ``/opt/homebrew/bin``，Windows 上多半是
   ``C:\\ffmpeg\\bin``，而这些目录不一定在 ``PATH`` 里。
   找不到就报清楚「装在哪 / 怎么指定」，不要让调用方拿到一个空结果。

2. **喂进去的是原始帧，不是图片**
   指纹提取要的是「帧序列的数值」，不是「给人看的图」。所以走
   ``rawvideo`` 管道直接吃字节，既省磁盘又省一次编解码 ——
   ``-f rawvideo`` 输出的是解码后的裸像素，不做任何格式转换。

3. **超时要真的超时**
   10 GB 的视频一旦参数写错（比如 ``-t`` 位置搞反），ffmpeg 会一直跑。
   所有调用都带超时，超时按失败处理而不是让整个扫描卡死。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# 抽帧用的帧边长（正方形）。32 是折中：
# - 太小（8）时广告卡上的文字差异抹平了，同族/跨族分不开
# - 太大（128）时哈希对轻微重新编码过于敏感，反而不如 32 稳
FRAME_SIZE = 32

# 默认超时（秒）。取 180 秒片头 + 4K 解码，30 秒绰绰有余
DEFAULT_TIMEOUT = 120


class FFmpegError(RuntimeError):
    """ffmpeg / ffprobe 调用失败。"""


@dataclass(frozen=True)
class MediaInfo:
    """视频的基本信息（来自 ffprobe）。"""

    path: Path
    duration: float
    width: int = 0
    height: int = 0
    has_video: bool = False
    has_audio: bool = False
    vcodec: str = ""
    acodec: str = ""
    fps: float = 0.0
    size: int = 0

    @property
    def broken(self) -> bool:
        """文件是否明显损坏（读不出时长 / 没有视频轨）。

        实测库里存在 0 字节的 ``DVMM-055A.mp4`` —— 这种文件必须跳过，
        绝不能让它进裁剪流程。
        """
        return self.duration <= 0 or not self.has_video


def _candidate_dirs() -> List[str]:
    """ffmpeg 可能安装的位置，按优先级排列。"""
    dirs: List[str] = []
    env = os.environ.get("FILE_TOOL_FFMPEG_DIR")
    if env:
        dirs.append(env)
    if os.name == "nt":
        for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                dirs.append(os.path.join(base, "ffmpeg", "bin"))
        dirs.append(r"C:\ffmpeg\bin")
        dirs.append(r"C:\ProgramData\chocolatey\bin")
    else:
        dirs.extend([
            "/opt/homebrew/bin",       # Apple Silicon Homebrew
            "/usr/local/bin",          # Intel Homebrew / manual
            "/usr/bin",
            "/snap/bin",
        ])
    return dirs


def find_executable(name: str) -> Optional[str]:
    """找到可执行文件路径，找不到返回 ``None``。

    顺序：``FILE_TOOL_FFMPEG`` 环境变量 → ``PATH`` → 常见安装目录。
    """
    override = os.environ.get("FILE_TOOL_FFMPEG")
    if override and name == "ffmpeg" and Path(override).exists():
        return override
    override_dir = os.environ.get("FILE_TOOL_FFMPEG_DIR")
    if override_dir:
        candidate = Path(override_dir) / (name + ".exe" if os.name == "nt" else name)
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    for directory in _candidate_dirs():
        candidate = Path(directory) / (name + ".exe" if os.name == "nt" else name)
        if candidate.exists():
            return str(candidate)
    return None


def require_executable(name: str) -> str:
    """找不到就抛一条**带修复建议**的错误。"""
    path = find_executable(name)
    if path is None:
        raise FFmpegError(
            f"找不到 {name}。请安装 ffmpeg（macOS: brew install ffmpeg；"
            f"Windows: 下载 ffmpeg 并把 bin 目录加入 PATH），"
            f"或用环境变量 FILE_TOOL_FFMPEG_DIR 指定其目录。"
        )
    return path


def run(
    args: Sequence[str],
    timeout: int = DEFAULT_TIMEOUT,
    capture_stderr: bool = True,
) -> subprocess.CompletedProcess:
    """执行一个 ffmpeg/ffprobe 命令，失败时把 stderr 尾巴带进异常。"""
    exe = require_executable(Path(args[0]).name)
    cmd = [exe, *args[1:]]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{Path(args[0]).name} 超时（{timeout}s）：{' '.join(args[1:4])}") from exc

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() if capture_stderr else ""
        tail = "\n".join(err.strip().splitlines()[-6:])
        raise FFmpegError(
            f"{Path(args[0]).name} 退出码 {proc.returncode}：{' '.join(args[1:4])}\n{tail}"
        )
    return proc


def probe(path: Path, timeout: int = 60) -> MediaInfo:
    """读取视频的基本信息。

    读不出时长（0 字节文件、损坏文件）**不抛异常**，而是返回
    ``duration=0`` —— 调用方要把这类文件跳过，而不是让整个批次崩掉。
    """
    exe = require_executable("ffprobe")
    cmd = [
        exe, "-v", "error",
        "-show_entries", "format=duration,size",
        "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "default=nw=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffprobe 超时：{path}") from exc

    text = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        # 损坏文件：ffprobe 会报错，这里按「读不出信息」处理
        return MediaInfo(path=path, duration=0.0, size=_safe_size(path))

    duration = 0.0
    size = 0
    width = height = 0
    has_video = has_audio = False
    vcodec = acodec = ""
    fps = 0.0

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("duration="):
            duration = _safe_float(line.split("=", 1)[1])
        elif line.startswith("size="):
            size = int(_safe_float(line.split("=", 1)[1]))
        elif line.startswith("codec_type="):
            codec_type = line.split("=", 1)[1]
            if codec_type == "video":
                has_video = True
            elif codec_type == "audio":
                has_audio = True
        elif line.startswith("width="):
            width = int(_safe_float(line.split("=", 1)[1]))
        elif line.startswith("height="):
            height = int(_safe_float(line.split("=", 1)[1]))
        elif line.startswith("r_frame_rate="):
            fps = _safe_rate(line.split("=", 1)[1])

    vcodec, acodec = _probe_codecs(path)
    return MediaInfo(
        path=path,
        duration=duration,
        width=width,
        height=height,
        has_video=has_video,
        has_audio=has_audio,
        vcodec=vcodec,
        acodec=acodec,
        fps=fps,
        size=size,
    )


def _probe_codecs(path: Path) -> Tuple[str, str]:
    """单独取视频/音频编码名。

    ``-show_entries stream=`` 的默认输出把同一流的所有字段挤在一行，
    上面那种逐行解析拿不准编码名属于哪个流，所以这里用 JSON 输出，
    一次拿准。
    """
    exe = require_executable("ffprobe")
    cmd = [
        exe, "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "", ""
    if proc.returncode != 0:
        return "", ""
    import json

    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return "", ""
    vcodec = acodec = ""
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            vcodec = stream.get("codec_name", "")
        elif stream.get("codec_type") == "audio":
            acodec = stream.get("codec_name", "")
    return vcodec, acodec


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _safe_float(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _safe_rate(text: str) -> float:
    try:
        num, _, den = text.partition("/")
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def extract_gray_frames(
    path: Path,
    seconds: float,
    fps: float = 1.0,
    size: int = FRAME_SIZE,
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    """取片头若干秒的灰度缩略帧，返回连续的裸像素。

    输出布局是「帧优先」：第 i 帧占 ``size*size`` 字节，帧内是行优先。

    为什么不抽成 PNG 再读：那样要多一次 PNG 编解码 + 落盘，而这里要的
    只是数值。``rawvideo`` 是解码后直接吐字节，最省。
    """
    exe = require_executable("ffmpeg")
    cmd = [
        exe, "-v", "error",
        "-t", f"{seconds:.3f}",
        "-i", str(path),
        "-vf", f"fps={fps},scale={size}:{size},format=gray",
        "-f", "rawvideo",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"取帧超时（{timeout}s）：{path}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        tail = "\n".join(err.splitlines()[-4:])
        raise FFmpegError(f"取帧失败：{path}\n{tail}")
    return proc.stdout


def keyframes(path: Path, seconds: float, timeout: int = DEFAULT_TIMEOUT) -> List[float]:
    """列出片头区间的关键帧时间戳（秒）。

    ``-read_intervals "%+N"`` 的含义是「从 0 秒读到 N 秒」——
    注意 ``+N`` 是「从 N 秒读到结尾」，写反了会拿到文件尾部的关键帧，
    裁剪点就会对到天上去。

    ``-skip_frame nokey`` 让 ffprobe 只解关键帧，所以扫 180 秒只要 1 秒出头。
    """
    exe = require_executable("ffprobe")
    cmd = [
        exe, "-v", "error",
        "-read_intervals", f"%+{seconds:.3f}",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"关键帧探测超时：{path}") from exc
    if proc.returncode != 0:
        return []
    out: List[float] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        text = line.strip().strip(",")
        if not text or text == "N/A":
            continue
        value = _safe_float(text)
        if value >= 0:
            out.append(value)
    return sorted(out)


def extract_segment_frames(
    path: Path,
    start: float,
    seconds: float,
    fps: float = 1.0,
    size: int = FRAME_SIZE,
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    """取视频中**任意区间**的灰度帧。

    学习模板时要用（广告不一定在 0 秒开始）；``-ss`` 放在 ``-i`` 之前
    是快速定位，定位之后再解码 —— 对 10 GB 的文件，精确 seek 到 30 秒
    只需要读前面一小段。
    """
    exe = require_executable("ffmpeg")
    cmd = [
        exe, "-v", "error",
        "-ss", f"{start:.3f}",
        "-t", f"{seconds:.3f}",
        "-i", str(path),
        "-vf", f"fps={fps},scale={size}:{size},format=gray",
        "-f", "rawvideo",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"取帧超时（{timeout}s）：{path}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        tail = "\n".join(err.splitlines()[-4:])
        raise FFmpegError(f"取帧失败：{path}\n{tail}")
    return proc.stdout
