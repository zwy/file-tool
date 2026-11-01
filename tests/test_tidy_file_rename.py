"""tidy 编排的补充测试：文件名规范化步骤 + 大小写比较 + 名单一致性。

这三件事都是实际踩出来的：

1. ``tidy`` 原本只改目录名，文件名里的番号要另外跑 ``rename-files`` ——
   「一个命令搞定」的说法就站不住。
2. 文件名与目录名**仅大小写不同**时（``GVH-067/gvh00067.mp4``），
   按大小写敏感比较会被误判成「别的番号」而跳过。
3. 执行阶段的改名用的是**全部条目名**，规划阶段只用目录名 ——
   顶层散文件可能被当成目录一起改名。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import engine
from file_tool import rename_files as rf
from file_tool import tidy as tidy_mod
from file_tool.tidy import StepKind

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return engine.load_ruleset(RULES)


def touch(root: Path, name: str, content: bytes = b"x") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# --------------------------------------------------------------------------
# 文件名规范化进了 tidy
# --------------------------------------------------------------------------


def test_tidy_includes_rename_files_step(ruleset, tmp_path):
    """tidy 的计划里要有「改文件名」这一步。"""
    touch(tmp_path, "SDMM028C/kfa11.com@SDMM028C.mp4")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    kinds = [s.kind for s in plan.steps]

    assert StepKind.RENAME_FILES in kinds
    # 必须在目录改名之后 —— 文件名的目标来自所在目录的规范名
    assert kinds.index(StepKind.RENAME) < kinds.index(StepKind.RENAME_FILES)


def test_tidy_apply_renames_files(ruleset, tmp_path):
    """一次 tidy 就把目录名和文件名都规范掉。"""
    touch(tmp_path, "SDMM028C/kfa11.com@SDMM028C.mp4")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    result = tidy_mod.apply_tidy_plan(tmp_path, ruleset, plan)

    assert result["renamed"] == 1
    assert result["renamed_files"] == 1
    assert (tmp_path / "SDMM-028C" / "SDMM-028C.mp4").exists()
    # 卷大小写不敏感，不能用 exists() 判断旧名还在不在 —— 看目录内容
    assert sorted(p.name for p in tmp_path.iterdir()) == ["SDMM-028C"]


def test_tidy_can_skip_file_rename(ruleset, tmp_path):
    """``--no-file-rename`` 时只改目录名。"""
    touch(tmp_path, "SDMM028C/kfa11.com@SDMM028C.mp4")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset, rename_file_names=False)
    assert StepKind.RENAME_FILES not in [s.kind for s in plan.steps]

    result = tidy_mod.apply_tidy_plan(tmp_path, ruleset, plan)
    assert result["renamed"] == 1
    assert result["renamed_files"] == 0
    assert (tmp_path / "SDMM-028C" / "kfa11.com@SDMM028C.mp4").exists()


def test_tidy_rename_step_only_touches_directories(ruleset, tmp_path):
    """顶层散文件不能被当成目录一起改名。

    规划阶段用「只含目录」的名单，执行阶段也必须用同一份 ——
    两边不一致的话，顶层恰好叫番号的散文件会被误改。

    用 ``.srt`` 而不是 ``.mp4``：媒体文件会被 archive 步骤分装进新目录，
    根本走不到 rename 步骤，测不到这个不一致。
    """
    touch(tmp_path, "SDMM028C/a.mp4")
    # 顶层散文件，名字恰好像番号，但不是媒体扩展名 -> archive 不碰它
    touch(tmp_path, "MIDE004.srt")

    plan = tidy_mod.build_tidy_plan(tmp_path, ruleset)
    result = tidy_mod.apply_tidy_plan(tmp_path, ruleset, plan)

    assert result["renamed"] == 1
    # 散文件原地不动，没被当成目录改名
    assert (tmp_path / "MIDE004.srt").exists()
    assert not (tmp_path / "MIDE-004.srt").exists()


# --------------------------------------------------------------------------
# 大小写比较
# --------------------------------------------------------------------------


def test_file_with_different_case_is_not_skipped(ruleset, tmp_path):
    """仅大小写 / 前导零不同的文件名要跟着目录一起规范化。

    实测 ``GVH-067/gvh00067.mp4``：目录名规范化成大写 ``GVH-067``，
    文件名却是小写 ``gvh00067``。按字面比较会被判成「别的番号」。
    """
    (tmp_path / "GVH-067").mkdir()
    (tmp_path / "GVH-067" / "gvh00067.mp4").write_bytes(b"x")

    plan = rf.build_file_plan(tmp_path, ruleset)

    assert len(plan.rename_items) == 1
    assert plan.rename_items[0].target == "GVH-067.mp4"
    assert plan.skip_items == []


def test_canonical_code_ignores_case_and_leading_zeros():
    """「同一作品」的比较键：忽略大小写和数字段的前导零。"""
    assert rf.canonical_code("GVH-067") == rf.canonical_code("gvh-00067")
    assert rf.canonical_code("SDMM-028C") == rf.canonical_code("sdmm-028c")
    # 但厂牌或版本不同就不是同一部
    assert rf.canonical_code("ABC-123") != rf.canonical_code("XYZ-123")
    assert rf.canonical_code("SDMM-028") != rf.canonical_code("SDMM-028C")


# --------------------------------------------------------------------------
# 番号后面带噪声后缀
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem,expected_base,expected_noise",
    [
        ("326FCT-043_en", "FCT-043", "_en"),          # 语言标记
        ("259LUXU-1812.mp4", "LUXU-1812", ".mp4"),     # 二次扩展名
        ("MKMP595-2f60f24e4602", "MKMP-595", "-2f60f24e4602"),  # 去重哈希
        ("STARS-542~nyap2p.com", "STARS-542", "~nyap2p.com"),   # 站点名
        ("332SYS-018-8b81fc1cd3cb", "SYS-018", "-8b81fc1cd3cb"),
        ("SDMM076C_ja", "SDMM-076C", "_ja"),
    ],
)
def test_split_code_suffix(ruleset, stem, expected_base, expected_noise):
    """番号后面带噪声后缀时，要能把番号切出来。"""
    base, noise, _rule = rf.split_code_suffix(stem, ruleset)
    assert base == expected_base
    assert noise == expected_noise


@pytest.mark.parametrize(
    "stem",
    ["001", "VID_20240713_021421_921", "一见面就湿透", "readme", "2024"],
)
def test_split_code_suffix_rejects_non_codes(ruleset, stem):
    """不是番号的名字不能被切出番号 —— 否则会误改中文标题。"""
    base, _noise, _rule = rf.split_code_suffix(stem, ruleset)
    assert base is None


def test_file_with_noise_suffix_is_renamed(ruleset, tmp_path):
    """带语言后缀的字幕要跟着目录规范化，且后缀保留。"""
    (tmp_path / "FCT-043").mkdir()
    (tmp_path / "FCT-043" / "aavv36.xyz@326FCT-043_en.srt").write_bytes(b"x")

    plan = rf.build_file_plan(tmp_path, ruleset)

    assert len(plan.rename_items) == 1
    assert plan.rename_items[0].target == "FCT-043_en.srt"


def test_genuinely_different_code_is_still_skipped(ruleset, tmp_path):
    """真的是别的番号，仍然不动 —— 不能放宽过头。"""
    (tmp_path / "STSK-108").mkdir()
    (tmp_path / "STSK-108" / "aavv38.xyz@STSK108A.mp4").write_bytes(b"x")

    plan = rf.build_file_plan(tmp_path, ruleset)

    assert plan.rename_items == []
    assert len(plan.skip_items) == 1
