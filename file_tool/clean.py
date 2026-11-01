"""clean：递归清理 macOS 资源叉垃圾。

背景
----
macOS 在 exFAT 等非 HFS+ 卷上，为每个文件建一个 ``._xxx`` 资源叉副本，
保存 Finder 信息和扩展属性。媒体文件没有这些元数据需求，
这些副本就是纯粹的垃圾 —— 实测 ``/Volumes/P7000Z/IMG`` 里嵌套层
有 24000 多个。

为什么单独做一个命令
--------------------
tidy 的垃圾清理只作用于**顶层**。嵌套层（番号目录下的分P目录）
里的资源叉不会被碰到，但它们数量更多。需要一个能递归清理、
又不碰真实文件的独立操作。

安全
----
只删名字以 ``._`` 开头、且**确实存在对应真实文件或目录**的资源叉。
不匹配任何对应物的孤儿条目也一并清理（它们同样是垃圾）。
真实文件、目录、结构一律不动。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class CleanItem(BaseModel):
    """一个待清理的资源叉文件。"""

    path: str
    size: int = 0
    has_counterpart: bool = False
    """是否有对应的真实文件/目录。"""


class CleanPlan(BaseModel):
    """一次清理的完整计划。"""

    root: str
    items: List[CleanItem] = Field(default_factory=list)
    dirs_scanned: int = 0
    files_scanned: int = 0

    @property
    def total_size(self) -> int:
        return sum(i.size for i in self.items)

    def summary(self) -> Dict[str, int]:
        return {
            "forks": len(self.items),
            "size": self.total_size,
            "dirs": self.dirs_scanned,
            "files": self.files_scanned,
        }


def _is_fork(name: str) -> bool:
    return name.startswith("._") or name in (".DS_Store", "Thumbs.db")


def build_clean_plan(root: Path, recursive: bool = True) -> CleanPlan:
    """扫描资源叉垃圾，生成清理计划。"""
    plan = CleanPlan(root=str(root))

    walker = root.rglob("*") if recursive else root.iterdir()
    for path in walker:
        if path.is_dir():
            plan.dirs_scanned += 1
            # 目录自己的资源叉（._dirname）
            fork = path.parent / f"._{path.name}"
            if fork.is_file():
                plan.items.append(CleanItem(
                    path=str(fork),
                    size=_safe_size(fork),
                    has_counterpart=True,
                ))
            continue

        plan.files_scanned += 1
        if not _is_fork(path.name):
            continue

        # 对应主体：去掉 ._ 前缀后的名字
        counterpart = path.parent / path.name[2:]
        plan.items.append(CleanItem(
            path=str(path),
            size=_safe_size(path),
            has_counterpart=counterpart.exists(),
        ))

    return plan


def _safe_size(path: Path) -> int:
    """取文件大小；取不到就当 0（被占用 / 无权限 / 名字非法）。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def apply_clean_plan(plan: CleanPlan) -> Dict[str, object]:
    """执行清理，返回摘要。"""
    removed = 0
    freed = 0
    failed: List[str] = []

    for item in plan.items:
        path = Path(item.path)
        try:
            if path.is_file():
                size = path.stat().st_size
                path.unlink()
                removed += 1
                freed += size
        except OSError:
            failed.append(item.path)

    return {
        "removed": removed,
        "freed": freed,
        "failed": failed,
    }
