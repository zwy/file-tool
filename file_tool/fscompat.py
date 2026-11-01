"""跨平台文件系统兼容层。

macOS 与 Windows 在**文件名规则、大小写语义、占用行为**上有实质差异。
这些差异如果不收敛到一处，就会散落到每个业务模块里 —— 而且每一处
都是「在 macOS 上测不出来、到 Windows 上炸」的坑。

本模块只做三件事：

1. **文件名合法性**
   Windows 有一批 macOS 允许、Windows 不允许的名字：保留设备名
   （``CON`` / ``NUL`` / ``COM1``）、非法字符（``< > : " / \\ | ? *``）、
   以及结尾的点和空格（会被静默剥掉）。

   exFAT 在 macOS 上允许建出带 ``:`` 的文件名，插到 Windows 上就成了
   碰不了的僵尸文件 —— ``stat`` 直接报「文件名无效」。所以扫描时必须
   提前发现并报告，而不是等移动时炸掉。

2. **大小写**
   Windows NTFS/exFAT 与 macOS APFS（默认）都**大小写不敏感**，
   「只改大小写」的改名必须走两步（先挪临时名），否则 rename 会
   「无事发生」—— 系统认为源和目标是同一个文件。

   但 Linux ext4、macOS 的**大小写敏感**分区是相反的：那里
   ``a.mp4`` 与 ``A.mp4`` 是两个不同的文件。

   这条差异不能问文件系统，得自己算 —— 否则同一个库在两种卷上
   整理出的结果会不一样；更糟的是，在大小写敏感的卷上整理完再拷到
   exFAT，两个只差大小写的文件就撞在了一起（exFAT 上根本放不下）。
   所以「目标名是否已被占用」一律走 :func:`colliding_entries`，
   与当前卷的语义无关。

3. **路径表示**
   相对路径统一用 ``/`` 分隔，保证 JSON 输出与日志跨平台一致。
   ``str(Path.relative_to())`` 在 Windows 上给的是 ``\\``，会让
   下游脚本在两个平台长得不一样。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

# Windows 保留设备名：这些名字即使带扩展名也不能用（CON.txt 也不行）
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Windows 文件名非法字符
WINDOWS_ILLEGAL_CHARS = '<>:"/\\|?*'

# 单个文件名（一个路径段）的长度上限
MAX_NAME_LENGTH = 255

# 两步改名时用的临时名前缀
_TEMP_PREFIX = "__file_tool_tmp__"


def on_windows() -> bool:
    """当前是否运行在 Windows 上。"""
    return os.name == "nt"


def blocks_on_windows(name: str) -> bool:
    """这个名字在**当前平台**上是不是根本用不了。

    只有 Windows 会卡住 —— macOS / Linux 上这些名字完全合法，
    甚至改名能顺手把问题修掉。所以「拦不拦」取决于当前平台，
    但「要不要告诉用户」是另一回事，两件事要分开看。
    """
    return on_windows() and is_windows_unsafe_name(name) is not None


def is_windows_unsafe_name(name: str) -> Optional[str]:
    """判断这个名字在 Windows 上能不能用。

    返回不可用的原因；可用则返回 ``None``。

    只在 Windows 上有实际意义 —— macOS / Linux 允许这些名字，
    但它们一旦出现在与 macOS 共用的 exFAT 卷上，插到 Windows 上
    就会变成碰不了的文件。
    """
    if not name:
        return "空文件名"
    if name in (".", ".."):
        return "相对路径标记"

    # 保留设备名看的是第一个点之前那一段：CON.txt 和 CON 一样是保留名
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if stem in WINDOWS_RESERVED_NAMES:
        return f"Windows 保留设备名（{stem}）"

    illegal = sorted({c for c in name if c in WINDOWS_ILLEGAL_CHARS or ord(c) < 32})
    if illegal:
        return "Windows 非法字符：" + "".join(illegal)

    if name[-1] in (".", " "):
        return "Windows 会静默剥掉结尾的点和空格"

    if len(name) > MAX_NAME_LENGTH:
        return f"文件名超过 {MAX_NAME_LENGTH} 字符"

    return None


def same_name_key(name: str) -> str:
    """名字的「同一性」比较键：**忽略大小写**。

    这是整个项目判断「两个名字是不是同一个」的统一口径。

    为什么不用 ``str.lower()``：``casefold()`` 是 Unicode 标准的
    大小写折叠，``ß`` 会折成 ``ss``，比 ``lower()`` 更接近
    NTFS / APFS 实际采用的大小写不敏感比较。

    为什么不用 ``os.path.normcase()``：它在 POSIX 上是空操作，
    可我们要的是**跨平台一致** —— 同一个库在 Windows 上和在
    Linux 上的整理结果必须一样，否则库换个平台就乱了。
    """
    return name.casefold()


def colliding_entries(dir_path: Path, name: str) -> List[Path]:
    """目录里所有「忽略大小写后与 ``name`` 相同」的条目。

    这是「目标名是否已被占用」的**唯一**判定入口，替代
    ``Path.exists()``。

    为什么不能直接用 ``exists()``：它的答案取决于**卷本身**的大小写
    语义。Windows NTFS/exFAT 与 macOS APFS（默认）上，
    ``exists("CLUB-810.mp4")`` 对实际叫 ``club-810.mp4`` 的文件
    返回 True；Linux ext4 上返回 False。

    同一个库在两种卷上的整理结果因此会分叉：
    ``CLUB810.mp4`` 与 ``club-810.mp4`` 在 Linux 上会被当成
    「目标名没被占用」而改名成功，在 exFAT 上却是**真冲突**。
    等库拷到 exFAT 上，两个只差大小写的文件就撞了。

    所以冲突判定必须自己算，且**永远忽略大小写** —— 整理出来的库
    最终是要放到 exFAT 上的。

    返回列表而不是单个路径：大小写敏感的卷上真可能同时存在
    ``a.mp4`` 和 ``A.mp4``，两个都是占用者。
    """
    key = same_name_key(name)
    try:
        entries = list(dir_path.iterdir())
    except OSError:
        return []
    return [p for p in entries if same_name_key(p.name) == key]


def find_unsafe_names(
    root: Path, recursive: bool = True
) -> List[Tuple[str, str]]:
    """扫出 root 下在 Windows 上不可用的文件名。

    返回 ``[(相对路径, 原因)]``，相对路径用 ``/`` 分隔。

    走 ``rglob`` 而不是 ``Path.__truediv__``：Windows 上 ``a:b.mp4``
    这种名字会被 pathlib 解析成「驱动器 a: + 文件 b.mp4」，
    ``.name`` 拿到的是 ``b.mp4``，冒号被吃掉，检测就失效了。
    ``rglob`` 内部用 ``_make_child_relpath`` 拼路径，原始名能保住。
    """
    found: List[Tuple[str, str]] = []
    walker = root.rglob("*") if recursive else root.iterdir()
    for path in walker:
        reason = is_windows_unsafe_name(path.name)
        if reason:
            found.append((to_posix_relpath(path, root), reason))
    return found


def to_posix_relpath(path: Path, root: Path) -> str:
    """相对 root 的相对路径，统一用 ``/`` 分隔。

    跨平台的 JSON 输出、下游脚本、日志比对都依赖这个表示统一。

    **不用** ``path.relative_to(root)``：Python 3.12 起，Windows 上
    ``relative_to`` 会把末段里带 ``:`` 的名字重新当成驱动器解析，
    ``ok/a:b.mp4`` 被截成 ``a:b.mp4`` —— 中间层级整个丢掉。
    病根在 3.12 的 ``PurePath.with_segments()``：它按位置重新解析
    每一段，``a:b.mp4`` 被读成「驱动器 a: + 文件 b.mp4」。

    自己按 ``parts`` 逐段比较，既跨版本稳定，也保得住原始名。
    """
    path_parts = path.parts
    root_parts = root.parts

    index = 0
    limit = min(len(path_parts), len(root_parts))
    while index < limit and _same_part(path_parts[index], root_parts[index]):
        index += 1

    if index == 0:
        # 根本不在 root 底下（不同盘 / 不同根）—— 退回绝对路径
        return path.as_posix()

    rel_parts = path_parts[index:]
    if not rel_parts:
        # path 就是 root 本身
        return "."
    return "/".join(rel_parts)


def _same_part(a: str, b: str) -> bool:
    """路径段比较：Windows 上忽略大小写，POSIX 上精确匹配。

    Windows 上 ``C:\\Users`` 与 ``c:\\users`` 是同一个目录；
    POSIX 上 ``Users`` 与 ``users`` 是两个目录 —— 卷的语义不同，
    这里也跟着不同。
    """
    if on_windows():
        return a.casefold() == b.casefold()
    return a == b


def is_same_file(a: Path, b: Path) -> bool:
    """两个路径是否指向同一个文件（或目录）。

    用于区分「目标已存在且是另一个文件」和「目标就是源本身，
    只是大小写不同」—— 后者必须走两步改名，不能当成冲突。

    「同一个」的判定**忽略大小写**：``a.mp4`` 与 ``A.mp4`` 在
    Windows / exFAT / APFS（默认）上就是同一个文件；在大小写敏感的
    卷上虽然路径不同，但也必须当成同一个 —— 否则整理结果会随卷
    的语义而变，库换个平台就乱了。

    所以先试 ``os.path.samefile``（大小写不敏感的卷上直接命中），
    失败再按「忽略大小写」在目录里找真实条目。
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        pass
    # 大小写敏感的卷上 b 可能压根不存在 —— 按忽略大小写找真实条目
    for entry in colliding_entries(b.parent, b.name):
        try:
            if os.path.samefile(a, entry):
                return True
        except OSError:
            continue
    return False


def rename_case_only(src: Path, dst: Path) -> None:
    """只差大小写的改名：先挪到临时名，再改到目标名。

    大小写不敏感的卷（Windows NTFS/exFAT、macOS APFS 默认）上，
    直接 ``src.rename(dst)`` 会「无事发生」—— 系统认为源和目标
    是同一个文件，名字里的那点大小写差异被忽略了。

    前提：``dst`` 指向的就是 ``src`` 本身（用 :func:`is_same_file` 判定）。
    """
    tmp = src.with_name(f"{_TEMP_PREFIX}{src.suffix}")
    counter = 0
    while tmp.exists():
        counter += 1
        tmp = src.with_name(f"{_TEMP_PREFIX}{counter}{src.suffix}")
    src.rename(tmp)
    tmp.rename(dst)
