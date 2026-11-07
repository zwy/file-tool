"""模板库：广告指纹的存取与学习。

模板库是一个 YAML 文件（默认 ``rules/ad_templates.yaml``），和
``rules/numbering.yaml`` 同构 —— 人类可读、可 diff、可回滚。

## 学习一条模板会发生什么

``ad-cut learn VIDEO --end 12.5`` 做的事情：

1. 取出 VIDEO 开头 12.5 秒的指纹
2. 拿去和库里现有的模板比
3. 命中已有模板 → **合并**（samples + 1，状态转 confirmed）
4. 没命中 → **新建**一条模板

这条模板从此就代表「这种片头广告」。以后凡是片头指纹能对上它的视频，
都会自动被裁。

## 为什么模板要按「精确长度」合并

同一支广告被重新编码后，长度可能差一两帧（多一个黑场帧之类）。
差得更多就说明**不是同一支广告** —— 比如同一档节目换了新的片头，
前 3 秒一样、后面不一样。这类要建成两条模板，不能混。

混在一起的代价是：模板被拉长，匹配时连续段提前断掉，
裁剪点会被算到广告中间去 —— 那比多裁一集更糟。
"""
from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from . import fingerprint
from .models import (
    AdTemplate,
    NegativeTemplate,
    TemplateBank,
    TemplateStatus,
)

# 合并判定：连续匹配至少占模板的这个比例
MERGE_PREFIX_RATIO = 0.85
# 长度差超过这么多帧就认为是不同的广告
MERGE_MAX_LEN_DELTA = 2


class TemplateError(RuntimeError):
    """模板库读写 / 学习失败。"""


def load_bank(path: Path) -> TemplateBank:
    """读取模板库；文件不存在就返回空库。"""
    if not path.exists():
        return TemplateBank()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TemplateError(f"模板库解析失败：{path}\n{exc}") from exc
    try:
        return TemplateBank.model_validate(raw)
    except Exception as exc:  # pydantic 校验错误
        raise TemplateError(f"模板库内容不合法：{path}\n{exc}") from exc


