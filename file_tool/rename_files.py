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
import re
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from file_tool import engine
from file_tool import fscompat
from file_tool.models import RuleSet

# 规范名里的数字段
_CODE_DIGITS = re.compile(r"\d+")


def canonical_code(code: str) -> str:
    """把规范名归一成「同一作品」的比较键。

    比较时必须忽略两件事：

    - **大小写** —— 卷本身大小写不敏感（Windows NTFS/exFAT、macOS APFS 默认），
      ``GVH-067`` 和 ``gvh-067`` 本来就是同一个目录。
    - **数字段的前导零** —— 下载站给同一部番号写过 ``GVH067`` 和 ``gvh00067``，
      数字位数不一样但值相同，是同一部片子。

    实测 ``GVH-067/gvh00067.mp4`` 就是这种：目录名规范化成 ``GVH-067``，
    文件名却是 ``gvh00067``。按字面比较会被误判成「别的番号」而跳过。
    """
    return _CODE_DIGITS.sub(lambda m: str(int(m.group())), code).casefold()


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
    inaccessible: List[str] = Field(default_factory=list)
    """在 Windows 上访问不了的文件（名字非法），无法改名。

    macOS 在 exFAT 上能建出带 ``:`` ``*`` ``?`` 的文件名，插到 Windows 上
    连 ``stat`` 都会失败。这类文件既不参与改名，也不该被当成「没番号」。
    """

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


# 文件名里可作为切分点的分隔符
_NOISE_SEPARATORS = re.compile(r"[._~@\- ]")


def _cut_search(stem: str, ruleset: RuleSet):
    """逐段截断，找出「番号 + 噪声后缀」的切分。

    切分点取在分隔符**之前**（分隔符归后缀）。切点从**后往前**试 ——
    长的前缀优先：番号前缀越长越具体，``START-589-U`` 应该整体算一个番号，
    而不是 ``START-589`` 加一条尾巴。

    但如果更短的切点解析出**同一个**番号，说明多出来的那截是被规则当成
    噪声剥掉的（``-2`` 这种分段标记）—— 那就该用短的那个，让标记留在
    后缀里，而不是被吞掉。

    遇到**另一个**番号就停：再往前切已经是另一部片子了。

    返回 (番号基础名, 噪声后缀, 规则名)；解析不出返回 (None, "", "")。
    """
    cuts = sorted({m.start() for m in _NOISE_SEPARATORS.finditer(stem)}, reverse=True)
    best: tuple = (None, "", "")
    for cut in cuts:
        head, tail = stem[:cut], stem[cut:]
        if not head:
            continue
        base, _target, rule = _resolve(head, ruleset)
        if not base:
            continue
        if best[0] is None:
            best = (base, tail, rule)
            continue
        if canonical_code(base) == canonical_code(best[0]):
            # 同一部片子 —— 用更短的切点，把被剥掉的标记留到后缀里
            best = (base, tail, rule)
            continue
        # 另一部片子 —— 长的那个才是对的
        break
    return best


