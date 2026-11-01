"""规则引擎：加载 YAML 规则集，生成改名计划。

设计要点
--------
- **剥离与匹配分离**：噪声剥离（水印域名、重复副本标记）是纯文本操作；
  番号匹配是领域知识。分开后校规则时只需动 ``matches`` 段。
- **剥离文本会被捕获并回贴**：``aavv38.xyz@SDMM028`` 里的域名不参与番号匹配，
  但 ``SDMM030AC(1)`` 的 ``(1)`` 是「重复副本」语义，必须回贴，
  否则会和 ``SDMM030AC`` 撞名。
- **纯函数**：``build_plan`` 不碰文件系统，只根据目录名列表算目标名；
  真正改名由 ``apply_plan`` 负责。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from file_tool.models import (
    Action,
    MatchRule,
    Plan,
    PlanItem,
    PreprocessRule,
    RuleSet,
)


class RuleError(Exception):
    """规则文件无法解析或结构非法。"""


def load_ruleset(path: Path) -> RuleSet:
    """从 YAML 文件加载规则集。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuleError(f"无法读取规则文件 {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RuleError(f"规则文件 YAML 解析失败 {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuleError(f"规则文件 {path} 顶层必须是映射（mapping）")

    # preprocess 的 capture 字段需要和 pattern 的命名分组一致
    for item in raw.get("preprocess", []) or []:
        if not isinstance(item, dict):
            raise RuleError(f"规则文件 {path} 中 preprocess 条目必须是映射")
        capture = item.get("capture") or ""
        if capture:
            groups = set(re.compile(item["pattern"]).groupindex)
            if capture not in groups:
                raise RuleError(
                    f"规则 {item.get('name')} 的 capture={capture!r} "
                    f"在 pattern 中没有对应分组（已有分组：{sorted(groups) or '无'}）"
                )

    try:
        return RuleSet.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic 校验错误统一转成友好提示
        raise RuleError(f"规则文件 {path} 校验失败：{exc}") from exc


def strip_noise(
    name: str, preprocess: Sequence[PreprocessRule]
) -> Tuple[str, Dict[str, str]]:
    """按顺序执行预处理规则，返回 (核心串, 捕获组字典)。

    条件规则（``conditional=True``）不在这里决定是否生效 —— 它需要知道
    剥离后能否匹配番号规则，交由 :func:`resolve_target` 统一处理。
    """
    captured: Dict[str, str] = {}
    core = name
    for rule in preprocess:
        if rule.conditional:
            continue
        match = rule.compiled().search(core)
        if not match:
            continue
        core = rule.compiled().sub(rule.replacement, core, count=1)
        if rule.capture:
            value = match.groupdict().get(rule.capture)
            if value is not None:
                captured[rule.capture] = value
    return core, captured


def _apply_one(
    core: str, rule: PreprocessRule
) -> Optional[Tuple[str, str]]:
    """尝试应用一条剥离规则，返回 (新核心串, 捕获值)。不匹配则返回 None。"""
    match = rule.compiled().search(core)
    if not match:
        return None
    new_core = rule.compiled().sub(rule.replacement, core, count=1)
    value = ""
    if rule.capture:
        value = match.groupdict().get(rule.capture) or ""
    return new_core, value


def _render(template: str, values: Dict[str, str]) -> str:
    """回填模板；未提供的占位符留空，不抛异常。"""

    class _Safe(dict):
        def __missing__(self, key: str) -> str:  # noqa: D105
            return ""

    return template.format_map(_Safe(values))


def _match_core(
    core: str,
    captured: Dict[str, str],
    matches: Sequence[MatchRule],
    dup_sources: Sequence[str] = ("dup",),
) -> Optional[Tuple[str, str, str, str]]:
    """在核心串上匹配番号规则。

    返回 (基础名, 完整目标名, 规则名, 说明)。

    基础名 = 不含重复副本标记的规范名，是「同一作品」的判定键；
    完整目标名 = 基础名 + 回贴的重复标记，是实际落盘用的名字。
    """
    for rule in matches:
        match = rule.compiled().fullmatch(core)
        if not match:
            continue
        values: Dict[str, str] = dict(match.groupdict())
        values["raw"] = core
        dup = _pick_dup(captured, dup_sources)
        if dup is not None:
            values["dup"] = dup
        base = _render(rule.template, values)
        target = base
        if rule.dup_suffix is not None and dup is not None:
            target = base + _render(rule.dup_suffix, values)
        return base, target, rule.name, rule.description
    return None


def _pick_dup(captured: Dict[str, str], dup_sources: Sequence[str]) -> Optional[str]:
    """按优先级取出重复副本标记。"""
    for source in dup_sources:
        value = captured.get(source)
        if value:
            return value
    return None


def resolve_target(
    core: str,
    captured: Dict[str, str],
    matches: Sequence[MatchRule],
    preprocess: Sequence[PreprocessRule] = (),
    dup_sources: Sequence[str] = ("dup",),
) -> Tuple[Optional[str], str, str, str, str]:
    """在核心串上匹配番号规则。

    返回 (基础名, 目标名, 规则名, 说明, 最终核心串)。

    基础名不含重复副本标记，是「同一作品」的判定键，merge 阶段靠它分组。

    条件剥离规则的判定逻辑：
    对每条条件规则，尝试剥掉它；如果剥掉后能匹配到番号规则，
    而**不剥**时匹配不到（或匹配结果不同），就采纳这次剥离。
    这样 ``DOW24072205-2`` 会剥掉 ``-2``，而 ``MAAN-961`` 保持原样。
    """
    baseline = _match_core(core, captured, matches, dup_sources)

    best = baseline
    for rule in preprocess:
        if not rule.conditional:
            continue
        attempt = _apply_one(core, rule)
        if attempt is None:
            continue
        new_core, value = attempt
        new_captured = dict(captured)
        if rule.capture and value:
            new_captured[rule.capture] = value
        candidate = _match_core(new_core, new_captured, matches, dup_sources)
        if candidate is None:
            continue
        # 剥掉之后能匹配、且匹配结果与不剥不同 → 采纳
        if baseline is None or candidate[1] != baseline[1]:
            core = new_core
            captured = new_captured
            baseline = candidate

    if baseline is None:
        return None, "", "", "", core
    return baseline[0], baseline[1], baseline[2], baseline[3], core


def build_plan(
    root: Path,
    ruleset: RuleSet,
    names: Sequence[str],
    existing: Optional[Sequence[str]] = None,
) -> Plan:
    """根据目录名列表生成改名计划。

    ``existing`` 是根目录下已存在的全部条目名（含待改名目录本身），
    用于检测目标名冲突。
    """
    existing_set = set(existing) if existing is not None else set(names)
    ordered = ruleset.ordered_matches()
    dup_sources = ruleset.dup_sources or ["dup"]
    items: List[PlanItem] = []
    planned: Dict[str, str] = {}  # 目标名 -> 原始名，检测批次内重名

    for name in names:
        core, captured = strip_noise(name, ruleset.preprocess)
        base, target, rule_name, desc, final_core = resolve_target(
            core, captured, ordered, ruleset.preprocess, dup_sources
        )

        if not target:
            items.append(
                PlanItem(
                    original=name,
                    stripped=final_core,
                    target=name,
                    base=base or "",
                    action=Action.SKIP,
                    reason=desc or "未匹配到番号规则",
                )
            )
            continue

        if target == name:
            items.append(
                PlanItem(
                    original=name,
                    stripped=final_core,
                    target=name,
                    base=base or name,
                    action=Action.KEEP,
                    rule=rule_name,
                    reason="已是规范名",
                )
            )
            continue

        if target in existing_set and target != name:
            items.append(
                PlanItem(
                    original=name,
                    stripped=final_core,
                    target=target,
                    base=base or target,
                    action=Action.CONFLICT,
                    rule=rule_name,
                    reason=f"目标名已存在：{target}",
                )
            )
            continue

        if target in planned:
            items.append(
                PlanItem(
                    original=name,
                    stripped=final_core,
                    target=target,
                    base=base or target,
                    action=Action.CONFLICT,
                    rule=rule_name,
                    reason=f"与 {planned[target]} 目标名重复",
                )
            )
            continue

        planned[target] = name
        items.append(
            PlanItem(
                original=name,
                stripped=final_core,
                target=target,
                base=base or target,
                action=Action.RENAME,
                rule=rule_name,
                reason=desc or "匹配番号规则",
            )
        )

    return Plan(root=str(root), items=items)


def apply_plan(root: Path, plan: Plan) -> List[Tuple[str, str]]:
    """执行改名，返回 (原名, 新名) 列表。

    冲突时用「加序号」兜底：``SDMM028`` 已存在时改名为 ``SDMM028 (2)``。
    """
    done: List[Tuple[str, str]] = []
    for item in plan.items:
        if item.action is not Action.RENAME:
            continue
        src = root / item.original
        dst = root / item.target
        if not src.exists():
            continue
        if dst.exists():
            stem, suffix = _split_suffix(item.target)
            counter = 2
            while (root / f"{stem} ({counter}){suffix}").exists():
                counter += 1
            dst = root / f"{stem} ({counter}){suffix}"
        src.rename(dst)
        done.append((item.original, dst.name))
    return done


def _split_suffix(name: str) -> Tuple[str, str]:
    """把 ``abc.mp4`` 拆成 (``abc``, ``.mp4``），无扩展名时后缀为空。"""
    if "." in name:
        stem, _, suffix = name.rpartition(".")
        if stem and "/" not in suffix:
            return stem, f".{suffix}"
    return name, ""
