"""把「变体 → 模板」批量写进模板库。

分两种状态写：

``confirmed``（人工确认过）
    边界是肉眼在时间轴拼图上量出来的 —— 只有 8 支初始模板。
    命中的视频**可以直接自动裁**。

``candidate``（机器推断）
    边界来自「跨片共享 / 组内共享」的自动检测，还没经过人眼确认。
    命中的视频一律进**人工确认档**，不会自动开刀 ——
    机器算出来的边界可能偏短（推广片尾部是变体独有时就会偏短，
    实测 CLIP-013C 偏短 45 秒），偏短只是留下广告，不会削到正片，
    但也不能当成定论。

用法::

    PYTHONPATH=. .venv/bin/python scripts/bootstrap_ad_templates.py /Volumes/P7000Z/R
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, detect, fingerprint, ffmpeg, templates  # noqa: E402
from file_tool.adcut.models import TemplateStatus  # noqa: E402

BANK_PATH = Path("rules/ad_templates.yaml")

# 肉眼在时间轴拼图上量出来的边界（秒）。
#
# 这 8 支是最初学的那批，边界当时是按 180 秒窗口看拼图定的 ——
# 窗口不够长，只看到推广片的前半段，边界全部偏早。
# 下面这些是重新用 300 秒窗口逐帧核对过的。
# 2026-11-09：原来的 tpl-0001「厂牌片头-黑底字幕卡」已从模板库移除 ——
# 那 11 秒黑底卡只有作品名和厂牌 logo，没有站点二维码/站方 logo，
# 按分类口径属于厂牌片头（保留），不是站方广告。编号 tpl-0001 空缺不复用。
VERIFIED = {
    "tpl-0002": ("ABF-094", 204),
    "tpl-0003": ("AKID-103", 227),
    "tpl-0004": ("CAWD-660", 204),
    "tpl-0005": ("CLT-013C", 193),
    "tpl-0006": ("DVDMS-813", 186),
    "tpl-0007": ("DVMM-055", 214),
    "tpl-0008": ("DVMM-055A", 235),
}


def load_signatures(root: Path, videos: List[Path]) -> Dict[str, cache.FrameSignature]:
    result = {}
    for path in videos:
        try:
            result[str(path.relative_to(root))] = cache.get_or_compute(
                path, 300.0, fps=1.0, size=ffmpeg.FRAME_SIZE
            )
        except Exception:
            continue
    return result


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/P7000Z/R")
    variants_path = Path(sys.argv[2] if len(sys.argv) > 2 else Path("/tmp/ad_variants.json"))

    variants = json.loads(variants_path.read_text(encoding="utf-8"))
    videos = detect.find_videos(root)
    sigs = load_signatures(root, videos)

    bank = templates.load_bank(BANK_PATH)
    print(f"模板库现有 {len(bank.templates)} 支模板", flush=True)

    # ---- 1. 修正已确认模板的边界 ----
    covered_reps = set()
    for template_id, (keyword, length) in VERIFIED.items():
        template = bank.get(template_id)
        if template is None:
            print(f"  ! 模板 {template_id} 不在库里，跳过")
            continue
        # 找到对应的样本视频
        sample_rel = next(
            (rel for rel in sigs if keyword in rel),
            None,
        )
        if sample_rel is None:
            print(f"  ! {template_id} 找不到样本视频（{keyword}），跳过")
            continue
        sig = sigs[sample_rel]
        hashes = list(sig.hashes[:length])
        brightness = list(sig.brightness[:length]) if sig.brightness else []
        old = len(template.hashes)
        template.hashes = hashes
        template.brightness = brightness
        template.sample = sample_rel
        template.samples += 1
        template.status = TemplateStatus.CONFIRMED
        print(f"  修正 {template_id}：{old} → {length} 帧（{sample_rel}）")
        covered_reps.add(sample_rel)

    # ---- 2. 其余变体：机器推断的候选模板 ----
    created = 0
    bootstrapped: set = set()
    for variant in variants:
        rep = variant["representative"]
        if rep in covered_reps:
            continue
        sig = sigs.get(rep)
        if sig is None:
            continue
        length = int(variant["length"])
        if length <= 0 or length > len(sig.hashes):
            continue
        hashes = list(sig.hashes[:length])
        brightness = list(sig.brightness[:length]) if sig.brightness else []
        note = (
            "边界由跨片共享自动推断"
            + ("（组内一致，较为可靠）" if variant["boundary_source"] == "group" else "（该变体只有 1 个样本，未验证）")
        )
        template, is_new = templates.learn_from_detection(
            bank, hashes, sample=rep,
            label=f"站方推广片·变体·{length}s",
            brightness=brightness,
        )
        if is_new:
            created += 1
            print(f"  新建候选 {template.id}：{length} 帧  {rep}")
        else:
            print(f"  合并到 {template.id}：{length} 帧  {rep}")
        bootstrapped.add(template.id)
        template.note = note
        if template.id not in VERIFIED:
            # 已经被人工确认过的那 8 支不能降级 ——
            # 变体合并进它们是好事（说明确实是同一支推广片）
            template.status = TemplateStatus.CANDIDATE

    # ---- 3. 自动推断的模板一律保持「候选」 ----
    #
    # ``learn_from_detection`` 的规则是「被机器认出第二次就升格」——
    # 但这里是一口气把几十个变体灌进库，**边界从来没被人眼确认过**，
    # 升格了就等于自动开刀。升格这件事只能由人来触发。
    for template in bank.templates:
        if template.id in bootstrapped and template.id not in VERIFIED:
            template.status = TemplateStatus.CANDIDATE

    templates.save_bank(bank, BANK_PATH)
    confirmed = sum(1 for t in bank.templates if t.status is TemplateStatus.CONFIRMED)
    print(f"\n模板库现有 {len(bank.templates)} 支模板"
          f"（已确认 {confirmed} 支 / 候选 {len(bank.templates) - confirmed} 支），已写入 {BANK_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
