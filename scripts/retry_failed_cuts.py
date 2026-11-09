#!/usr/bin/env python3
"""重试上轮裁剪失败的文件，把每一支的失败原因打出来。

**先判断是不是已经裁好了**：上轮失败清单是照着「裁剪前基线」算出来的，
但有些文件在基线之后又被别的操作裁过一次（比如试水阶段）。
不先核对就重裁，会把已经裁好的片子再裁一遍 —— 这比不裁更糟。

用法：
    python scripts/retry_failed_cuts.py <素材根> <裁剪前基线.json> [<相对路径> ...]
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from file_tool.adcut import boundaries as bnd_mod
from file_tool.adcut import cut as cut_mod

ROOT = Path(sys.argv[1])
BASELINE = Path(sys.argv[2])
TARGETS = sys.argv[3:]

# 被裁两遍的由 scripts/repair_double_cut.py 处理（要先从备份还原）
DOUBLE_CUT = {
    "DVMM-012/DVMM-012.mp4",
    "STSK-108/aavv38.xyz@STSK108A.mp4",
    "SVDVD-739/SVDVD-739.mp4",
}

# 时长对不上的容差：流拷贝吸附到关键帧会有零点几秒的误差
TOLERANCE = 1.5


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def main() -> int:
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    before = {x["path"]: x for x in base["detections"]}

    book = bnd_mod.load_boundaries(Path("rules/ad_boundaries.yaml"))
    bmap = {b.path: b for b in book.boundaries}

    targets = TARGETS or sorted(
        p for p, x in before.items()
        if x["verdict"] == "cut" and p not in DOUBLE_CUT
    )

    failed = 0
    skipped = 0
    for rel in targets:
        if rel in DOUBLE_CUT:
            print(f"– {rel}：被裁两遍，跳过（走 repair_double_cut.py）")
            continue
        item = before.get(rel)
        boundary = bmap.get(rel)
        if item is None and boundary is None:
            print(f"– {rel}：基线和边界表里都没有，跳过")
            continue

        path = ROOT / rel
        if not path.exists():
            print(f"✗ {rel}：文件不存在")
            failed += 1
            continue

        cut_at = boundary.cut_at if boundary else item["cut_at"]

        # 已经裁好了就别再裁
        if item is not None and item["duration"] > 0:
            now = probe_duration(path)
            want = item["duration"] - cut_at
            if now > 0 and abs(now - want) <= TOLERANCE:
                print(f"✓ {rel}：已裁好（{now:.1f}s / 期望 {want:.1f}s），跳过")
                skipped += 1
                continue

        outcome = cut_mod.cut_video(path, cut_at=cut_at, keep_original=True)
        if outcome.ok:
            print(f"✓ {rel}：{outcome.reason}  时长 {outcome.after:.1f}s")
        else:
            print(f"✗ {rel}：{outcome.reason}")
            failed += 1

    print(f"\n失败 {failed} 支，已裁好跳过 {skipped} 支")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
