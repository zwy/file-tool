"""isolate 测试：把不符合番号规范的目录挪到别处。"""
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


def make(root: Path, name: str, files: dict[str, bytes] | None = None) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def test_numbered_dir_is_kept(ruleset, tmp_path):
    make(tmp_path, "SDMM028C", {"a.mp4": b"x"})
    item = isolate_mod.judge_directory("SDMM028C", tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP
    assert "番号" in item.reason


def test_domian_watermarked_numbered_dir_is_kept(ruleset, tmp_path):
    """带水印域名的番号目录也要保留。"""
    make(tmp_path, "aavv38.xyz@SDMM028", {"a.mp4": b"x"})
    item = isolate_mod.judge_directory("aavv38.xyz@SDMM028", tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP


def test_domestic_dir_is_moved(ruleset, tmp_path):
    """非番号目录（中文标题）要挪走。"""
    make(tmp_path, "02", {"视频/mp4_ (1).mp4": b"x"})
    item = isolate_mod.judge_directory("02", tmp_path, ruleset)
    assert item.isolation is Isolation.MOVE


def test_collection_with_codes_is_kept(ruleset, tmp_path):
    """名字不像番号、但内含正规番号的合集目录要保留。"""
    make(tmp_path, "Uncensored Leaked_001", {
        "1024fj.com_ABP-001_uncensored.mp4": b"x",
        "1024fj.com_ABP-002_uncensored.mp4": b"x",
    })
    item = isolate_mod.judge_directory("Uncensored Leaked_001", tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP
    assert item.code_samples
    assert "ABP-001" in item.code_samples


def test_collection_nested_codes_are_found(ruleset, tmp_path):
    """番号在子目录里也要能发现。"""
    make(tmp_path, "合集", {"子目录/AV-20608 sample.mp4": b"x"})
    item = isolate_mod.judge_directory("合集", tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP
    assert any("AV-20608" in c for c in item.code_samples)


def test_chinese_titles_only_is_moved(ruleset, tmp_path):
    """全是中文标题 -> 挪走。"""
    make(tmp_path, "collection-cn-a", {
        "500万高利贷姐姐被迫肉身偿还/1.mp4": b"x",
        "SM情侣狗奴调教/2.mp4": b"x",
    })
    item = isolate_mod.judge_directory("collection-cn-a", tmp_path, ruleset)
    assert item.isolation is Isolation.MOVE


def test_empty_dir_handling(ruleset, tmp_path):
    make(tmp_path, "empty")
    assert isolate_mod.judge_directory(
        "empty", tmp_path, ruleset, move_empty=True
    ).isolation is Isolation.MOVE
    assert isolate_mod.judge_directory(
        "empty", tmp_path, ruleset, move_empty=False
    ).isolation is Isolation.KEEP


def test_apply_moves_to_target(ruleset, tmp_path):
    root = tmp_path / "R"
    target = tmp_path / "GC"
    make(root, "02", {"a.mp4": b"x"})
    make(root, "SDMM028C", {"a.mp4": b"x"})

    plan = isolate_mod.build_isolation_plan(root, target, ruleset)
    result = isolate_mod.apply_isolation_plan(root, target, plan)

    assert result["moved"] == ["02"]
    assert (target / "02" / "a.mp4").exists()
    assert not (root / "02").exists()
    # 番号目录原地不动
    assert (root / "SDMM028C").exists()


def test_apply_creates_target_dir(ruleset, tmp_path):
    root = tmp_path / "R"
    make(root, "02", {"a.mp4": b"x"})
    target = tmp_path / "sub" / "GC"

    plan = isolate_mod.build_isolation_plan(root, target, ruleset)
    isolate_mod.apply_isolation_plan(root, target, plan)

    assert target.is_dir()
    assert (target / "02").is_dir()


def test_existing_name_gets_suffix(ruleset, tmp_path):
    """目标已有同名目录 -> 加序号，不覆盖。"""
    root = tmp_path / "R"
    target = tmp_path / "GC"
    make(root, "02", {"a.mp4": b"new"})
    make(target, "02", {"a.mp4": b"existing"})

    plan = isolate_mod.build_isolation_plan(root, target, ruleset)
    result = isolate_mod.apply_isolation_plan(root, target, plan)

    assert result["renamed"]
    assert (target / "02" / "a.mp4").read_bytes() == b"existing"
    assert (target / "02 (2)" / "a.mp4").read_bytes() == b"new"


def test_plan_reports_conflicts(ruleset, tmp_path):
    root = tmp_path / "R"
    target = tmp_path / "GC"
    make(root, "02", {"a.mp4": b"new"})
    make(target, "02", {"a.mp4": b"existing"})

    plan = isolate_mod.build_isolation_plan(root, target, ruleset)
    assert "02" in plan.conflicts


def test_only_dirs_are_considered(ruleset, tmp_path):
    """平铺文件不参与隔离判定。"""
    root = tmp_path / "R"
    root.mkdir()
    (root / "loose.mp4").write_bytes(b"x")
    make(root, "SDMM028C", {"a.mp4": b"x"})

    plan = isolate_mod.build_isolation_plan(root, tmp_path / "GC", ruleset)
    assert len(plan.items) == 1
    assert plan.items[0].name == "SDMM028C"


def test_target_dir_itself_is_not_moved(ruleset, tmp_path):
    """目标目录本身不能被挪进自己。"""
    root = tmp_path / "R"
    target = tmp_path / "GC"
    make(root, "02", {"a.mp4": b"x"})
    make(target, "existing", {"b.mp4": b"x"})

    plan = isolate_mod.build_isolation_plan(root, target, ruleset)
    moved = [i.name for i in plan.move_items]
    assert "GC" not in moved
    assert moved == ["02"]


def test_target_inside_source_is_rejected(ruleset, tmp_path):
    """目标在源目录内部会造成嵌套，必须拒绝。"""
    root = tmp_path / "R"
    make(root, "02", {"a.mp4": b"x"})

    with pytest.raises(ValueError, match="内部"):
        isolate_mod.build_isolation_plan(root, root / "GC", ruleset)


def test_sibling_target_works(ruleset, tmp_path):
    """同级目标目录（真实用法）正常工作。"""
    root = tmp_path / "R"
    target = tmp_path / "GC"
    make(root, "02", {"a.mp4": b"x"})
    make(root, "SDMM028C", {"a.mp4": b"x"})

    plan = isolate_mod.build_isolation_plan(root, target, ruleset)
    result = isolate_mod.apply_isolation_plan(root, target, plan)

    assert result["moved"] == ["02"]
    assert (target / "02" / "a.mp4").exists()
    assert (root / "SDMM028C").is_dir()


# --------------------------------------------------------------------------
# 国产内容把番号形态的码塞进中文标题 —— 那不是番号
# --------------------------------------------------------------------------
#
# 实测踩过的坑：国产站给内容套一个番号形态的码（SATM-035 / DMX-0067 /
# AH-001 / BOI096…），而且是塞在**中文标题里** ——
# 「【AI增强】SATM-035美少女模特为了凑医药费出卖肉体.mp4」。
# 旧判定只看「名字里有没有番号形态的片段」，于是这批国产目录全被判成
# 「含番号的合集目录」留在原地 —— 国产内容就这么卡在番号库里，隔离不出去。
#
# 判定改成「名字里含中日韩字符就不算番号形态」。真番号的文件名是纯 ASCII，
# 实测 F / G / H 三个番号库的番号形态命中名里含中日韩字符的有 0 条，
# 所以这条收紧对番号库零影响。


def test_code_in_chinese_title_is_not_a_code(ruleset, tmp_path):
    """中文标题里的番号形态片段不算番号。"""
    d = tmp_path / "2026.4.27国产直播黄播自拍网黄大神精选"
    d.mkdir()
    (d / "【AI增强】SATM-035美少女模特为了凑医药费出卖肉体.mp4").write_bytes(b"x")
    (d / "【AI增强】SATM-040寂寞少妇跳弹无法满足电话约炮友.mp4").write_bytes(b"x")

    assert isolate_mod.find_codes_in_dir(d, ruleset=ruleset) == []


def test_code_in_dir_name_with_chinese_is_not_a_code(ruleset, tmp_path):
    """目录名里的网红账号名也不是番号。"""
    d = tmp_path / "推特极品白虎美鲍烧鸡"
    d.mkdir()
    (d / "-SP站】【XueNi-888】超高颜值妩媚动人小骚逼裸舞自慰喷水③.mp4").write_bytes(b"x")

    assert isolate_mod.find_codes_in_dir(d, ruleset=ruleset) == []


def test_domestic_dir_with_code_in_title_is_moved(ruleset, tmp_path):
    """整个目录都要挪走，不能因为文件名里有番号形态就留在原地。"""
    d = tmp_path / "2026.4.27国产直播黄播自拍网黄大神精选"
    d.mkdir()
    (d / "【AI增强】SATM-035美少女模特为了凑医药费出卖肉体.mp4").write_bytes(b"x")

    item = isolate_mod.judge_directory(d.name, tmp_path, ruleset)
    assert item.isolation is Isolation.MOVE


def test_page_export_names_are_not_codes(ruleset, tmp_path):
    """``Page0000.jpg`` 这类扫描件导出名不能被认成番号。

    实测国产相册目录里全是 Page0000.jpg ~ Page0027.jpg，
    不排掉的话整个目录会被判成「含番号」而留在番号库里。
    """
    d = tmp_path / "(C107) [ロボぞうきん] ヨシマコムラレクチャー"
    d.mkdir()
    for i in range(3):
        (d / ("Page%04d.jpg" % i)).write_bytes(b"x")

    assert isolate_mod.find_codes_in_dir(d, ruleset=ruleset) == []


def test_real_codes_in_ascii_names_are_still_found(ruleset, tmp_path):
    """收紧不能过头：纯 ASCII 文件名里的真番号照样要认出来。"""
    d = tmp_path / "合集"
    d.mkdir()
    (d / "489155.com@CLUB-925.mp4").write_bytes(b"x")
    (d / "hhd800.com@DVMM-379.ja.srt").write_bytes(b"x")

    codes = isolate_mod.find_codes_in_dir(d, ruleset=ruleset)
    assert "CLUB-925" in codes
    assert "DVMM-379" in codes


def test_collection_with_ascii_codes_is_still_kept(ruleset, tmp_path):
    """合集目录里的真番号不能被收紧误伤。"""
    make(tmp_path, "合集", {
        "ABP-001/ABP-001.mp4": b"x",
        "AV-20608/AV-20608.mp4": b"x",
    })

    item = isolate_mod.judge_directory("合集", tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP
    assert item.code_samples


def test_cjk_named_subdir_with_code_is_still_kept(ruleset, tmp_path):
    """含中日韩字符的**子目录名**是例外：下载站起的复合名确实是番号。

    「第一會所新片@SIS001@CLUB-936」这种名字带中文，但它是番号。
    收紧之后只剩这里能兜住这一类证据 —— 漏了就会把合集目录误挪走。
    """
    make(tmp_path, "合集", {
        "第一會所新片@SIS001@CLUB-936/CLUB-936.mp4": b"x",
    })

    item = isolate_mod.judge_directory("合集", tmp_path, ruleset)
    assert item.isolation is Isolation.KEEP
    assert any("CLUB-936" in c for c in item.code_samples)


def test_numbered_dir_with_chinese_series_prefix_is_kept(ruleset, tmp_path):
    """目录名本身是番号（含中文系列名）-> 保留，判定走的是名字规则。"""
    make(tmp_path, "第一會所新片@SIS001@SKMJ-777", {"SKMJ-777.mp4": b"x"})

    item = isolate_mod.judge_directory(
        "第一會所新片@SIS001@SKMJ-777", tmp_path, ruleset
    )
    assert item.isolation is Isolation.KEEP
