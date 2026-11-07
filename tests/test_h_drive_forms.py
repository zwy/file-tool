"""H 盘实测形态的回归测试。

H 盘（第三块 exFAT 外置盘，170 个子目录）暴露的问题跟 F/G 都不一样：

1. ``第一會所新片@SIS001@CLUB-936`` —— **中文系列名 + @ + 番号 + @ + 番号**。
   两个番号里后一个才是这部片子真正的番号（目录里的文件就叫 ``CLUB-936.mp4``）。
   旧规则集剥不掉前缀 -> 目录名解析不出番号。

2. ``SDMM-215.[4K]@R90s`` —— **番号 + 方括号画质标记 + @ + 站点标签**。
   旧规则集既认不出画质标记也认不出站点标签 -> 目录名解析不出番号。

3. 上面这两个目录因此**同时**栽在 isolate 的两道判定上：目录名解析不出番号，
   目录内的文件名 ``START-489V.mp4`` 又因为版本后缀字母 ``V`` 不被番号正则
   认出来 —— 两道判定同时失手，**真番号目录被当成国产内容挪进 GC**。

4. 顶层平铺着 ``hhd800.com@DVDMS-977.mp4`` 这种**带水印域名的媒体文件**。
   archive 拿整串去 fullmatch 番号形态，域名前缀让它匹配不上 ->
   文件永远分不进番号目录，带水印的名字也永远规范化不掉。

5. 顶层还有 7 个**字幕文件**（``.ja.srt`` / ``.en.srt`` / ``.zh.srt``）。
   字幕不在媒体扩展名里 -> 视频被分装进目录、字幕留在顶层，两者分离，
   而且带水印的字幕名永远规范化不掉。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool import archive as archive_mod
from file_tool import engine
from file_tool import isolate as isolate_mod
from file_tool import rename_files as rf
from file_tool.isolate import Isolation

RULES = Path(__file__).resolve().parent.parent / "rules" / "numbering.yaml"


@pytest.fixture(scope="module")
def ruleset():
    return engine.load_ruleset(RULES)


def plan_for(ruleset, name: str):
    return engine.build_plan(Path("/fake"), ruleset, [name]).items[0]


def resolve(ruleset, name: str):
    """返回 (核心串, 规范名, 命中规则名)。"""
    ordered = ruleset.ordered_matches()
    dup_sources = ruleset.dup_sources or ["dup"]
    core, captured = engine.strip_noise(name, ruleset.preprocess)
    base, target, rule, _desc, _core = engine.resolve_target(
        core, captured, ordered, ruleset.preprocess, dup_sources, ruleset.exclude
    )
    return core, target, rule


# --------------------------------------------------------------------------
# 1. 中文系列名 + 双番号
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "original,expected",
    [
        ("第一會所新片@SIS001@CLUB-936", "CLUB-936"),
        ("第一會所新片@SIS001@CLUB-937", "CLUB-937"),
        ("第一會所新片@SIS001@CLUB-938", "CLUB-938"),
        ("第一會所新片@SIS001@START-489V", "START-489V"),
        ("第一會所新片@SIS001@START-580", "START-580"),
        ("第一會所新片@SIS001@START-589-U", "START-589-U"),
    ],
)
def test_series_prefix_is_stripped(ruleset, original, expected):
    """系列名和站点的中间番号都要剥掉，只留最后一个番号。"""
    _core, target, rule = resolve(ruleset, original)
    assert target == expected, f"{original} -> {target}（期望 {expected}）"
    assert rule, "应该命中某条番号规则"


# --------------------------------------------------------------------------
# 2. 番号 + 画质标记 + 站点标签
# --------------------------------------------------------------------------

def test_quality_bracket_and_tag_is_stripped(ruleset):
    """``SDMM-215.[4K]@R90s`` 里的画质标记和站点标签都不该进规范名。"""
    _core, target, _rule = resolve(ruleset, "SDMM-215.[4K]@R90s")
    assert target == "SDMM-215"


def test_quality_bracket_keeps_version_letter(ruleset):
    """版本后缀字母要留着 —— ``START-489V`` 的 V 不是画质标记。"""
    _core, target, _rule = resolve(ruleset, "START-489V")
    assert target == "START-489V"


# --------------------------------------------------------------------------
# 3. 幂等性：规范名必须能被同一套规则反解回自己
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "第一會所新片@SIS001@CLUB-936",
        "第一會所新片@SIS001@START-489V",
        "第一會所新片@SIS001@START-589-U",
        "SDMM-215.[4K]@R90s",
    ],
)
def test_normalized_name_is_idempotent(ruleset, name):
    """规则输出的规范名，再跑一遍必须还是它自己。

    这是 rules/numbering.yaml 头部写明的硬要求 —— 不满足的话，
    第二次跑 tidy 就会把已经整理好的目录改坏。
    """
    _core, target, _rule = resolve(ruleset, name)
    assert target, f"{name} 应该解析出规范名"
    _core2, target2, _rule2 = resolve(ruleset, target)
    assert target2 == target, f"{name} -> {target} -> {target2}（第二次就变了）"


# --------------------------------------------------------------------------
# 4. 国产目录不能被这些新规则误当成番号
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "-P站极品反差少女小母狗【MomoYih】最新作品⑴，巨物假阳具粉穴潮吹极限调教",
        "0142_P站极品反差少女小母狗【MomoYih】最新作品⑴，巨物假阳具粉穴潮吹极限调教(3V)",
        "055-深圳校服清純甜美鄰家妹,胸型完,奶子Q彈,木耳粉嫩插入跳蛋扭屁股",
        "057-青春活力05後黃毛丫頭,外表清純,長相乖巧聽話,臉茓同框不斷揉搓粉嫩木耳",
        "080",
        "30 湖南体育生开苞高二小萝莉八块腹肌强悍身材抱起来野蛮输出各种各样的性爱姿势层出不穷小美女叫声一浪接一浪~1",
        "30 老牌顶级网红，推特微博小红书UP主，反差小姐姐蛇信子最新流出2部，线下课堂+淫荡女仆自慰被发现后的惩罚(2V)1",
    ],
)
def test_domestic_dirs_are_not_codes(ruleset, name):
    """国产内容的中文名不能被新规则解析成番号。"""
    _core, target, _rule = resolve(ruleset, name)
    assert not target, f"{name} 不该解析出番号，却得到 {target}"


# --------------------------------------------------------------------------
# 5. isolate 的番号识别要认版本后缀字母
# --------------------------------------------------------------------------

def test_code_in_name_accepts_version_suffix():
    """``START-489V`` 这种带版本后缀字母的番号要认得出来。

    旧正则要求数字后面紧跟非字母数字，``V`` 挡住匹配 ——
    「目录名解析不出番号」和「目录内文件名也认不出番号」两道判定
    同时失手，真番号目录就被当国产挪走了。
    """
    assert isolate_mod.CODE_IN_NAME.search("START-489V.mp4")
    assert isolate_mod.CODE_IN_NAME.search("START-589-U.mp4")
    assert isolate_mod.CODE_IN_NAME.search("CLUB-936.mp4")


def test_code_in_name_still_ignores_export_names():
    """相机导出名仍然不能被认成番号。

    ``_is_export_name`` 判定的是**匹配到的番号 token**（如 ``IMG_6481``），
    命中导出工具前缀表就把它排除掉 —— 国产合集因此不会因为
    ``IMG_6481.jpg`` 这类文件名被误判成「含番号」而留在番号库里。
    """
    for token in ("IMG_6481", "photo_2023", "dw9358"):
        assert isolate_mod._is_export_name(token), token
    # 真实厂牌不撞导出工具前缀
    for token in ("CLUB-936", "SDMM-215", "START-489V"):
        assert not isolate_mod._is_export_name(token), token


# --------------------------------------------------------------------------
# 6. archive 要认得出带水印域名的平铺文件
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stem,expected",
    [
        ("DVDMS-577", "DVDMS-577"),
        ("hhd800.com@DVDMS-977", "DVDMS-977"),
        ("hhd800.com@DVDMS-977.ja", "DVDMS-977"),
        ("hhd800.com@DVDMS-979.en", "DVDMS-979"),
        ("hhd800.com@SETM-046.ja", "SETM-046"),
        ("hhd800.com@SVBGR-001.ja", "SVBGR-001"),
        ("489155.com@CLUB-908", "CLUB-908"),
        ("CLUB830C", "CLUB-830"),
    ],
)
def test_archive_normalize_key_strips_watermark(stem, expected):
    """水印域名和语言后缀都不该挡住番号识别。"""
    assert archive_mod.normalize_key(stem) == expected


@pytest.mark.parametrize(
    "stem",
    ["陈园长淫史记", "[sxsy.org]娱乐：天才导演", "P站极品反差少女小母狗"],
)
def test_archive_normalize_key_rejects_non_codes(stem):
    """不是番号的名字不该被归档成分装目录。"""
    assert archive_mod.normalize_key(stem) is None


def test_subtitles_are_media_extensions():
    """字幕必须参与归档 —— 否则视频进了目录、字幕留在顶层，两者分离。"""
    for ext in (".srt", ".ass", ".ssa", ".sub", ".vtt"):
        assert ext in archive_mod.MEDIA_EXTENSIONS, ext


def test_subtitle_archives_with_its_video(tmp_path):
    """带水印的字幕要和它的视频分到同一个目录。"""
    (tmp_path / "hhd800.com@DVDMS-977.mp4").write_bytes(b"v")
    (tmp_path / "hhd800.com@DVDMS-977.ja.srt").write_bytes(b"s")

    plan = archive_mod.build_archive_plan(tmp_path)
    assert len(plan.groups) == 1
    assert plan.groups[0].name == "DVDMS-977"
    assert sorted(i.filename for i in plan.groups[0].items) == [
        "hhd800.com@DVDMS-977.ja.srt",
        "hhd800.com@DVDMS-977.mp4",
    ]


# --------------------------------------------------------------------------
# 8. 文件名里的分段标记（-2）不能被规则吞掉
# --------------------------------------------------------------------------

def test_part_marker_is_not_swallowed(ruleset):
    """``sdjs-361-2`` 的 ``-2`` 是分段标记，要留在文件名里。

    ``strip_hyphen_dup`` 规则会把尾部的 ``-2`` 当成「重复副本标记」剥掉，
    于是整串解析只剩 ``sdjs-361``。跟「逐段截断」解析出同一部片子时
    取截断结果 —— 被剥掉的标记就回到后缀里，不会消失。
    """
    base, noise, _rule = rf.split_code_suffix("sdjs-361-2", ruleset)
    assert base == "SDJS-361"
    assert noise == "-2"


def test_part_marker_is_kept_with_language_suffix(ruleset):
    """``sdjs-361-2.ja`` 的 ``-2`` 同样要留住。"""
    base, noise, _rule = rf.split_code_suffix("sdjs-361-2.ja", ruleset)
    assert base == "SDJS-361"
    assert noise == "-2.ja"


def test_dash_version_code_is_not_split(ruleset):
    """``START-589-U`` 整体是一个番号，不能切成 ``START-589`` 加尾巴。"""
    base, noise, _rule = rf.split_code_suffix("START-589-U.ja", ruleset)
    assert base == "START-589-U"
    assert noise == ".ja"


def test_dup_marker_is_not_doubled(ruleset, tmp_path):
    """目录名自带的重复标记不能再加到文件名上。

    目录 ``CLUB-880 (2)`` 里的文件 ``CLUB-880 (2).mp4`` —— 标记 ``(2)``
    目录名里已经有了，文件名再加一次就成 ``CLUB-880 (2) (2).mp4``。
    """
    from file_tool import rename_files as rf_mod

    (tmp_path / "CLUB-880 (2)").mkdir()
    (tmp_path / "CLUB-880 (2)" / "CLUB-880 (2).mp4").write_bytes(b"x")

    plan = rf_mod.build_file_plan(tmp_path, ruleset)
    assert plan.rename_items == []
    assert len(plan.keep_items) == 1


def test_variant_file_uses_its_own_code(ruleset, tmp_path):
    """目录里的别作作品按**它自己的**番号规范化。

    ``DVDMS-980/`` 里放的是 A/B 两个变体 —— 番号不能套目录的
    ``DVDMS-980``（会把变体信息抹掉），但水印域名是纯噪声，该剥掉。
    """
    from file_tool import rename_files as rf_mod

    (tmp_path / "DVDMS-980").mkdir()
    (tmp_path / "DVDMS-980" / "hhd800.com@DVDMS-980-A.mp4").write_bytes(b"x")

    plan = rf_mod.build_file_plan(tmp_path, ruleset)
    assert len(plan.rename_items) == 1
    assert plan.rename_items[0].target == "DVDMS-980-A.mp4"


def test_variant_file_conflict_is_not_overwritten(ruleset, tmp_path):
    """变体文件剥掉水印后撞上同名文件 -> 两份都留着，绝不覆盖。"""
    from file_tool import rename_files as rf_mod

    (tmp_path / "OFJE-697").mkdir()
    (tmp_path / "OFJE-697" / "hhd800.com@OFJE-697-A.mp4").write_bytes(b"x")
    (tmp_path / "OFJE-697" / "OFJE-697-A.mp4").write_bytes(b"y" * 10)

    plan = rf_mod.build_file_plan(tmp_path, ruleset)
    # 大的是已在位的那个，它占着规范名；小的加序号
    item = next(i for i in plan.items if i.original == "hhd800.com@OFJE-697-A.mp4")
    assert item.action is rf.FileAction.RENAME
    assert item.target == "OFJE-697-A (2).mp4"

    result = rf_mod.apply_file_plan(tmp_path, plan)
    assert not result["conflicts"]
    kept = {p.name: p.read_bytes() for p in (tmp_path / "OFJE-697").iterdir()}
    assert kept == {
        "OFJE-697-A.mp4": b"y" * 10,
        "OFJE-697-A (2).mp4": b"x",
    }


# --------------------------------------------------------------------------
# 7. 真番号目录不能被 isolate 误隔离
# --------------------------------------------------------------------------

def _judge(ruleset, tmp_path, name, files):
    for fname in files:
        path = tmp_path / name / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    item = isolate_mod.judge_directory(name, tmp_path, ruleset)
    return item


def test_real_code_dir_is_kept(ruleset, tmp_path):
    """带版本后缀的真番号目录必须留在番号库里。"""
    item = _judge(ruleset, tmp_path, "第一會所新片@SIS001@START-489V", ["START-489V.mp4"])
    assert item.isolation is Isolation.KEEP, item.reason


def test_quality_bracket_code_dir_is_kept(ruleset, tmp_path):
    """``SDMM-215.[4K]@R90s`` 里的文件是 ``SDMM-215_[4K].mkv``，也是真番号。"""
    item = _judge(
        ruleset,
        tmp_path,
        "SDMM-215.[4K]@R90s",
        ["169bbs.com@SDMM-215_[4K].mkv", "169bbs.com@SDMM-215_[4K].ja.srt"],
    )
    assert item.isolation is Isolation.KEEP, item.reason


@pytest.mark.parametrize(
    "name,files",
    [
        ("080", ["V (1).mp4", "V (2).mp4", "V (3).mp4"]),
        (
            "055-深圳校服清純甜美鄰家妹,胸型完,奶子Q彈,木耳粉嫩插入跳蛋扭屁股",
            ["1024社区-安卓发布器.apk", "深圳校服清純甜美鄰家妹.mp4"],
        ),
        (
            "⚫️P站极品反差少女小母狗【MomoYih】最新作品⑵，巨物道具极限调教嫩穴，下下到底猛插高潮喷水",
            ["mp4_ (1).mp4", "mp4_ (2).mp4"],
        ),
    ],
)
def test_domestic_dirs_are_still_isolated(ruleset, tmp_path, name, files):
    """国产目录照旧要被判为「不符合番号规范」，挪去 GC。"""
    item = _judge(ruleset, tmp_path, name, files)
    assert item.isolation is Isolation.MOVE, item.reason
