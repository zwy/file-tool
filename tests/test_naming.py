"""命名公共零件：扩展名拆分 + 序号命名。"""
from __future__ import annotations

from file_tool import naming


def test_numbered_name_goes_before_extension():
    """序号加在扩展名之前 —— ``foo (2).mp4``，不是 ``foo.mp4 (2)``。"""
    assert naming.numbered_name("CLUB-806C.mp4", 2) == "CLUB-806C (2).mp4"
    assert naming.numbered_name("CLUB-806C.mp4", 3) == "CLUB-806C (3).mp4"


def test_numbered_name_without_extension():
    assert naming.numbered_name("CLUB-806C", 2) == "CLUB-806C (2)"


def test_numbered_name_index_below_two_is_identity():
    """序号 1 不存在 —— ``(1)`` 留给下载站副本标记。"""
    assert naming.numbered_name("X.mp4", 1) == "X.mp4"
    assert naming.numbered_name("X.mp4", 0) == "X.mp4"


def test_split_suffix():
    assert naming.split_suffix("a.mp4") == ("a", ".mp4")
    assert naming.split_suffix("a.b.mp4") == ("a.b", ".mp4")
    assert naming.split_suffix("noext") == ("noext", "")
    assert naming.split_suffix(".hidden") == (".hidden", "")
