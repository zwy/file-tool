"""archive 阶段测试：平铺文件按归一番号分装。"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import archive as archive_mod


def touch(root: Path, name: str) -> None:
    """在 root 下创建文件；name 可以是相对路径。"""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_normalize_key_strips_hyphen_and_version():
    assert archive_mod.normalize_key("CLUB810") == "CLUB-810"
    assert archive_mod.normalize_key("CLUB-810") == "CLUB-810"
    assert archive_mod.normalize_key("CLUB830C") == "CLUB-830"
    assert archive_mod.normalize_key("CLUB830") == "CLUB-830"
    assert archive_mod.normalize_key("CLUB-806C") == "CLUB-806"
    assert archive_mod.normalize_key("CLUB806C") == "CLUB-806"


def test_normalize_key_rejects_non_codes():
    assert archive_mod.normalize_key("readme") is None
    assert archive_mod.normalize_key("some random file") is None


def test_is_fork_detects_garbage():
    assert archive_mod.is_fork("._CLUB810.mp4")
    assert archive_mod.is_fork(".DS_Store")
    assert not archive_mod.is_fork("CLUB810.mp4")


def test_plan_groups_by_normalized_code(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "CLUB810.jpg")
    touch(tmp_path, "CLUB-810.mp4")
    touch(tmp_path, "CLUB830.mp4")
    touch(tmp_path, "CLUB830C.mp4")

    plan = archive_mod.build_archive_plan(tmp_path)
    groups = {g.name: g for g in plan.groups}

    assert set(groups) == {"CLUB-810", "CLUB-830"}
    # 横杠变体归一到同一组
    assert len(groups["CLUB-810"].items) == 3
    assert groups["CLUB-810"].merged
    # C 后缀归一到同一组
    assert len(groups["CLUB-830"].items) == 2
    assert groups["CLUB-830"].merged
    assert groups["CLUB-830"].variants == ["CLUB830", "CLUB830C"]


def test_forks_are_collected_not_grouped(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "._CLUB810.mp4")
    touch(tmp_path, ".DS_Store")

    plan = archive_mod.build_archive_plan(tmp_path)
    assert len(plan.forks) == 2
    assert len(plan.groups) == 1
    assert len(plan.groups[0].items) == 1


def test_non_media_files_stay_untouched(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "notes.txt")
    touch(tmp_path, "cover.jpg")

    plan = archive_mod.build_archive_plan(tmp_path)
    assert "notes.txt" in plan.untouched
    assert "cover.jpg" in plan.untouched


def test_apply_creates_dirs_and_moves_files(tmp_path):
    for name in ["CLUB810.mp4", "CLUB810.jpg", "CLUB-810.mp4", "CLUB830C.mp4"]:
        touch(tmp_path, name)

    plan = archive_mod.build_archive_plan(tmp_path)
    result = archive_mod.apply_archive_plan(tmp_path, plan)

    assert result["created_dirs"] == 2
    assert result["moved"] == 4
    assert (tmp_path / "CLUB-810" / "CLUB810.mp4").exists()
    assert (tmp_path / "CLUB-810" / "CLUB-810.mp4").exists()
    assert (tmp_path / "CLUB-830" / "CLUB830C.mp4").exists()
    # 顶层已清空文件
    assert not any(p.is_file() for p in tmp_path.iterdir())


def test_apply_removes_forks(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "._CLUB810.mp4")

    plan = archive_mod.build_archive_plan(tmp_path)
    result = archive_mod.apply_archive_plan(tmp_path, plan, clean_forks=True)

    assert result["forks_removed"] == 1
    assert not (tmp_path / "._CLUB810.mp4").exists()


def test_keep_forks_option(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "._CLUB810.mp4")

    plan = archive_mod.build_archive_plan(tmp_path)
    archive_mod.apply_archive_plan(tmp_path, plan, clean_forks=False)

    assert (tmp_path / "._CLUB810.mp4").exists()


def test_existing_dir_is_reused_not_overwritten(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    (tmp_path / "CLUB-810").mkdir()
    (tmp_path / "CLUB-810" / "CLUB810.mp4").write_bytes(b"original")

    plan = archive_mod.build_archive_plan(tmp_path)
    result = archive_mod.apply_archive_plan(tmp_path, plan)

    # 目标已有同名文件 -> 冲突跳过，不覆盖
    assert result["conflicts"] == 1
    assert (tmp_path / "CLUB-810" / "CLUB810.mp4").read_bytes() == b"original"
    # 原文件仍在顶层
    assert (tmp_path / "CLUB810.mp4").exists()


def test_dir_name_conflicting_with_file_is_blocked(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "CLUB-810")  # 一个同名普通文件

    plan = archive_mod.build_archive_plan(tmp_path)
    assert plan.blocked == ["CLUB-810"]
    # 不建目录、不动文件
    assert not (tmp_path / "CLUB-810").is_dir()
    assert (tmp_path / "CLUB810.mp4").exists()


def test_existing_subdirs_are_not_recursed(tmp_path):
    touch(tmp_path, "CLUB810.mp4")
    (tmp_path / "already").mkdir()
    (tmp_path / "already" / "x.mp4").write_bytes(b"x")

    plan = archive_mod.build_archive_plan(tmp_path)
    # 「already」是目录，不参与分装
    assert len(plan.groups) == 1
    assert (tmp_path / "already" / "x.mp4").exists()


def test_forks_are_cleaned_before_moving(tmp_path):
    """垃圾文件必须在移动之前清掉。

    macOS 在非 HFS+ 卷上给文件建 ``._xxx`` 资源叉，``shutil.move``
    会把它一起带走；移动之后按原名找不到，垃圾就残留在新目录里。
    """
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "CLUB810.jpg")
    touch(tmp_path, "._CLUB810.mp4")

    plan = archive_mod.build_archive_plan(tmp_path)
    archive_mod.apply_archive_plan(tmp_path, plan, clean_forks=True)

    # 目录里不该有资源叉残留
    assert not list((tmp_path / "CLUB-810").glob("._*"))
    assert not list(tmp_path.glob("._*"))
    assert (tmp_path / "CLUB-810" / "CLUB810.mp4").exists()


def test_all_forks_removed_including_nested(tmp_path):
    """移动后新产生的资源叉也要清掉（含目录的）。"""
    touch(tmp_path, "CLUB810.mp4")
    # 模拟 macOS 为新建目录生成的资源叉
    (tmp_path / "._CLUB-810").write_bytes(b"dir fork")

    plan = archive_mod.build_archive_plan(tmp_path)
    archive_mod.apply_archive_plan(tmp_path, plan, clean_forks=True)

    assert not (tmp_path / "._CLUB-810").exists()
    assert (tmp_path / "CLUB-810" / "CLUB810.mp4").exists()
