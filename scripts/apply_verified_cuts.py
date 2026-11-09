#!/usr/bin/env python3
"""把逐帧核验结果落到模板库和边界表。

两件事：

1. **模板长度修正** —— 一条模板的所有成员实测长度一致时，把模板本身改掉
   （重新抽指纹），边界表就不用为它们每条都记一笔。
2. **边界表兜底** —— 成员长度不一致时，模板记不住「这支广告多长」，
   就把偏离的文件逐个写进 rules/ad_boundaries.yaml 覆盖模板推断值。

注意：算模板起点(offset)时必须**不带边界表扫描** ——
``ad-cut scan`` 默认就会加载仓库自带的边界表，带着它算出来的
「起点」是拿边界值反推的，模板指纹会抽错位置。

用法：python apply_verified_cuts.py <素材根> <模板库> <边界表> <核验数据>
"""
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, ".")

from file_tool.adcut import boundaries as bnd_mod
from file_tool.adcut import fingerprint
from file_tool.adcut import templates as tpl_mod
from file_tool.adcut.models import AdBoundary, BoundaryBook

R = Path(sys.argv[1])
TPL_PATH = Path(sys.argv[2])
BND_PATH = Path(sys.argv[3])

sys.path.insert(0, "reports")
from adcut_verified_cuts import VERIFIED

VERIFIED_ON = "2026-11-09"
NO_BOUNDARIES = Path("/tmp/_adcut_no_boundaries.yaml")   # 不存在 -> 不加载任何边界


