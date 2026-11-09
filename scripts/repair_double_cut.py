#!/usr/bin/env python3
"""修复「被裁了两遍」的文件。

## 出了什么事

全量裁剪跑完，发现 3 支文件的时长比「原时长 − 核验切点」少了将近一整段广告 ——
它们被裁了**两遍**。

根因是判定层的一条规则：「片头没匹配到任何模板，但路径在人工边界表里 →
照样按边界裁」。这条规则本意是兜住「模板库还没学到这支广告」的情况，
但它同时也命中了**已经裁过一遍**的文件 —— 那些文件片头已经没有广告了，
模板自然匹配不上，于是边界表又把刀递过来一次。

## 修法

1. 把 `.adcut-tmp.orig.*` 备份还原回原位（那是**第一遍裁之前**的原始文件）；
2. 用修正后的判定重新裁一次（只裁一遍）；
3. 校验新时长 == 原时长 − 核验切点。

用法：

    python scripts/repair_double_cut.py <素材根> <裁剪前基线.json> [<相对路径> ...]
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from file_tool.adcut import boundaries as bnd_mod
from file_tool.adcut import cut as cut_mod
from file_tool.adcut import detect as detect_mod

ROOT = Path(sys.argv[1])
BASELINE = Path(sys.argv[2])
TARGETS = sys.argv[3:]


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
    before = {x["path"]: x for x in base["detections"] if x["verdict"] == "cut"}

    book = bnd_mod.load_boundaries(Path("rules/ad_boundaries.yaml"))
    bmap = {b.path: b for b in book.boundaries}

    bad = []
    for path, item in sorted(before.items()):
        target = ROOT / path
        now = probe_duration(target)
        want = item["duration"] - item["cut_at"]
        if now - want < -1.5:
            bad.append(path)
    if TARGETS:
        bad = [p for p in bad if p in TARGETS]
    if not bad:
        print("没有被裁两遍的文件，不用修")
        return 0

    print(f"发现 {len(bad)} 支被裁了两遍：")
    for path in bad:
        print(f"  {path}")

    failed = 0
    for path in bad:
        target = ROOT / path
        backup = None
        for candidate in target.parent.glob(f"{target.stem}.adcut-tmp.orig.*"):
            backup = candidate
            break
        if backup is None:
            print(f"\n✗ {path}：找不到原片备份，只能重下")
            failed += 1
            continue

        item = before[path]
        bak_dur = probe_duration(backup)
        if abs(bak_dur - item["duration"]) > 1.0:
            print(f"\n✗ {path}：备份时长 {bak_dur:.1f}s 与基线 {item['duration']:.1f}s 对不上，不动")
            failed += 1
            continue

        # 1) 还原原片 —— 用复制而不是移动，备份留着等验证通过再删
        import shutil
        shutil.copy2(backup, target)
        print(f"\n{path}：已从备份还原（{bak_dur:.1f}s）")

        # 2) 重新裁一次 —— 用边界表里的核验切点
        boundary = bmap.get(path)
        cut_at = boundary.cut_at if boundary else item["cut_at"]
        outcome = cut_mod.cut_video(target, cut_at=cut_at)
        if not outcome.ok:
            print(f"  ✗ 重裁失败：{outcome.reason}（原片已还原，未再动）")
            failed += 1
            continue

        # 3) 校验
        final = probe_duration(target)
        want = item["duration"] - item["cut_at"]
        ok = abs(final - want) <= 1.5
        print(f"  {'✓' if ok else '✗'} 重裁 {outcome.cut_at:.2f}s（{outcome.mode}）"
              f"  时长 {final:.1f}s（期望 {want:.1f}s）")
        if ok:
            # 验证通过，备份才可以扔
            backup.unlink()
            print(f"    备份已删除：{backup.name}")
        else:
            failed += 1
            print(f"    备份保留（校验没过）：{backup}")

    print(f"\n{'✗ 有 ' + str(failed) + ' 支没修好' if failed else '✓ 全部修复'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
