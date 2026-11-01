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
from pydantic import BaseModel, Field

from file_tool import fscompat
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


# 下载站水印域名的后缀白名单
# ----------------------------
# 这份列表原先在 rules/numbering.yaml 的 5 条 preprocess 规则里各抄一遍，
# 抄漏一个就有一条规则认不出那种域名。实测踩过的坑：G 盘的
# ``wbrr.life@ADN-683CX.mp4`` 因为 ``.life`` 没抄进去，剥不掉域名，
# 文件名就一直规范化不了。
#
# 现在规则文件里写 ``{tlds}`` 占位，加载时由这里统一注入 —— 加后缀只改这一处。
#
# 为什么是白名单而不是「任意后缀都行」：白名单能挡住
# ``xxx.jpg@番号`` 这类**文件名**里的 @，避免把图片名当域名剥掉。
WATERMARK_TLDS = (
    "com net org xyz cc tv me info top club shop wang live site space pw "
    "fun icu vip life ink pro store ltd biz art xxx lol monster beauty cam "
    "sexy date faith love one global zone".split()
)


def _inject_tlds(raw: dict) -> dict:
    """把 ``{tlds}`` 占位替换成实际的域名后缀列表。

    规则文件里写 ``(?:{tlds})``，这里替换成 ``(?:com|net|org|...)``。
    """
    tlds = "|".join(WATERMARK_TLDS)
    for section in ("preprocess", "matches"):
        for item in raw.get(section) or []:
            if not isinstance(item, dict):
                continue
            pattern = item.get("pattern")
            if isinstance(pattern, str) and "{tlds}" in pattern:
                item["pattern"] = pattern.replace("{tlds}", tlds)
    return raw


