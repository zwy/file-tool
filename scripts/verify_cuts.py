#!/usr/bin/env python3
"""校验：扫描结果里的切点是否逐支等于核验值。

用法：python verify_cuts.py <素材根> <核验数据>
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "reports")
from adcut_verified_cuts import VERIFIED

R = Path(sys.argv[1])
sys.path.insert(0, "reports")

cmd = [sys.executable, "-m", "file_tool.cli", "ad-cut", "scan", str(R), "--json"]
out = subprocess.run(cmd, capture_output=True, text=True,
                     env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin:/usr/local/bin"})
dets = json.loads(out.stdout)["detections"]
by = {d["path"]: d for d in dets}

bad, ok, missing = [], 0, []
for p, cut in sorted(VERIFIED.items()):
    d = by.get(p)
    if d is None:
        missing.append(p)
        continue
    if abs((d["cut_at"] or 0) - cut) < 0.01 and d["verdict"] == "cut":
        ok += 1
    else:
        bad.append((p, cut, d["cut_at"], d["verdict"], d.get("reason", "")[:60]))

print(f"核验 {len(VERIFIED)} 支 | 相符 {ok} | 不符 {len(bad)} | 扫描没列出 {len(missing)}")
for x in bad:
    print("  !!", x)
for p in missing:
    print("  ?? 扫描没列出:", p)

# 匹配质量：模板指纹被重抽过，看看有没有掉匹配
weak = [(d["path"], d["matched_frames"], d["template_frames"], d["coverage"])
        for d in dets
        if d.get("template_id") and d["template_frames"]
        and d["matched_frames"] < d["template_frames"]]
print(f"\n模板没对上满格的文件：{len(weak)}")
for w in weak[:20]:
    print("  ", w)

from collections import Counter
print("\n判定分布:", dict(Counter(d["verdict"] for d in dets)))
sys.exit(1 if bad else 0)
