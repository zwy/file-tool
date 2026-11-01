"""merge 阶段测试：内容比对判定与合并执行。"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import engine
from file_tool import merge as merge_mod
from file_tool.merge import MergeVerdict

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return engine.load_ruleset(RULES)


def make_dir(root: Path, name: str, files: dict[str, bytes]) -> None:
    d = root / name
    d.mkdir(parents=True)
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def test_identical_dirs_are_duplicates(ruleset, tmp_path):
    """内容完全一致 -> 判定为可合并。"""
    payload = {"a.mp4": b"same-content", "b.jpg": b"same-image"}
    make_dir(tmp_path, "SDMM030AC", payload)
    make_dir(tmp_path, "SDMM030AC(1)", payload)

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM030AC", "SDMM030AC(1)"])
    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert group.verdict is MergeVerdict.DUPLICATE
    assert group.base == "SDMM-030AC"


def test_different_content_is_not_merged(ruleset, tmp_path):
    """同名但内容不同 -> 判定为不同版本，绝不能合并。"""
    make_dir(tmp_path, "SDMM030BC", {"SDMM030BC.mp4": b"version-one" * 100})
    make_dir(tmp_path, "SDMM030BC(1)", {"SDMM030BC.mp4": b"version-two" * 900})

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM030BC", "SDMM030BC(1)"])
    group = plan.groups[0]
    assert group.verdict is MergeVerdict.DIFFERENT
    assert group.conflicting_files


def test_partial_overlap(ruleset, tmp_path):
    """部分同名不同、部分独有 -> PARTIAL。"""
    make_dir(tmp_path, "TEA24080824", {"shared.mp4": b"same", "only-a.mp4": b"aaa"})
    make_dir(tmp_path, "TEA24080824 2", {"shared.mp4": b"same", "only-b.mp4": b"bbb"})

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["TEA24080824", "TEA24080824 2"])
    group = plan.groups[0]
    assert group.verdict is MergeVerdict.PARTIAL
    assert group.unique_files


def test_watermark_duplicate_grouping(ruleset, tmp_path):
    """带水印域名和 (1) 副本的目录归到同一基础名。"""
    payload = {"DVMM055A.jpg": b"cover", "DVMM055A.mp4": b"movie"}
    make_dir(tmp_path, "aavv38.xyz@DVMM055A", payload)
    make_dir(tmp_path, "aavv38(1).xyz@DVMM055A", payload)

    plan = merge_mod.build_merge_plan(
        tmp_path, ruleset, ["aavv38.xyz@DVMM055A", "aavv38(1).xyz@DVMM055A"]
    )
    assert len(plan.groups) == 1
    assert plan.groups[0].base == "DVMM-055A"
    assert plan.groups[0].verdict is MergeVerdict.DUPLICATE


def test_single_dir_forms_no_group(ruleset, tmp_path):
    """只有一个目录时不触发内容比对。"""
    make_dir(tmp_path, "SDMM028C", {"x.mp4": b"x"})
    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM028C"])
    assert plan.groups == []


def test_apply_merge_removes_duplicate(ruleset, tmp_path):
    """合并后副本目录消失，主目录内容完好。"""
    payload = {"a.mp4": b"same-content", "b.jpg": b"same-image"}
    make_dir(tmp_path, "SDMM030AC", payload)
    make_dir(tmp_path, "SDMM030AC(1)", payload)

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM030AC", "SDMM030AC(1)"])
    merge_mod.apply_merge_plan(tmp_path, plan)

    assert not (tmp_path / "SDMM030AC(1)").exists()
    assert (tmp_path / "SDMM030AC" / "a.mp4").exists()
    assert (tmp_path / "SDMM030AC" / "b.jpg").exists()


def test_apply_merge_keeps_conflicting_file(ruleset, tmp_path):
    """内容冲突时副本目录保留，绝不覆盖主目录的文件。"""
    make_dir(tmp_path, "SDMM030BC", {"SDMM030BC.mp4": b"version-one"})
    make_dir(tmp_path, "SDMM030BC(1)", {"SDMM030BC.mp4": b"version-two"})

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM030BC", "SDMM030BC(1)"])
    results = merge_mod.apply_merge_plan(tmp_path, plan)

    # 主目录内容未被改动
    assert (tmp_path / "SDMM030BC" / "SDMM030BC.mp4").read_bytes() == b"version-one"
    # 副本目录仍在，冲突文件保留
    assert (tmp_path / "SDMM030BC(1)" / "SDMM030BC.mp4").read_bytes() == b"version-two"
    # 且明确报告为 skip
    assert all(r["action"] == "skip" for r in results)


def test_apply_merge_partial_is_skipped_by_default(ruleset, tmp_path):
    """默认只合并完全一致的组，部分重叠不动。"""
    make_dir(tmp_path, "TEA24080824", {"shared.mp4": b"same", "only-a.mp4": b"aaa"})
    make_dir(tmp_path, "TEA24080824 2", {"shared.mp4": b"same", "only-b.mp4": b"bbb"})

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["TEA24080824", "TEA24080824 2"])
    merge_mod.apply_merge_plan(tmp_path, plan)

    assert (tmp_path / "TEA24080824").is_dir()
    assert (tmp_path / "TEA24080824 2").is_dir()


def test_apply_merge_partial_when_enabled(ruleset, tmp_path):
    """显式开启后，并入独有文件，冲突文件保留。"""
    make_dir(tmp_path, "TEA24080824", {"shared.mp4": b"same", "only-a.mp4": b"aaa"})
    make_dir(tmp_path, "TEA24080824 2", {"shared.mp4": b"same", "only-b.mp4": b"bbb"})

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["TEA24080824", "TEA24080824 2"])
    merge_mod.apply_merge_plan(tmp_path, plan, only_duplicates=False)

    primary = tmp_path / "TEA24080824"
    assert (primary / "only-a.mp4").exists()
    assert (primary / "only-b.mp4").exists()


def test_nested_subdirectories_are_compared(ruleset, tmp_path):
    """嵌套子目录也要参与内容比对。"""
    make_dir(tmp_path, "DOW24072205", {"01/1.mp4": b"part-one"})
    make_dir(tmp_path, "DOW24072205-2", {"02/11.mp4": b"part-two"})

    plan = merge_mod.build_merge_plan(
        tmp_path, ruleset, ["DOW24072205", "DOW24072205-2"]
    )
    group = plan.groups[0]
    assert group.verdict is MergeVerdict.PARTIAL
    assert any("11.mp4" in f for f in group.unique_files)


def test_macos_resource_forks_ignored(ruleset, tmp_path):
    """._ 开头的资源叉文件不参与比对。"""
    make_dir(tmp_path, "SDMM030AC", {"a.mp4": b"same"})
    make_dir(tmp_path, "SDMM030AC(1)", {"a.mp4": b"same"})
    (tmp_path / "SDMM030AC" / "._a.mp4").write_bytes(b"fork")
    (tmp_path / "SDMM030AC(1)" / "._a.mp4").write_bytes(b"fork")

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM030AC", "SDMM030AC(1)"])
    assert plan.groups[0].verdict is MergeVerdict.DUPLICATE


def test_already_renamed_dirs_still_group(ruleset, tmp_path):
    """改名后的形态（'DVMM-055A (1)'）仍要能和主目录归到一组。

    改名后的名字带尾部 ' (1)'，规则引擎解析不出番号，
    分组时必须能剥掉标记再解析，否则 merge 会漏掉这些组。
    """
    payload = {"DVMM055A.jpg": b"cover", "DVMM055A.mp4": b"movie"}
    make_dir(tmp_path, "DVMM-055A", payload)
    make_dir(tmp_path, "DVMM-055A (1)", payload)

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["DVMM-055A", "DVMM-055A (1)"])
    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert group.base == "DVMM-055A"
    assert group.verdict is MergeVerdict.DUPLICATE
    assert group.primary == "DVMM-055A"


def test_rename_then_merge_pipeline(ruleset, tmp_path):
    """完整两阶段：先改名，再合并。"""
    payload = {"DVMM055A.mp4": b"movie"}
    make_dir(tmp_path, "aavv38.xyz@DVMM055A", payload)
    make_dir(tmp_path, "aavv38(1).xyz@DVMM055A", payload)

    names = sorted(p.name for p in tmp_path.iterdir())
    rename_plan = engine.build_plan(tmp_path, ruleset, names)
    engine.apply_plan(tmp_path, rename_plan)
    assert (tmp_path / "DVMM-055A").is_dir()
    assert (tmp_path / "DVMM-055A (1)").is_dir()

    names2 = sorted(p.name for p in tmp_path.iterdir())
    merge_plan = merge_mod.build_merge_plan(tmp_path, ruleset, names2)
    merge_mod.apply_merge_plan(tmp_path, merge_plan)

    assert (tmp_path / "DVMM-055A").is_dir()
    assert not (tmp_path / "DVMM-055A (1)").exists()
    assert (tmp_path / "DVMM-055A" / "DVMM055A.mp4").exists()
