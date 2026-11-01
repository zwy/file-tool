"""Windows 语义下的执行层行为：文件被占用、非法名。

macOS 允许删改正在打开的文件，Windows 不允许。没有这层兜底，
一个被资源管理器预览窗占住的文件就能让整次操作抛异常中断，
留下「并了一半」的目录 —— 这在 Windows 上是常态，不是边缘情况。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from file_tool import archive as archive_mod
from file_tool import fscompat
from file_tool import isolate as isolate_mod
from file_tool import merge as merge_mod
from file_tool.engine import load_ruleset

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return load_ruleset(RULES)


def touch(root: Path, name: str, content: bytes = b"x") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# --------------------------------------------------------------------------
# merge：文件被占用
# --------------------------------------------------------------------------


def test_merge_skips_locked_file_and_continues(ruleset, tmp_path, monkeypatch):
    """一个文件搬不动，其余照搬，目录不被删。

    用 PARTIAL 组而不是完全一致的组：只有**源目录独有**的文件才会走
    ``shutil.move``，完全一致的组走的是「删副本」那条路。
    两个目录文件数相同，主目录才会落在「无重复标记」的那个上，
    源目录才持有那个独有文件。
    """
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "SDMM028C" / "shared.mp4").write_bytes(b"same")
    (tmp_path / "SDMM028C" / "only-a.mp4").write_bytes(b"aaa")
    (tmp_path / "SDMM028C 2").mkdir()
    (tmp_path / "SDMM028C 2" / "shared.mp4").write_bytes(b"same")
    (tmp_path / "SDMM028C 2" / "only-b.mp4").write_bytes(b"bbb")

    real_move = shutil.move

    def flaky_move(src, dst, *args, **kwargs):
        if str(src).endswith("only-b.mp4"):
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(merge_mod.shutil, "move", flaky_move)

    plan = merge_mod.build_merge_plan(
        tmp_path, ruleset, ["SDMM028C", "SDMM028C 2"]
    )
    assert plan.groups[0].verdict is merge_mod.MergeVerdict.PARTIAL
    assert plan.groups[0].primary == "SDMM028C"

    results = merge_mod.apply_merge_plan(tmp_path, plan, only_duplicates=False)

    # 结果里主目录是 KEEP、副本是 MERGE
    merge_results = [r for r in results if r["action"] == "merge"]
    assert len(merge_results) == 1
    result = merge_results[0]
    # 内容相同的直接删掉副本那份
    assert "shared.mp4" in result["moved"]
    # 搬不动的记为失败
    assert "only-b.mp4" in result["failed"]
    # 关键：副本目录因为还有残留而保留，主目录内容没丢
    assert (tmp_path / "SDMM028C 2" / "only-b.mp4").exists()
    assert (tmp_path / "SDMM028C" / "shared.mp4").exists()


def test_merge_never_loses_data_on_failure(ruleset, tmp_path, monkeypatch):
    """合并中途失败也不能把源文件弄丢。"""
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "SDMM028C" / "shared.mp4").write_bytes(b"same")
    (tmp_path / "SDMM028C" / "only-a.mp4").write_bytes(b"aaa")
    (tmp_path / "SDMM028C 2").mkdir()
    (tmp_path / "SDMM028C 2" / "shared.mp4").write_bytes(b"same")
    (tmp_path / "SDMM028C 2" / "only-b.mp4").write_bytes(b"bbb")

    def always_fail(src, dst, *args, **kwargs):
        raise PermissionError(32, "占用")

    monkeypatch.setattr(merge_mod.shutil, "move", always_fail)
    merge_mod.apply_merge_plan(
        tmp_path,
        merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM028C", "SDMM028C 2"]),
        only_duplicates=False,
    )

    # 搬不动的源文件必须还在
    assert (tmp_path / "SDMM028C 2" / "only-b.mp4").exists()
    assert (tmp_path / "SDMM028C" / "shared.mp4").exists()


# --------------------------------------------------------------------------
# archive：文件被占用
# --------------------------------------------------------------------------


def test_archive_skips_locked_file_and_continues(tmp_path, monkeypatch):
    """搬不动的文件记为失败，其余照搬。"""
    touch(tmp_path, "CLUB810.mp4")
    touch(tmp_path, "CLUB810.jpg")

    real_move = shutil.move

    def flaky_move(src, dst, *args, **kwargs):
        if str(src).endswith("CLUB810.mp4"):
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(archive_mod.shutil, "move", flaky_move)

    plan = archive_mod.build_archive_plan(tmp_path)
    result = archive_mod.apply_archive_plan(tmp_path, plan)

    assert result["moved"] == 1
    assert result["failed"] == ["CLUB810.mp4"]
    # 没搬动的还留在原地
    assert (tmp_path / "CLUB810.mp4").exists()
    assert (tmp_path / "CLUB-810" / "CLUB810.jpg").exists()


# --------------------------------------------------------------------------
# 非法文件名：exFAT 上 macOS 建出来、插到 Windows 上碰不了的名字
# --------------------------------------------------------------------------


def test_archive_reports_unsafe_names_without_crashing(tmp_path, monkeypatch):
    """非法名文件不能进分装计划，但要如实报告。"""
    touch(tmp_path, "CLUB810.mp4")
    unsafe = Path(str(tmp_path) + os.sep + "a:b.mp4")

    # 模拟 exFAT 上真实存在的条目：名字带冒号
    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        for entry in real_iterdir(self):
            yield entry
        if self == tmp_path:
            yield unsafe

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    plan = archive_mod.build_archive_plan(tmp_path)

    assert plan.unsafe, "非法名必须被报告"
    assert any("a:b.mp4" in item for item in plan.unsafe)
    # 正常文件照常分装
    assert len(plan.groups) == 1


def test_isolate_reports_unsafe_dirs_without_crashing(tmp_path, ruleset, monkeypatch):
    """非法名目录不参与隔离，但要被报告。"""
    (tmp_path / "SDMM-028C").mkdir()
    unsafe = Path(str(tmp_path) + os.sep + "bad:dir")

    real_iterdir = Path.iterdir

    def fake_iterdir(self):
        for entry in real_iterdir(self):
            yield entry
        if self == tmp_path:
            yield unsafe

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    target = tmp_path.parent / "gc-target"
    plan = isolate_mod.build_isolation_plan(tmp_path, target, ruleset)

    assert plan.unsafe, "非法名目录必须被报告"
    assert any("bad:dir" in item for item in plan.unsafe)
    # 正常番号目录照常判定
    assert any(i.name == "SDMM-028C" for i in plan.items)


def test_blocks_on_windows_only_blocks_on_windows():
    """macOS 上这些名字完全合法 —— 不能在 macOS 上误伤。"""
    assert fscompat.blocks_on_windows("a:b.mp4") == fscompat.on_windows()
    assert fscompat.blocks_on_windows("normal.mp4") is False
