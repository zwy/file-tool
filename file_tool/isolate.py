"""isolate：把「不符合番号规范」的目录挪到另一个目录。

场景
----
番号库里混进了不该在的目录 —— 非番号内容、散装合集、随手下载的杂项。
``isolate`` 把它们挪到另一个目录（典型是 ``/Volumes/P7000Z/GC``），
让番号库只留番号。

判定依据：内容，不是名字
------------------------
只看目录名会误判。实测 ``/Volumes/P7000Z/R`` 的 17 个未匹配目录：

======================================  ==========================  ==============
目录                                     名字像番号？                实际内容
======================================  ==========================  ==============
``02`` ``03`` ``10`` ``31``             否                          非番号，文件名 ``mp4_ (1).mp4``
``05`` ``09`` ``11``                     否                          非番号，中文标题
``175_中文标题片段...``              否                          非番号，中文标题
``collection-cn-a``                        否                          非番号合集，52 个中文标题子目录
``collection-cn-b``                     否                          非番号合集
``collection-cn-c``            否                          非番号合集
``kfa11.com@中文标题...``                 否                          非番号
``Uncensored Leaked_001``                否                          **正规番号合集**：ABP-001 等 15 个
``XXX-AV系列合集``                        否                          **正规番号合集**：AV-20608 等 28 个
======================================  ==========================  ==============

后两个目录名也不像番号，但内含**正规番号**，不该被当成杂项移走。
所以判定要下探到内容：目录内（含子目录）是否出现番号形态的文件名。

三级判定
--------
``KEPT``     内含番号形态的文件名  → 保留，这是合集目录，番号在更深层
``MOVED``    不含任何番号形态        → 挪走
``UNKNOWN``  无法判断（空目录等）   → 挪走还是保留由参数决定
"""
from __future__ import annotations

import re
import shutil
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from file_tool import engine
from file_tool import fscompat
from file_tool.models import RuleSet

# 番号形态：厂牌字母 + 数字（可带横杠），如 ABP-001、AV-20608、SDMM028C
CODE_IN_NAME = re.compile(
    r"(?<![A-Za-z0-9])"          # 左边不是字母数字
    r"[A-Za-z]{2,6}[-_]?\d{3,6}"  # 厂牌 + 序号
    r"(?![A-Za-z0-9])"           # 右边不是字母数字
)

# 相机 / 手机 / 相册软件的原始导出名 —— 它们长得像番号，但不是番号。
#
# 实测踩过的坑：国产合集目录 ``322`` 里全是 ``IMG_6481.jpg`` /
# ``photo_2023.jpg`` / ``dw9358.jpg`` 这类文件名，宽松的番号正则会把它们
# 认成番号，导致整个国产合集被判定为「含番号」而留在番号库里 ——
# 国产内容就这么混进了番号库。
#
# 这些前缀都是**导出工具的名字**，不是厂牌：厂牌名不会叫 IMG / DSC / DJI。
# 所以把它们排除掉是安全的 —— 真实番号不会用这些前缀。
_EXPORT_NAME_PREFIXES = (
    "img", "dsc", "dj", "dji", "gopr", "mvhd", "p1000", "p_", "vid", "mov",
    "mvi", "photo", "image", "picture", "pic", "video", "screenshot",
    "screen", "wechat", "weixin", "whatsapp", "signal", "telegram",
    "snap", "pxl", "dcim", "vlog", "movie",
    # 厂牌段只有 2 个字母的导出名 —— 正则的厂牌段下限就是 2，
    # 所以 dw 这类必须单独列出来。
    "dw",
)


def _is_export_name(token: str) -> bool:
    """这个 token 是不是原始导出名（相机/手机导出的默认文件名）。

    只看**厂牌段**（数字前的字母部分）：``IMG_6481`` 的厂牌段是 ``IMG``，
    ``dw9358`` 的厂牌段是 ``dw``，都命中导出工具前缀表。

    真实的番号厂牌（ABP / SDMM / MAAN / KIN8…）不会撞上这些前缀。
    """
    m = re.match(r"^[A-Za-z]{2,6}", token)
    if not m:
        return False
    return m.group(0).lower() in _EXPORT_NAME_PREFIXES



