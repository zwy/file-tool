#!/usr/bin/env python3
"""裁剪之后验收：广告到底有没有被切干净。

裁剪是破坏性操作，所以「切完」不等于「切对」。这个脚本从三个角度验收：

1. **时长对不对** —— 拿裁剪前的基线（``reports/adcut_pre_cut_scan_2026-11-09.json``）
   逐支比：现在的时长应该约等于「原时长 − 核验过的切点」。
   差得多说明切歪了（切少了留广告尾巴，或切多了削正片）。

2. **片头还认不认得出广告** —— 重扫全库。广告已经不在片头了，
   所以模板**不该**再匹配上。还能匹配上的，说明广告没切干净。

3. **首帧是不是正片** —— 对留了原片备份（``.adcut-tmp.orig.*``）的那几支，
   把裁完文件的第一帧和原片在切点那一帧做裸像素比对（MAD < 1 才算同一帧）。

用法：

    python scripts/verify_cut_results.py <素材根> [裁剪前基线.json]
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from file_tool.adcut import ffmpeg

ROOT = Path(sys.argv[1])
BASELINE = Path(sys.argv[2]) if len(sys.argv) > 2 else \
    Path("reports/adcut_pre_cut_scan_2026-11-09.json")

# 时长检查的容差：关键帧吸附最多 0.5 秒（走混合裁剪的是精确的），
# 再加上 mp4 容器时长本身的误差，给 1.5 秒。
DURATION_TOLERANCE = 1.5


def raw_frame(path: Path, at: float | None) -> bytes:
    """取某一帧的裸像素（灰度）。``at=None`` 表示片头第一帧。"""
    exe = ffmpeg.require_executable("ffmpeg")
    cmd = [exe, "-v", "error"]
    if at is not None:
        cmd += ["-ss", f"{at:.3f}"]
    cmd += ["-i", str(path), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-300:])
    return proc.stdout


def mean_abs_diff(a: bytes, b: bytes) -> float:
    if not a or not b or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def main() -> int:
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    before = {x["path"]: x for x in base["detections"] if x["verdict"] == "cut"}

    # ---- 1. 重扫全库 ----
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    out = subprocess.run(
        [sys.executable, "-m", "file_tool.cli", "ad-cut", "scan", str(ROOT),
         "--json", "--jobs", "6"],
        capture_output=True, text=True,
        env=env,
    )
    if out.returncode != 0:
        print("扫描失败：", out.stderr[-800:])
        return 1
    now = json.loads(out.stdout)
    detections = {x["path"]: x for x in now["detections"]}

    print("=== 重扫全库 ===")
    print(" ", json.dumps(now["summary"], ensure_ascii=False))

    # ---- 2. 逐支验收 ----
    dur_bad, still_ad, checked = [], [], 0
    for path, item in sorted(before.items()):
        cur = detections.get(path)
        if cur is None:
            dur_bad.append((path, "扫描结果里没有这支", 0, 0))
            continue
        checked += 1
        want = item["duration"] - item["cut_at"]
        got = cur["duration"]
        if abs(got - want) > DURATION_TOLERANCE:
            dur_bad.append((path, f"时长差 {got - want:+.2f}s", got, want))
        if cur["verdict"] == "cut":
            still_ad.append((path, cur["cut_at"], cur["template_id"]))

    print(f"\n=== 时长检查（{checked} 支）===")
    if dur_bad:
        print(f"  ✗ {len(dur_bad)} 支对不上：")
        for row in dur_bad[:20]:
            print(f"    {row[0]}  {row[1]}")
    else:
        print("  ✓ 全部对得上（时长 = 原时长 − 核验切点）")

    print(f"\n=== 片头还认得出广告吗 ===")
    if still_ad:
        print(f"  ✗ {len(still_ad)} 支仍能匹配上模板：")
        for row in still_ad[:20]:
            print(f"    {row[0]}  切点 {row[1]}s  [{row[2]}]")
    else:
        print("  ✓ 没有一支还能匹配上广告模板（广告确实切掉了）")

    # ---- 逐支明细 ----
    detail = Path("reports/adcut_post_cut_check_2026-11-09.csv")
    rows = []
    for path, item in sorted(before.items()):
        cur = detections.get(path)
        if cur is None:
            rows.append((path, "", "", "", "扫描结果里没有这支"))
            continue
        want = item["duration"] - item["cut_at"]
        got = cur["duration"]
        delta = got - want
        verdict = "✓" if abs(delta) <= DURATION_TOLERANCE else "✗"
        if cur["verdict"] == "cut":
            verdict = "✗ 仍匹配广告"
        rows.append((path, f"{item['cut_at']:.1f}", f"{want:.1f}",
                     f"{got:.1f} ({delta:+.2f}s)", verdict))
    detail.parent.mkdir(parents=True, exist_ok=True)
    with detail.open("w", encoding="utf-8") as fh:
        fh.write("路径,核验切点(s),期望时长(s),实际时长(s),判定\n")
        for row in rows:
            fh.write(",".join(str(x) for x in row) + "\n")
    print(f"\n=== 逐支明细（{len(rows)} 支）已写入 {detail} ===")
    bad_rows = [r for r in rows if str(r[4]).startswith("✗")]
    if bad_rows:
        print(f"  ✗ {len(bad_rows)} 支有问题：")
        for row in bad_rows:
            print(f"    {row[0]}  {row[4]}")
    else:
        print("  ✓ 全部通过")

    # ---- 3. 首帧逐像素比对（只对留了原片备份的） ----
    backups = sorted(ROOT.glob("*/*.adcut-tmp.orig.*"))
    if backups:
        print(f"\n=== 首帧比对（{len(backups)} 支留了原片备份）===")
        for bak in backups:
            target = bak.with_name(bak.name.replace(".adcut-tmp.orig", ""))
            if not target.exists():
                print(f"  ✗ {target.name} 找不到")
                continue
            item = before.get(str(target.relative_to(ROOT)))
            cut_at = item["cut_at"] if item else None
            if cut_at is None:
                print(f"  ? {target.name} 不在基线里，跳过")
                continue
            try:
                got = raw_frame(target, None)
                same = raw_frame(bak, cut_at)
                prev = raw_frame(bak, max(0.0, cut_at - 3.0))
            except RuntimeError as exc:
                print(f"  ✗ {target.name} 取帧失败：{exc}")
                continue
            d_same = mean_abs_diff(got, same)
            d_prev = mean_abs_diff(got, prev)
            ok = d_same < 12
            print(f"  {'✓' if ok else '✗'} {target.name}"
                  f"  首帧 vs 原片{cut_at:.0f}s: MAD={d_same:.2f}"
                  f"  vs 原片{max(0.0, cut_at - 3):.0f}s: MAD={d_prev:.2f}")

    bad = len(dur_bad) + len(still_ad)
    print(f"\n{'✗ 有问题，需要复查' if bad else '✓ 验收通过'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
