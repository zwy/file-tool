"""人工核验边界表的测试。

边界表解决的是模板库表达不了的东西：**同一支广告在不同片子里长度不一样**。
所以这里测三件事：读得进、查得到、**覆盖生效**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from file_tool.adcut import boundaries
from file_tool.adcut.models import AdBoundary, AdTemplate, BoundaryBook, TemplateBank, TemplateStatus, Verdict


class TestLoad:
    def test_missing_file_is_empty(self, tmp_path: Path):
        """表是可选的 —— 不存在就当没有，不能因此报错。"""
        book = boundaries.load_boundaries(tmp_path / "nope.yaml")
        assert book.boundaries == []

    def test_reads_entries(self, tmp_path: Path):
        path = tmp_path / "ad_boundaries.yaml"
        path.write_text(
            "version: 1\n"
            "boundaries:\n"
            "- path: DIR/a.mp4\n"
            "  cut_at: 319\n"
            "  note: 站方推广片到 318 秒结束\n",
            encoding="utf-8",
        )
        book = boundaries.load_boundaries(path)
        assert len(book.boundaries) == 1
        item = book.boundaries[0]
        assert item.path == "DIR/a.mp4"
        assert item.cut_at == 319
        assert "318" in item.note

    def test_rejects_non_positive_cut(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            "boundaries:\n- path: a.mp4\n  cut_at: 0\n", encoding="utf-8"
        )
        with pytest.raises(boundaries.BoundaryError):
            boundaries.load_boundaries(path)

    def test_rejects_garbage(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text("boundaries: 42\n", encoding="utf-8")
        with pytest.raises(boundaries.BoundaryError):
            boundaries.load_boundaries(path)


class TestLookup:
    def test_map_and_lookup(self):
        book = BoundaryBook(boundaries=[
            AdBoundary(path="DIR/a.mp4", cut_at=319),
            AdBoundary(path="DIR/b.mp4", cut_at=222),
        ])
        assert boundaries.as_map(book) == {"DIR/a.mp4": 319, "DIR/b.mp4": 222}
        assert boundaries.lookup(book, "DIR/b.mp4").cut_at == 222
        assert boundaries.lookup(book, "DIR/missing.mp4") is None
        assert boundaries.lookup(None, "DIR/a.mp4") is None


class TestShippedTable:
    """仓库里那张表本身要自洽。"""

    def test_shipped_table_loads(self):
        path = Path(__file__).resolve().parents[1] / "rules" / "ad_boundaries.yaml"
        book = boundaries.load_boundaries(path)
        assert book.boundaries, "边界表不该是空的"
        assert book.version == 1

    def test_paths_are_relative_and_unique(self):
        path = Path(__file__).resolve().parents[1] / "rules" / "ad_boundaries.yaml"
        book = boundaries.load_boundaries(path)
        seen = set()
        for item in book.boundaries:
            assert not item.path.startswith("/"), f"路径不该是绝对路径：{item.path}"
            assert "\\" not in item.path, f"路径该用 POSIX 分隔符：{item.path}"
            assert item.path not in seen, f"路径重复：{item.path}"
            seen.add(item.path)

    def test_cut_points_are_sane(self):
        """切点得落在合理区间 —— 太短多半是标错了。"""
        path = Path(__file__).resolve().parents[1] / "rules" / "ad_boundaries.yaml"
        book = boundaries.load_boundaries(path)
        for item in book.boundaries:
            assert item.cut_at >= 10, f"切点短得离谱：{item.path} {item.cut_at}"
            assert item.cut_at <= 360, f"切点长过片头了：{item.path} {item.cut_at}"


class TestDetectOverride:
    """边界表要真的**盖掉**模板长度的推断值。"""

    def _fake_video(self, tmp_path: Path, seconds: int = 40) -> Path:
        """造一段每帧都一样的视频（指纹恒定）。"""
        path = tmp_path / "clip.mp4"
        import subprocess
        import shutil
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("没有 ffmpeg")
        subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
             f"color=c=gray:s=64x64:d={seconds}", "-pix_fmt", "yuv420p", str(path)],
            check=True, capture_output=True,
        )
        return path

    def test_manual_boundary_wins(self, tmp_path: Path):
        from file_tool.adcut import detect

        path = self._fake_video(tmp_path)
        bank = TemplateBank(templates=[AdTemplate(
            id="tpl-0001", label="测试广告",
            hashes=[0] * 30, brightness=[128.0] * 30, fps=1.0,
            sample="x.mp4", status=TemplateStatus.CONFIRMED,
        )])
        manual = {"clip.mp4": AdBoundary(path="clip.mp4", cut_at=25, note="人工核验")}
        result = detect.detect_video(
            path, bank, root=tmp_path, head_seconds=40.0, boundaries=manual,
        )
        # 模板长度是 30（→ 切 30 秒），人工边界是 25 —— 人工的赢
        assert result.cut_at == 25
        assert result.verdict is Verdict.CUT
        assert "人工核验" in result.reason

    def test_without_manual_uses_template(self, tmp_path: Path):
        from file_tool.adcut import detect

        path = self._fake_video(tmp_path)
        bank = TemplateBank(templates=[AdTemplate(
            id="tpl-0001", label="测试广告",
            hashes=[0] * 30, brightness=[128.0] * 30, fps=1.0,
            sample="x.mp4", status=TemplateStatus.CONFIRMED,
        )])
        result = detect.detect_video(path, bank, root=tmp_path, head_seconds=40.0)
        assert result.cut_at == 30

    def test_manual_boundary_survives_unmatched_template(self, tmp_path: Path):
        """模板没匹配上，但人眼量过边界 —— 照样该裁。"""
        from file_tool.adcut import detect

        path = self._fake_video(tmp_path)
        bank = TemplateBank(templates=[AdTemplate(
            id="tpl-0001", label="不相关的广告",
            hashes=[999] * 30, brightness=[10.0] * 30, fps=1.0,
            sample="x.mp4", status=TemplateStatus.CONFIRMED,
        )])
        manual = {"clip.mp4": AdBoundary(path="clip.mp4", cut_at=18, note="人工核验")}
        result = detect.detect_video(
            path, bank, root=tmp_path, head_seconds=40.0, boundaries=manual,
        )
        assert result.verdict is Verdict.CUT
        assert result.cut_at == 18


class TestSuffixMatch:
    """扫子目录时相对路径少了几级，要能按后缀找到边界。"""

    def test_unique_suffix_hits(self):
        from file_tool.adcut import detect

        manual = {"DIR/SUB/a.mp4": AdBoundary(path="DIR/SUB/a.mp4", cut_at=319)}
        assert detect._manual_boundary(manual, "a.mp4").cut_at == 319
        assert detect._manual_boundary(manual, "DIR/SUB/a.mp4").cut_at == 319

    def test_ambiguous_suffix_is_ignored(self):
        """同名文件不止一份时宁可不匹配 —— 猜错切点比不裁更糟。"""
        from file_tool.adcut import detect

        manual = {
            "DIR1/a.mp4": AdBoundary(path="DIR1/a.mp4", cut_at=319),
            "DIR2/a.mp4": AdBoundary(path="DIR2/a.mp4", cut_at=222),
        }
        assert detect._manual_boundary(manual, "a.mp4") is None

    def test_no_boundaries(self):
        from file_tool.adcut import detect

        assert detect._manual_boundary(None, "a.mp4") is None
        assert detect._manual_boundary({}, "a.mp4") is None
