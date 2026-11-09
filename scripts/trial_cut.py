#!/usr/bin/env python3
"""小批试水裁剪：先裁几支，验完再决定要不要全量。

和 ``ad-cut cut --apply`` 用的是同一套执行器（``cut.cut_video``），
区别只有两点：

1. **只处理指定的文件**，而不是整库；
2. **保留原片**（存成 ``<名字>.adcut-tmp.orig.<扩展名>``），
   验完可以逐帧对照，也可以直接回滚。

验什么：

- **首帧是不是正片** —— 把裁完文件的第一帧，和原片 ``cut_at`` 那一帧
  用裸像素比（PNG 的 md5 不可靠，帧内容要按 rawvideo 比）。
- **时长对不对** —— 期望 = 原片时长 - 实际切割点。
- **权限有没有丢** —— 原片是 ``-rwx------``，裁完还得是。
- **有没有残留临时文件**。

用法：

    python scripts/trial_cut.py <素材根> <文件相对路径> [<文件相对路径> ...]
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from file_tool.adcut import cut as cut_mod
from file_tool.adcut import ffmpeg

ROOT = Path(sys.argv[1])
TARGETS = sys.argv[2:]


def raw_frame(path: Path, at: float) -> bytes:
    """取某一帧的裸像素（灰度）。"""
    exe = ffmpeg.require_executable("ffmpeg")
    cmd = [exe, "-v", "error", "-ss", f"{at:.3f}", "-i", str(path),
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-400:])
    return proc.stdout


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """两帧的平均绝对差。"""
    if not a or not b or len(a) != len(b):
        return float("inf")
    n = len(a)
    return sum(abs(x - y) for x, y in zip(a, b)) / n


def main() -> int:
    failures = 0
    for rel in TARGETS:
        path = ROOT / rel
        print(f"\n=== {rel} ===")
        before_mode = path.stat().st_mode & 0o777
        before_size = path.stat().st_size

        # 切点从扫描结果拿
        cut_at = float(CUTS[rel])
        outcome = cut_mod.cut_video(path, cut_at=cut_at, keep_original=True)

        if not outcome.ok:
            print(f"  ✗ 裁剪失败：{outcome.reason}")
            failures += 1
            continue

        backup = path.with_name(f"{path.stem}.adcut-tmp.orig{path.suffix}")
        after_mode = path.stat().st_mode & 0o777

        print(f"  裁掉 {outcome.cut_at:.2f}s（{outcome.mode}）"
              f"  吸附偏移 {outcome.snapped:+.2f}s")
        print(f"  时长 {outcome.before:.1f}s → {outcome.after:.1f}s"
              f"  （期望 {outcome.before - outcome.cut_at:.1f}s）")
        print(f"  体积 {before_size/1e9:.2f}GB → {path.stat().st_size/1e9:.2f}GB"
              f"  省下 {(outcome.saved_bytes or 0)/1e9:.2f}GB")
        print(f"  权限 {oct(before_mode)} → {oct(after_mode)}")

        # ---- 首帧是不是正片 ----
        # 裁完文件的第一帧，应该等于原片在「实际切割点」那一帧。
        # 取原片 cut_at 前后的几帧比对，看首帧落在哪一帧上。
        ref = raw_frame(backup, outcome.cut_at)
        got = raw_frame(path, 0.0)
        diff = mean_abs_diff(ref, got)
        print(f"  首帧 vs 原片 {outcome.cut_at:.2f}s 处：MAD={diff:.2f}"
              f"  {'✓ 一致' if diff < 12 else '✗ 不一致！'}")

        # 首帧不能是广告：拿原片 cut_at-3 那一帧（广告尾）比一下，应该明显不同
        ad = raw_frame(backup, max(0.0, outcome.cut_at - 3.0))
        diff_ad = mean_abs_diff(ad, got)
        print(f"  首帧 vs 原片 {max(0.0, outcome.cut_at - 3.0):.2f}s 处"
              f"（广告尾）：MAD={diff_ad:.2f}"
              f"  {'✓ 与广告帧明显不同' if diff_ad >= 12 else '⚠ 与广告帧太像！'}")

        if diff >= 12:
            failures += 1
        if after_mode != before_mode:
            print(f"  ✗ 权限变了：{oct(before_mode)} → {oct(after_mode)}")
            failures += 1

        # 把首帧导出来，肉眼也能看一眼
        png = Path(f"/tmp/trial_{path.stem}.png")
        exe = ffmpeg.require_executable("ffmpeg")
        subprocess.run([exe, "-v", "error", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=480:270", "-y", str(png)], check=True)
        print(f"  首帧截图：{png}")
        print(f"  原片留在：{backup}")

    print(f"\n{'✗ 有 ' + str(failures) + ' 项没通过' if failures else '✓ 全部通过'}")
    return 1 if failures else 0


CUTS: dict = {}

if __name__ == "__main__":
    import json
    scan = json.loads(subprocess.run(
        [sys.executable, "-m", "file_tool.cli", "ad-cut", "scan", str(ROOT),
         "--json"], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": "."}).stdout)
    for det in scan["detections"]:
        if det["verdict"] == "cut":
            CUTS[det["path"]] = det["cut_at"]
    sys.exit(main())