def scan(root: Path, boundaries: Path):
    cmd = [sys.executable, "-m", "file_tool.cli", "ad-cut", "scan", str(root),
           "--json", "--boundaries", str(boundaries)]
    env = {"PYTHONPATH": ".", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    out = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise SystemExit(f"扫描失败：{out.stderr[-2000:]}")
    return json.loads(out.stdout)["detections"]


def main():
    bank = tpl_mod.load_bank(TPL_PATH)
    # 改动前的快照 —— 回归检查不通过时要原样退回去
    snapshot = {t.id: t.model_copy(deep=True) for t in bank.templates}

    # ---- 不带边界表扫描，拿到模板推断的切点 -> 反推每个成员的模板起点 ----
    dets = scan(R, NO_BOUNDARIES)
    info = {}
    for d in dets:
        tid = d.get("template_id")
        if not tid:
            continue
        t = bank.get(tid)
        L = len(t.hashes)
        info[d["path"]] = (tid, d["cut_at"] - L, L)

    members = defaultdict(list)
    for p, (tid, off, L) in info.items():
        if p in VERIFIED:
            members[tid].append((p, off))

    changed = []
    for tid, rows in members.items():
        implied = Counter(VERIFIED[p] - off for p, off in rows)
        if len(implied) != 1:
            continue                        # 成员长度不一致 -> 交给边界表
        new_len = int(next(iter(implied)))
        t = bank.get(tid)
        old_len = len(t.hashes)
        if new_len == old_len:
            continue                        # 长度本来就是对的
        if new_len <= 0:
            print(f"  !! {tid} 新长度非法（{new_len}），跳过")
            continue
        # 从某个成员文件重新抽指纹（区间 = 模板起点 + 新长度）
        src, off = sorted(rows)[0]
        sig = fingerprint.segment_signature_of(R / src, float(off), float(new_len))
        hashes = list(sig.hashes)[:new_len]
        if len(hashes) < new_len:
            print(f"  !! {tid} 抽到的指纹不足（{len(hashes)}/{new_len}），跳过")
            continue
        brightness = list(sig.brightness)[:new_len]
        tpl_mod.replace_template(
            bank, tid, hashes,
            sample=src,
            label=t.label,
            note=(f"长度经逐帧核验修正（{old_len}s → {new_len}s）："
                  f"成员实测长度一致，按模板长度统一裁剪"),
            fps=t.fps or 1.0,
            brightness=brightness,
        )
        changed.append((tid, old_len, new_len, len(rows)))
        print(f"  模板 {tid}: {old_len}s → {new_len}s（{len(rows)} 支成员实测一致）")

    tpl_mod.save_bank(bank, TPL_PATH)
    print(f"模板库已更新 {len(changed)} 条")

    # ---- 回归检查：改完长度后，成员还满格匹配吗 ----
    #
    # 模板的**最后一帧**是广告的过渡帧，这一帧的 dHash 在不同文件之间不稳定。
    # 长度 +1 之后它从「可有可无」变成「必须对上」，于是部分成员会从
    # 148/148 掉到 147/148，判定退回 REVIEW —— 那是把安全的东西改坏了。
    # 所以：**只有所有成员仍然满格匹配，长度才允许改**；
    # 改不了的保持原样，它那些成员一律进边界表。
    if changed:
        dets2 = scan(R, NO_BOUNDARIES)
        dropped = defaultdict(list)
        for d in dets2:
            tid = d.get("template_id")
            if not tid or not d.get("template_frames"):
                continue
            if d["matched_frames"] < d["template_frames"]:
                dropped[tid].append(d["path"])
        reverted = []
        for tid, old_len, new_len, _ in changed:
            if tid not in dropped:
                continue
            old = snapshot[tid]
            t = bank.get(tid)
            t.hashes = list(old.hashes)
            t.brightness = list(old.brightness or [])
            t.fps = old.fps
            t.sample = old.sample
            t.label = old.label
            t.note = old.note
            t.status = old.status
            t.samples = old.samples
            t.first_seen = old.first_seen
            t.last_seen = old.last_seen
            reverted.append(tid)
            print(f"  退回 {tid}: {new_len}s（原 {old_len}s）"
                  f"—— {len(dropped[tid])} 支成员掉匹配")
        tpl_mod.save_bank(bank, TPL_PATH)
        print(f"回归检查：退回 {len(reverted)} 条模板"
              f"（{', '.join(reverted) if reverted else '无'}）")

    # ---- 重新扫描，算出模板现在推断出的切点 ----
    dets2 = scan(R, NO_BOUNDARIES)
    implied_cut = {
        d["path"]: d["cut_at"] for d in dets2
        if d.get("template_id") and d["path"] in VERIFIED
    }

    old_desc = ""
    old_map = {}
    if BND_PATH.exists():
        _old = bnd_mod.load_boundaries(BND_PATH)
        old_desc = _old.description
        old_map = {b.path: b for b in _old.boundaries}

    # 每支的说明文字 —— 说清楚「站方广告到哪结束、后面保留的是什么」
    #
    # 注意括注里的数字是**模板按自身长度推出的切点**，
    # 而前半句的秒数是**站方广告的最后一帧** —— 两者差 1 秒是定义使然，
    # 所以括注必须写明是「切点」，不然读起来像自相矛盾。
    def note_for(path: str, cut: int, imp: float | None) -> str:
        stem = Path(path).stem
        detail = KEEPS.get(stem)
        head = f"站方推广片到 {cut - 1} 秒结束"
        if detail:
            head = f"{head}；{detail}"
        if imp is None:
            return head
        return (f"{head}；模板按自身长度推的切点是 {imp:.0f}s")

    def note_is_stale(old_note: str, cut: int) -> bool:
        """旧说明里的「站方广告终点」跟 cut_at 对不上，就算过期。

        说明统一写成「站方推广片到 N 秒结束」，N 应该是 cut_at - 1。
        修正切点后旧说明不会自己跟着变 —— 上一轮就是因此留下了
        「到 338 秒结束」这种和 cut_at=291 打架的文案。

        只看**第一个**秒数（广告终点）；后面括注里的「模板切点」是别的量。
        """
        m = re.search(r"(\d+)\s*秒", old_note or "")
        if not m:
            return False
        return int(m.group(1)) != cut - 1

    rows = []
    added = updated = 0
    for p, cut in sorted(VERIFIED.items()):
        imp = implied_cut.get(p)
        old = old_map.get(p)
        if imp is not None and abs(imp - cut) < 0.01:
            # 模板推断已经对得上 —— 原来记过就更新说明，没记过就不必写
            if old is not None:
                old.cut_at = float(cut)
                old.site_ad_end = float(cut)
                old.verified = VERIFIED_ON
                if note_is_stale(old.note or "", cut):
                    old.note = note_for(p, cut, imp)
                rows.append(old)
                updated += 1
            continue
        rows.append(AdBoundary(
            path=p, cut_at=float(cut), site_ad_end=float(cut),
            verified=VERIFIED_ON,
            note=note_for(p, cut, imp),
        ))
        added += 1

    book = BoundaryBook(version=1, boundaries=rows)
    if old_desc:
        book.description = old_desc
    bnd_mod.save_boundaries(book, BND_PATH)
    print(f"边界表：共 {len(rows)} 条（新增/改值 {added} 条，沿用旧说明 {updated} 条）")


# 裁掉站方广告后**要保留**的厂牌片头/作品说明卡 —— 写进说明里备查
#
# 这张表是 note 文案的唯一来源：note 统一写成
#   「站方推广片到 {cut_at - 1} 秒结束；<这里>」
# 其中秒数必须是 cut_at - 1（cut_at 是切掉的秒数，站方广告的最后一帧是 cut_at - 1）。
# 改口径时记得同步改这里，否则重跑脚本会把说明文字写回旧值。
KEEPS = {
    "SVDVD-739": "其后是厂牌片头（嬌薬 Aphrodisiac 卡），保留",
    "BBAN-433": "其后是 produced by bibian 作品封面卡，保留",
    "HHE-003": "其后是厂牌片头（PRESTIGE logo 卡），保留",
    "YRK-020": "其后是厂牌片头（PRESTIGE logo + ネトラシリーズ 片名卡），保留",
    "SSIS-518C": "其后是紫色作品名卡，保留",
    "SSIS-688C": "其后是紫色作品名卡，保留",
    "SPZ-01114": "其后是黒底白字作品说明卡，保留",
    "HJMO-478": "其后是红字黑底作品说明卡，保留",
    "HOIZ-030A": "其后是厂牌片头（acus logo + 注意事项免责卡），保留",
    "SVDVD-888": "其后是厂牌片头（acus logo + 注意事项免责卡），保留",
    "KAM-157": "其后是厂牌片头（免责说明卡），保留",
    "KAM-159": "其后是厂牌片头（免责说明卡），保留",
    "MIAA-954C": "其后是厂牌片头（MOODYZ PRESENTS 卡），保留",
    "NHDTB-869": "204 秒起是作品名卡，保留",
    "NHDTB-623A": "227 秒起是正片",
    "dvdms-00300b": "其后是演员档案卡，保留",
    "SDMM-122C": "其后是作品自己的宣传卡，保留",
}


if __name__ == "__main__":
    main()
