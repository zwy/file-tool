"""tidy：智能整理一个目录。

现有三个命令（rename / merge / archive）各自对应一种固定操作，
但真实磁盘上的目录形态是混杂的 —— 用户不该为每个目录手工挑命令。
tidy 自动判断形态、编排动作、给出统一的执行计划。

形态识别
--------
以**顶层**为界，靠「有多少个子目录名能被番号规则识别」来判定：

============  ==========================================  ==============
形态          特征                                        动作
============  ==========================================  ==============
``flat``      0 个子目录，文件平铺                         archive
``nested``    子目录名大多是番号                          rename + merge
``mixed``     番号目录 + 平铺文件同时存在                   archive + rename + merge
``loose``     有子目录，但没几个像番号                      只报告
============  ==========================================  ==============

为什么只看顶层
--------------
实测 ``/Volumes/P7000Z/R``：第 1 层是番号（``DOW24070403``），
第 2 层是**原始标题**（``《某中文标题》副标题...``）。
第二层不是番号，对它套番号规则只会破坏信息。所以递归只用于
**内容比对**（merge 需要看内部文件），不用于改名。

执行编排
--------
``merge`` 必须排在 ``rename`` 之前：合并后目录数减少，
改名时的冲突检测更准；反过来先改名，合并要在改名后的名字里
重新识别重复标记，多一道解析。

``archive`` 与其他两者互斥：它是把平铺文件装进新目录，
之后再对新目录做 rename / merge 才对。
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from file_tool import archive as archive_mod
from file_tool import engine
from file_tool import isolate as isolate_mod
from file_tool import merge as merge_mod
from file_tool import rename_files as rename_files_mod
from file_tool.models import Action, RuleSet


class Shape(str, Enum):
    """目录形态。"""

    FLAT = "flat"      # 纯平铺文件
    NESTED = "nested"  # 子目录是番号
    MIXED = "mixed"    # 番号目录 + 平铺文件
    LOOSE = "loose"    # 有子目录但不像番号


class StepKind(str, Enum):
    """计划中的动作类型。"""

    CLEAN = "clean"          # 清理系统垃圾文件
    ISOLATE = "isolate"      # 隔离不符合规范的目录
    ARCHIVE = "archive"      # 平铺文件分装
    MERGE = "merge"          # 合并重复目录
    RENAME = "rename"        # 改名（目录名）
    RENAME_FILES = "rename-files"  # 改名（文件名里的番号）


class TidyStep(BaseModel):
    """一个执行步骤。"""

    kind: StepKind
    detail: str = ""
    count: int = 0
    """涉及的文件/目录数量。"""
    names: List[str] = Field(default_factory=list)
    """涉及的名称（预览用，量大时截断）。"""
    dry_summary: str = ""


class TidyPlan(BaseModel):
    """一次 tidy 的完整计划。"""

    root: str
    shape: Shape = Shape.LOOSE
    shape_reason: str = ""
    steps: List[TidyStep] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    """给用户看的提示（无法处理的项、需要人工确认的项）。"""

    @property
    def total_actions(self) -> int:
        return sum(s.count for s in self.steps)

    def summary(self) -> Dict[str, object]:
        return {
            "shape": self.shape.value,
            "steps": {s.kind.value: s.count for s in self.steps},
            "total": self.total_actions,
        }


def detect_shape(
    root: Path,
    ruleset: RuleSet,
    names: Optional[Sequence[str]] = None,
) -> tuple[Shape, str, List[str], List[str]]:
    """识别目录形态。

    返回 (形态, 判定理由, 番号型子目录名, 平铺文件名)。
    """
    if names is None:
        names = sorted(p.name for p in root.iterdir())

    dirs: List[str] = []
    files: List[str] = []
    for name in names:
        path = root / name
        if path.is_dir():
            dirs.append(name)
        else:
            files.append(name)

    numbered = [d for d in dirs if _looks_numbered(d, ruleset)]
    loose = [d for d in dirs if d not in numbered]

    # 平铺文件里，排除 macOS 垃圾
    real_files = [f for f in files if not archive_mod.is_fork(f)]

    if not dirs:
        return (
            Shape.FLAT,
            f"没有子目录，{len(real_files)} 个平铺文件",
            [],
            files,
        )

    if not real_files:
        if len(numbered) >= max(1, len(dirs) // 2):
            return (
                Shape.NESTED,
                f"{len(numbered)}/{len(dirs)} 个子目录名可识别为番号",
                numbered,
                files,
            )
        return (
            Shape.LOOSE,
            f"只有 {len(numbered)}/{len(dirs)} 个子目录名像番号，多数不像",
            numbered,
            files,
        )

    if len(numbered) >= max(1, len(dirs) // 2):
        return (
            Shape.MIXED,
            f"{len(numbered)} 个番号目录 + {len(real_files)} 个平铺文件",
            numbered,
            files,
        )
    return (
        Shape.LOOSE,
        f"只有 {len(numbered)}/{len(dirs)} 个子目录名像番号，且有 {len(real_files)} 个平铺文件",
        numbered,
        files,
    )


def _looks_numbered(name: str, ruleset: RuleSet) -> bool:
    """目录名能否被番号规则识别为番号。"""
    ordered = ruleset.ordered_matches()
    dup_sources = ruleset.dup_sources or ["dup"]
    core, captured = engine.strip_noise(name, ruleset.preprocess)
    base, _target, _rule, _desc, _core = engine.resolve_target(
        core, captured, ordered, ruleset.preprocess, dup_sources,
        ruleset.exclude, ruleset.uppercase,
    )
    return bool(base)


def build_tidy_plan(
    root: Path,
    ruleset: RuleSet,
    names: Optional[Sequence[str]] = None,
    isolate_to: Optional[Path] = None,
    rename_file_names: bool = True,
) -> TidyPlan:
    """生成智能整理计划。

    ``isolate_to`` 非空时，额外加入「隔离不符合规范的目录」步骤。

    ``rename_file_names=False`` 时不做文件名规范化（只改目录名）。
    """
    if names is None:
        names = sorted(p.name for p in root.iterdir())

    shape, reason, numbered, files = detect_shape(root, ruleset, names)
    plan = TidyPlan(root=str(root), shape=shape, shape_reason=reason)

    if shape is Shape.LOOSE:
        plan.notes.append(
            "此目录的子目录名大多不像番号，未做任何改动。"
            "如果是想按番号整理，请先确认目录选对了。"
        )
        return plan

    # ---- Step 0: clean（系统垃圾，与形态无关）----
    forks = [n for n in names if not (root / n).is_dir() and archive_mod.is_fork(n)]
    if forks:
        plan.steps.append(
            TidyStep(
                kind=StepKind.CLEAN,
                detail="清理系统垃圾文件（.DS_Store / ._ 资源叉）",
                count=len(forks),
                names=forks[:20],
            )
        )

    # ---- Step 1: isolate（挪走不符合规范的目录）----
    if isolate_to is not None:
        iso_plan = isolate_mod.build_isolation_plan(root, isolate_to, ruleset, names)
        move_items = iso_plan.move_items
        if move_items:
            plan.steps.append(
                TidyStep(
                    kind=StepKind.ISOLATE,
                    detail=f"把不符合番号规范的目录挪到 {isolate_to}",
                    count=len(move_items),
                    names=[i.name for i in move_items],
                )
            )
        kept_with_codes = [i for i in iso_plan.keep_items if i.code_samples]
        if kept_with_codes:
            plan.notes.append(
                f"{len(kept_with_codes)} 个合集目录内含正规番号，已保留："
                f"{', '.join(i.name for i in kept_with_codes[:3])}"
                + ("…" if len(kept_with_codes) > 3 else "")
            )

    # ---- Step 1: merge（先做，目录数减少后改名冲突更少）----
    if shape in (Shape.NESTED, Shape.MIXED):
        merge_plan = merge_mod.build_merge_plan(root, ruleset, names)
        mergeable = merge_plan.duplicate_groups
        partial = merge_plan.partial_groups
        different = merge_plan.different_groups

        if mergeable or partial or different:
            step = TidyStep(
                kind=StepKind.MERGE,
                detail="把同名目录合并成一个（文件级撞名按体积分配）",
                count=len(mergeable) + len(partial) + len(different),
                names=[g.base for g in mergeable + partial + different],
            )
            plan.steps.append(step)

        if partial or different:
            plan.notes.append(
                f"{len(partial) + len(different)} 组内容不完全一致（{len(partial)} 组部分重叠、"
                f"{len(different)} 组内容不同），也会合并："
                f"{', '.join([g.base for g in (partial + different)][:5])}"
                + ("…" if len(partial) + len(different) > 5 else "")
                + "。同名文件按体积分配 —— 大的占规范名，小的改名 (2)；"
                "内容完全相同的只留一份。"
            )

    # ---- Step 2: archive（平铺文件分装）----
    if shape in (Shape.FLAT, Shape.MIXED):
        archive_plan = archive_mod.build_archive_plan(root, names)
        if archive_plan.groups:
            merged_count = len(archive_plan.merged_groups)
            detail = f"把平铺文件按番号分装进 {len(archive_plan.groups)} 个目录"
            if merged_count:
                detail += f"（{merged_count} 组由多个变体归一）"
            plan.steps.append(
                TidyStep(
                    kind=StepKind.ARCHIVE,
                    detail=detail,
                    count=archive_plan.total_files,
                    names=[g.name for g in archive_plan.groups],
                )
            )
        if archive_plan.forks:
            pass  # 垃圾由 CLEAN 步骤统一处理，此处不重复报告
        if archive_plan.untouched:
            # 排除垃圾文件（由 CLEAN 步骤处理），只报告真正的非媒体文件
            real_untouched = [
                n for n in archive_plan.untouched if not archive_mod.is_fork(n)
            ]
            if real_untouched:
                plan.notes.append(
                    f"{len(real_untouched)} 个非媒体文件原地保留："
                    f"{', '.join(real_untouched[:5])}"
                    + ("…" if len(real_untouched) > 5 else "")
                )
        if archive_plan.blocked:
            plan.notes.append(
                f"{len(archive_plan.blocked)} 组因目录名与现有文件冲突未处理："
                f"{', '.join(archive_plan.blocked)}"
            )

    # ---- Step 3: rename（番号目录规范化）----
    if shape in (Shape.NESTED, Shape.MIXED):
        # 只把**目录**交给改名引擎 —— 文件由 archive 步骤处理
        dir_names = [n for n in names if (root / n).is_dir()]
        rename_plan = engine.build_plan(root, ruleset, dir_names)
        to_rename = rename_plan.rename_items
        if to_rename:
            plan.steps.append(
                TidyStep(
                    kind=StepKind.RENAME,
                    detail="按番号规则规范化目录名",
                    count=len(to_rename),
                    names=[i.original for i in to_rename],
                )
            )
        skipped = rename_plan.skip_items
        if skipped:
            plan.notes.append(
                f"{len(skipped)} 个目录名不是番号，原样保留："
                f"{', '.join(i.original for i in skipped[:5])}"
                + ("…" if len(skipped) > 5 else "")
            )

    # ---- Step 4: rename-files（文件名里的番号规范化）----
    # 必须在 rename 之后：文件名的目标来自**所在目录的规范名**，
    # 目录名先规范化，文件才有正确的目标可对。
    if rename_file_names and shape in (Shape.NESTED, Shape.MIXED):
        file_plan = rename_files_mod.build_file_plan(root, ruleset)
        if file_plan.rename_items:
            plan.steps.append(
                TidyStep(
                    kind=StepKind.RENAME_FILES,
                    detail="把文件名里的番号规范化成所在目录的番号",
                    count=len(file_plan.rename_items),
                    names=[i.relpath for i in file_plan.rename_items],
                )
            )
        if file_plan.conflict_items:
            plan.notes.append(
                f"{len(file_plan.conflict_items)} 个文件的目标名已被占用，保持原样："
                + ", ".join(i.relpath for i in file_plan.conflict_items[:3])
                + ("…" if len(file_plan.conflict_items) > 3 else "")
            )

    if not plan.steps:
        plan.notes.append("目录已经是规范状态，无需改动。")

    return plan


def apply_tidy_plan(
    root: Path,
    ruleset: RuleSet,
    plan: TidyPlan,
    keep_separate: bool = False,
    clean_forks: bool = True,
    isolate_to: Optional[Path] = None,
) -> Dict[str, object]:
    """执行 tidy 计划。

    严格按 build_tidy_plan 编排的顺序执行：
    clean → isolate → merge → archive → rename。
    """
    names = sorted(p.name for p in root.iterdir())
    result: Dict[str, object] = {
        "merged": 0,
        "archived": 0,
        "renamed": 0,
        "renamed_files": 0,
        "isolated": 0,
        "forks_removed": 0,
        "conflicts": 0,
        "failed": 0,
    }

    for step in plan.steps:
        if step.kind is StepKind.CLEAN:
            removed = 0
            if clean_forks:
                for name in step.names:
                    path = root / name
                    try:
                        if path.is_file():
                            path.unlink()
                            removed += 1
                    except OSError:
                        continue
            result["forks_removed"] += removed
            names = sorted(p.name for p in root.iterdir())

        elif step.kind is StepKind.ISOLATE:
            if isolate_to is not None:
                iso_plan = isolate_mod.build_isolation_plan(
                    root, isolate_to, ruleset, names
                )
                iso_result = isolate_mod.apply_isolation_plan(
                    root, isolate_to, iso_plan
                )
                result["isolated"] = len(iso_result["moved"])
                result["failed"] = int(result["failed"]) + len(iso_result["failed"])
                names = sorted(p.name for p in root.iterdir())

        elif step.kind is StepKind.MERGE:
            merge_plan = merge_mod.build_merge_plan(root, ruleset, names)
            merge_results = merge_mod.apply_merge_plan(
                root, merge_plan, only_duplicates=keep_separate
            )
            result["merged"] = sum(
                1 for r in merge_results if r["action"] == merge_mod.MergeAction.MERGE.value
            )
            result["failed"] = int(result["failed"]) + sum(
                len(r.get("failed", [])) for r in merge_results
            )
            names = sorted(p.name for p in root.iterdir())

        elif step.kind is StepKind.ARCHIVE:
            archive_plan = archive_mod.build_archive_plan(root, names)
            archive_result = archive_mod.apply_archive_plan(
                root, archive_plan, clean_forks=False  # 垃圾已在 CLEAN 步骤处理
            )
            result["archived"] = archive_result["moved"]
            result["conflicts"] = archive_result["conflicts"]
            result["failed"] = int(result["failed"]) + len(archive_result["failed"])
            names = sorted(p.name for p in root.iterdir())

        elif step.kind is StepKind.RENAME:
            # 只把**目录**交给改名引擎 —— 文件由 archive / rename-files 处理。
            # 这里必须和 build_tidy_plan 用同一份名单（规划阶段也是只用目录名），
            # 否则顶层散文件会被当成目录一起改名。
            dir_names = [n for n in names if (root / n).is_dir()]
            rename_plan = engine.build_plan(root, ruleset, dir_names)
            done = engine.apply_plan(root, rename_plan)
            result["renamed"] = len(done.renamed)
            result["failed"] = int(result["failed"]) + len(done.failed)
            names = sorted(p.name for p in root.iterdir())

        elif step.kind is StepKind.RENAME_FILES:
            file_plan = rename_files_mod.build_file_plan(root, ruleset)
            file_result = rename_files_mod.apply_file_plan(root, file_plan)
            result["renamed_files"] = len(file_result["renamed"])
            result["conflicts"] = int(result["conflicts"]) + len(file_result["conflicts"])
            result["failed"] = int(result["failed"]) + len(file_result["failed"])

    return result