class Isolation(str, Enum):
    """单个目录的隔离判定。"""

    KEEP = "keep"        # 含番号，保留
    MOVE = "move"        # 不含番号，挪走
    UNKNOWN = "unknown"  # 空目录等，无法判断


class IsolationItem(BaseModel):
    """一个待隔离判断的目录。"""

    name: str
    isolation: Isolation = Isolation.UNKNOWN
    reason: str = ""
    code_samples: List[str] = Field(default_factory=list)
    """发现到的番号样本（最多几个）。"""

    @property
    def will_move(self) -> bool:
        return self.isolation is Isolation.MOVE


class IsolationPlan(BaseModel):
    """一次隔离任务的完整计划。"""

    root: str
    target: str
    items: List[IsolationItem] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    """目标目录里已存在同名目录，挪动时会加序号。"""
    unsafe: List[str] = Field(default_factory=list)
    """在 Windows 上挪不动的目录名（名字非法），原地保留。

    macOS 在 exFAT 上能建出带 ``:`` ``*`` ``?`` 的目录名，插到 Windows 上
    连目标路径都拼不出来。这类目录不参与隔离，只如实报告。
    """

    @property
    def move_items(self) -> List[IsolationItem]:
        return [i for i in self.items if i.isolation is Isolation.MOVE]

    @property
    def keep_items(self) -> List[IsolationItem]:
        return [i for i in self.items if i.isolation is Isolation.KEEP]

    def summary(self) -> Dict[str, int]:
        return {
            "total": len(self.items),
            "move": len(self.move_items),
            "keep": len(self.keep_items),
        }


def find_codes_in_dir(directory: Path, limit: int = 5) -> List[str]:
    """在目录（含子目录）里找出番号形态的文件名。

    只看**文件名**和**目录名**，不看文件内容 —— 内容扫描代价太高，
    而番号几乎总是出现在文件名里。

    原始导出名（``IMG_6481`` / ``photo_2023``）会被排除 ——
    它们长得像番号但不是番号，见 :func:`_is_export_name`。
    """
    found: List[str] = []
    seen = set()

    for path in sorted(directory.rglob("*")):
        if path.name.startswith("._"):
            continue
        for text in (path.name,):
            for match in CODE_IN_NAME.finditer(text):
                code = match.group(0)
                if code in seen:
                    continue
                if _is_export_name(code):
                    continue
                seen.add(code)
                found.append(code)
                if len(found) >= limit:
                    return found
    return found


def judge_directory(
    name: str,
    root: Path,
    ruleset: RuleSet,
    move_empty: bool = True,
) -> IsolationItem:
    """判断单个目录该不该被挪走。"""
    directory = root / name
    item = IsolationItem(name=name)

    # 目录名本身就是番号 -> 一定保留
    ordered = ruleset.ordered_matches()
    dup_sources = ruleset.dup_sources or ["dup"]
    core, captured = engine.strip_noise(name, ruleset.preprocess)

    # 命中排除名单 -> 整类目录都不是番号内容，直接挪走，不再下探内容。
    # 这一步必须放在内容判定之前：国产目录里全是原始导出名
    # （``image_2023`` / ``photo_2023`` / ``IMG_7847`` / ``dw9358``），
    # 宽松的番号正则会把它们误认成番号，导致国产内容被留在番号库里。
    if ruleset.is_excluded(core):
        files = [
            p for p in directory.rglob("*")
            if p.is_file() and not p.name.startswith("._")
        ]
        item.isolation = Isolation.MOVE
        item.reason = f"命中排除名单（非番号形态，{len(files)} 个文件）"
        return item

    base, _target, _rule, _desc, _core = engine.resolve_target(
        core, captured, ordered, ruleset.preprocess, dup_sources, ruleset.exclude
    )
    if base:
        item.isolation = Isolation.KEEP
        item.reason = "目录名是番号"
        return item

    codes = find_codes_in_dir(directory)
    if codes:
        item.isolation = Isolation.KEEP
        item.code_samples = codes
        item.reason = f"内含番号（{', '.join(codes[:3])}）—— 是合集目录"
        return item

    # 统计内容，判断是空目录还是有实际文件
    files = [p for p in directory.rglob("*") if p.is_file() and not p.name.startswith("._")]
    if not files:
        item.isolation = Isolation.MOVE if move_empty else Isolation.KEEP
        item.reason = "空目录" if move_empty else "空目录（保留）"
        return item

    item.isolation = Isolation.MOVE
    item.reason = f"无番号特征（{len(files)} 个文件）"
    return item