def split_code_suffix(stem: str, ruleset: RuleSet):
    """在文件名里找出「番号 + 噪声后缀」的切分。

    下载站常在番号后面接一串东西，把整个文件名挡在解析之外：

    ============================  ======================  ==============
    原始文件名                      噪声                    结果
    ============================  ======================  ==============
    ``326FCT-043_en``             语言标记 ``_en``        ``FCT-043_en``
    ``259LUXU-1812.mp4``          二次扩展名 ``.mp4``      ``LUXU-1812.mp4``
    ``MKMP595-2f60f24e4602``      去重哈希 ``-2f60...``   ``MKMP-595-2f60...``
    ``STARS-542~nyap2p.com``      站点名 ``~nyap2p.com``  ``STARS-542~nyap2p.com``
    ============================  ======================  ==============

    做法是**逐段截断试解析**：把分隔符之后的部分当成噪声后缀，
    前缀能解析成番号就用。噪声后缀**原样保留** —— 语言标记是有意义的信息，
    删掉就毁了。

    **两路解析取长补短**
    ------------------
    整串解析（``_resolve(stem)``）和逐段截断各算一遍：

    - 两者指向**同一部片子**时，取逐段截断的结果。
      这是关键 —— ``sdjs-361-2.mp4`` 里的 ``-2`` 会被
      ``strip_hyphen_dup`` 规则当成「重复副本标记」剥掉，整串解析
      只剩 ``sdjs-361``；而截断解析切成 ``sdjs-361`` + ``-2``，
      ``-2`` 这个分段标记就留在了文件名里。
      不做这一步，``4k2.me@sdjs-361-2.mp4`` 会被算成 ``sdjs-361.mp4``
      —— 跟已有的真身撞名，然后被当成「别的番号」跳过，水印永远去不掉。
    - 两者指向**不同**的片子时，取整串解析的结果。
      ``ABF-292-AI`` 整串解析是 ``ABF-292-AI``，截断却是 ``ABF-292``
      —— 截断结果更短但不是同一部片子，按截断来会拼出
      ``ABF-292-AI-AI.mp4`` 这种重复后缀。

    返回 (番号基础名, 噪声后缀, 规则名)；解析不出返回 (None, "", "")。
    """
    whole_base, _whole_target, whole_rule = _resolve(stem, ruleset)
    cut_base, cut_tail, cut_rule = _cut_search(stem, ruleset)

    if whole_base:
        if cut_base and canonical_code(cut_base) == canonical_code(whole_base):
            # 同一部分片 —— 取截断结果，让被剥掉的标记留在后缀里
            return cut_base, cut_tail, cut_rule
        return whole_base, "", whole_rule
    if cut_base:
        return cut_base, cut_tail, cut_rule
    return None, "", ""


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
                # Windows 上名字非法的文件连 stat 都会失败 —— 这里如实报告
                if fscompat.on_windows():
                    reason = fscompat.is_windows_unsafe_name(path.name)
                    if reason:
                        rel = fscompat.to_posix_relpath(path, root)
                        plan.inaccessible.append(f"{rel}（{reason}）")
                continue
            if path.name.startswith("._"):
                continue  # 资源叉，交给 clean 处理

            stem, ext = split_extension(path.name)
            file_base, noise, _frule = split_code_suffix(stem, ruleset)

            if not file_base:
                plan.no_code_files += 1
                continue

            relpath = fscompat.to_posix_relpath(path, root)
            item = FileItem(relpath=relpath, directory=child.name, original=path.name)

            if canonical_code(file_base) != canonical_code(dir_base):
                # 不是同一部番号 —— **不能**套目录的番号。
                #
                # 那个名字本身就是内容标识：``OFJE-594-A.mp4`` 一眼就认得出
                # 是 A 变体，套上目录的番号 ``OFJE-594`` 会把变体信息抹掉。
                #
                # 但「不动」不等于「不规范化」：文件名里带的水印域名是
                # **纯噪声**，跟内容标识无关。所以这里用**文件自己的番号**
                # 来规范化 —— 水印剥掉，番号本身也规范化成厂牌-序号，
                # 变体后缀原样保留。
                #
                # 实测踩过的坑：H 盘 ``DVDMS-980/`` 里放的是 A/B 两个变体，
                # 文件名是 ``hhd800.com@DVDMS-980-A.mp4``。旧逻辑整个文件
                # 不动，水印一直留着 —— 「文件名规范化」就差这么一步。
                new_name = file_base + noise + ext
                if new_name == path.name:
                    item.target = new_name
                    item.action = FileAction.KEEP
                    item.reason = "已是规范名"
                    plan.items.append(item)
                    continue

                dst_path = path.parent / new_name
                if dst_path.exists():
                    if _is_same_file(path, dst_path):
                        # 同一个文件，仅大小写不同 —— 不是冲突
                        item.target = new_name
                        item.action = FileAction.RENAME
                        item.reason = "仅大小写不同"
                        plan.items.append(item)
                        continue
                    item.target = new_name
                    item.action = FileAction.CONFLICT
                    item.reason = "目标名已存在，未覆盖"
                    plan.items.append(item)
                    continue

                item.target = new_name
                item.action = FileAction.RENAME
                item.reason = f"文件名是别的番号（{file_base}），按它自己的番号规范化"
                plan.items.append(item)
                continue

            # 目录名自带的重复标记（如 ``CLUB-880 (2)``）已经体现在
            # ``dir_target`` 里了。文件名里的同一个标记不能再加一遍 ——
            # 否则会拼出 ``CLUB-880 (2) (2).mp4`` 这种重复后缀。
            # 目录没有标记时（``sdjs-361``），文件自己的分段标记
            # （``-2``）要留着 —— 那是区分不同分段的唯一信息。
            dir_marker = dir_target[len(dir_base):] if dir_target.startswith(dir_base) else ""
            if dir_marker and noise.startswith(dir_marker):
                noise = noise[len(dir_marker):]

            new_name = dir_target + noise + ext
            if new_name == path.name:
                item.target = new_name
                item.action = FileAction.KEEP
                item.reason = "已是规范名"
                plan.items.append(item)
                continue

            # 冲突检测不能用 ``Path.exists()`` ——
            # exFAT / APFS（默认）大小写不敏感，``CLUB-880.mp4`` 和
            # ``club-880.mp4`` 指的是**同一个文件**，exists() 恒为真。
            # 那样每个「目录名小写、文件名大写」的目录都会被误判成冲突，
            # 文件名就永远规范化不了。
            #
            # 真正的冲突是「另一个文件占了目标名」。判定靠文件身份
            # （inode + 设备号），同一个文件只是大小写不同 -> 那是
            # 纯大小写改名，走两步 rename，不算冲突。
            dst_path = path.parent / new_name
            if dst_path.exists():
                if _is_same_file(path, dst_path):
                    # 同一个文件，仅大小写不同 —— 不是冲突
                    item.target = new_name
                    item.action = FileAction.RENAME
                    item.reason = "仅大小写不同"
                    plan.items.append(item)
                    continue
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
    fscompat.rename_case_only(src, dst)


def _is_same_file(a: Path, b: Path) -> bool:
    """两个路径是不是同一个文件（仅大小写不同）。

    优先用 inode + 设备号判定；拿不到（比如某些网络卷）时退回
    内容指纹 —— 宁可漏判成「不是同一个文件」，也不能误判成同一个。
    """
    return fscompat.is_same_file(a, b)


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
