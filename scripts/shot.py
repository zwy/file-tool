"""按指定窗口给单个视频出「时间轴拼图」，用于人眼核验广告边界。

用法::

    PYTHONPATH=. .venv/bin/python scripts/shot.py <video> <start秒> <间隔秒> <格数> <输出png>

拼图是 5 列 × N 行，每格对应「start + i*间隔」秒那一帧。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import ffmpeg  # noqa: E402

COLUMNS = 5
TILE_WIDTH = 256


def shot(video: str, start: float, interval: float, cells: int, out: Path) -> list[float]:
    rows = (cells + COLUMNS - 1) // COLUMNS
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg.require_executable("ffmpeg"), "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{interval * cells:.3f}",
        "-i", str(video),
        "-vf", f"fps=1/{interval},scale={TILE_WIDTH}:-1,tile={COLUMNS}x{rows}",
        "-frames:v", "1", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg 失败：{proc.stderr.decode('utf-8', 'replace')[-400:]}")
    return [start + i * interval for i in range(cells)]


def main() -> int:
    video = sys.argv[1]
    start = float(sys.argv[2])
    interval = float(sys.argv[3])
    cells = int(sys.argv[4])
    out = Path(sys.argv[5])
    times = shot(video, start, interval, cells, out)
    rows = (cells + COLUMNS - 1) // COLUMNS
    print(f"{out}  video={video}  start={start:.0f}s interval={interval:.0f}s")
    for row in range(rows):
        line = []
        for col in range(COLUMNS):
            idx = row * COLUMNS + col
            if idx < len(times):
                line.append(f"{times[idx]:6.0f}s")
            else:
                line.append("       ")
        print("   " + " ".join(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
