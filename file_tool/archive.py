"""archive 阶段：把平铺在一个目录里的文件，按归一番号分装进子目录。

场景
----
``/Volumes/P7000Z/CLUB`` 这种目录里 **0 个子目录、208 个平铺文件**，
需要变成「一个番号一个目录」，和 ``/Volumes/P7000Z/R`` 的组织方式一致。

归一规则
--------
两种变体需要合并到同一个目录：

1. **横杠变体** —— ``CLUB810`` 与 ``CLUB-810`` 是同一番号（可能不同片源）。
   文件指纹证实两者内容不同（1.6GB vs 3.0GB），所以是**同一作品的不同版本**，
   应归到一起而不是当成两个作品。

2. **C 后缀** —— ``CLUB830`` 与 ``CLUB830C`` 是同一作品，``C`` 表示中文字幕版。
   封面指纹完全相同（``3f0299f4750ceea9``）证实了这一点。

安全语义
--------
- 归一是**合并进同一个目录**，不删除任何视频文件
- 目标目录已存在同名文件时**跳过并报告**，绝不覆盖
- ``._`` 资源叉文件是 macOS 垃圾，默认清理（``--keep-forks`` 可保留）
- 目录名与现有文件撞名时**跳过该组**，不覆盖
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from file_tool import fscompat

# 已知媒体扩展名。只有这些文件参与分装，其他文件原地保留。
MEDIA_EXTENSIONS = {
    ".mp4", ".mpg", ".mpeg", ".avi", ".wmv", ".mov", ".mkv", ".flv", ".ts",
    ".jpg", ".jpeg", ".png", ".webp", ".bmp",
}


class ArchiveItem(BaseModel):
    """一个待分装的文件。"""

    filename: str
    group: str = ""
    """归一后的目录名（如 ``CLUB-830``）。"""
    variant: str = ""
    """原始文件名去扩展名（如 ``CLUB830C``），用于展示归一来源。"""
    dest: str = ""
    """在目标目录下的最终文件名。"""

    @property
    def moved(self) -> bool:
        return bool(self.dest)


class ArchiveGroup(BaseModel):
    """一个归一后的番号组。"""

    name: str
    """目标目录名，如 ``CLUB-830``。"""
    items: List[ArchiveItem] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    """目标目录内已存在同名文件，未能移入的。"""

    @property
    def variants(self) -> List[str]:
        seen: List[str] = []
        for item in self.items:
            if item.variant not in seen:
                seen.append(item.variant)
        return seen

    @property
    def merged(self) -> bool:
        """是否由多个原始变体归一而来。"""
        return len(self.variants) > 1


class ArchivePlan(BaseModel):
    """一次分装任务的完整计划。"""

    root: str
    groups: List[ArchiveGroup] = Field(default_factory=list)
    forks: List[str] = Field(default_factory=list)
    """待清理的 ``._`` 资源叉文件。"""
    untouched: List[str] = Field(default_factory=list)
    """不参与分装的文件（非媒体类型）。"""
    blocked: List[str] = Field(default_factory=list)
    """因目标名与现有文件冲突而无法处理的组。"""
    unsafe: List[str] = Field(default_factory=list)
    """在 Windows 上碰不了的文件名（原因）。

    macOS 在 exFAT 上能建出带 ``:`` ``*`` ``?`` 的文件名，插到 Windows 上
    就是僵尸文件 —— 连 ``stat`` 都会失败。这类文件不参与分装，原地保留。
    """

    @property
    def merged_groups(self) -> List[ArchiveGroup]:
        return [g for g in self.groups if g.merged]

    @property
    def total_files(self) -> int:
        return sum(len(g.items) for g in self.groups)

    def summary(self) -> Dict[str, int]:
        return {
            "groups": len(self.groups),
            "merged_groups": len(self.merged_groups),
            "files": self.total_files,
            "forks": len(self.forks),
            "untouched": len(self.untouched),
            "blocked": len(self.blocked),
            "unsafe": len(self.unsafe),
        }


def normalize_key(stem: str) -> Optional[str]:
    """把文件名（去扩展名）归一成番号键。

    ``CLUB810`` / ``CLUB-810`` -> ``CLUB-810``
    ``CLUB830C`` / ``CLUB830``  -> ``CLUB-830``（``C`` 是字幕版标记，剥掉）

    返回 None 表示无法识别。
    """
    import re

    m = re.fullmatch(r"(?P<studio>[A-Za-z]{2,6})[-_]?(?P<num>\d{2,5})(?P<ver>[A-Za-z]{1,2})?", stem)
    if not m:
        return None
    return f"{m.group('studio')}-{m.group('num')}"


def is_fork(name: str) -> bool:
    """是否为 macOS 资源叉 / AppleDouble 垃圾文件。"""
    if name.startswith("._"):
        return True
    if name in (".DS_Store", "Thumbs.db"):
        return True
    return False


def build_archive_plan(
    root: Path,
    names: Optional[Sequence[str]] = None,
) -> ArchivePlan:
    """扫描平铺文件，生成归档计划。

    只处理**文件**；已存在的子目录原样保留（不递归、不合并）。
    """
    if names is None:
        names = sorted(p.name for p in root.iterdir())

    plan = ArchivePlan(root=str(root))

    # 先记下已存在的目录名 —— 它们是「目的地已存在」，要单独判断
    existing_dirs: set[str] = set()
    existing_files: set[str] = set()
    for name in names:
        if (root / name).is_dir():
            existing_dirs.add(name)
        else:
            existing_files.add(name)

    buckets: Dict[str, List[str]] = {}
    for name in names:
        # 先看名字 —— 不能用 ``root / name`` 拼路径：
        # Windows 上 ``a:b.mp4`` 会被 pathlib 解析成「驱动器 a: + 文件 b.mp4」，
        # 原始名就丢了，后面既认不出也搬不动。
        reason = fscompat.is_windows_unsafe_name(name)
        if reason:
            plan.unsafe.append(f"{name}（{reason}）")
            if fscompat.blocks_on_windows(name):
                continue

        path = root / name
        if path.is_dir():
            continue
        if is_fork(name):
            plan.forks.append(name)
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in MEDIA_EXTENSIONS:
            plan.untouched.append(name)
            continue
        stem = os.path.splitext(name)[0]
        key = normalize_key(stem)
        if key is None:
            plan.untouched.append(name)
            continue
        buckets.setdefault(key, []).append(name)

    for key in sorted(buckets):
        group = ArchiveGroup(name=key)

        # 目标目录名与现有文件撞名 -> 整组不处理
        if key in existing_files:
            plan.blocked.append(key)
            continue

        target_dir = root / key
        # 目标目录已存在时，内部的同名文件算冲突
        existing_inside: set[str] = set()
        if key in existing_dirs:
            for p in target_dir.iterdir():
                if p.is_file():
                    existing_inside.add(p.name)

        for name in buckets[key]:
            stem, ext = os.path.splitext(name)
            item = ArchiveItem(
                filename=name,
                group=key,
                variant=stem,
                dest=name if name not in existing_inside else "",
            )
            if name in existing_inside:
                group.conflicts.append(name)
            else:
                existing_inside.add(name)
            group.items.append(item)

        plan.groups.append(group)

    return plan


def apply_archive_plan(
    root: Path,
    plan: ArchivePlan,
    clean_forks: bool = True,
) -> Dict[str, object]:
    """执行归档：清垃圾 -> 建目录 -> 移文件。

    **清理必须在移动之前**。macOS 在 exFAT 等非 HFS+ 卷上用 ``._xxx``
    存资源叉；``shutil.move`` 会把资源叉一起带走，移动后按原名找不到，
    垃圾就残留在新目录里。先清掉源文件的资源叉，移动时就不会带出新的。

    返回执行摘要。
    """
    moved = 0
    conflicts = 0
    created_dirs = 0
    failed: List[str] = []

    # ---- 先清垃圾 ----
    forks_removed = 0
    if clean_forks:
        for name in plan.forks:
            path = root / name
            try:
                if path.is_file():
                    path.unlink()
                    forks_removed += 1
            except OSError:
                continue

    # ---- 再建目录、移文件 ----
    for group in plan.groups:
        target_dir = root / group.name
        if not target_dir.exists():
            try:
                target_dir.mkdir(parents=True)
                created_dirs += 1
            except OSError:
                failed.append(group.name)
                continue

        for item in group.items:
            if not item.dest:
                conflicts += 1
                continue
            src = root / item.filename
            dst = target_dir / item.dest
            if not src.exists():
                continue
            if dst.exists():
                conflicts += 1
                continue
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except OSError:
                # Windows 上文件被占用是常态 —— 记下来，继续处理下一个
                failed.append(item.filename)

    # ---- 兜底：清掉移动过程中新产生的资源叉 ----
    # 源文件删除后，卷上可能留下孤立的 ._ 条目（无对应真实文件）
    if clean_forks:
        forks_removed += _remove_orphan_forks(root)

    return {
        "moved": moved,
        "created_dirs": created_dirs,
        "conflicts": conflicts,
        "failed": failed,
        "forks_removed": forks_removed,
        "groups": len(plan.groups),
        "blocked": plan.blocked,
        "unsafe": plan.unsafe,
    }


def _remove_orphan_forks(root: Path) -> int:
    """删除 root 下的所有 ``._`` 资源叉副本。

    macOS 在 exFAT 等非 HFS+ 卷上，为**文件和目录**都建 ``._`` 副本。
    移动文件、新建目录时还会产生新的。这些副本保存的是 Finder 信息和
    扩展属性，对媒体文件没有价值，属于纯粹的垃圾。

    判定只看名字前缀，不看「有没有对应主体」—— 因为移动之后
    ``._foo.mp4`` 和 ``foo.mp4`` 都搬进了新目录，对应关系仍然成立，
    靠孤儿判断会漏掉。
    """
    removed = 0
    for path in list(root.rglob("._*")):
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
