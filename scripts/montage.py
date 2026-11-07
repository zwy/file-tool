"""把多支视频的「边界邻域」拼成一张大图，一眼扫完一组变体。

每支视频取 ``CELLS`` 张连续 1 秒的帧（5 列），纵向堆叠。

用法::

    PYTHONPATH=. .venv/bin/python scripts/montage.py <输出png> <视频:起始秒> [...]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import ffmpeg  # noqa: E402

FF = ffmpeg.require_executable("ffmpeg")
W, H = 256, 144
COLUMNS = 5
CELLS = int(__import__("os").environ.get("MONTAGE_CELLS", "10"))
"""每支视频取多少张 1 秒间隔的帧。"""


def tile(video: str, start: float, out: Path) -> None:
    rows = (CELLS + COLUMNS - 1) // COLUMNS
    cmd = [
        FF, "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{CELLS:.3f}",
        "-i", str(video),
        "-vf", f"fps=1,scale={W}:{H}:force_original_aspect_ratio=decrease,"
               f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,tile={COLUMNS}x{rows}",
        "-frames:v", "1", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"拼图失败 {video}: {proc.stderr.decode('utf-8', 'replace')[-300:]}")


def main() -> int:
    out = Path(sys.argv[1])
    specs = []
    for arg in sys.argv[2:]:
        path, _, start = arg.rpartition(":")
        if not path:
            path, start = start, "0"
        specs.append((path, float(start)))

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        tiles = []
        for idx, (path, start) in enumerate(specs):
            tile_path = tmpdir / f"t{idx}.png"
            tile(path, start, tile_path)
            tiles.append(tile_path)
        args = []
        for t in tiles:
            args += ["-i", str(t)]
        chain = "".join(f"[{i}:v]" for i in range(len(tiles)))
        cmd = [FF, "-v", "error", "-y"] + args + [
            "-filter_complex", f"{chain}vstack=inputs={len(tiles)}[v]",
            "-map", "[v]", "-frames:v", "1", str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise SystemExit(f"堆叠失败：{proc.stderr.decode('utf-8', 'replace')[-300:]}")
    for path, start in specs:
        cells = " ".join(f"{start + i:.0f}" for i in range(CELLS))
        print(f"{Path(path).parent.name:22s} {cells}")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
