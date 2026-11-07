"""给一批视频出「边界核对拼图」，并打印格号↔时间对照表。

用法::

    PYTHONPATH=. .venv/bin/python scripts/sheet_batch.py <模板库> <输出目录> <tpl-id> [...]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import ffmpeg, templates  # noqa: E402

BACK = 20
"""拼图起点比当前边界早多少秒 —— 要能看到广告是怎么开始的。"""

SPAN = 100
"""拼图覆盖多少秒。"""

INTERVAL = 5
"""每格多少秒。"""

COLUMNS = 5
ROWS = 4


def main() -> int:
    bank_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    bank = templates.load_bank(bank_path)
    root = Path("/Volumes/P7000Z/R")

    for template_id in sys.argv[3:]:
        template = bank.get(template_id)
        if template is None:
            print(f"! 没有 {template_id}")
            continue
        path = root / template.sample
        if not path.exists():
            print(f"! 样本不存在：{path}")
            continue
        start = max(0.0, template.duration - BACK)
        target = out_dir / f"{template_id}.png"
        cmd = [
            ffmpeg.require_executable("ffmpeg"), "-v", "error", "-y",
            "-ss", f"{start:.3f}", "-t", f"{SPAN * (COLUMNS * ROWS) // (ROWS * 1) * ROWS // ROWS:.0f}",
            "-i", str(path),
            "-vf", f"fps=1/{INTERVAL},scale=256:-1,tile={COLUMNS}x{ROWS}",
            "-frames:v", "1", str(target),
        ]
        cmd[cmd.index("-t") + 1] = f"{INTERVAL * COLUMNS * ROWS:.0f}"
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            print(f"! {template_id} 拼图失败：{proc.stderr.decode('utf-8', 'replace')[-200:]}")
            continue

        times = [start + i * INTERVAL for i in range(COLUMNS * ROWS)]
        print(f"{template_id}  当前边界 {template.duration:.0f}s  样本 {template.sample}")
        print(f"  拼图 {target}")
        for row in range(ROWS):
            cells = []
            for col in range(COLUMNS):
                index = row * COLUMNS + col
                mark = " ←当前边界" if abs(times[index] - template.duration) < INTERVAL / 2 else ""
                cells.append(f"{times[index]:.0f}s{mark}")
            print("    " + "  ".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
