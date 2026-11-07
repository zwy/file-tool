"""给一支视频出「边界条带图」，并附上每格的数值表。

每一格是 1 秒间隔的一帧；下面的表格给出该帧的**时间**、平均亮度和
MAD。人眼读图容易串行，配上表格就不会把「第几格」认错。

可以一次给多支视频，条带纵向拼在一起（每支视频一段）。

用法::

    PYTHONPATH=. .venv/bin/python scripts/strip.py <输出png> <视频:起始秒> [...]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, ffmpeg  # noqa: E402

FF = ffmpeg.require_executable("ffmpeg")
CELL_W = 512
COLUMNS = 3
CELLS = 9
ROOT = Path("/Volumes/P7000Z/R")


def make_strip(rel: str, start: int, tmp: Path, idx: int) -> Path:
    path = ROOT / rel
    out = tmp / f"s{idx}.png"
    cmd = [
        FF, "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{CELLS:.3f}", "-i", str(path),
        "-vf", f"fps=1,scale={CELL_W}:-1,tile={COLUMNS}x3",
        "-frames:v", "1", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"{rel}: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
    return out


def main() -> int:
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    specs = []
    for arg in sys.argv[2:]:
        rel, _, start = arg.rpartition(":")
        specs.append((rel, int(start)))

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        strips = [make_strip(rel, start, tmpdir, i) for i, (rel, start) in enumerate(specs)]
        if len(strips) == 1:
            proc = subprocess.run([FF, "-v", "error", "-y", "-i", str(strips[0]),
                                   "-frames:v", "1", str(out)], capture_output=True)
        else:
            args = []
            for s in strips:
                args += ["-i", str(s)]
            chain = "".join(f"[{i}:v]" for i in range(len(strips)))
            cmd = [FF, "-v", "error", "-y"] + args + [
                "-filter_complex", f"{chain}vstack=inputs={len(strips)}[v]",
                "-map", "[v]", "-frames:v", "1", str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr.decode("utf-8", "replace")[-300:])

    print(f"→ {out}")
    for rel, start in specs:
        path = ROOT / rel
        sig = cache.load(path, fps=1.0, seconds=300.0, size=ffmpeg.FRAME_SIZE)
        print(f"\n{rel}  起始 {start}s")
        print("  格号  时间   平均亮度   MAD")
        for k in range(CELLS):
            idx = start + k
            if sig is None or idx >= len(sig.hashes):
                continue
            b = sig.brightness[idx] if idx < len(sig.brightness) else -1
            m = sig.mad[idx] if idx < len(sig.mad) else -1
            flag = "  ← 画面硬切" if m >= 12 else ""
            print(f"  {k:3d} {idx:5d}s {b:9.1f} {m:6.1f}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
