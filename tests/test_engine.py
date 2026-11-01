"""规则引擎测试：噪声剥离与番号匹配的关键行为。"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import engine
from file_tool.models import Action

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return engine.load_ruleset(RULES)


def plan_for(ruleset, name: str):
    plan = engine.build_plan(Path("/fake"), ruleset, [name])
    return plan.items[0]


@pytest.mark.parametrize(
    "original,expected",
    [
        # 水印域名
        ("aavv38.xyz@SDMM028", "SDMM-028"),
        ("kfa11.com@MAAN-957", "MAAN-957"),
        ("aavv39.xyz@WAAA355", "WAAA-355"),
        # 厂牌 + 数字（无横杠）
        ("SDMM028C", "SDMM-028C"),
        ("MON004", "MON-004"),
        ("CAWD660CX", "CAWD-660CX"),
        ("DNW102A", "DNW-102A"),
        # 日期型
        ("DOW24070403", "DOW-240704-03"),
        ("TEA24080208", "TEA-240802-08"),
        ("7K24042301", "7K-240423-01"),
        # FLUP 系列：数字段整体保留
        ("FLUP11201", "FLUP-11201"),
        ("FLUP113730", "FLUP-113730"),
        ("FLUP1122285", "FLUP-1122285"),
        # 小写厂牌
        ("xfxfz14468", "xfxfz-14468"),
        ("dvdms00300a", "dvdms-00300a"),
        # 数字前缀
        ("230ORECO-640CX", "ORECO-640CX"),
        ("107MMGH-313", "MMGH-313"),
        # HD_ 前缀
        ("HD_URKK-072", "URKK-072"),
    ],
)
def test_normalize(ruleset, original, expected):
    item = plan_for(ruleset, original)
    assert item.target == expected, f"{original} -> {item.target}（期望 {expected}）"
    assert item.action is Action.RENAME


@pytest.mark.parametrize(
    "original,expected",
    [
        # 本来就带横杠的规范名，保持不动
        ("MAAN-961", "MAAN-961"),
        ("IPZZ-118C", "IPZZ-118C"),
        ("SSIS-863C", "SSIS-863C"),
        ("avop-235-C", "avop-235-C"),
        ("FC2-PPV-4329956", "FC2-PPV-4329956"),
    ],
)
def test_already_normalized_is_kept(ruleset, original, expected):
    """已符合规范的名字应保持 KEEP，不产生多余改名。"""
    item = plan_for(ruleset, original)
    assert item.target == expected
    assert item.action is Action.KEEP


@pytest.mark.parametrize(
    "original,expected",
    [
        # 尾部重复副本标记要回贴，不能与主目录撞名
        ("SDMM030AC(1)", "SDMM-030AC (1)"),
        ("SDMM076(1)", "SDMM-076 (1)"),
        ("TEA24080824 2", "TEA-240808-24 (2)"),
        ("FLUP11223-2", "FLUP-11223 (2)"),
        ("HEA24060902-2", "HEA-240609-02 (2)"),
        # 域名里带 (N) 的副本
        ("aavv38(1).xyz@DVMM055A", "DVMM-055A (1)"),
    ],
)
def test_duplicate_suffix_is_kept(ruleset, original, expected):
    item = plan_for(ruleset, original)
    assert item.target == expected


def test_hyphen_in_code_is_not_treated_as_duplicate(ruleset):
    """MAAN-961 的横杠属于番号本身，不能被当成重复标记剥掉。"""
    item = plan_for(ruleset, "MAAN-961")
    assert item.target == "MAAN-961"
    assert item.stripped == "MAAN-961"


@pytest.mark.parametrize(
    "original",
    [
        "02",
        "collection-cn-a",
        "collection-cn-b",
        "Uncensored Leaked_001",
    ],
)
def test_non_number_names_are_skipped(ruleset, original):
    item = plan_for(ruleset, original)
    assert item.action is Action.SKIP
    assert item.target == original
    # 未匹配的名字没有「基础名」——空串表示不参与 merge 分组
    assert item.base == ""


def test_conflict_is_detected(ruleset):
    """两个目录映射到同一个目标名时必须报冲突，不能静默覆盖。"""
    plan = engine.build_plan(
        Path("/fake"), ruleset, ["aavv38.xyz@DVMM055A", "aavv38(1).xyz@DVMM055A"]
    )
    targets = [i.target for i in plan.items]
    assert len(set(targets)) == 2, "两个目录的目标名必须唯一"
    assert not plan.conflict_items


def test_apply_plan_renames_on_disk(tmp_path):
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "aavv38.xyz@MON004").mkdir()
    ruleset = engine.load_ruleset(RULES)
    names = sorted(p.name for p in tmp_path.iterdir())
    plan = engine.build_plan(tmp_path, ruleset, names)
    done = engine.apply_plan(tmp_path, plan)
    assert len(done) == 2
    assert (tmp_path / "SDMM-028C").is_dir()
    assert (tmp_path / "MON-004").is_dir()


def test_apply_plan_keeps_original_when_conflict(tmp_path):
    """目标名已存在时，加序号兜底而不是覆盖。"""
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "SDMM-028C").mkdir()
    ruleset = engine.load_ruleset(RULES)
    names = sorted(p.name for p in tmp_path.iterdir())
    plan = engine.build_plan(tmp_path, ruleset, names)
    engine.apply_plan(tmp_path, plan)
    assert (tmp_path / "SDMM-028C").is_dir()


def test_bad_capture_name_is_rejected(tmp_path):
    import yaml

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "preprocess": [{"name": "x", "pattern": "abc", "capture": "nope"}],
                "matches": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(engine.RuleError):
        engine.load_ruleset(bad)


def test_invalid_regex_is_rejected(tmp_path):
    import yaml

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {"version": 1, "preprocess": [], "matches": [{"name": "x", "pattern": "([a-z"}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(engine.RuleError):
        engine.load_ruleset(bad)
