"""G 盘实测形态的回归测试。

G 盘（另一块 exFAT 外置盘）出现了 F 盘没有的三种包装形态，
以及一个「国产合集被误判成番号库」的坑。这里逐条锁住。

背景
----
G 盘实测（307 个子目录）暴露的问题：

1. ``dccdom.com@230ORECO894C`` —— 数字前缀 + 厂牌 + 序号，**厂牌与序号之间无横杠**。
   旧规则集里只有带横杠的 ``prefix_num_studio``，这条解析不出来 ->
   isolate 会把它当成国产内容挪进 GC，**正规番号被误隔离**。

2. ``dxxdom.com@KIN8-4069`` —— 大写厂牌**含数字**（KIN8 是厂牌名的一部分）
   带横杠。旧规则集只有小写版 ``lower_dash_digit_studio``。

3. ``dxxdom.comSONE815`` —— 域名与番号**直接相连**，中间没有 ``@`` 也没有空格。
   旧的 ``strip_watermark_domain`` 要求域名后面有 ``@``，这条剥不掉。

4. 国产合集目录 ``322`` 里全是 ``IMG_6481.jpg`` / ``photo_2023.jpg``
   这类**相机原始导出名**。宽松的番号正则把它们认成番号 ->
   整个国产合集被判为「含番号」而留在番号库里，**国产内容混进番号库**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import engine
from file_tool import isolate as isolate_mod
from file_tool.isolate import Isolation

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return engine.load_ruleset(RULES)


def plan_for(ruleset, name: str):
    return engine.build_plan(Path("/fake"), ruleset, [name]).items[0]


# --------------------------------------------------------------------------
# 1. 数字前缀 + 厂牌 + 序号（无横杠）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "original,expected",
    [
        ("230ORECO894C", "ORECO-894C"),
        ("dccdom.com@230ORECO894C", "ORECO-894C"),
    ],
)
def test_prefix_num_studio_without_dash(ruleset, original, expected):
    """数字前缀形态没有横杠时也要能解析。"""
    item = plan_for(ruleset, original)
    assert item.target == expected, f"{original} -> {item.target}（期望 {expected}）"


# --------------------------------------------------------------------------
# 2. 大写厂牌含数字
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "original,expected",
    [
        ("KIN8-4069", "KIN8-4069"),
        ("dxxdom.com@KIN8-4069", "KIN8-4069"),
    ],
)
def test_uppercase_studio_with_digit(ruleset, original, expected):
    """大写厂牌含数字带横杠（KIN8-4069）。"""
    item = plan_for(ruleset, original)
    assert item.target == expected, f"{original} -> {item.target}（期望 {expected}）"


# --------------------------------------------------------------------------
# 3. 域名与番号直接相连（无 @ 分隔）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "original,expected",
    [
        ("dxxdom.comSONE815", "SONE-815"),
        ("dxxdom.comSONE821", "SONE-821"),
    ],
)
def test_domain_glued_to_code(ruleset, original, expected):
    """域名紧贴番号（没有 @ 分隔）也要能剥掉域名。"""
    item = plan_for(ruleset, original)
    assert item.target == expected, f"{original} -> {item.target}（期望 {expected}）"


# --------------------------------------------------------------------------
# 幂等性：新规则产出的规范名必须能被同一套规则反解回自己
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "canonical",
    [
        "ORECO-894C",
        "KIN8-4069",
        "SONE-815",
        "SONE-821",
    ],
)
def test_new_forms_are_idempotent(ruleset, canonical):
    """新规则输出的规范名再跑一次 rename 必须保持 KEEP。

    这是规则文件里写死的纪律：规范名必须能被自己解析回来，
    否则第二次跑 rename 就会把整理好的目录改坏。
    """
    item = plan_for(ruleset, canonical)
    assert item.target == canonical, f"{canonical} -> {item.target}（不该变）"
    assert item.action.value == "keep", f"{canonical} 不该被再次改名"


# --------------------------------------------------------------------------
# 4. 国产合集不能被误判成「含番号」
# --------------------------------------------------------------------------

def test_export_names_are_not_codes(ruleset, tmp_path):
    """相机/手机导出名不是番号。

    实测 G 盘的国产合集 ``322`` 里全是这类文件名：
    ``IMG_6481.jpg`` / ``photo_2023.jpg`` / ``dw9358.jpg``。
    以前它们被宽松正则认成番号，导致整个国产合集留在番号库里。
    """
    d = tmp_path / "322"
    (d / "子目录").mkdir(parents=True)
    for name in (
        "IMG_6481.jpg",
        "IMG_5245.jpg",
        "photo_2023.jpg",
        "dw9358.jpg",
        "P1000123.jpg",
        "VID_20240101.mp4",
    ):
        (d / "子目录" / name).write_bytes(b"x")

    assert isolate_mod.find_codes_in_dir(d) == [], "导出名不该被认成番号"


def test_domestic_collection_is_moved(ruleset, tmp_path):
    """只含导出名的国产合集目录应当被判定为「挪走」。"""
    d = tmp_path / "322"
    (d / "子目录").mkdir(parents=True)
    for name in ("IMG_6481.jpg", "photo_2023.jpg", "dw9358.jpg"):
        (d / "子目录" / name).write_bytes(b"x")

    item = isolate_mod.judge_directory("322", tmp_path, ruleset)
    assert item.isolation is Isolation.MOVE, "国产合集应被挪到 GC"
    assert item.code_samples == []


def test_real_code_in_collection_is_still_kept(ruleset, tmp_path):
    """收紧导出名之后，真正的番号仍然要能被发现。"""
    d = tmp_path / "合集"
    d.mkdir()
    (d / "IMG_6481.jpg").write_bytes(b"x")
    (d / "ABP-001.mp4").write_bytes(b"x")

    item = isolate_mod.judge_directory("合集", tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP, "内含正规番号的合集应保留"
    assert "ABP-001" in item.code_samples


# --------------------------------------------------------------------------
# 5. 正规番号不能被 isolate 误当国产挪走
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "dccdom.com@230ORECO894C",
        "dxxdom.com@KIN8-4069",
        "dxxdom.comSONE815",
        "dxxdom.comSONE821",
    ],
)
def test_numbered_dir_is_not_isolated(ruleset, tmp_path, name):
    """这四个是正规番号，绝不能被挪进 GC。

    这是这次修复的**主要风险**：isolate 排在 rename 之前执行，
    规则不认识番号 -> isolate 判定「无番号特征」-> 正规番号被误隔离。
    """
    d = tmp_path / name
    d.mkdir()
    (d / f"{name}.mp4").write_bytes(b"x")

    item = isolate_mod.judge_directory(name, tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP, f"{name} 是正规番号，不该被隔离"
    assert "番号" in item.reason
