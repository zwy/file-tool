"""跨平台兼容层测试：Windows 文件名规则、大小写改名、路径表示。

这些行为在 macOS 上测不出来 —— 恰恰是 Windows 上最容易翻车的地方，
所以每一条都在这里用「平台无关的纯逻辑」固定下来。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from file_tool import engine
from file_tool import fscompat
from file_tool.models import Action, Plan, PlanItem

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


# --------------------------------------------------------------------------
# Windows 文件名合法性
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "CON", "con", "CON.txt", "PRN", "AUX", "NUL", "nul.mp4",
        "COM1", "COM9", "LPT1", "LPT9", "CONIN$", "CONOUT$",
    ],
)
def test_reserved_device_names_are_unsafe(name):
    """保留设备名：CON.txt 和 CON 一样不能用。"""
    reason = fscompat.is_windows_unsafe_name(name)
    assert reason is not None
    assert "保留设备名" in reason


@pytest.mark.parametrize(
    "name",
    ['a:b.txt', 'a<b.txt', 'a>b.txt', 'a"b.txt', 'a\\b.txt', 'a|b.txt', 'a?b.txt', 'a*b.txt'],
)
def test_illegal_characters_are_unsafe(name):
    """Windows 不允许的字符。"""
    reason = fscompat.is_windows_unsafe_name(name)
    assert reason is not None
    assert "非法字符" in reason


@pytest.mark.parametrize("name", ["trailing.", "trailing ", "a.mp4.", "a.mp4 "])
def test_trailing_dot_or_space_is_unsafe(name):
    """结尾的点和空格会被 Windows 静默剥掉 —— 改名后会「找不到文件」。"""
    reason = fscompat.is_windows_unsafe_name(name)
    assert reason is not None
    assert "结尾" in reason


@pytest.mark.parametrize(
    "name",
    [
        "SDMM-028C.mp4", "ACHJ-038.jpg", "一见面就湿透.mp4",
        "club810.mp4", "a.mp4", "x.srt", "CONCERT.mp4", "AUXILIARY.mp4",
    ],
)
def test_normal_names_are_safe(name):
    """正常文件名不受影响 —— 尤其 CONCERT 这类不能误判成保留名。"""
    assert fscompat.is_windows_unsafe_name(name) is None


def test_control_characters_are_unsafe():
    reason = fscompat.is_windows_unsafe_name("bad\x01name.mp4")
    assert reason is not None
    assert "非法字符" in reason


def test_empty_and_relative_names_are_unsafe():
    assert fscompat.is_windows_unsafe_name("") is not None
    assert fscompat.is_windows_unsafe_name(".") is not None
    assert fscompat.is_windows_unsafe_name("..") is not None


def test_overlong_name_is_unsafe():
    reason = fscompat.is_windows_unsafe_name("x" * 256)
    assert reason is not None
    assert "255" in reason


# --------------------------------------------------------------------------
# 路径表示
# --------------------------------------------------------------------------


def test_relpath_uses_posix_separator(tmp_path):
    """相对路径统一用 ``/`` —— JSON 输出跨平台一致。"""
    nested = tmp_path / "ACHJ-038" / "子目录"
    nested.mkdir(parents=True)
    target = nested / "x.mp4"
    target.write_text("x")

    rel = fscompat.to_posix_relpath(target, tmp_path)
    assert rel == "ACHJ-038/子目录/x.mp4"
    assert "\\" not in rel


def test_relpath_outside_root_falls_back(tmp_path):
    """不在 root 下时退回绝对路径，不抛异常。"""
    other = tmp_path.parent / "elsewhere.txt"
    assert fscompat.to_posix_relpath(other, tmp_path).endswith("elsewhere.txt")


def _raw_child(parent: Path, name: str) -> Path:
    """造一个「原始名里带冒号」的路径。

    不能用 ``parent / name`` —— pathlib 会把 ``a:`` 当成驱动器，
    路径整个被改写掉，原始名就丢了。这里要模拟的是**磁盘上真实的
    条目名**（macOS 在 exFAT 上建出来的），所以得绕过 pathlib 的解析。
    """
    return Path(str(parent) + os.sep + name)


def test_find_unsafe_names_scans_recursively(tmp_path, monkeypatch):
    """递归扫出非法名。

    Windows 上根本建不出 ``a:b.mp4``，所以这里用假的目录项模拟 ——
    真实场景是 macOS 在 exFAT 上建好后插过来。
    """
    fake = _raw_child(tmp_path / "ok", "a:b.mp4")
    monkeypatch.setattr(
        Path, "rglob",
        lambda self, pattern: iter([fake]),
    )

    found = fscompat.find_unsafe_names(tmp_path)
    assert len(found) == 1
    rel, reason = found[0]
    assert rel == "ok/a:b.mp4"
    assert "非法字符" in reason


def test_find_unsafe_names_non_recursive(tmp_path, monkeypatch):
    fake = _raw_child(tmp_path, "c:d.mp4")
    monkeypatch.setattr(
        Path, "iterdir",
        lambda self: iter([fake]),
    )

    found = fscompat.find_unsafe_names(tmp_path, recursive=False)
    assert [rel for rel, _ in found] == ["c:d.mp4"]


def test_raw_name_survives_pathlib_parsing(tmp_path):
    """守住这个前提：``rglob`` 拿到的 ``.name`` 必须还是原始名。

    pathlib 在 Windows 上把 ``a:b.mp4`` 解析成「驱动器 a: + 文件 b.mp4」，
    ``.name`` 就变成 ``b.mp4``，冒号没了 —— 非法名检测会静默失效。
    ``rglob`` 用 ``_make_child_relpath`` 拼路径，不走重新解析，名字能保住。
    """
    raw = _raw_child(tmp_path, "a:b.mp4")
    child = tmp_path._make_child_relpath("a:b.mp4")
    assert child.name == "a:b.mp4"
    # 对照组：用 / 拼会被改写
    assert (tmp_path / "a:b.mp4").name != "a:b.mp4"
    assert raw.name == "a:b.mp4"


# --------------------------------------------------------------------------
# 大小写改名
# --------------------------------------------------------------------------


def test_rename_case_only_changes_case(tmp_path):
    """两步改名：只改大小写。"""
    src = tmp_path / "a.mp4"
    src.write_text("x")
    dst = tmp_path / "A.mp4"

    fscompat.rename_case_only(src, dst)

    assert dst.exists()
    assert dst.read_text() == "x"
    assert src.name not in [p.name for p in tmp_path.iterdir()]


def test_rename_case_only_works_for_dirs(tmp_path):
    src = tmp_path / "abc"
    src.mkdir()
    dst = tmp_path / "ABC"

    fscompat.rename_case_only(src, dst)

    assert dst.is_dir()
    # 卷是大小写不敏感的，不能用 exists() 判断 —— 看实际目录内容
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ABC"]


def test_rename_case_only_avoids_existing_temp_name(tmp_path):
    """临时名撞上了已有文件时要换一个，不能把人家的文件顶掉。"""
    (tmp_path / "__file_tool_tmp__.mp4").write_text("keep me")
    src = tmp_path / "a.mp4"
    src.write_text("x")
    dst = tmp_path / "A.mp4"

    fscompat.rename_case_only(src, dst)

    assert dst.read_text() == "x"
    assert (tmp_path / "__file_tool_tmp__.mp4").read_text() == "keep me"


def test_is_same_file_detects_case_only(tmp_path):
    """仅大小写不同的两个路径指向同一个文件。"""
    src = tmp_path / "a.mp4"
    src.write_text("x")
    dst = tmp_path / "A.mp4"

    assert fscompat.is_same_file(src, dst) is True

    other = tmp_path / "b.mp4"
    other.write_text("y")
    assert fscompat.is_same_file(src, other) is False


# --------------------------------------------------------------------------
# engine.apply_plan 的大小写处理
# --------------------------------------------------------------------------


def test_apply_plan_case_only_rename_does_not_add_counter(tmp_path):
    """仅大小写不同的改名必须真的改，不能变成 ``X (2)``。

    这在 Windows 和 macOS APFS（默认）上都会踩：卷是大小写不敏感的，
    目标名「已存在」，但它就是源文件本身。

    规则模板只做原样插值，产不出纯大小写变化的目标名，所以这里直接
    构造计划项 —— 守住的是**执行层**的语义，不是规则层。
    """
    src = tmp_path / "club810"
    src.mkdir()
    plan = Plan(
        root=str(tmp_path),
        items=[
            PlanItem(
                original="club810",
                target="CLUB810",
                action=Action.RENAME,
            )
        ],
    )

    done = engine.apply_plan(tmp_path, plan)

    assert not done.failed
    assert (tmp_path / "CLUB810").is_dir()
    # 关键：不能出现加了序号的文件
    assert not (tmp_path / "CLUB810 (2)").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["CLUB810"]


def test_apply_plan_reports_failure_instead_of_crashing(tmp_path, monkeypatch):
    """改名失败要记成失败，不能把整个批次带崩。"""
    ruleset = engine.load_ruleset(RULES)
    (tmp_path / "SDMM028C").mkdir()
    (tmp_path / "aavv38.xyz@MIDE004").mkdir()
    names = sorted(p.name for p in tmp_path.iterdir())
    plan = engine.build_plan(tmp_path, ruleset, names)
    assert len(plan.rename_items) == 2

    real_rename = Path.rename

    def flaky_rename(self, target, **kwargs):
        if self.name == "SDMM028C":
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_rename(self, target, **kwargs)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    done = engine.apply_plan(tmp_path, plan)

    assert len(done.renamed) == 1
    assert len(done.failed) == 1
    assert done.failed[0][0] == "SDMM028C"
    # 失败的那个目录原地不动
    assert (tmp_path / "SDMM028C").is_dir()
