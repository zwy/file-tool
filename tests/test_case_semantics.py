"""冲突判定必须与「卷的大小写语义」无关。

同一个库最终要放到 exFAT 上（Windows / macOS 默认都是大小写不敏感），
但工具本身也跑在 Linux ext4、macOS 的**大小写敏感**分区上 —— 那里
``a.mp4`` 与 ``A.mp4`` 是两个不同的文件。

如果「目标名是否被占用」交给文件系统回答（``Path.exists()``），
同一个库在两种卷上的整理结果就会分叉：

- 规则保留厂牌的原始大小写：``club810`` -> ``club-810``、
  ``CLUB810`` -> ``CLUB-810``，两个目标只差大小写 —— 在 exFAT 上是
  **同一个名字**
- 在大小写敏感的卷上 ``exists()`` 说「没被占用」，于是改名成功
- 等库拷到 exFAT，两个只差大小写的文件就撞了 —— exFAT 根本放不下

所以占用判定一律自己算，且**永远忽略大小写**：
见 :func:`file_tool.fscompat.colliding_entries`。

下面的测试都不依赖当前卷的大小写语义：要么用纯函数
（``engine.build_plan`` 不碰文件系统），要么把「只差大小写」的两个文件
放在**不同目录**里 —— 这样大小写敏感的卷也建得出来。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from file_tool import archive as archive_mod
from file_tool import engine
from file_tool import fscompat
from file_tool import isolate as isolate_mod
from file_tool import merge as merge_mod
from file_tool import rename_files as rf
from file_tool.engine import load_ruleset
from file_tool.models import Action

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return load_ruleset(RULES)


# --------------------------------------------------------------------------
# fscompat：忽略大小写的占用判定
# --------------------------------------------------------------------------


def test_colliding_entries_ignores_case(tmp_path):
    """大小写不同也算撞名 —— 问的是「名字」，不是「路径」。"""
    (tmp_path / "CLUB-810.mp4").write_bytes(b"x")

    hits = fscompat.colliding_entries(tmp_path, "club-810.mp4")

    assert [p.name for p in hits] == ["CLUB-810.mp4"]


def test_colliding_entries_empty_when_name_is_free(tmp_path):
    (tmp_path / "CLUB-810.mp4").write_bytes(b"x")

    assert fscompat.colliding_entries(tmp_path, "other.mp4") == []


def test_colliding_entries_on_missing_dir_is_empty(tmp_path):
    """目录不存在时不能抛异常 —— 返回「没被占用」。"""
    assert fscompat.colliding_entries(tmp_path / "nope", "a.mp4") == []


def test_same_name_key_is_case_insensitive():
    assert fscompat.same_name_key("CLUB-810") == fscompat.same_name_key("club-810")
    assert fscompat.same_name_key("a.mp4") != fscompat.same_name_key("b.mp4")


# --------------------------------------------------------------------------
# engine.build_plan：纯函数，冲突判定不受卷的语义影响
# --------------------------------------------------------------------------


def test_build_plan_treats_case_variant_target_as_conflict(tmp_path, ruleset):
    """``club810`` 的目标名是 ``club-810``；已存在 ``CLUB-810`` -> 冲突。

    两者在 exFAT 上是同一个名字。旧实现按字符串比，在大小写敏感的卷上
    会判成「不冲突」，整理完再拷到 exFAT 就撞名。
    """
    plan = engine.build_plan(
        tmp_path, ruleset, names=["club810"], existing=["CLUB-810"]
    )

    assert plan.items[0].action is Action.CONFLICT
    assert plan.items[0].target == "club-810"


def test_build_plan_batch_duplicate_ignores_case(tmp_path, ruleset):
    """同一批次里两个只差大小写的目标名 -> 第二个判冲突。

    ``club810`` -> ``club-810``、``CLUB810`` -> ``CLUB-810``。
    """
    plan = engine.build_plan(
        tmp_path,
        ruleset,
        names=["club810", "CLUB810"],
        existing=["club810", "CLUB810"],
    )

    actions = {i.original: i.action for i in plan.items}
    assert actions["club810"] is Action.RENAME
    assert actions["CLUB810"] is Action.CONFLICT


# --------------------------------------------------------------------------
# archive：目标目录名与现有文件撞名（忽略大小写）
# --------------------------------------------------------------------------


def test_archive_group_blocked_by_case_variant_file(tmp_path):
    """顶层有个叫 ``club-810`` 的文件 -> ``CLUB-810`` 这一组不能建目录。

    建出来的话，exFAT 上「目录 CLUB-810」和「文件 club-810」是同一个名字。
    """
    (tmp_path / "CLUB810.mp4").write_bytes(b"x")
    (tmp_path / "club-810").write_bytes(b"x")

    plan = archive_mod.build_archive_plan(tmp_path)

    assert plan.blocked == ["CLUB-810"]
    assert plan.groups == []


# --------------------------------------------------------------------------
# merge：并入时只差大小写的同名文件不能被覆盖
# --------------------------------------------------------------------------


def test_merge_keeps_case_variant_file(tmp_path, ruleset):
    """主目录有 ``A.mp4``，副本有 ``a.mp4`` 且内容不同 -> 副本那份必须留下。

    主目录和副本是**两个目录**，所以大小写敏感的卷上也建得出来这两个文件
    —— 测试在两种卷上语义一致。

    旧实现在大小写敏感的卷上会认为 ``a.mp4`` 没被占用，直接搬进去，
    主目录里就多出两个只差大小写的文件 —— exFAT 上直接撞名。
    """
    primary = tmp_path / "SDMM028C"
    duplicate = tmp_path / "SDMM028C (1)"
    primary.mkdir()
    duplicate.mkdir()
    (primary / "A.mp4").write_bytes(b"primary")
    (duplicate / "a.mp4").write_bytes(b"copy")

    plan = merge_mod.build_merge_plan(
        tmp_path, ruleset, ["SDMM028C", "SDMM028C (1)"]
    )
    results = merge_mod.apply_merge_plan(tmp_path, plan, only_duplicates=False)

    merged = [r for r in results if r["action"] == "merge"]
    assert len(merged) == 1
    assert merged[0]["skipped"] == ["a.mp4"]
    # 主目录里没被塞进一个只差大小写的副本，原文件也没被动。
    # 不能用 exists() 判断 —— 大小写不敏感的卷上 primary/"a.mp4"
    # 会解析到 A.mp4，恒为真。要看目录里**实际存的名字**。
    assert sorted(p.name for p in primary.iterdir()) == ["A.mp4"]
    assert (primary / "A.mp4").read_bytes() == b"primary"
    assert (duplicate / "a.mp4").read_bytes() == b"copy"


# --------------------------------------------------------------------------
# isolate：目标目录里已有只差大小写的同名目录
# --------------------------------------------------------------------------


def test_isolate_conflict_ignores_case(tmp_path, ruleset):
    """GC 里已经有 ``MISC``，源目录的 ``misc`` 挪过去就会撞名。"""
    (tmp_path / "misc").mkdir()
    (tmp_path / "misc" / "x.txt").write_text("x")
    target = tmp_path.parent / f"{tmp_path.name}-gc"
    target.mkdir()
    (target / "MISC").mkdir()

    plan = isolate_mod.build_isolation_plan(tmp_path, target, ruleset)

    assert plan.conflicts == ["misc"]


# --------------------------------------------------------------------------
# rename_files：目标名只差大小写 -> 冲突，不是「可以改名」
# --------------------------------------------------------------------------


def test_rename_files_case_variant_target_is_conflict(tmp_path, ruleset):
    """目录里已有 ``club-810.mp4``，``CLUB810.mp4`` 的目标名撞名 -> 冲突。"""
    (tmp_path / "CLUB-810").mkdir()
    (tmp_path / "CLUB-810" / "CLUB810.mp4").write_bytes(b"new")
    (tmp_path / "CLUB-810" / "club-810.mp4").write_bytes(b"original")

    plan = rf.build_file_plan(tmp_path, ruleset)

    item = next(i for i in plan.items if i.original == "CLUB810.mp4")
    assert item.action is rf.FileAction.CONFLICT


# --------------------------------------------------------------------------
# 路径表示：Python 3.12 的 relative_to 会吃掉带冒号的末段
# --------------------------------------------------------------------------


def test_relpath_keeps_colon_in_last_segment(tmp_path):
    """3.12 起 ``relative_to`` 把 ``a:b.mp4`` 当成驱动器重解析，中间层级丢失。

    病根在 ``PurePath.with_segments()`` —— 它按位置重新解析每一段，
    ``a:b.mp4`` 被读成「驱动器 a: + 文件 b.mp4」，于是 ``ok/a:b.mp4``
    被截成 ``a:b.mp4``。
    """
    fake = Path(str(tmp_path / "ok") + os.sep + "a:b.mp4")

    assert fscompat.to_posix_relpath(fake, tmp_path) == "ok/a:b.mp4"


def test_relpath_keeps_colon_in_middle_segment(tmp_path):
    """中间层带冒号同样不能被吃掉。"""
    fake = Path(str(tmp_path / "ok") + os.sep + "a:b" + os.sep + "x.mp4")

    assert fscompat.to_posix_relpath(fake, tmp_path) == "ok/a:b/x.mp4"
