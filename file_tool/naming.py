"""命名的公共零件：扩展名拆分 + 序号命名。

冲突分配本身有两处实现，各自带上下文，所以不在这里统一：

- ``rename_files`` —— 同一目录内多个文件想要同一个名字，按体积分配
  （见 :func:`file_tool.rename_files._allocate_by_size`）
- ``merge`` —— 合并时文件撞名，让位后的名字怎么起
  （见 :func:`file_tool.merge._yield_name`）

两边共用的只有「序号加在扩展名之前」这一条形态。
"""
from __future__ import annotations

from typing import Tuple


def split_suffix(name: str) -> Tuple[str, str]:
    """把 ``abc.mp4`` 拆成 (``abc``, ``.mp4``)；无扩展名时后缀为空。

    与 :func:`file_tool.engine._split_suffix` 同样的语义，但独立一份，
    避免文件层逻辑依赖引擎的私有函数。
    """
    if "." in name:
        stem, _, suffix = name.rpartition(".")
        if stem and "/" not in suffix:
            return stem, f".{suffix}"
    return name, ""


def numbered_name(base: str, index: int) -> str:
    """给基础名加序号：``CLUB-806C.mp4`` + 2 -> ``CLUB-806C (2).mp4``。

    序号加在**扩展名之前**：``foo (2).mp4`` 而不是 ``foo.mp4 (2)`` ——
    后者在 Windows 资源管理器和部分播放器里显示得很别扭。

    序号形态沿用目录副本标记的写法（``DVMM-055A (1)``），所以序号在
    目录名和文件名之间是一致的。

    ``(2)`` 而不是 ``(1)``：``(1)`` 在这套体系里已经表示「下载站给的
    第一个副本」（水印域名里的 ``(1)`` 会回贴成 `` (1)``），文件名上的
    序号表示的是「同一目录里的第几份」，两者不是一回事，从 2 开始
    不会和副本标记混淆。
    """
    if index < 2:
        return base
    stem, ext = split_suffix(base)
    return f"{stem} ({index}){ext}"
