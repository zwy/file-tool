#!/usr/bin/env python3
"""收尾：把「成员已全部人工核验」的候选模板升为 confirmed，
并保证每一支已核验文件都能自动裁（置信 1.0）。

为什么：

* ``candidate`` 模板的置信度被封顶在 0.79 —— 机器自己认出来的模板
  不足以让后续视频自动开刀。这是设计意图。
* 但现在这些模板的**每一个成员都经过人工逐帧核验**了，
  「人工确认过」这个前提已经成立，模板就该升档。
* 升档后还要保证切点正确 —— 成员长度一致的直接用模板长度；
  不一致的写进边界表覆盖模板推断值。

用法：python promote_verified.py <素材根> <模板库> <边界表> <核验数据>
"""
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from file_tool.adcut import boundaries as bnd_mod
from file_tool.adcut import templates as tpl_mod
from file_tool.adcut.models import TemplateStatus

R = Path(sys.argv[1])
TPL_PATH = Path(sys.argv[2])
BND_PATH = Path(sys.argv[3])

sys.path.insert(0, "reports")
from adcut_verified_cuts import VERIFIED

NO_BND = Path("/tmp/_adcut_no_boundaries.yaml")


def scan(root: Path, boundaries: Path):
    cmd = [sys.executable, "-m", "file_tool.cli", "ad-cut", "scan", str(root),
           "--json", "--boundaries", str(boundaries)]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin:/usr/local/bin"})
    if out.returncode != 0:
        raise SystemExit(f"扫描失败：{out.stderr[-2000:]}")
    return json.loads(out.stdout)["detections"]


def main():
    bank = tpl_mod.load_bank(TPL_PATH)
    dets = scan(R, NO_BND)

    members = defaultdict(list)
    for d in dets:
        tid = d.get("template_id")
        if tid:
            members[tid].append(d)

    # ---- 升档：所有成员都核验过、且实测长度一致的候选模板 ----
    promoted = []
    for tid, rows in members.items():
        t = bank.get(tid)
        if t is None or t.status == TemplateStatus.CONFIRMED:
            continue
        if not rows or not all(d["path"] in VERIFIED for d in rows):
            continue
        off = rows[0]["cut_at"] - len(t.hashes)
        implied = Counter(VERIFIED[d["path"]] - off for d in rows)
        if len(implied) != 1:
            continue
        t.status = TemplateStatus.CONFIRMED
        t.note = (f"全部 {len(rows)} 支成员经逐帧核验，长度一致（{list(implied)[0]:.0f}s），"
                  f"升为已确认")
        t.last_seen = "2026-11-09"
        promoted.append((tid, len(rows)))
        print(f"  升档 {tid}: candidate → confirmed（{len(rows)} 支成员全部核验过）")

    tpl_mod.save_bank(bank, TPL_PATH)
    print(f"升档 {len(promoted)} 条模板")

    # ---- 边界表：核验值与模板推断值不一致的文件一律写进去 ----
    dets2 = scan(R, NO_BND)
    implied_cut = {d["path"]: d["cut_at"] for d in dets2
                   if d.get("template_id") and d["path"] in VERIFIED}
    # 模板还是 candidate 时置信度被封顶在 0.79 -> 判定停在 REVIEW。
    # 明明切点是对的，也得靠一条边界条目把它提上去（置信 1.0）。
    not_cut = {d["path"] for d in dets2
               if d["path"] in VERIFIED and d["verdict"] != "cut"}

    old_desc = ""
    old_map = {}
    if BND_PATH.exists():
        _old = bnd_mod.load_boundaries(BND_PATH)
        old_desc = _old.description
        old_map = {b.path: b for b in _old.boundaries}

    rows, added = [], 0
    for p, cut in sorted(VERIFIED.items()):
        imp = implied_cut.get(p)
        old = old_map.get(p)
        if imp is not None and abs(imp - cut) < 0.01 and p not in not_cut:
            if old is not None:
                old.cut_at = cut
                old.site_ad_end = cut
                rows.append(old)
            continue
        if old is not None:
            old.cut_at = cut
            old.site_ad_end = cut
            old.verified = "2026-11-09"
            if not old.note:
                old.note = f"站方推广片到 {cut - 1} 秒结束"
            rows.append(old)
        else:
            rows.append(bnd_mod.AdBoundary(
                path=p, cut_at=cut, site_ad_end=cut, verified="2026-11-09",
                note=f"站方推广片到 {cut - 1} 秒结束",
            ))
        added += 1

    book = bnd_mod.BoundaryBook(version=1, boundaries=rows)
    if old_desc:
        book.description = old_desc
    bnd_mod.save_boundaries(book, BND_PATH)
    print(f"边界表：共 {len(rows)} 条（新增/改值 {added} 条）")


if __name__ == "__main__":
    main()
