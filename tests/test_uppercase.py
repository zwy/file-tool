"""统一大写：规范名、噪声剥离的先后顺序、排除名单的大小写敏感度。

这一组测试守的是三个互相牵制的约束：

1. **规范名统一大写** —— 整理出来的库最终要放到 exFAT 上，而 exFAT /
   NTFS / APFS（默认）全都大小写不敏感，只差大小写的两个名字在那边
   就是同一个。
2. **大写必须发生在噪声剥离之后** —— 剥离规则靠「域名小写、番号大写」
   把两段切开，先大写就切不开了。
3. **排除名单必须忽略大小写** —— 否则大写化会把国产内容洗成番号。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import engine
from file_tool.engine import load_ruleset

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return load_ruleset(RULES)


def plan_for(ruleset, name):
    plan = engine.build_plan(Path("/fake"), ruleset, [name])
    return plan.items[0]


# --------------------------------------------------------------------------
# 1. 规范名统一大写
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "original,expected",
    [
        ("avop-235-C", "AVOP-235-C"),
        ("dvdms-00300a", "DVDMS-00300A"),
        ("sdmu-00877b", "SDMU-00877B"),
        ("kin8-3980", "KIN8-3980"),
        ("sgm00030", "SGM-00030"),
        ("nizuia00877", "NIZUIA-00877"),
        # 本来就是大写的，形态不变
        ("MAAN-961", "MAAN-961"),
        ("SDMM-028C", "SDMM-028C"),
        ("FC2-PPV-2549115", "FC2-PPV-2549115"),
        ("KIN8-4069", "KIN8-4069"),
    ],
)
def test_canonical_name_is_uppercase(ruleset, original, expected):
    """规范名统一大写。"""
    assert plan_for(ruleset, original).target == expected


@pytest.mark.parametrize(
    "canonical",
    [
        "AVOP-235-C", "DVDMS-00300A", "SDMU-00877B", "KIN8-3980",
        "SGM-00030", "MAAN-961", "SDMM-028C", "FC2-PPV-2549115",
    ],
)
def test_uppercase_name_is_idempotent(ruleset, canonical):
    """大写化之后必须能反解回自己 —— 否则第二次跑就会把目录改坏。"""
    item = plan_for(ruleset, canonical)
    assert item.target == canonical
    assert item.action is engine.Action.KEEP


def test_extension_and_noise_suffix_are_untouched(ruleset):
    """大写只作用于规范名本身 —— 扩展名和噪声后缀原样保留。"""
    item = plan_for(ruleset, "STARS-542.HD")
    assert item.target == "STARS-542"


def test_uppercase_can_be_turned_off():
    """``uppercase: false`` 时回到旧行为（小写厂牌保持小写）。"""
    ruleset = load_ruleset(RULES).model_copy(update={"uppercase": False})
    item = plan_for(ruleset, "dvdms00300a")
    assert item.target == "dvdms-00300a"


# --------------------------------------------------------------------------
# 2. 大写必须发生在噪声剥离之后
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "original,expected",
    [
        # 域名与番号紧贴（靠大小写切分两段）
        ("dxxdom.comSONE815", "SONE-815"),
        # 番号在前、域名在后
        ("SDMM128Caavv38.xyz@", "SDMM-128C"),
        # 域名 + @ + 番号
        ("aavv38.xyz@SDMM028", "SDMM-028"),
        # 横杠分隔
        ("htbet166.com-OKK007C", "OKK-007C"),
        # 空格分隔多个域名
        ("madoubt.com 853252.xyz DOKS-657", "DOKS-657"),
    ],
)
def test_watermark_stripping_still_works(ruleset, original, expected):
    """大写化不能破坏域名剥离 —— 剥离靠「域名小写、番号大写」切分。"""
    assert plan_for(ruleset, original).target == expected


def test_uppercase_output_does_not_break_requery(ruleset):
    """规范名大写之后，再解析一次必须还是它自己。"""
    for name in ["dxxdom.comSONE815", "SDMM128Caavv38.xyz@", "avop-235-C"]:
        first = plan_for(ruleset, name).target
        second = plan_for(ruleset, first).target
        assert second == first, f"{name} -> {first} -> {second}"


# --------------------------------------------------------------------------
# 3. 排除名单忽略大小写
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "xfxfz14468", "XFXFZ14468", "XFXFZ-14468", "xfxfz-14681",
        "mon004", "MON004", "MON-004",
        "FLUP-1131428", "flup-1131428", "FLUP113730",
        "DOW24070403", "dow-24070403", "tea-240813-08",
    ],
)
def test_exclude_ignores_case(ruleset, name):
    """国产编号的写法没有大小写规范 —— 排除必须忽略大小写。

    否则统一大写之后，国产内容会因为「变成了大写形态」而漏掉排除，
    正好和这次改动的目标相反。
    """
    item = plan_for(ruleset, name)
    assert item.action is engine.Action.SKIP, f"{name} 不该被当成番号"
    assert item.target == name, "国产目录名不该被改动"


def test_is_excluded_ignores_case(ruleset):
    assert ruleset.is_excluded("XFXFZ-14468")
    assert ruleset.is_excluded("xfxfz-14468")
    assert ruleset.is_excluded("MON-004")
    assert not ruleset.is_excluded("MAAN-961")
