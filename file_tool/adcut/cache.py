"""指纹缓存。

## 为什么必须缓存

取片头指纹的瓶颈在 ffmpeg 解码（实测 1.5~7 秒/个视频）。
276 个视频全量扫一遍要十几分钟，而**同一个视频会被反复扫**：
先 ``scan`` 看有没有广告，学完模板再 ``scan`` 一次确认，然后 ``cut`` 一次。
没有缓存的话，同一个文件要解码三遍。

缓存键包含**文件大小和修改时间** —— 文件被换掉或重新下载后，
指纹自动失效重算，不会拿旧指纹去对新文件。

## 缓存放哪

按平台的惯例目录放，不放在项目目录里：

- macOS  ``~/Library/Caches/file-tool/adcut``
- Windows ``%LOCALAPPDATA%\\file-tool\\adcut``
- Linux  ``$XDG_CACHE_HOME/file-tool/adcut`` 或 ``~/.cache/file-tool/adcut``

缓存是**纯加速**的：删掉它只是变慢，不会影响正确性。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from .fingerprint import FrameSignature


def cache_dir() -> Path:
    """返回当前平台的缓存目录（不存在就建）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base) / "file-tool" / "adcut"
    elif os.sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "file-tool" / "adcut"
    else:
        base = os.environ.get("XDG_CACHE_HOME")
        root = (Path(base) if base else Path.home() / ".cache") / "file-tool" / "adcut"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 缓存写不进去不该拖垮主流程，退化成「每次都重算」
        root = Path(tempfile.gettempdir()) / "file-tool-adcut"
        root.mkdir(parents=True, exist_ok=True)
    return root


def _key(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()[:20]


def _stat_sig(path: Path) -> Optional[tuple]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, int(stat.st_mtime))


def load(path: Path, fps: float, seconds: float, size: int) -> Optional[FrameSignature]:
    """取缓存里的指纹；文件变了 / 参数变了 / 缓存不存在就返回 ``None``。"""
    sig = _stat_sig(path)
    if sig is None:
        return None
    file = cache_dir() / f"{_key(path)}.json"
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("size") != sig[0] or data.get("mtime") != sig[1]:
        return None
    if data.get("fps") != fps or data.get("seconds") != seconds or data.get("size_px") != size:
        return None
    hashes = data.get("hashes") or []
    mad = data.get("mad") or []
    if not hashes:
        return None
    return FrameSignature(hashes=hashes, mad=mad, fps=fps)


def store(path: Path, signature: FrameSignature, seconds: float, size: int) -> None:
    """写缓存。写失败无所谓 —— 缓存只是加速。"""
    sig = _stat_sig(path)
    if sig is None:
        return
    payload = {
        "size": sig[0],
        "mtime": sig[1],
        "fps": signature.fps,
        "seconds": seconds,
        "size_px": size,
        "hashes": list(signature.hashes),
        "mad": [round(v, 4) for v in signature.mad],
    }
    file = cache_dir() / f"{_key(path)}.json"
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(file.parent), prefix=".fp-", suffix=".tmp", delete=False,
        )
        with handle:
            json.dump(payload, handle)
        os.replace(handle.name, file)
    except OSError:
        pass


def get_or_compute(
    path: Path,
    seconds: float,
    fps: float = 1.0,
    size: int = 32,
) -> FrameSignature:
    """拿指纹，优先走缓存。"""
    cached = load(path, fps=fps, seconds=seconds, size=size)
    if cached is not None:
        return cached
    from . import ffmpeg

    raw = ffmpeg.extract_gray_frames(path, seconds, fps=fps, size=size)
    from .fingerprint import signature_from_raw

    signature = signature_from_raw(raw, fps=fps, size=size)
    store(path, signature, seconds=seconds, size=size)
    return signature


def clear() -> int:
    """清空缓存，返回删掉的文件数。"""
    root = cache_dir()
    count = 0
    for item in root.glob("*.json"):
        try:
            item.unlink()
            count += 1
        except OSError:
            pass
    return count
