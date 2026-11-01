"""tidy 测试：形态识别与动作编排。"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import engine
from file_tool import tidy as tidy_mod
from file_tool.tidy import Shape, StepKind

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return engine.load_ruleset(RULES)


def touch(root: Path, name: str, content: bytes = b"x") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_detect_flat(ruleset, tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "CLUB810.jpg")

    shape, reason, numbered, files = tidy_mod.detect_shape(tmp_path, ruleset)
    assert shape is Shape.FLAT
    assert "没有子目录" in reason


def test_detect_nested(ruleset, tmp_path):
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "MIDE004").mkdir()

    shape, reason, numbered, files = tidy_mod.detect_shape(tmp_path, ruleset)
    assert shape is Shape.NESTED
    assert set(numbered) == {"SDMM028C", "MIDE004"}


def test_detect_mixed(ruleset, tmp_path):
    (tmp_path / "SDMM028C").mkdir()
    touch(tmp_path, "CLUB810.mp4")

    shape, reason, numbered, files = tidy_mod.detect_shape(tmp_path, ruleset)
    assert shape is Shape.MIXED


def test_detect_loose(ruleset, tmp_path):
    """子目录都不像番号 -> loose，不做任何改动。"""
    (tmp_path / "some random folder").mkdir()
    (tmp_path / "另一个目录").mkdir()

    shape, reason, numbered, files = tidy_mod.detect_shape(tmp_path, ruleset)
    assert shape is Shape.LOOSE
    assert numbered == []


def test_loose_produces_no_steps(ruleset, tmp_path):
    (tmp_path / "random").mkdir()
    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    assert plan.steps == []
    assert plan.notes


def test_flat_produces_archive_step(ruleset, tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "CLUB810.jpg")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    kinds = [s.kind for s in plan.steps]
    assert StepKind.ARCHIVE in kinds
    assert StepKind.RENAME not in kinds


def test_nested_produces_merge_and_rename(ruleset, tmp_path):
    payload = {"a.mp4": b"same"}
    touch(tmp_path, "SDMM030AC/a.mp4", b"same")
    touch(tmp_path, "SDMM030AC(1)/a.mp4", b"same")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    kinds = [s.kind for s in plan.steps]
    assert StepKind.MERGE in kinds
    assert StepKind.RENAME in kinds
    # merge 必须排在 rename 之前
    assert kinds.index(StepKind.MERGE) < kinds.index(StepKind.RENAME)


def test_clean_step_for_system_garbage(ruleset, tmp_path):
    touch(tmp_path, ".DS_Store")
    touch(tmp_path, "._.DS_Store")
    (tmp_path / "SDMM028C").mkdir()

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    clean = [s for s in plan.steps if s.kind is StepKind.CLEAN]
    assert len(clean) == 1
    assert clean[0].count == 2
    # clean 排在最前
    assert plan.steps[0].kind is StepKind.CLEAN


def test_apply_tidy_end_to_end(ruleset, tmp_path):
    """平铺文件 -> 分装 -> 目录建好。"""
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "CLUB810.jpg")
    touch(tmp_path, "CLUB-810.mp4")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    result = tidy_mod.apply_tidy_plan(tmp_path, ruleset, plan)

    assert result["archived"] == 3
    assert (tmp_path / "CLUB-810" / "CLUB810.mp4").exists()
    assert (tmp_path / "CLUB-810" / "CLUB-810.mp4").exists()
    assert (tmp_path / "CLUB-810" / "CLUB810.jpg").exists()


def test_apply_tidy_merge_then_rename(ruleset, tmp_path):
    """重复目录先合并，再改名。"""
    touch(tmp_path, "SDMM030AC/a.mp4", b"same")
    touch(tmp_path, "SDMM030AC(1)/a.mp4", b"same")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    result = tidy_mod.apply_tidy_plan(tmp_path, ruleset, plan)

    assert result["merged"] == 1
    assert result["renamed"] >= 1
    assert (tmp_path / "SDMM-030AC" / "a.mp4").exists()
    assert not (tmp_path / "SDMM030AC(1)").exists()


def test_apply_tidy_cleans_forks(ruleset, tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "._CLUB810.mp4")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    result = tidy_mod.apply_tidy_plan(tmp_path, ruleset, plan)

    assert result["forks_removed"] == 1
    assert not (tmp_path / "._CLUB810.mp4").exists()


def test_apply_tidy_keeps_forks_when_asked(ruleset, tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "._CLUB810.mp4")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    tidy_mod.apply_tidy_plan(tmp_path, ruleset, plan, clean_forks=False)

    assert (tmp_path / "._CLUB810.mp4").exists()


def test_nested_titles_are_not_renamed(ruleset, tmp_path):
    """嵌套层的原始标题不是番号，不能被改名。"""
    (tmp_path / "SDMM028C").mkdir()
    title = "某个中文标题.mp4"
    touch(tmp_path, f"SDMM028C/{title}")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    renamed = [s for s in plan.steps if s.kind is StepKind.RENAME]
    # 只改顶层目录名，嵌套内容不动
    assert renamed
    assert renamed[0].count == 1
    assert (tmp_path / "SDMM028C" / title).exists()
