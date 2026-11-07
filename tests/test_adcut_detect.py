"""判定层与裁剪层测试。

这里需要真的调 ffmpeg，所以造的是**合成视频**（lavfi 生成的色条/测试图），
不是真实素材 —— 合成视频是确定性的，两次生成出来的帧完全一样，
正好用来验证「同一支广告在不同视频里能不能认出来」。

没有 ffmpeg 的环境直接跳过，不让 CI 因为缺个外部工具就红。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from file_tool.adcut import cut as cut_mod
from file_tool.adcut import detect, ffmpeg, templates
from file_tool.adcut.models import TemplateBank, Verdict

HAS_FFMPEG = ffmpeg.find_executable("ffmpeg") is not None and \
    ffmpeg.find_executable("ffprobe") is not None

pytestmark = pytest.mark.skipif(
    not HAS_FFMPEG, reason="需要 ffmpeg / ffprobe"
)


def make_video(path: Path, ad_seconds: int = 5, content_seconds: int = 10) -> Path:
    """造一段视频：前 ad_seconds 是「广告」，后面是「正片」。

    两段用不同的画面源（测试图 vs 彩条），这样 dHash 一定分得开。
    同样的命令跑两次，出来的帧完全一致。
    """
    cmd = [
        ffmpeg.require_executable("ffmpeg"), "-v", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=320x240:rate=10:duration={ad_seconds}",
        "-f", "lavfi", "-i",
        f"smptebars=size=320x240:rate=10:duration={content_seconds}",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    return path


@pytest.fixture()
def ad_bank(tmp_path: Path) -> TemplateBank:
    """一个模板库里有一条学好的广告模板。"""
    bank = TemplateBank()
    video = make_video(tmp_path / "sample.mp4")
    signature = detect.fingerprint.segment_signature_of(video, 0.0, 5.0)
    templates.learn(bank, signature.hashes, sample=str(video), label="合成广告")
    return bank


class TestFindVideos:
    def test_skips_resource_forks(self, tmp_path: Path):
        """exFAT 上的 ``._xxx.mp4`` 是资源叉垃圾，不是视频。"""
        (tmp_path / "._real.mp4").write_bytes(b"x" * 4096)
        (tmp_path / "real.mp4").write_bytes(b"x" * 10)
        found = detect.find_videos(tmp_path)
        assert [item.name for item in found] == ["real.mp4"]

    def test_skips_temp_files(self, tmp_path: Path):
        """裁剪过程留下的 ``.adcut-tmp.mp4`` 不能被当成待处理视频。"""
        (tmp_path / "a.adcut-tmp.mp4").write_bytes(b"x")
        (tmp_path / "b.mp4").write_bytes(b"x")
        found = detect.find_videos(tmp_path)
        assert [item.name for item in found] == ["b.mp4"]

    def test_ignores_non_video(self, tmp_path: Path):
        (tmp_path / "cover.jpg").write_bytes(b"x")
        (tmp_path / "movie.mp4").write_bytes(b"x")
        found = detect.find_videos(tmp_path)
        assert [item.name for item in found] == ["movie.mp4"]


class TestDetect:
    def test_no_template_means_no_ad(self, tmp_path: Path):
        video = make_video(tmp_path / "a.mp4")
        detection = detect.detect_video(video, TemplateBank())
        assert detection.verdict is Verdict.NONE

    def test_learned_ad_is_found(self, tmp_path: Path, ad_bank: TemplateBank):
        """学过的广告，在**另一个**视频里也要认出来。"""
        other = make_video(tmp_path / "other.mp4")
        detection = detect.detect_video(other, ad_bank)
        assert detection.verdict is Verdict.CUT
        assert detection.cut_at == pytest.approx(5.0, abs=1.5)
        assert detection.template_label == "合成广告"

    def test_broken_file_is_skipped(self, tmp_path: Path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"")
        detection = detect.detect_video(broken, TemplateBank())
        assert detection.verdict is Verdict.SKIP

    def test_negative_suppresses_detection(self, tmp_path: Path, ad_bank: TemplateBank):
        """人工否定过的片头不该再被判成广告。"""
        other = make_video(tmp_path / "other.mp4")
        signature = detect.fingerprint.segment_signature_of(other, 0.0, 5.0)
        templates.reject(
            ad_bank, list(signature.hashes), sample=str(other), reason="这里没有广告",
        )
        detection = detect.detect_video(other, ad_bank)
        assert detection.verdict is Verdict.NONE


class TestPlanCutPoint:
    def test_prefers_keyframe_after_cut(self):
        """切割点之后有关键帧 —— 用它，广告切得干净。"""
        real, mode, snap = cut_mod.plan_cut_point(10.0, [0.0, 8.0, 11.0, 20.0])
        assert real == 11.0
        assert mode == "copy"
        assert snap == pytest.approx(1.0)

    def test_falls_back_to_keyframe_before(self):
        """之后的关键帧太远 —— 用之前的那个，会留一截广告尾巴。"""
        real, mode, snap = cut_mod.plan_cut_point(10.0, [0.0, 9.5, 30.0])
        assert real == 9.5
        assert mode == "copy"
        assert snap == pytest.approx(-0.5)

    def test_reencode_when_too_far(self):
        """两边都远 —— 重编码，切在精确位置。"""
        real, mode, snap = cut_mod.plan_cut_point(10.0, [0.0, 40.0])
        assert real == 10.0
        assert mode == "reencode"
        assert snap == 0.0

    def test_zero_cut_is_rejected(self):
        with pytest.raises(cut_mod.CutError):
            cut_mod.plan_cut_point(0.0, [0.0, 5.0])


class TestCut:
    def test_cut_removes_head(self, tmp_path: Path):
        video = make_video(tmp_path / "a.mp4")
        before = ffmpeg.probe(video).duration
        outcome = cut_mod.cut_video(video, cut_at=5.0)
        assert outcome.ok, outcome.reason
        after = ffmpeg.probe(video).duration
        assert after < before
        assert before - after == pytest.approx(5.0, abs=3.0)

    def test_cut_is_verified_and_video_still_playable(self, tmp_path: Path):
        video = make_video(tmp_path / "a.mp4")
        outcome = cut_mod.cut_video(video, cut_at=5.0)
        assert outcome.ok
        info = ffmpeg.probe(video)
        assert info.has_video
        assert info.duration > 0

    def test_refuses_impossible_cut(self, tmp_path: Path):
        """裁剪点超过片长 —— 拒绝，别把文件搞坏。"""
        video = make_video(tmp_path / "a.mp4")
        before = video.stat().st_size
        outcome = cut_mod.cut_video(video, cut_at=9999.0)
        assert not outcome.ok
        assert video.stat().st_size == before

    def test_no_temp_file_left_behind(self, tmp_path: Path):
        video = make_video(tmp_path / "a.mp4")
        cut_mod.cut_video(video, cut_at=5.0)
        leftovers = [item.name for item in tmp_path.iterdir()
                     if cut_mod.TEMP_MARK in item.name]
        assert leftovers == []

    def test_missing_file_is_reported(self, tmp_path: Path):
        outcome = cut_mod.cut_video(tmp_path / "nope.mp4", cut_at=5.0)
        assert not outcome.ok
        assert "不存在" in outcome.reason
