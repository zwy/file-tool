"""双平台一致性测试。

这个项目同时支持 macOS 和 Windows，两边的**文件系统规则不一样**。
本文件守住两件事：

1. **平台无关的核心逻辑在两边行为一致**（规则解析、判重、改名语义）
2. **平台差异只体现在一个闸门上** —— :func:`file_tool.fscompat.blocks_on_windows`。
   它是唯一决定「Windows 上跳过、macOS 上照常处理」的地方，
   一旦它判断错，macOS 就会平白损失一批能正常处理的文件。

Windows 上建不出 ``a:b.mp4`` 这种名字（NTFS 根本不允许），所以
真实验证「macOS 上会怎么处理它」只能在 POSIX 上做 —— 那部分测试
在 Windows 上跳过，交给 CI 的 macOS runner 跑。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from file_tool import archive as archive_mod
from file_tool import engine
from file_tool import fscompat
from file_tool import isolate as isolate_mod
from file_tool import rename_files as rf
from file_tool.engine import load_ruleset

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="Windows/NTFS 建不出非法文件名，只有 POSIX 能真实验证",
)


@pytest.fixture(scope="module")
def ruleset():
    return load_ruleset(RULES)


@pytest.fixture
def pretend_macos(monkeypatch):
    """把平台闸门伪装成 macOS。"""
    monkeypatch.setattr(fscompat, "on_windows", lambda: False)
    monkeypatch.setattr(archive_mod.fscompat, "on_windows", lambda: False)
    monkeypatch.setattr(isolate_mod.fscompat, "on_windows", lambda: False)
    monkeypatch.setattr(rf.fscompat, "on_windows", lambda: False)


# --------------------------------------------------------------------------
# 平台闸门
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["a:b.mp4", "CON", "CON.mp4", "NUL", "COM1", "x?y.mp4", "x*y.mp4", "trailing.", "trailing "],
)
def test_blocks_only_on_windows(name, pretend_macos):
    """macOS 上这些名字全都合法 —— 一个都不能拦。"""
    assert fscompat.is_windows_unsafe_name(name) is not None
    assert fscompat.blocks_on_windows(name) is False


@pytest.mark.parametrize(
    "name",
    ["a:b.mp4", "CON", "CON.mp4", "NUL", "COM1", "x?y.mp4", "trailing.", "trailing "],
)
def test_blocks_on_real_windows(name):
    """真 Windows 上这些名字必须拦。"""
    expected = os.name == "nt"
    assert fscompat.blocks_on_windows(name) is expected


def test_normal_names_never_blocked():
    """正常文件名在任何平台都不拦。"""
    for name in ["CLUB810.mp4", "SDMM-028C", "一见面就湿透.mp4", "srt", "01.jpg"]:
        assert fscompat.blocks_on_windows(name) is False


# --------------------------------------------------------------------------
# 相对路径表示：POSIX 上 str() 和 as_posix() 必须完全一致
# --------------------------------------------------------------------------


def test_relpath_is_identical_on_posix(tmp_path):
    """POSIX 上 ``str(rel)`` 和 ``rel.as_posix()`` 本来就一样。

    改成 as_posix 之后，POSIX 上的输出**一个字节都没变** ——
    这是「不破坏 macOS」的硬证据。
    """
    nested = tmp_path / "ACHJ-038" / "子目录"
    nested.mkdir(parents=True)
    target = nested / "x.mp4"
    target.write_text("x")

    rel = target.relative_to(tmp_path)
    if os.name != "nt":
        assert str(rel) == rel.as_posix()
    assert fscompat.to_posix_relpath(target, tmp_path) == rel.as_posix()


def test_merge_fingerprint_keys_are_posix(tmp_path):
    """merge 的指纹字典键统一用 ``/``。"""
    d = tmp_path / "SDMM028C"
    (d / "子目录").mkdir(parents=True)
    (d / "子目录" / "a.mp4").write_bytes(b"x")

    from file_tool import merge as merge_mod

    entries = merge_mod.fingerprint_dir(d)
    assert list(entries) == ["子目录/a.mp4"]


# --------------------------------------------------------------------------
# macOS 上真实验证：带非法名的文件要被照常处理
# --------------------------------------------------------------------------


@POSIX_ONLY
def test_posix_real_illegal_filename_is_still_processed(tmp_path, ruleset):
    """macOS 上 ``a:b.mp4`` 是合法文件 —— 必须照常参与分装。

    只有 POSIX 能真建出这种名字，所以这是 macOS 行为的**真实**验证，
    由 CI 的 macOS runner 执行。
    """
    illegal = tmp_path / "a:b.mp4"
    illegal.write_bytes(b"x")
    (tmp_path / "CLUB810.mp4").write_bytes(b"x")

    plan = archive_mod.build_archive_plan(tmp_path)

    # 报告出来了（提醒用户插到 Windows 上会出问题）
    assert any("a:b.mp4" in item for item in plan.unsafe)
    # 但没有把它排除掉 —— 正常文件照常分装
    assert len(plan.groups) == 1
    assert plan.groups[0].name == "CLUB-810"


@POSIX_ONLY
def test_posix_illegal_dir_is_still_isolated(tmp_path, ruleset):
    """macOS 上带冒号的目录照样能挪走。"""
    illegal = tmp_path / "bad:dir"
    illegal.mkdir()
    (illegal / "x.txt").write_text("x")

    target = tmp_path.parent / "gc-target"
    plan = isolate_mod.build_isolation_plan(tmp_path, target, ruleset)

    # 报告了
    assert any("bad:dir" in item for item in plan.unsafe)
    # 但仍然参与判定、会被挪走
    assert any(i.name == "bad:dir" for i in plan.move_items)


@POSIX_ONLY
def test_posix_illegal_filename_is_still_renamed(tmp_path, ruleset):
    """macOS 上 ``kfa11.com@ACHJ038.jpg␠``（结尾带空格）要照常改名。

    结尾空格在 Windows 上会被静默剥掉，所以这种文件在 Windows 上根本
    建不出来 —— 只有 POSIX 能真实验证「macOS 不会误伤它」。
    """
    d = tmp_path / "ACHJ-038"
    d.mkdir()
    illegal = d / "kfa11.com@ACHJ038.jpg "
    illegal.write_bytes(b"x")

    assert fscompat.is_windows_unsafe_name(illegal.name) is not None

    plan = rf.build_file_plan(tmp_path, ruleset)

    # 不能被当成「Windows 上访问不了」—— macOS 上它完全正常
    assert plan.inaccessible == []
    # 照常进入改名计划
    assert len(plan.rename_items) == 1
    assert plan.rename_items[0].target == "ACHJ-038.jpg "


# --------------------------------------------------------------------------
# 平台无关的核心语义
# --------------------------------------------------------------------------


def test_rename_semantics_are_platform_agnostic(tmp_path, ruleset):
    """改名规则和判定逻辑与平台无关。"""
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "aavv38.xyz@MIDE004").mkdir()
    names = sorted(p.name for p in tmp_path.iterdir())
    plan = engine.build_plan(tmp_path, ruleset, names)

    targets = {i.original: i.target for i in plan.items}
    assert targets["SDMM028C"] == "SDMM-028C"
    assert targets["aavv38.xyz@MIDE004"] == "MIDE-004"


def test_merge_verdict_is_content_based(tmp_path, ruleset):
    """判重只看内容，与平台无关。"""
    from file_tool import merge as merge_mod
    from file_tool.merge import MergeVerdict

    for name in ("SDMM030AC", "SDMM030AC(1)"):
        d = tmp_path / name
        d.mkdir()
        (d / "a.mp4").write_bytes(b"same")

    plan = merge_mod.build_merge_plan(tmp_path, ruleset, ["SDMM030AC", "SDMM030AC(1)"])
    assert plan.groups[0].verdict is MergeVerdict.DUPLICATE


def test_case_only_rename_works_on_both_platforms(tmp_path):
    """仅大小写不同的改名：两边都要走两步，不能变成 ``X (2)``。

    APFS（默认）和 NTFS 都是大小写不敏感的 —— 这条在两个平台都成立。
    """
    from file_tool.models import Action, Plan, PlanItem

    src = tmp_path / "club810"
    src.mkdir()
    plan = Plan(
        root=str(tmp_path),
        items=[PlanItem(original="club810", target="CLUB810", action=Action.RENAME)],
    )
    done = engine.apply_plan(tmp_path, plan)

    assert not done.failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["CLUB810"]


def test_python_version_supported():
    """两个平台都要求 Python 3.11+。"""
    assert sys.version_info >= (3, 11)