class ApplyResult(BaseModel):
    """一次改名执行的结果明细。"""

    renamed: List[Tuple[str, str]] = Field(default_factory=list)
    """成功改名的 (原名, 新名)。"""
    failed: List[Tuple[str, str]] = Field(default_factory=list)
    """改失败的 (原名, 原因) —— 通常是文件被占用或无权限。"""


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

    raw = _inject_tlds(raw)

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
    """回填模板；未提供的占位符留空，不抛异常。

    注意 ``None`` 也要当空串处理：正则里没参与匹配的**可选分组**取值是
    ``None``（如 ``(?P<ver>-[A-Z]{1,3})?`` 在 ``xfxfz-14681`` 上没匹配上），
    直接渲染会得到字符串 ``"None"``，把 ``xfxfz-14681`` 变成
    ``xfxfz-14681None``。
    """

    class _Safe(dict):
        def __missing__(self, key: str) -> str:  # noqa: D105
            return ""

    safe = _Safe({k: ("" if v is None else v) for k, v in values.items()})
    return template.format_map(safe)


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
    exclude: Sequence[str] = (),
) -> Tuple[Optional[str], str, str, str, str]:
    """在核心串上匹配番号规则。

    返回 (基础名, 目标名, 规则名, 说明, 最终核心串)。

    基础名不含重复副本标记，是「同一作品」的判定键，merge 阶段靠它分组。

    ``exclude`` 是永不视为番号的名称形态（见 :attr:`RuleSet.exclude`）。
    命中时直接返回「不匹配」—— 国产内容的编号（如 ``FLUP-1131428``）
    因此不会被套上番号规范。

    条件剥离规则的判定逻辑：
    **只有在基准匹配不上时才尝试条件剥离** —— 否则剥离结果会覆盖掉正确的解析，
    把已经规范的目录改坏（详见下方注释）。
    这样 ``DOW24072205-2`` 会剥掉 ``-2``，而 ``MAAN-961`` 保持原样。
    """
    # 排除名单优先于一切匹配：国产内容的编号不该被套番号规范
    if exclude and any(re.search(pattern, core) for pattern in exclude):
        return None, "", "", "命中排除名单（非番号形态）", core

    baseline = _match_core(core, captured, matches, dup_sources)

    # 条件剥离**只在基准匹配不上时才尝试**。
    # 这是关键：`DOW-240706-24` 本身就能被日期型规则正确解析，
    # 而剥掉尾部 `-24` 后剩下的 `DOW-240706` 会被「厂牌-数字」规则抢先匹配，
    # 得到一个错误的结果 `DOW-240706 (24)` —— 规范名会在第二次运行时被改坏。
    #
    # `MAAN-961` 与 `DOW24072205-2` 的区分不依赖这条：
    # 前者剥掉 `-961` 后剩下 `MAAN` 匹配不上任何规则，剥离自然被拒绝；
    # 后者基准本来就匹配不上，剥离才有机会生效。
    if baseline is None:
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
            core = new_core
            captured = new_captured
            baseline = candidate
            break

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
    # 冲突判定一律**忽略大小写**。规则模板保留厂牌的原始大小写
    # （club810 -> club-810、CLUB810 -> CLUB-810），两个目标只差大小写，
    # 在 exFAT 上是同一个名字。判定交给文件系统的话，在大小写敏感的
    # 卷上会漏判，整理完再拷到 exFAT 就撞名了。
    existing_keys = {fscompat.same_name_key(n) for n in existing_set}
    ordered = ruleset.ordered_matches()
    dup_sources = ruleset.dup_sources or ["dup"]
    items: List[PlanItem] = []
    planned: Dict[str, str] = {}  # 目标名 -> 原始名，检测批次内重名
    planned_keys: Dict[str, str] = {}  # 同上，但忽略大小写

    for name in names:
        core, captured = strip_noise(name, ruleset.preprocess)
        base, target, rule_name, desc, final_core = resolve_target(
            core, captured, ordered, ruleset.preprocess, dup_sources, ruleset.exclude
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

        if fscompat.same_name_key(target) in existing_keys and target != name:
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

        if fscompat.same_name_key(target) in planned_keys:
            items.append(
                PlanItem(
                    original=name,
                    stripped=final_core,
                    target=target,
                    base=base or target,
                    action=Action.CONFLICT,
                    rule=rule_name,
                    reason=f"与 {planned_keys[fscompat.same_name_key(target)]} 目标名重复",
                )
            )
            continue

        planned[target] = name
        planned_keys[fscompat.same_name_key(target)] = name
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


def apply_plan(root: Path, plan: Plan) -> "ApplyResult":
    """执行改名，返回改名与失败的明细。

    冲突时用「加序号」兜底：``SDMM028`` 已存在时改名为 ``SDMM028 (2)``。

    三种情况必须分清，否则会改坏数据：

    - **目标名已被另一个文件占用** → 加序号，绝不覆盖
    - **目标就是源本身、只差大小写** → 两步改名（大小写不敏感的卷上
      直接 rename 会「无事发生」）
    - **源文件被占用 / 无权限** → 记为失败，继续处理下一个
      （Windows 上文件被资源管理器或播放器占住是常态，macOS 上不存在）
    """
    renamed: List[Tuple[str, str]] = []
    failed: List[Tuple[str, str]] = []

    # 根目录的条目索引（忽略大小写）建一次 —— 逐条查目录是 O(n²)，
    # 3000 个顶层目录实测 14 秒。每次改名后同步更新索引。
    index = fscompat.build_name_index(root)

    for item in plan.items:
        if item.action is not Action.RENAME:
            continue
        src = root / item.original
        dst = root / item.target
        if not src.exists():
            fscompat.index_drop(index, item.original)
            continue
        try:
            # 目标名是否被占用 —— **忽略大小写**判定，见 fscompat.colliding_entries。
            # 不能用 dst.exists()：那取决于卷的大小写语义，会让同一个库
            # 在不同卷上整理出不同结果。
            blockers = fscompat.colliding_entries(root, item.target, index)
            if not blockers and dst.exists():
                # 索引建好之后、真正改名之前，目标名被别的进程占了 ——
                # 以磁盘为准。宁可多判一次冲突，也不能覆盖掉别人的文件。
                blockers = [dst]
            if blockers:
                same = []
                others = []
                for blocker in blockers:
                    if fscompat.is_same_file(src, blocker):
                        same.append(blocker)
                    else:
                        others.append(blocker)
                if same and not others:
                    # 仅大小写不同 —— 是同一个文件，必须走两步改名
                    fscompat.rename_case_only(src, dst)
                    fscompat.index_drop(index, item.original)
                    fscompat.index_add(index, dst)
                    renamed.append((item.original, dst.name))
                    continue
                # 真冲突：另一个文件占了目标名，加序号兜底
                stem, suffix = _split_suffix(item.target)
                counter = 2
                while fscompat.colliding_entries(root, f"{stem} ({counter}){suffix}", index):
                    counter += 1
                dst = root / f"{stem} ({counter}){suffix}"
            src.rename(dst)
            fscompat.index_drop(index, item.original)
            fscompat.index_add(index, dst)
            renamed.append((item.original, dst.name))
        except OSError as exc:
            failed.append((item.original, str(exc)))

    return ApplyResult(renamed=renamed, failed=failed)


def _split_suffix(name: str) -> Tuple[str, str]:
    """把 ``abc.mp4`` 拆成 (``abc``, ``.mp4``），无扩展名时后缀为空。"""
    if "." in name:
        stem, _, suffix = name.rpartition(".")
        if stem and "/" not in suffix:
            return stem, f".{suffix}"
    return name, ""
