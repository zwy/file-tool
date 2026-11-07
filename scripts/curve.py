"""打印缓存里某支视频的逐秒亮度 / MAD / dHash，用于精确定位边界。

用法::

    PYTHONPATH=. .venv/bin/python scripts/curve.py <视频路径> [起始秒] [结束秒]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, ffmpeg  # noqa: E402


def main() -> int:
    path = Path(sys.argv[1])
    lo = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    hi = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    sig = cache.load(path, fps=1.0, seconds=300.0, size=ffmpeg.FRAME_SIZE)
    if sig is None:
        sig = cache.get_or_compute(path, 300.0, fps=1.0, size=ffmpeg.FRAME_SIZE)
    if sig is None:
        print("拿不到指纹")
        return 1
    print(f"{path.name}  帧数={len(sig)}")
    for i in range(max(0, lo), min(hi, len(sig))):
        b = sig.brightness[i] if i < len(sig.brightness) else -1
        m = sig.mad[i] if i < len(sig.mad) else -1
        h = sig.hashes[i]
        print(f"{i:5d}s  bright={b:6.1f}  mad={m:6.1f}  dhash={h:#018x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
