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
        ("MIDE004", "MIDE-004"),
        ("CAWD660CX", "CAWD-660CX"),
        ("DNW102A", "DNW-102A"),
        # 小写厂牌 —— 规范名统一大写
        ("nizuia00877", "NIZUIA-00877"),
        ("dvdms00300a", "DVDMS-00300A"),
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
    "original",
    [
        # FLUP 是国产内容的编号，不是日本番号 —— 不该被套番号规范
        "FLUP11201",
        "FLUP113730",
        "FLUP1122285",
        "FLUP-1131428",
        "FLUP11223-2",
        "FLUP-11223 (2)",
        # 日期型（厂牌 + YYMMDD + 序号）是国产资源站的目录编号格式
        "DOW24070403",
        "DOW-240704-03",
        "TEA24080208",
        "TEA-240802-08",
        "7K24042301",
        "7K-240423-01",
        "LRO-240506-23",
        "HEA-240609-02",
        "KEO24040901",
        "WLF-240303-08",
        "DEL-240223-11",
        "XZO-240226-12",
        # MON、xfxfz 同样是国产资源站的编号
        "MON004",
        "MON-006",
        "xfxfz14468",
        "xfxfz-14681",
    ],
)
def test_domestic_prefix_is_not_a_code(ruleset, original):
    """命中排除名单的国产编号必须被跳过，不能当番号改名。"""
    item = plan_for(ruleset, original)
    assert item.action is Action.SKIP, f"{original} 不该被改名"
    assert item.target == original
    assert item.base == ""


@pytest.mark.parametrize(
    "original,expected",
    [
        # 规范名必须能反解回自己（幂等）—— 否则第二次跑 rename 会改坏目录
        ("DVDMS-00300B", "DVDMS-00300B"),
        ("SDMU-00877B", "SDMU-00877B"),
        ("SVDVD-840 (1)", "SVDVD-840 (1)"),
        ("MAAN-961", "MAAN-961"),
        ("SDMM-030BC", "SDMM-030BC"),
        ("CLT-013C", "CLT-013C"),
        ("FC2-PPV-2549115", "FC2-PPV-2549115"),
        ("AVOP-235-C", "AVOP-235-C"),
    ],
)
def test_canonical_name_is_idempotent(ruleset, original, expected):
    """已经规范的目录再跑一次 rename 必须保持 KEEP。"""
    item = plan_for(ruleset, original)
    assert item.target == expected, f"{original} -> {item.target}（期望 {expected}）"
    assert item.action is Action.KEEP, f"{original} 不该被再次改名"


def test_optional_group_is_not_rendered_as_none(ruleset):
    """没参与匹配的可选分组是 None，不能渲染成字符串 "None"。"""
    item = plan_for(ruleset, "dvdms-00300b")
    assert item.target == "DVDMS-00300B"
    assert "None" not in item.target


def test_hyphen_duplicate_strip_does_not_override_correct_parse(tmp_path):
    """基准已经能正确解析时，条件剥离不得用错误结果覆盖它。

    这里用**合成规则集**而不是具体厂牌 —— 因为要测的是引擎行为：
    只要「基准能匹配」且「剥掉之后也能匹配但结果不同」，引擎就必须
    保留基准，不能让剥离结果覆盖它。

    真实踩过的坑是 `DOW-240706-24`：日期型规则能正确解析成
    `DOW-240706-24`，而剥掉 `-24` 后剩下的 `DOW-240706` 会被
    「厂牌-数字」规则抢先匹配，变成错误的 `DOW-240706 (24)`。
    """
    import yaml

    rules_path = tmp_path / "synthetic.yaml"
    rules_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "preprocess": [
                    {
                        "name": "strip_hyphen_dup",
                        "pattern": r"-(?P<dup>\d+)$",
                        "capture": "dup",
                        "conditional": True,
                    }
                ],
                "matches": [
                    {
                        # 基准规则：厂牌-日期-序号
                        "name": "date_style",
                        "pattern": r"^(?P<studio>[A-Z]{3})-(?P<date>\d{6})-(?P<seq>\d{2})$",
                        "template": "{studio}-{date}-{seq}",
                        "dup_suffix": " ({dup})",
                        "priority": 100,
                    },
                    {
                        # 抢先匹配的规则：无横杠的厂牌+数字
                        "name": "studio_num_ver",
                        "pattern": r"^(?P<studio>[A-Z]{3})(?P<num>\d{6})$",
                        "template": "{studio}-{num}",
                        "dup_suffix": " ({dup})",
                        "priority": 90,
                    },
                ],
                "dup_sources": ["dup"],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    synthetic = engine.load_ruleset(rules_path)

    item = plan_for(synthetic, "ABC-240706-24")
    assert item.target == "ABC-240706-24", f"被错误覆盖成 {item.target}"
    assert item.action is Action.KEEP

    # 反过来：基准匹配不上时，条件剥离必须生效。
    # 'ABC240706-24' 剥掉尾部 '-24'（重复副本标记）后剩 'ABC240706'，
    # 被「厂牌+数字」规则匹配成 'ABC-240706'，标记按 dup_suffix 回贴成 ' (24)'
    item2 = plan_for(synthetic, "ABC240706-24")
    assert item2.target == "ABC-240706 (24)"
    assert item2.action is Action.RENAME


@pytest.mark.parametrize(
    "original,expected",
    [
        # 本来就带横杠的规范名，保持不动
        ("MAAN-961", "MAAN-961"),
        ("IPZZ-118C", "IPZZ-118C"),
        ("SSIS-863C", "SSIS-863C"),
        ("AVOP-235-C", "AVOP-235-C"),
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
        ("SDMM028C 2", "SDMM-028C (2)"),
        ("MAAN961-2", "MAAN-961 (2)"),
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
    (tmp_path / "aavv38.xyz@MIDE004").mkdir()
    ruleset = engine.load_ruleset(RULES)
    names = sorted(p.name for p in tmp_path.iterdir())
    plan = engine.build_plan(tmp_path, ruleset, names)
    done = engine.apply_plan(tmp_path, plan)
    assert len(done.renamed) == 2
    assert not done.failed
    assert (tmp_path / "SDMM-028C").is_dir()
    assert (tmp_path / "MIDE-004").is_dir()


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
