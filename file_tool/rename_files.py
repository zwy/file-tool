"""rename-files：把文件名里带的番号规范化成所在目录的番号。

背景
----
目录名已经规范化过了（``ACHJ-038``），但目录里的文件还留着下载时的原样
（``kfa11.com@ACHJ038.jpg``）。本命令把文件名里的番号写法统一成目录名。

番号身份只在**顶层目录**
----------------------
实测的目录结构比想象中深 —— 番号目录往下还有 2~5 层：

=========================================  ==========================
层级                                        内容
=========================================  ==========================
``ACHJ-038/``                               顶层 = 番号目录
``FLUP-1131784/广东财经大学 校鸡公交车.../``  原标题子目录
``TEA-240813-08/132 -SSQW/SSQWQS/图片/``    厂牌 / 分类分组
``xfxfz-14501/3505.7z/SF/1/``                解压残留（压缩包名当目录）
=========================================  ==========================

所以番号统一取**顶层目录**的番号，往下几层都不承载番号信息。

只处理「文件名里本来就有番号」的
------------------------------
文件名解析不出番号的一律不动 —— 那些名字本身就是内容标识：

- 中文标题（``一见面就湿透.mp4``）—— 标题就是身份，套番号会毁掉信息
- 图片序列（``001.jpg``）—— 序号有意义
- 原始导出名（``VID_20240713_021421_921.mp4``、``dzud43.jpg``）

安全约束
--------
- **冲突绝不覆盖**：目标名已存在且不是同一个文件 -> 跳过并报告
- 扩展名原样保留（``.srt`` ``.jpg`` ``.mp4`` 一视同仁，不做白名单）
- 资源叉 ``._xxx`` 跳过（由 ``clean`` 负责清理）
- 大小写不敏感的文件系统（exFAT / APFS 默认）上，仅大小写不同的改名
  走两步（先挪临时名再改目标名），否则 rename 会「无事发生」
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from file_tool import engine
from file_tool.models import RuleSet


class FileAction(str, Enum):
    """单个文件的处置动作。"""

    RENAME = "rename"      # 会改名
    KEEP = "keep"          # 已是规范名，不动
    SKIP = "skip"          # 判定为不动（番号对不上等）
    CONFLICT = "conflict"  # 目标名已被占用，跳过


class FileItem(BaseModel):
    """一个待改名文件的完整决策记录。"""

    relpath: str
    """相对 root 的完整路径，如 ``ACHJ-038/kfa11.com@ACHJ038.jpg``。"""
    directory: str
    """所在的顶层番号目录名。"""
    original: str
    target: str = ""
    action: FileAction = FileAction.SKIP
    reason: str = ""


class FilePlan(BaseModel):
    """一次文件改名任务的完整计划。"""

    root: str
    items: List[FileItem] = Field(default_factory=list)
    """只收录「文件名里能解析出番号」的文件。

    解析不出番号的文件不进入计划 —— 它们本来就不该动，收进来只会
    把计划撑大几十倍、淹没真正要改的那几百条。
    """
    no_code_dirs: List[str] = Field(default_factory=list)
    """不参与处理的顶层目录（不是番号目录）。"""
    code_dirs: List[str] = Field(default_factory=list)
    """参与处理的顶层番号目录名。"""
    no_code_files: int = 0
    """文件名里没有番号、因而保持原样的文件数。"""

    @property
    def rename_items(self) -> List[FileItem]:
        return [i for i in self.items if i.action is FileAction.RENAME]

    @property
    def keep_items(self) -> List[FileItem]:
        return [i for i in self.items if i.action is FileAction.KEEP]

    @property
    def skip_items(self) -> List[FileItem]:
        return [i for i in self.items if i.action is FileAction.SKIP]

    @property
    def conflict_items(self) -> List[FileItem]:
        return [i for i in self.items if i.action is FileAction.CONFLICT]

    def summary(self) -> Dict[str, int]:
        return {
            "total": len(self.items),
            "rename": len(self.rename_items),
            "keep": len(self.keep_items),
            "skip": len(self.skip_items),
            "conflict": len(self.conflict_items),
            "no_code_files": self.no_code_files,
            "no_code_dirs": len(self.no_code_dirs),
            "code_dirs": len(self.code_dirs),
        }


def split_extension(name: str) -> tuple[str, str]:
    """把 ``abc.mp4`` 拆成 (``abc``, ``.mp4``)；无扩展名时后缀为空。

    与 :func:`file_tool.engine._split_suffix` 同样的语义，但独立一份，
    避免文件改名逻辑依赖引擎的私有函数。
    """
    if "." in name:
        stem, _, suffix = name.rpartition(".")
        if stem and "/" not in suffix:
            return stem, f".{suffix}"
    return name, ""


def _resolve(name: str, ruleset: RuleSet):
    """解析一个名称，返回 (基础名, 规范名, 规则名)。解析不出返回 (None, "", "")。"""
    ordered = ruleset.ordered_matches()
    dup_sources = ruleset.dup_sources or ["dup"]
    core, captured = engine.strip_noise(name, ruleset.preprocess)
    base, target, rule, _desc, _core = engine.resolve_target(
        core, captured, ordered, ruleset.preprocess, dup_sources, ruleset.exclude
    )
    return base, target, rule


def build_file_plan(root: Path, ruleset: RuleSet) -> FilePlan:
    """扫描 root 下所有番号目录，生成文件改名计划。"""
    plan = FilePlan(root=str(root))

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        dir_base, dir_target, _rule = _resolve(child.name, ruleset)
        if not dir_base:
            # 不是番号目录 —— 不认识就不动
            plan.no_code_dirs.append(child.name)
            continue
        plan.code_dirs.append(child.name)

        for path in sorted(child.rglob("*")):
            if not path.is_file():
                continue
            if path.name.startswith("._"):
                continue  # 资源叉，交给 clean 处理

            stem, ext = split_extension(path.name)
            file_base, _ftarget, _frule = _resolve(stem, ruleset)

            if not file_base:
                plan.no_code_files += 1
                continue

            relpath = str(path.relative_to(root))
            item = FileItem(relpath=relpath, directory=child.name, original=path.name)

            if file_base != dir_base:
                item.action = FileAction.SKIP
                item.reason = f"文件名是别的番号（{file_base}），不动"
                plan.items.append(item)
                continue

            new_name = dir_target + ext
            if new_name == path.name:
                item.target = new_name
                item.action = FileAction.KEEP
                item.reason = "已是规范名"
                plan.items.append(item)
                continue

            if (path.parent / new_name).exists():
                item.target = new_name
                item.action = FileAction.CONFLICT
                item.reason = "目标名已存在，未覆盖"
                plan.items.append(item)
                continue

            item.target = new_name
            item.action = FileAction.RENAME
            item.reason = "番号规范化"
            plan.items.append(item)

    return plan


def _rename_case_only(src: Path, dst: Path) -> None:
    """仅大小写不同的改名。

    exFAT 与 APFS（默认）都是大小写不敏感的，``a.mp4`` -> ``A.mp4``
    直接 rename 会「无事发生」。必须先挪到临时名再改到目标名。
    """
    tmp = src.with_name("__file_tool_tmp__")
    counter = 0
    while tmp.exists():
        counter += 1
        tmp = src.with_name(f"__file_tool_tmp__{counter}")
    src.rename(tmp)
    tmp.rename(dst)


def apply_file_plan(root: Path, plan: FilePlan) -> Dict[str, object]:
    """执行文件改名。冲突绝不覆盖。"""
    renamed: List[tuple[str, str]] = []
    # 计划阶段就判定的冲突也算数 —— 它们虽然不在 rename_items 里，
    # 但用户需要知道哪些文件因为「目标已存在」而没被规范化
    conflicts: List[str] = [i.relpath for i in plan.conflict_items]
    failed: List[str] = []

    for item in plan.rename_items:
        src = root / item.relpath
        if not src.is_file():
            continue
        dst = src.parent / item.target

        if dst.exists():
            # 目标已存在 —— 只有「同一个文件、仅大小写不同」才继续
            try:
                if os.path.samefile(src, dst):
                    _rename_case_only(src, dst)
                    renamed.append((item.relpath, item.target))
                    continue
            except OSError:
                pass
            conflicts.append(item.relpath)
            continue

        try:
            src.rename(dst)
            renamed.append((item.relpath, item.target))
        except OSError:
            failed.append(item.relpath)

    return {
        "renamed": renamed,
        "conflicts": conflicts,
        "failed": failed,
    }
