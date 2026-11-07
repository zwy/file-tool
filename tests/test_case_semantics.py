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
    """``club810`` 的目标名是 ``CLUB-810``；已存在 ``CLUB-810`` -> 冲突。

    两者在 exFAT 上是同一个名字。旧实现按字符串比，在大小写敏感的卷上
    会判成「不冲突」，整理完再拷到 exFAT 就撞名。
    """
    plan = engine.build_plan(
        tmp_path, ruleset, names=["club810"], existing=["CLUB-810"]
    )

    assert plan.items[0].action is Action.CONFLICT
    assert plan.items[0].target == "CLUB-810"


def test_build_plan_treats_source_itself_as_not_a_conflict(tmp_path, ruleset):
    """目标名就是源自己、只差大小写 -> 不是冲突，是纯大小写改名。

    统一大写之后，每个小写厂牌的目录都会走到这一步：``avop-235-C``
    的规范名是 ``AVOP-235-C``。这个名字在磁盘上被**源自己**占着 ——
    它不是「被别人占了」，所以不能判成冲突，否则大写化永远推不动。

    真正的撞名是「旁边还站着另一个条目」，那种情况由
    :func:`test_build_plan_treats_case_variant_target_as_conflict` 守着。

    执行层的两步改名（先挪临时名再改目标名）在
    ``tests/test_fscompat.py::test_apply_plan_case_only_rename_does_not_add_counter``
    里有覆盖。
    """
    plan = engine.build_plan(tmp_path, ruleset, names=["avop-235-C"])

    assert plan.items[0].action is Action.RENAME
    assert plan.items[0].target == "AVOP-235-C"


def test_build_plan_rejects_case_variant_when_another_entry_exists(tmp_path, ruleset):
    """源自己和另一个条目占着同一个名字 -> 真冲突。

    大小写敏感的卷上 ``avop-235-C`` 与 ``AVOP-235-C`` 能同时存在；
    整理到 exFAT 上它们会撞在一起。源自己占位不算冲突，
    但「旁边还有另一个」就算。
    """
    plan = engine.build_plan(
        tmp_path, ruleset, names=["avop-235-C"], existing=["avop-235-C", "AVOP-235-C"]
    )

    assert plan.items[0].action is Action.CONFLICT


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
    # 副本那份没被覆盖 —— 它改名进了主目录（``A (2).mp4``）
    assert (tmp_path / "SDMM028C" / "A.mp4").read_bytes() == b"primary"
    assert (tmp_path / "SDMM028C" / "A (2).mp4").read_bytes() == b"copy"
    # 主目录里没被塞进一个只差大小写的副本（``a.mp4``），原文件也没被动。
    # 不能用 exists() 判断 —— 大小写不敏感的卷上 primary/"a.mp4"
    # 会解析到 A.mp4，恒为真。要看目录里**实际存的名字**。
    assert sorted(p.name for p in primary.iterdir()) == ["A (2).mp4", "A.mp4"]


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
# rename_files：目标名只差大小写 -> 两份都留着，不覆盖
# --------------------------------------------------------------------------


def test_rename_files_case_variant_target_keeps_both(tmp_path, ruleset):
    """目录里已有 ``club-810.mp4``，``CLUB810.mp4`` 的目标名撞名。

    撞名不是「冲突放弃」—— 按体积分配：大的占规范名，小的加序号。
    关键仍然是**绝不覆盖**。
    """
    (tmp_path / "CLUB-810").mkdir()
    (tmp_path / "CLUB-810" / "CLUB810.mp4").write_bytes(b"new")
    (tmp_path / "CLUB-810" / "club-810.mp4").write_bytes(b"original payload")

    plan = rf.build_file_plan(tmp_path, ruleset)

    item = next(i for i in plan.items if i.original == "CLUB810.mp4")
    assert item.action is rf.FileAction.RENAME
    assert item.target == "CLUB-810 (2).mp4"


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


# --------------------------------------------------------------------------
# 相对路径：不在 root 底下时必须退回绝对路径
# --------------------------------------------------------------------------


def test_relpath_outside_root_falls_back_to_absolute(tmp_path):
    """路径不在 root 底下时，必须给绝对路径。

    按 ``parts`` 逐段比对有个坑：POSIX 上绝对路径的第一段永远是 ``/``，
    所以「第一段就不一样 -> 退回绝对路径」这个判断**永远不触发**，
    结果会返回一个看着像相对路径、其实是错的东西
    （``/other/a.mp4`` 被算成 ``other/a.mp4``）。

    旧实现（``relative_to`` 抛 ``ValueError`` 时）给的是绝对路径 ——
    宁可给个绝对路径，也不能给一个会骗人的相对路径。
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other" / "a.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"x")

    rel = fscompat.to_posix_relpath(outside, root)
    assert rel == outside.as_posix()


def test_relpath_when_root_is_a_subpath_falls_back_to_absolute(tmp_path):
    """root 是源目录的子路径时同样退回绝对路径。"""
    outside = tmp_path / "a" / "b.mp4"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")
    root = tmp_path / "a" / "b" / "c"

    rel = fscompat.to_posix_relpath(outside, root)
    assert rel == outside.as_posix()


# --------------------------------------------------------------------------
# 占用索引：必须是活的快照，不能是死的
# --------------------------------------------------------------------------


def test_apply_plan_index_tracks_freed_names(tmp_path):
    """改名腾出来的名字，后面要用的话必须还能用。

    占用判定走索引（建一次、按键查），但索引是**快照** ——
    改名之后必须同步更新，否则会被误判成冲突、加上 ``(2)`` 后缀。
    """
    from file_tool.models import Plan, PlanItem

    (tmp_path / "a").mkdir()
    (tmp_path / "c").mkdir()

    plan = Plan(
        root=str(tmp_path),
        items=[
            PlanItem(original="a", target="b", action=Action.RENAME),
            PlanItem(original="c", target="a", action=Action.RENAME),
        ],
    )
    done = engine.apply_plan(tmp_path, plan)

    assert not done.failed
    # "a" 先被腾空，再被 "c" 占用 —— 没有多出 "a (2)"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b"]


def test_apply_plan_checks_disk_before_overwriting(tmp_path, monkeypatch):
    """索引建好之后目标名被别人占了 —— 必须以磁盘为准，不能覆盖。

    索引是快照，看不见「建好之后才出现」的文件。
    这一条是索引优化必须带的安全网：宁可多判一次冲突，
    也不能把别人的文件盖掉。
    """
    from file_tool.models import Plan, PlanItem

    (tmp_path / "a").write_text("mine")

    real_build = fscompat.build_name_index

    def build_then_sneak(dir_path):
        index = real_build(dir_path)
        (dir_path / "b").write_text("theirs")   # 索引建好之后才出现
        return index

    monkeypatch.setattr(fscompat, "build_name_index", build_then_sneak)

    plan = Plan(
        root=str(tmp_path),
        items=[PlanItem(original="a", target="b", action=Action.RENAME)],
    )
    done = engine.apply_plan(tmp_path, plan)

    # 别人的文件没被覆盖，我们的改名让到了序号上
    assert (tmp_path / "b").read_text() == "theirs"
    assert (tmp_path / "b (2)").read_text() == "mine"
    assert not done.failed
