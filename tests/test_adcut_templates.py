"""模板库测试。

核心是三件事：**学得进、合得上、存不坏**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool.adcut import fingerprint, templates
from file_tool.adcut.models import TemplateBank, TemplateStatus


def _hashes(value: int, count: int = 12) -> list:
    """造一串可区分的指纹。"""
    return [value * 1000 + i for i in range(count)]


class TestLearn:
    def test_creates_template_on_first_learn(self):
        bank = TemplateBank()
        template, created = templates.learn(
            bank, _hashes(1), sample="/videos/a.mp4", label="片头广告",
        )
        assert created
        assert template.id == "tpl-0001"
        assert template.label == "片头广告"
        assert template.samples == 1
        assert template.status is TemplateStatus.CONFIRMED
        assert len(bank.templates) == 1

    def test_merges_same_ad(self):
        """同一支广告学第二次 —— 合并，samples 递增，不新建模板。"""
        bank = TemplateBank()
        templates.learn(bank, _hashes(1), sample="/videos/a.mp4")
        template, created = templates.learn(bank, _hashes(1), sample="/videos/b.mp4")
        assert not created
        assert len(bank.templates) == 1
        assert template.samples == 2

    def test_separates_different_ad(self):
        """完全不同的广告 —— 新建模板。"""
        bank = TemplateBank()
        templates.learn(bank, _hashes(1), sample="/videos/a.mp4")
        template, created = templates.learn(bank, _hashes(999), sample="/videos/b.mp4")
        assert created
        assert len(bank.templates) == 2
        assert template.id == "tpl-0002"

    def test_separates_different_length(self):
        """长度差太多 —— 不是同一支广告，不能合并。

        合并会把模板拉长，匹配时连续段提前断掉，裁剪点会落到广告中间。
        """
        bank = TemplateBank()
        templates.learn(bank, _hashes(1, count=12), sample="/videos/a.mp4")
        _, created = templates.learn(bank, _hashes(1, count=30), sample="/videos/b.mp4")
        assert created
        assert len(bank.templates) == 2

    def test_empty_hashes_rejected(self):
        bank = TemplateBank()
        with pytest.raises(templates.TemplateError):
            templates.learn(bank, [], sample="/videos/a.mp4")

    def test_learn_from_detection_starts_as_candidate(self):
        """机器自己认出来的第一回 —— 不能直接信，状态先是 candidate。"""
        bank = TemplateBank()
        template, created = templates.learn_from_detection(
            bank, _hashes(1), sample="/videos/a.mp4",
        )
        assert created
        assert template.status is TemplateStatus.CANDIDATE

    def test_learn_from_detection_promotes_after_second_hit(self):
        bank = TemplateBank()
        templates.learn_from_detection(bank, _hashes(1), sample="/videos/a.mp4")
        template, created = templates.learn_from_detection(
            bank, _hashes(1), sample="/videos/b.mp4",
        )
        assert not created
        assert template.samples == 2
        assert template.status is TemplateStatus.CONFIRMED


class TestNegative:
    def test_reject_records_negative(self):
        bank = TemplateBank()
        negative = templates.reject(
            bank, _hashes(5), sample="/videos/a.mp4", reason="这里是正片",
        )
        assert negative.id == "neg-0001"
        assert len(bank.negatives) == 1
        assert negative.reason == "这里是正片"

    def test_negative_suppresses_match(self):
        bank = TemplateBank()
        templates.reject(bank, _hashes(5), sample="/videos/a.mp4")
        found = templates.is_negative(bank, _hashes(5))
        assert found is not None

    def test_unrelated_is_not_negative(self):
        bank = TemplateBank()
        templates.reject(bank, _hashes(5), sample="/videos/a.mp4")
        assert templates.is_negative(bank, _hashes(777)) is None


class TestPersistence:
    def test_roundtrip(self, tmp_path: Path):
        bank = TemplateBank()
        templates.learn(bank, _hashes(1), sample="/videos/a.mp4", label="广告A")
        templates.reject(bank, _hashes(5), sample="/videos/b.mp4", reason="误报")

        path = tmp_path / "ad_templates.yaml"
        templates.save_bank(bank, path)
        loaded = templates.load_bank(path)

        assert len(loaded.templates) == 1
        assert loaded.templates[0].label == "广告A"
        assert loaded.templates[0].hashes == _hashes(1)
        assert len(loaded.negatives) == 1
        assert loaded.negatives[0].reason == "误报"

    def test_missing_file_is_empty_bank(self, tmp_path: Path):
        bank = templates.load_bank(tmp_path / "nope.yaml")
        assert bank.templates == []
        assert bank.negatives == []

    def test_broken_yaml_raises(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text("templates: [\n  - id: x\n", encoding="utf-8")
        with pytest.raises(templates.TemplateError):
            templates.load_bank(path)

    def test_save_keeps_file_readable(self, tmp_path: Path):
        """保存后的文件必须是合法 YAML —— 不能只写了一半。"""
        bank = TemplateBank()
        templates.learn(bank, _hashes(1), sample="/videos/a.mp4", label="广告A")
        path = tmp_path / "ad_templates.yaml"
        templates.save_bank(bank, path)
        text = path.read_text(encoding="utf-8")
        assert "tpl-0001" in text
        assert "广告A" in text

    def test_no_temp_files_left_behind(self, tmp_path: Path):
        """原子写：临时文件不能留在目录里。"""
        bank = TemplateBank()
        templates.learn(bank, _hashes(1), sample="/videos/a.mp4")
        path = tmp_path / "ad_templates.yaml"
        templates.save_bank(bank, path)
        leftovers = [item for item in tmp_path.iterdir() if item.name != path.name]
        assert leftovers == []


class TestMatching:
    def test_find_matching_template(self):
        bank = TemplateBank()
        templates.learn(bank, _hashes(1), sample="/videos/a.mp4")
        found, result = templates.find_matching_template(bank, _hashes(1))
        assert found is not None
        assert result.prefix_len == 12

    def test_next_id_increments(self):
        bank = TemplateBank()
        first, _ = templates.learn(bank, _hashes(1), sample="/a.mp4")
        bank.templates.append(bank.templates[0].model_copy(update={"id": "tpl-0009"}))
        assert bank.next_id() == "tpl-0010"
        assert first.id == "tpl-0001"

    def test_remove_template(self):
        bank = TemplateBank()
        template, _ = templates.learn(bank, _hashes(1), sample="/a.mp4")
        assert templates.remove_template(bank, template.id)
        assert not templates.remove_template(bank, template.id)
        assert bank.templates == []


class TestReplace:
    """改边界用 ``replace``，不用 ``learn``。

    实测踩过的坑：模板长度划错了（把一段 200 秒的推广片记成 137 秒），
    走 ``learn`` 的话长度差超过 2 帧就**新建**一条模板 ——
    库里会同时躺着「错的旧模板」和「对的新模板」，两条都能匹配上，
    谁赢看分数，结果就是随机切。覆盖才是对的。
    """

    def test_replaces_fingerprints_and_keeps_id(self):
        bank = TemplateBank()
        template, _ = templates.learn(bank, _hashes(1, count=12), sample="/a.mp4")
        replaced = templates.replace_template(
            bank, template.id, _hashes(1, count=200), sample="/b.mp4",
        )
        assert replaced.id == template.id
        assert len(replaced.hashes) == 200
        assert bank.get(template.id) is replaced
        assert len(bank.templates) == 1

    def test_replacement_keeps_history(self):
        """覆盖之后 id、计数、状态都保留 —— 它还是同一支广告。"""
        bank = TemplateBank()
        template, _ = templates.learn(bank, _hashes(1, count=12), sample="/a.mp4")
        templates.replace_template(bank, template.id, _hashes(1, count=200))
        assert bank.templates[0].samples == 2
        assert bank.templates[0].status is TemplateStatus.CONFIRMED
        assert bank.templates[0].first_seen

    def test_replacement_can_relabel(self):
        bank = TemplateBank()
        template, _ = templates.learn(bank, _hashes(1, count=12), sample="/a.mp4")
        templates.replace_template(
            bank, template.id, _hashes(1, count=200), label="新标签", note="改过边界",
        )
        assert bank.templates[0].label == "新标签"
        assert bank.templates[0].note == "改过边界"

    def test_replace_unknown_id_fails(self):
        bank = TemplateBank()
        with pytest.raises(templates.TemplateError):
            templates.replace_template(bank, "tpl-9999", _hashes(1))

    def test_replace_empty_fingerprints_fails(self):
        bank = TemplateBank()
        template, _ = templates.learn(bank, _hashes(1), sample="/a.mp4")
        with pytest.raises(templates.TemplateError):
            templates.replace_template(bank, template.id, [])