def save_bank(bank: TemplateBank, path: Path) -> None:
    """原子写回模板库。

    先写同目录下的临时文件再 ``os.replace`` —— 中途断电 / Ctrl-C
    不会留下半个 YAML 把模板库搞坏。模板库是长期资产，坏了很麻烦。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = bank.model_dump(mode="json")
    text = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    header = (
        "# 片头广告模板库 —— 由 `file-tool ad-cut learn` 自动维护。\n"
        "#\n"
        "# 每条模板存一支广告的逐帧感知哈希（dHash）。长度即广告时长。\n"
        "# 这份文件是长期资产：手工改坏了匹配就全乱了，改之前先备份。\n"
    )
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=".ad_templates-",
        suffix=".yaml", delete=False,
    )
    try:
        with handle:
            handle.write(header)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _today() -> str:
    return date.today().isoformat()


def find_matching_template(
    bank: TemplateBank,
    hashes: List[int],
    tolerance: int = fingerprint.DEFAULT_TOLERANCE,
    brightness: Optional[List[float]] = None,
) -> Tuple[Optional[AdTemplate], fingerprint.MatchResult]:
    """在模板库里找最匹配的一条。"""
    best: Optional[AdTemplate] = None
    best_result = fingerprint.MatchResult(matched=False)
    for template in bank.templates:
        result = fingerprint.match(
            template.hashes, hashes, tolerance=tolerance,
            template_brightness=template.brightness or None,
            video_brightness=brightness or None,
        )
        if best is None or fingerprint._better(result, best_result):
            best = template
            best_result = result
    return best, best_result


def is_negative(
    bank: TemplateBank,
    hashes: List[int],
    tolerance: int = fingerprint.DEFAULT_TOLERANCE,
    brightness: Optional[List[float]] = None,
) -> Optional[NegativeTemplate]:
    """这个片头是不是被人工否过（= 这里面其实没有广告）。"""
    for negative in bank.negatives:
        result = fingerprint.match(
            negative.hashes, hashes, tolerance=tolerance,
            template_brightness=negative.brightness or None,
            video_brightness=brightness or None,
        )
        if result.prefix_len >= max(3, int(len(negative.hashes) * 0.7)):
            return negative
    return None


def learn(
    bank: TemplateBank,
    hashes: List[int],
    sample: str,
    label: str = "",
    fps: float = 1.0,
    note: str = "",
    brightness: Optional[List[float]] = None,
) -> Tuple[AdTemplate, bool]:
    """把一段广告指纹学进模板库。

    返回 ``(模板, 是否新建)``。命中已有模板时合并，否则新建。
    """
    if not hashes:
        raise TemplateError("没有可学习的指纹（片头太短？）")

    template, result = find_matching_template(bank, hashes, brightness=brightness)
    if template is not None:
        length_delta = abs(len(template.hashes) - len(hashes))
        prefix_ok = result.prefix_len >= max(
            fingerprint.MIN_PREFIX_FRAMES,
            int(min(len(template.hashes), len(hashes)) * MERGE_PREFIX_RATIO),
        )
        if prefix_ok and length_delta <= MERGE_MAX_LEN_DELTA:
            template.samples += 1
            template.status = TemplateStatus.CONFIRMED
            template.last_seen = _today()
            if not template.first_seen:
                template.first_seen = _today()
            if label and not template.label:
                template.label = label
            if note:
                template.note = note
            return template, False

    new_template = AdTemplate(
        id=bank.next_id(),
        label=label,
        hashes=list(hashes),
        brightness=list(brightness or []),
        fps=fps,
        sample=sample,
        samples=1,
        status=TemplateStatus.CONFIRMED,
        first_seen=_today(),
        last_seen=_today(),
        note=note,
    )
    bank.templates.append(new_template)
    return new_template, True


def learn_from_detection(
    bank: TemplateBank,
    hashes: List[int],
    sample: str,
    label: str = "",
    fps: float = 1.0,
    brightness: Optional[List[float]] = None,
) -> Tuple[AdTemplate, bool]:
    """把一次**自动检测**的结果登记进模板库。

    和 :func:`learn` 的区别只是来源：这条指纹是机器自己认出来的，
    不是人划的。所以它只让模板的 ``samples`` 增长，
    状态要攒够次数才升到 confirmed —— 机器认出来的第一回，
    不足以让后续视频自动开刀。
    """
    template, result = find_matching_template(bank, hashes, brightness=brightness)
    if template is not None:
        length_delta = abs(len(template.hashes) - len(hashes))
        prefix_ok = result.prefix_len >= max(
            fingerprint.MIN_PREFIX_FRAMES,
            int(min(len(template.hashes), len(hashes)) * MERGE_PREFIX_RATIO),
        )
        if prefix_ok and length_delta <= MERGE_MAX_LEN_DELTA:
            template.samples += 1
            template.last_seen = _today()
            if template.samples >= 2 and template.status is TemplateStatus.CANDIDATE:
                template.status = TemplateStatus.CONFIRMED
            return template, False

    new_template = AdTemplate(
        id=bank.next_id(),
        label=label,
        hashes=list(hashes),
        brightness=list(brightness or []),
        fps=fps,
        sample=sample,
        samples=1,
        status=TemplateStatus.CANDIDATE,
        first_seen=_today(),
        last_seen=_today(),
    )
    bank.templates.append(new_template)
    return new_template, True


def reject(
    bank: TemplateBank,
    hashes: List[int],
    sample: str,
    reason: str = "",
    fps: float = 1.0,
    brightness: Optional[List[float]] = None,
) -> NegativeTemplate:
    """记一条负样本：这个片头没有广告，别再切它。"""
    if not hashes:
        raise TemplateError("没有可记录的负样本指纹（片头太短？）")
    negative = NegativeTemplate(
        id=f"neg-{len(bank.negatives) + 1:04d}",
        hashes=list(hashes),
        brightness=list(brightness or []),
        fps=fps,
        sample=sample,
        reason=reason,
        created=_today(),
    )
    bank.negatives.append(negative)
    return negative


def remove_template(bank: TemplateBank, template_id: str) -> bool:
    """删掉一条模板。"""
    before = len(bank.templates)
    bank.templates = [t for t in bank.templates if t.id != template_id]
    return len(bank.templates) != before