def build_isolation_plan(
    root: Path,
    target: Path,
    ruleset: RuleSet,
    names: Optional[Sequence[str]] = None,
    move_empty: bool = True,
) -> IsolationPlan:
    """生成隔离计划。

    只考虑**子目录** —— 平铺的散文件不属于「文件夹」范畴，
    由 archive 阶段处理。
    """
    if names is None:
        names = sorted(p.name for p in root.iterdir())

    plan = IsolationPlan(root=str(root), target=str(target))

    # 先按原始名筛 —— Windows 上 ``a:b`` 这类名字用 ``root / n`` 拼出来的
    # 路径是错的（pathlib 会把 ``a:`` 当成驱动器），必须先挑出来。
    dirs = []
    for n in names:
        reason = fscompat.is_windows_unsafe_name(n)
        if reason:
            plan.unsafe.append(f"{n}（{reason}）")
            if fscompat.blocks_on_windows(n):
                continue
        if (root / n).is_dir():
            dirs.append(n)

    # 目标目录本身不能被挪进自己
    target_resolved = target.resolve() if target.exists() else target.absolute()
    dirs = [d for d in dirs if (root / d).resolve() != target_resolved]

    # 目标在源目录内部 -> 会把源的一部分挪进自己的子目录，必须拒绝
    root_resolved = root.resolve()
    if target_resolved != root_resolved and root_resolved in target_resolved.parents:
        raise ValueError(
            f"隔离目标 {target} 位于源目录 {root} 内部，"
            "会造成目录嵌套，请指定源目录之外的位置"
        )

    target_existing = set()
    if target.exists():
        target_existing = {p.name for p in target.iterdir()}

    for name in dirs:
        item = judge_directory(name, root, ruleset, move_empty=move_empty)
        plan.items.append(item)
        if item.will_move and name in target_existing:
            plan.conflicts.append(name)

    return plan


def apply_isolation_plan(
    root: Path,
    target: Path,
    plan: IsolationPlan,
) -> Dict[str, object]:
    """执行隔离：把待挪的目录移到 target。

    目标已有同名目录时加序号（``02`` -> ``02 (2)``），绝不覆盖。
    """
    target.mkdir(parents=True, exist_ok=True)

    moved: List[str] = []
    renamed: List[str] = []
    failed: List[str] = []

    for item in plan.move_items:
        src = root / item.name
        if not src.is_dir():
            continue
        dst = target / item.name
        if dst.exists():
            stem = item.name
            counter = 2
            while (target / f"{stem} ({counter})").exists():
                counter += 1
            dst = target / f"{stem} ({counter})"
            renamed.append(f"{item.name} -> {dst.name}")
        try:
            shutil.move(str(src), str(dst))
            moved.append(item.name)
        except OSError:
            failed.append(item.name)

    return {
        "moved": moved,
        "renamed": renamed,
        "failed": failed,
        "kept": [i.name for i in plan.keep_items],
    }
