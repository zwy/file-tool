"""rename-files 测试：文件名里的番号规范化。"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import rename_files as rf
from file_tool.engine import load_ruleset

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return load_ruleset(RULES)


def make(root: Path, relpath: str, content: str = "x") -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def plan_for(root: Path, ruleset):
    return rf.build_file_plan(root, ruleset)


def find(plan, relpath):
    for item in plan.items:
        if item.relpath == relpath:
            return item
    return None


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def test_watermark_domain_is_stripped(tmp_path, ruleset):
    """水印域名 + 缺横杠 -> 目录的规范名。"""
    make(tmp_path, "ACHJ-038/kfa11.com@ACHJ038.jpg")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "ACHJ-038/kfa11.com@ACHJ038.jpg")
    assert item is not None
    assert item.action is rf.FileAction.RENAME
    assert item.target == "ACHJ-038.jpg"


def test_dashless_name_is_normalized(tmp_path, ruleset):
    """缺横杠 -> 补上横杠。"""
    make(tmp_path, "BDSR-436/BDSR436.mp4")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "BDSR-436/BDSR436.mp4")
    assert item.action is rf.FileAction.RENAME
    assert item.target == "BDSR-436.mp4"


def test_studio_digit_prefix_is_dropped(tmp_path, ruleset):
    """厂牌数字前缀（230ORECO-640CX）-> 规范名。"""
    make(tmp_path, "ORECO-640CX/230ORECO-640CX.mp4")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "ORECO-640CX/230ORECO-640CX.mp4")
    assert item.action is rf.FileAction.RENAME
    assert item.target == "ORECO-640CX.mp4"


def test_already_normalized_is_kept(tmp_path, ruleset):
    make(tmp_path, "MAAN-961/MAAN-961.mp4")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "MAAN-961/MAAN-961.mp4")
    assert item.action is rf.FileAction.KEEP


def test_different_code_is_not_touched(tmp_path, ruleset):
    """文件名是别的番号 -> 判定不动，不进改名。"""
    make(tmp_path, "STSK-108/aavv38.xyz@STSK108A.mp4")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "STSK-108/aavv38.xyz@STSK108A.mp4")
    assert item.action is rf.FileAction.SKIP
    assert item.target == ""


def test_file_without_code_is_left_alone(tmp_path, ruleset):
    """文件名里没有番号 -> 不进计划，只计数。

    中文标题、``001.jpg`` 序列、原始导出名都属于这一类 —— 它们的名字
    本身就是内容标识，套番号会毁掉信息。
    """
    make(tmp_path, "ACHJ-038/原标题/001.jpg")
    make(tmp_path, "ACHJ-038/一见面就湿透.mp4")
    make(tmp_path, "ACHJ-038/VID_20240713_021421_921.mp4")
    plan = plan_for(tmp_path, ruleset)
    assert plan.items == []
    assert plan.no_code_files == 3


def test_non_code_directory_is_skipped(tmp_path, ruleset):
    """不是番号目录的顶层目录整个不碰。"""
    make(tmp_path, "国产合集/某个标题.mp4")
    plan = plan_for(tmp_path, ruleset)
    assert plan.items == []
    assert "国产合集" in plan.no_code_dirs
    assert plan.code_dirs == []


def test_domestic_prefix_directory_is_skipped(tmp_path, ruleset):
    """FLUP 是国产编号，不是番号 —— 整个目录连遍历都不做。"""
    make(tmp_path, "FLUP-1131428/446 -2729/2729.mp4")
    plan = plan_for(tmp_path, ruleset)
    assert plan.items == []
    assert plan.no_code_files == 0
    assert "FLUP-1131428" in plan.no_code_dirs
    assert plan.code_dirs == []


# --------------------------------------------------------------------------
# 递归与扩展名
# --------------------------------------------------------------------------


def test_nested_files_are_handled(tmp_path, ruleset):
    """番号目录往下的子目录里的文件也要处理。"""
    make(tmp_path, "ACHJ-038/原标题子目录/001.jpg")
    make(tmp_path, "ACHJ-038/132 -SSQW/SSQWQS/图片/x.jpg")
    plan = plan_for(tmp_path, ruleset)
    # 两个文件的文件名里都没有番号 -> 不进计划
    assert plan.items == []
    assert plan.no_code_files == 2


def test_nested_file_with_code_is_handled(tmp_path, ruleset):
    make(tmp_path, "ACHJ-038/子目录/kfa11.com@ACHJ038.mp4")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "ACHJ-038/子目录/kfa11.com@ACHJ038.mp4")
    assert item.action is rf.FileAction.RENAME
    assert item.target == "ACHJ-038.mp4"


@pytest.mark.parametrize("ext", [".srt", ".jpg", ".mp4", ".JPG", ".ts", ""])
def test_extension_is_preserved(tmp_path, ruleset, ext):
    """扩展名原样保留 —— .srt 等一律支持，不做白名单。"""
    make(tmp_path, f"ACHJ-038/ACHJ038{ext}")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, f"ACHJ-038/ACHJ038{ext}")
    assert item is not None
    assert item.action is rf.FileAction.RENAME
    assert item.target == f"ACHJ-038{ext}"


def test_resource_fork_is_skipped(tmp_path, ruleset):
    make(tmp_path, "ACHJ-038/._kfa11.com@ACHJ038.jpg")
    plan = plan_for(tmp_path, ruleset)
    assert plan.items == []
    assert plan.no_code_files == 0


# --------------------------------------------------------------------------
# 冲突
# --------------------------------------------------------------------------


def test_existing_target_is_conflict(tmp_path, ruleset):
    """目标已存在 -> 冲突，绝不覆盖。"""
    make(tmp_path, "CLUB-810/CLUB810.mp4")
    make(tmp_path, "CLUB-810/CLUB-810.mp4")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "CLUB-810/CLUB810.mp4")
    assert item.action is rf.FileAction.CONFLICT
    assert item.target == "CLUB-810.mp4"


def test_case_only_difference_is_not_a_conflict(tmp_path, ruleset):
    """仅大小写不同 -> 不是冲突，是纯大小写改名。

    实测 G 盘踩到的坑：目录名是 ``club-880``（小写），文件名是
    ``CLUB-880.mp4``（大写）。目录名规范化后目标名是 ``club-880.mp4``，
    而在 exFAT / APFS（默认）这种大小写不敏感的卷上，
    ``Path.exists()`` 判定「已存在」恒为真 —— 明明是同一个文件，
    却被判成冲突，文件名就永远规范化不了。

    正确判定靠文件身份（inode + 设备号），不是路径字符串。
    """
    make(tmp_path, "club-880/CLUB-880.mp4")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "club-880/CLUB-880.mp4")
    assert item is not None
    assert item.action is rf.FileAction.RENAME, "仅大小写不同不该判成冲突"
    assert item.target == "club-880.mp4"


def test_case_only_rename_actually_applies(tmp_path, ruleset):
    """纯大小写改名要真的落地（两步改名），不能「无事发生」。

    断言方式说明：Windows / exFAT / APFS 默认都是大小写不敏感的，
    ``Path.exists()`` 对 ``CLUB-880.mp4`` 和 ``club-880.mp4`` 都返回 True ——
    那是同一个文件。所以只能看**目录列表里实际存的名字**。
    """
    make(tmp_path, "club-880/CLUB-880.mp4", "payload")
    plan = plan_for(tmp_path, ruleset)
    result = rf.apply_file_plan(tmp_path, plan)
    assert len(result["renamed"]) == 1

    names = sorted(p.name for p in (tmp_path / "club-880").iterdir())
    assert names == ["club-880.mp4"], f"实际文件名 {names}"
    assert (tmp_path / "club-880" / "club-880.mp4").read_text() == "payload"


def test_real_conflict_with_different_case_is_still_conflict(tmp_path, ruleset):
    """真的是两个文件（哪怕只差大小写）-> 仍然是冲突，绝不覆盖。"""
    make(tmp_path, "CLUB-810/CLUB810.mp4", "new")
    make(tmp_path, "CLUB-810/club-810.mp4", "original")
    plan = plan_for(tmp_path, ruleset)
    item = find(plan, "CLUB-810/CLUB810.mp4")
    assert item.action is rf.FileAction.CONFLICT


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def test_apply_renames_file(tmp_path, ruleset):
    make(tmp_path, "ACHJ-038/kfa11.com@ACHJ038.jpg")
    plan = plan_for(tmp_path, ruleset)
    result = rf.apply_file_plan(tmp_path, plan)
    assert len(result["renamed"]) == 1
    assert (tmp_path / "ACHJ-038" / "ACHJ-038.jpg").exists()
    assert not (tmp_path / "ACHJ-038" / "kfa11.com@ACHJ038.jpg").exists()


def test_apply_never_overwrites(tmp_path, ruleset):
    """冲突时原文件保持不动，目标文件内容不被改写。"""
    make(tmp_path, "CLUB-810/CLUB810.mp4", "new")
    make(tmp_path, "CLUB-810/CLUB-810.mp4", "original")
    plan = plan_for(tmp_path, ruleset)
    result = rf.apply_file_plan(tmp_path, plan)
    assert len(result["conflicts"]) == 1
    assert (tmp_path / "CLUB-810" / "CLUB810.mp4").read_text() == "new"
    assert (tmp_path / "CLUB-810" / "CLUB-810.mp4").read_text() == "original"


def test_apply_leaves_untouched_files(tmp_path, ruleset):
    """没有番号的文件不会被碰。"""
    make(tmp_path, "ACHJ-038/一见面就湿透.mp4")
    make(tmp_path, "ACHJ-038/kfa11.com@ACHJ038.jpg")
    plan = plan_for(tmp_path, ruleset)
    rf.apply_file_plan(tmp_path, plan)
    assert (tmp_path / "ACHJ-038" / "一见面就湿透.mp4").exists()


def test_apply_is_idempotent(tmp_path, ruleset):
    """跑第二遍不应该再产生改动。"""
    make(tmp_path, "ACHJ-038/kfa11.com@ACHJ038.jpg")
    plan = plan_for(tmp_path, ruleset)
    rf.apply_file_plan(tmp_path, plan)
    plan2 = plan_for(tmp_path, ruleset)
    assert plan2.rename_items == []
    assert len(plan2.keep_items) == 1
