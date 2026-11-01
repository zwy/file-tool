"""merge 阶段：识别「同一作品的重复目录」，按内容比对决定是否合并。

为什么必须比对内容
------------------
目录名带 ``(1)`` / `` 2`` / ``-2`` **不代表是复制副本**。实测 /Volumes/P7000Z/R：

- ``SDMM030AC`` vs ``SDMM030AC(1)``  → mp4 大小完全相同 → 真副本
- ``SDMM030BC`` vs ``SDMM030BC(1)``  → 封面 jpg 相同、mp4 大小差 4 倍 → **不同画质版本**
- ``DOW24072205`` vs ``DOW24072205-2`` → 一个装 1-10.mp4、另一个装 11-14.mp4 → **分P**

只看名字就合并会直接销毁数据。所以判定依据是**文件内容指纹**，名字只是候选条件。

判定结果
--------
``DUPLICATE``  内容完全一致 → 可合并（把副本并进主目录，删除副本）
``DIFFERENT``  内容不同     → 不可合并，各自保留
``PARTIAL``    部分文件相同、部分不同 → 保守处理：只并入不冲突的部分，冲突文件保留并报告

执行语义
--------
合并 = 把副本目录里的文件搬进主目录；同名且内容相同的文件直接丢弃副本那份；
同名但内容不同的文件**保留在副本目录**（不覆盖），并在结果里报告冲突。
这样任何情况下都不会丢数据。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from file_tool.models import RuleSet

# 指纹读取的字节数：首尾各读这么多，避免为几十 GB 的视频做全量哈希
_CHUNK = 1 << 20  # 1 MiB


class MergeVerdict(str, Enum):
    """同一基础名下多个目录的关系判定。"""

    DUPLICATE = "duplicate"  # 内容完全一致，可合并
    DIFFERENT = "different"  # 内容完全不同，各自保留
    PARTIAL = "partial"      # 部分重叠，只并入不冲突的部分


class MergeAction(str, Enum):
    """单个目录在合并中的处置。"""

    KEEP = "keep"        # 主目录，保留
    MERGE = "merge"      # 副本，内容并入主目录后删除
    SKIP = "skip"        # 不动（内容不同）


class FileEntry(BaseModel):
    """一个文件的指纹条目。"""

    relpath: str
    size: int
    digest: str


class MergeGroup(BaseModel):
    """一个基础名对应的目录组。"""

    base: str
    dirs: List[str] = Field(default_factory=list)
    verdict: MergeVerdict = MergeVerdict.DIFFERENT
    primary: str = ""
    """主目录：保留不动的那个。"""
    detail: str = ""
    identical_files: int = 0
    """组内各目录完全一致的文件数（内容相同的文件）。"""
    conflicting_files: List[str] = Field(default_factory=list)
    """同名但内容不同的文件（相对主目录），不会被覆盖。"""
    unique_files: List[str] = Field(default_factory=list)
    """只在副本里存在、可以安全并入主目录的文件。"""

    @property
    def mergeable(self) -> bool:
        return self.verdict is not MergeVerdict.DIFFERENT


class MergePlan(BaseModel):
    """一次合并任务的完整计划。"""

    root: str
    groups: List[MergeGroup] = Field(default_factory=list)

    @property
    def duplicate_groups(self) -> List[MergeGroup]:
        return [g for g in self.groups if g.verdict is MergeVerdict.DUPLICATE]

    @property
    def different_groups(self) -> List[MergeGroup]:
        return [g for g in self.groups if g.verdict is MergeVerdict.DIFFERENT]

    @property
    def partial_groups(self) -> List[MergeGroup]:
        return [g for g in self.groups if g.verdict is MergeVerdict.PARTIAL]

    def summary(self) -> Dict[str, int]:
        return {
            "groups": len(self.groups),
            "duplicate": len(self.duplicate_groups),
            "partial": len(self.partial_groups),
            "different": len(self.different_groups),
        }


def _is_hidden(rel: Path) -> bool:
    """macOS 的 ``._xxx`` 资源叉文件与 ``.DS_Store`` 不参与比对。"""
    if rel.name == ".DS_Store":
        return True
    return any(part.startswith("._") for part in rel.parts)


def file_digest(path: Path) -> str:
    """文件快速指纹：大小 + 首尾各 1MiB 的 sha256。

    不用全量哈希是因为视频动辄几十 GB；首尾 + 大小足以区分不同片源，
    代价是理论上存在极小的碰撞概率。对「是否同一份文件」的判定足够。
    """
    size = path.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with path.open("rb") as fh:
        h.update(fh.read(_CHUNK))
        if size > 2 * _CHUNK:
            fh.seek(-_CHUNK, os.SEEK_END)
            h.update(fh.read(_CHUNK))
    return h.hexdigest()[:16]


def fingerprint_dir(root: Path) -> Dict[str, FileEntry]:
    """递归收集目录下所有文件的指纹，返回 {相对路径: 条目}。"""
    entries: Dict[str, FileEntry] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _is_hidden(rel):
            continue
        entries[str(rel)] = FileEntry(
            relpath=str(rel),
            size=path.stat().st_size,
            digest=file_digest(path),
        )
    return entries


def _has_dup_marker(name: str) -> int:
    """目录名是否带重复副本标记。

    覆盖三种形态：``x (1)``（空格）、``x(1)``（括号）、``x-2``（连字符）。
    ``x(1)`` 可能藏在水印域名里（``aavv38(1).xyz@SDMM028``），
    这时也要算作副本。
    """
    if re.search(r"\(\d+\)$", name):
        return 1
    if re.search(r" \d+$", name):
        return 1
    if re.search(r"-\d+$", name):
        return 1
    if re.search(r"\(\d+\)\.[A-Za-z]{2,}@", name):
        return 1
    return 0


def compare_group(
    base: str,
    dirs: Sequence[str],
    root: Path,
) -> MergeGroup:
    """比对一个目录组的内容，给出合并判定。

    主目录选取：选**内容是其他目录超集**的那个 —— 合并的目标是把零散内容
    汇集到一个目录，主目录应当是「全集」。用文件名后缀判断不可靠
    （``aavv38(1).xyz@DVMM055A`` 与 ``aavv38.xyz@DVMM055A`` 文件数相同，
    但前者是副本），所以按实际内容算超集关系。
    """
    group = MergeGroup(base=base, dirs=list(dirs))

    fingerprints = {d: fingerprint_dir(root / d) for d in dirs}

    # 每个目录的内容集合
    content_sets = {d: set(fingerprints[d]) for d in dirs}

    def superset_score(name: str) -> Tuple[int, int, int]:
        # 1) 自己包含其他目录内容 -> 更像主目录（取负号让更大的排前面）
        covers = sum(
            1 for other in dirs
            if other != name and content_sets[other] <= content_sets[name]
        )
        # 2) 内容多的优先
        # 3) 无重复标记的优先（`x (1)` / `x(1)` / `x-2` 是副本，`x` 才是原始目录）
        return (-covers, -len(content_sets[name]), _has_dup_marker(name))

    primary = sorted(dirs, key=superset_score)[0]
    group.primary = primary

    primary_fp = fingerprints[primary]

    identical = 0
    conflicting: List[str] = []
    unique: List[str] = []

    for other in dirs:
        if other == primary:
            continue
        other_fp = fingerprints[other]
        for relpath, entry in other_fp.items():
            base_entry = primary_fp.get(relpath)
            if base_entry is None:
                unique.append(f"{other}:{relpath}")
            elif base_entry.digest == entry.digest:
                identical += 1
            else:
                conflicting.append(f"{other}:{relpath}")

    group.identical_files = identical
    group.conflicting_files = sorted(conflicting)
    group.unique_files = sorted(unique)

    if not conflicting and not unique:
        group.verdict = MergeVerdict.DUPLICATE
        group.detail = f"{len(dirs)} 个目录内容完全一致（{identical} 个文件）"
    elif conflicting and not unique:
        group.verdict = MergeVerdict.DIFFERENT
        group.detail = (
            f"{len(dirs)} 个目录内容不同：{len(conflicting)} 个同名文件内容冲突，"
            "属于不同版本/画质，保留各自独立"
        )
    else:
        group.verdict = MergeVerdict.PARTIAL
        group.detail = (
            f"{len(dirs)} 个目录部分重叠：{len(unique)} 个文件可并入，"
            f"{len(conflicting)} 个同名文件冲突保留"
        )

    return group


def build_merge_plan(
    root: Path,
    ruleset: RuleSet,
    names: Sequence[str],
) -> MergePlan:
    """按基础名分组并逐组判定，生成合并计划。

    只有**同一个基础名下有 2 个以上目录**才会触发内容比对 ——
    723 个目录里只有 10 组需要比对，避免无谓的 IO。

    分组键的取法很关键：目录名可能是**改名后**的形态（``DVMM-055A (1)``），
    这种名字带尾部重复标记，规则引擎解析不出番号。所以这里先剥掉尾部标记
    再算基础名，保证改名前后都能正确归组。
    """
    from file_tool import engine

    ordered = ruleset.ordered_matches()
    dup_sources = ruleset.dup_sources or ["dup"]

    buckets: Dict[str, List[str]] = {}
    for name in names:
        base = _resolve_base(name, ruleset, ordered, dup_sources, engine)
        if not base:
            continue
        buckets.setdefault(base, []).append(name)

    groups: List[MergeGroup] = []
    for base, dirs in sorted(buckets.items()):
        if len(dirs) < 2:
            continue
        groups.append(compare_group(base, dirs, root))

    return MergePlan(root=str(root), groups=groups)


def _resolve_base(
    name: str,
    ruleset: RuleSet,
    ordered,
    dup_sources,
    engine_mod,
) -> str:
    """算出一个目录的分组键（基础名）。

    先用规则引擎解析；解析不出来时，剥掉尾部重复标记再解析一次。
    """
    core, captured = engine_mod.strip_noise(name, ruleset.preprocess)
    base, _target, _rule, _desc, _core = engine_mod.resolve_target(
        core, captured, ordered, ruleset.preprocess, dup_sources
    )
    if base:
        return base

    # 改名后的形态：尾部带 ' (N)' / '(N)' / '-N'，规则解析不出番号
    stripped = re.sub(r"[ _-]?\(\d+\)$", "", name)
    stripped = re.sub(r"[ _-]\d+$", "", stripped)
    if stripped == name:
        return ""

    core2, captured2 = engine_mod.strip_noise(stripped, ruleset.preprocess)
    base2, _t, _r, _d, _c = engine_mod.resolve_target(
        core2, captured2, ordered, ruleset.preprocess, dup_sources
    )
    return base2 or ""


def apply_merge_plan(
    root: Path,
    plan: MergePlan,
    only_duplicates: bool = True,
) -> List[Dict[str, object]]:
    """执行合并。

    ``only_duplicates=True``（默认）时，只合并内容完全一致的组。
    内容不同的组一律不动 —— 那多半是不同版本或分P，合并会丢数据。

    返回每个目录的处置明细。
    """
    results: List[Dict[str, object]] = []

    for group in plan.groups:
        if group.verdict is MergeVerdict.DIFFERENT:
            for d in group.dirs:
                results.append({
                    "base": group.base,
                    "dir": d,
                    "action": MergeAction.SKIP.value,
                    "reason": group.detail,
                })
            continue

        if group.verdict is MergeVerdict.PARTIAL and only_duplicates:
            for d in group.dirs:
                results.append({
                    "base": group.base,
                    "dir": d,
                    "action": MergeAction.SKIP.value,
                    "reason": "部分重叠，保守模式不处理",
                })
            continue

        primary_dir = root / group.primary
        for other in group.dirs:
            if other == group.primary:
                results.append({
                    "base": group.base,
                    "dir": other,
                    "action": MergeAction.KEEP.value,
                    "reason": "主目录",
                })
                continue

            other_dir = root / other
            if not other_dir.exists():
                continue

            moved, skipped = _merge_into(other_dir, primary_dir)
            results.append({
                "base": group.base,
                "dir": other,
                "action": MergeAction.MERGE.value,
                "moved": moved,
                "skipped": skipped,
                "reason": f"并入 {group.primary}",
            })

            # 副本目录已空 -> 删除；仍有残留（冲突文件）-> 保留
            _remove_if_empty(other_dir)

    return results


def _merge_into(src: Path, dst: Path) -> Tuple[List[str], List[str]]:
    """把 src 下的文件并入 dst。

    - 目标不存在 → 移动过去
    - 目标存在且内容相同 → 删掉源文件（真重复）
    - 目标存在但内容不同 → 保留源文件，记为冲突（绝不覆盖）

    返回 (已并入的文件, 被保留的冲突文件)。
    """
    moved: List[str] = []
    skipped: List[str] = []

    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if _is_hidden(rel):
            # macOS 资源叉文件直接丢
            path.unlink(missing_ok=True)
            continue

        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        if not target.exists():
            shutil.move(str(path), str(target))
            moved.append(str(rel))
            continue

        if file_digest(target) == file_digest(path):
            path.unlink()
            moved.append(str(rel))
        else:
            skipped.append(str(rel))

    return moved, skipped


def _remove_if_empty(path: Path) -> None:
    """目录为空（只含空子目录）则删除，否则保留。"""
    if not path.exists():
        return
    for sub in sorted(path.rglob("*"), reverse=True):
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()
    if not any(path.iterdir()):
        shutil.rmtree(path)
