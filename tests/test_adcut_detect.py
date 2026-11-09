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
from file_tool.adcut.models import TemplateBank, TemplateStatus, Verdict

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


def make_video_with_prelude(
    path: Path, prelude_seconds: int = 2, ad_seconds: int = 5, content_seconds: int = 10
) -> Path:
    """造一段「前头还挂着别的东西」的视频。

    用来复现实测里「广告不是从第 0 帧开始」的情况 ——
    有些发行版前面还有站点 logo 或黑场。裁剪点必须把这几秒算进去，
    否则会切早，把广告尾巴留在片头。
    """
    cmd = [
        ffmpeg.require_executable("ffmpeg"), "-v", "error", "-y",
        "-f", "lavfi", "-i",
        f"rgbtestsrc=size=320x240:rate=10:duration={prelude_seconds}",
        "-f", "lavfi", "-i",
        f"testsrc2=size=320x240:rate=10:duration={ad_seconds}",
        "-f", "lavfi", "-i",
        f"smptebars=size=320x240:rate=10:duration={content_seconds}",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    return path


def make_sparse_keyframe_video(path: Path, seconds: int = 20) -> Path:
    """造一段关键帧稀疏的视频（每 5 秒一个）。

    用来逼出「裁剪点附近没有关键帧」的情况 —— 那时必须走混合裁剪，
    而不是整部重编码。
    """
    cmd = [
        ffmpeg.require_executable("ffmpeg"), "-v", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=320x240:rate=10:duration={seconds}",
        "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    return path


def corrupt_one_packet(path: Path, skip_first: int = 0) -> float:
    """把某个视频包的长度前缀写成 0 —— 人为制造一个损坏的访问单元。

    mp4 里的 H.264 是长度前缀格式：每个访问单元前面 4 个字节是它自己的
    长度。写成 0 之后，h264 解析器读到就会报 ``Invalid NAL unit size``，
    ``concat`` 解复用器读到那里会直接停止 —— 正是素材库里那几支坏包的成因。

    返回被破坏那个包的时间点（秒）。
    """
    import json

    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,pos,size", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=120,
    )
    packets = json.loads(proc.stdout)["packets"]
    victim = packets[skip_first + 1]
    with open(path, "r+b") as fh:
        fh.seek(int(victim["pos"]))
        fh.write(b"\x00\x00\x00\x00")
    return float(victim["pts_time"])


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

    def test_cut_point_accounts_for_ad_offset(self, tmp_path: Path, ad_bank: TemplateBank):
        """广告前面还挂着东西时，裁剪点要把这段偏移算进去。

        回归测试：原来裁剪点直接取「模板长度」，忘了加上
        ``match()`` 找出来的 offset —— 广告从第 2 秒开始时，
        裁剪点会早 2 秒，广告尾巴留在片头。
        """
        video = make_video_with_prelude(tmp_path / "prelude.mp4", prelude_seconds=2)
        detection = detect.detect_video(video, ad_bank)
        assert detection.verdict is Verdict.CUT
        # 广告 2~7 秒 → 裁剪点应该是 7 秒，不是 5 秒
        assert detection.cut_at == pytest.approx(7.0, abs=1.5)


class TestTemplateChoice:
    """挑哪条模板的优先级：整条匹配 > 人工确认 > 匹配得长 > 覆盖率高。"""

    def _result(self, prefix_len: int, template_len: int):
        from file_tool.adcut import fingerprint
        return fingerprint.MatchResult(
            matched=True, offset=0, prefix_len=prefix_len,
            coverage=prefix_len / template_len, template_len=template_len,
        )

    def test_full_match_beats_partial(self):
        assert detect._better_template(
            self._result(50, 50), TemplateStatus.CANDIDATE,
            self._result(200, 227), TemplateStatus.CONFIRMED,
        )

    def test_confirmed_beats_longer_candidate(self):
        """人工确认过的模板优先，哪怕匹配得短一点。

        回归测试：模板长度就是裁剪点，人眼量过的边界该信人眼那条。
        """
        assert detect._better_template(
            self._result(227, 227), TemplateStatus.CONFIRMED,
            self._result(230, 230), TemplateStatus.CANDIDATE,
        )

    def test_longer_prefix_wins_within_same_status(self):
        assert detect._better_template(
            self._result(105, 160), TemplateStatus.CONFIRMED,
            self._result(90, 160), TemplateStatus.CONFIRMED,
        )

    def test_same_length_falls_back_to_coverage(self):
        from file_tool.adcut import fingerprint
        low = self._result(100, 160)
        high = fingerprint.MatchResult(
            matched=True, prefix_len=100, coverage=0.9, template_len=160,
        )
        assert detect._better_template(high, TemplateStatus.CONFIRMED, low, TemplateStatus.CONFIRMED)


class TestConfidence:
    """置信度分档的规则。

    核心是三道封顶：模板没人工确认过、匹配不完整、裁剪点处没有硬切。
    """

    def _result(self, prefix_len: int, template_len: int):
        from file_tool.adcut import fingerprint
        return fingerprint.MatchResult(
            matched=True, offset=0, prefix_len=prefix_len,
            coverage=prefix_len / template_len, template_len=template_len,
            score=fingerprint.score_of(fingerprint.MatchResult(
                matched=True, prefix_len=prefix_len, template_len=template_len,
            )),
        )

    def test_full_match_with_hard_cut_is_auto_cut(self):
        """模板整条都对上，裁剪点处又有硬切 —— 直接裁。"""
        result = self._result(160, 160)
        confidence, incomplete = detect.compute_confidence(
            result, mad_value=40.0, status=TemplateStatus.CONFIRMED, template_len=160,
        )
        assert not incomplete
        assert confidence >= detect.AUTO_CUT_THRESHOLD

    def test_incomplete_match_never_auto_cuts(self):
        """模板只对上一部分 —— 绝不能自动裁。

        回归测试：实测 105/160 这种比例，公式算出 0.78，叠上硬切的
        +0.03 正好 0.81，越过 0.80 的自动裁剪线 —— 而那批视频的
        实际广告长度根本不是 160 秒，切下去会削掉最多 55 秒正片。
        """
        result = self._result(105, 160)
        confidence, incomplete = detect.compute_confidence(
            result, mad_value=40.0, status=TemplateStatus.CONFIRMED, template_len=160,
        )
        assert incomplete
        assert confidence < detect.AUTO_CUT_THRESHOLD
        assert confidence >= detect.REVIEW_THRESHOLD

    def test_candidate_template_never_auto_cuts(self):
        """机器自己认出来的模板 —— 不允许直接开刀。"""
        result = self._result(160, 160)
        confidence, incomplete = detect.compute_confidence(
            result, mad_value=40.0, status=TemplateStatus.CANDIDATE, template_len=160,
        )
        assert not incomplete
        assert confidence < detect.AUTO_CUT_THRESHOLD

    def test_flat_boundary_lowers_score(self):
        """裁剪点处没有硬切 —— 降分。"""
        result = self._result(160, 160)
        hard, _ = detect.compute_confidence(
            result, mad_value=40.0, status=TemplateStatus.CONFIRMED, template_len=160,
        )
        flat, _ = detect.compute_confidence(
            result, mad_value=1.0, status=TemplateStatus.CONFIRMED, template_len=160,
        )
        assert flat < hard


class TestPlanCutPoint:
    def test_uses_nearby_keyframe(self):
        """关键帧离裁剪点够近 —— 无损流拷贝，误差在容忍范围内。"""
        real, mode, snap = cut_mod.plan_cut_point(10.0, [0.0, 8.0, 10.2, 20.0])
        assert real == 10.2
        assert mode == "copy"
        assert snap == pytest.approx(0.2)

    def test_prefers_the_closest_keyframe(self):
        """前后都有关键帧 —— 取最近的那个，误差最小。"""
        real, mode, snap = cut_mod.plan_cut_point(10.0, [0.0, 9.8, 11.0])
        assert real == 9.8
        assert mode == "copy"
        assert snap == pytest.approx(-0.2)

    def test_hybrid_when_no_keyframe_is_close(self):
        """关键帧都离得远 —— 混合裁剪：精确切在裁剪点，又不用整部重编码。"""
        real, mode, snap = cut_mod.plan_cut_point(10.0, [0.0, 40.0])
        assert real == 10.0
        assert mode == "hybrid"
        assert snap == 0.0

    def test_hybrid_when_keyframe_error_is_too_big(self):
        """关键帧差 1.8 秒 —— 误差看得见，宁可混合裁剪切精确。"""
        real, mode, snap = cut_mod.plan_cut_point(10.0, [0.0, 8.2, 40.0])
        assert real == 10.0
        assert mode == "hybrid"
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

    def test_hybrid_cut_lands_exactly_on_cut_point(self, tmp_path: Path):
        """裁剪点附近没有关键帧时走混合裁剪。

        要验两件事：
        1. 切点是**精确**的 —— 不能退化成「切在最近的关键帧上」；
        2. 不用整部重编码 —— 混合模式下只有开头几秒被重新编码。
        """
        video = make_sparse_keyframe_video(tmp_path / "a.mp4")
        before = ffmpeg.probe(video).duration
        outcome = cut_mod.cut_video(video, cut_at=7.0)
        assert outcome.ok, outcome.reason
        assert outcome.mode == "hybrid"
        after = ffmpeg.probe(video).duration
        # 关键帧在 0/5/10/15 —— 切 7.0 秒只有混合裁剪能切准
        assert before - after == pytest.approx(7.0, abs=0.5)

    def test_hybrid_cut_leaves_no_temp_files(self, tmp_path: Path):
        video = make_sparse_keyframe_video(tmp_path / "a.mp4")
        cut_mod.cut_video(video, cut_at=7.0)
        leftovers = [item.name for item in tmp_path.iterdir()
                     if cut_mod.TEMP_MARK in item.name]
        assert leftovers == []

    def test_hybrid_cut_result_is_playable(self, tmp_path: Path):
        video = make_sparse_keyframe_video(tmp_path / "a.mp4")
        assert cut_mod.cut_video(video, cut_at=7.0).ok
        info = ffmpeg.probe(video)
        assert info.has_video
        assert info.duration > 0

    def test_missing_file_is_reported(self, tmp_path: Path):
        outcome = cut_mod.cut_video(tmp_path / "nope.mp4", cut_at=5.0)
        assert not outcome.ok
        assert "不存在" in outcome.reason


class TestProbeDetails:
    """``probe()`` 里几个容易踩坑的细节。"""

    def test_fps_comes_from_video_track(self, tmp_path: Path):
        """帧率必须取视频轨的。

        音频轨的 ``r_frame_rate`` 是 ``0/0``。以前按行解析，
        音频轨那行会把帧率冲成 0 —— 坏包自愈靠帧率换算断点位置，
        帧率是 0 就定位不到断点了。
        """
        cmd = [
            ffmpeg.require_executable("ffmpeg"), "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-map", "0:v", "-map", "1:a",
            "-pix_fmt", "yuv420p", str(tmp_path / "av.mp4"),
        ]
        subprocess.run(cmd, capture_output=True, check=True, timeout=120)
        info = ffmpeg.probe(tmp_path / "av.mp4")
        assert info.has_video and info.has_audio
        assert info.fps == pytest.approx(25.0, abs=0.01)

    def test_video_frame_count(self, tmp_path: Path):
        video = make_video(tmp_path / "a.mp4", ad_seconds=2, content_seconds=3)
        count = ffmpeg.video_frame_count(video)
        assert count > 0

    def test_keyframes_between_finds_middle_of_file(self, tmp_path: Path):
        """指定区间的关键帧探测要能落到片子中段。"""
        video = make_sparse_keyframe_video(tmp_path / "a.mp4")
        found = ffmpeg.keyframes_between(video, 4.0, 8.0)
        assert any(4.0 <= k <= 12.0 for k in found)

    def test_corrupt_points_finds_zero_length_prefix(self, tmp_path: Path):
        """人为把某个包的长度前缀写成 0 —— 扫描要能把它找出来。"""
        video = make_sparse_keyframe_video(tmp_path / "a.mp4")
        target = corrupt_one_packet(video, skip_first=3)
        points = ffmpeg.corrupt_points(video)
        assert len(points) == 1
        assert abs(points[0] - target) < 0.5


class TestCorruptPacketRecovery:
    """源文件带坏包时，混合裁剪要能绕过去。

    有些源文件自带损坏的 H.264 访问单元。单个 mp4 拷贝遇到它只是打个
    警告，但 ``concat`` 解复用器读到那里就**停止读** —— 而且 ffmpeg 的
    退出码还是 0，产物于是被静默截断（少掉一整段片子尾巴）。

    所以混合裁剪拼完必须核对长度，短了就绕开坏包重拼。
    """

    def test_cut_survives_a_corrupt_packet(self, tmp_path: Path):
        video = make_sparse_keyframe_video(tmp_path / "a.mp4")
        before = ffmpeg.probe(video).duration
        # 坏包要落在**正文**里（裁剪点之后），才会触发拼接截断
        corrupt_one_packet(video, skip_first=120)
        outcome = cut_mod.cut_video(video, cut_at=7.0)
        assert outcome.ok, outcome.reason
        after = ffmpeg.probe(video).duration
        assert before - after == pytest.approx(7.0, abs=1.0)

    def test_cut_is_refused_without_losing_content_when_recovery_fails(
        self, tmp_path: Path, monkeypatch
    ):
        """绕不过去的时候要报错，而且**原片不能动**。"""
        video = make_sparse_keyframe_video(tmp_path / "a.mp4")
        size_before = video.stat().st_size
        corrupt_one_packet(video, skip_first=120)
        monkeypatch.setattr(
            cut_mod.ffmpeg, "corrupt_points", lambda *a, **k: []
        )
        monkeypatch.setattr(
            cut_mod.ffmpeg, "video_frame_count", lambda *a, **k: 0
        )
        outcome = cut_mod.cut_video(video, cut_at=7.0)
        assert not outcome.ok
        assert video.stat().st_size == size_before


class TestCluster:
    def test_clusters_group_by_shared_head(self, tmp_path: Path):
        """共享同一段片头的视频要聚到一组。"""
        bank = TemplateBank()
        videos = [make_video(tmp_path / f"v{i}.mp4") for i in range(3)]
        plan = detect.scan_tree(tmp_path, bank)
        signatures = detect.collect_signatures(videos, tmp_path)
        clusters = detect.cluster_unknown(plan, signatures=signatures)
        assert len(clusters) == 1
        assert clusters[0].size == 3

    def test_different_heads_are_not_grouped(self, tmp_path: Path):
        """内容完全不同的视频不能被凑成一组。

        这条是回归测试：曾经聚类时代表列表和分组列表下标错位，
        把 29 个毫无关系的视频挂进了同一个组。
        """
        bank = TemplateBank()
        a = make_video(tmp_path / "a.mp4")
        b = make_video(tmp_path / "b.mp4")
        # b 用另一种画面源 —— 指纹完全不同
        subprocess.run([
            ffmpeg.require_executable("ffmpeg"), "-v", "error", "-y",
            "-f", "lavfi", "-i", "smptebars=size=320x240:rate=10:duration=15",
            "-pix_fmt", "yuv420p", str(b),
        ], capture_output=True, check=True, timeout=120)

        plan = detect.scan_tree(tmp_path, bank)
        signatures = detect.collect_signatures([a, b], tmp_path)
        clusters = detect.cluster_unknown(plan, signatures=signatures)
        assert all(cluster.size == 1 for cluster in clusters)

    def test_review_variants_group_by_divergence(self, tmp_path: Path, ad_bank: TemplateBank):
        """REVIEW 的视频按「匹配长度」（变体分叉点）分组。"""
        video = make_video(tmp_path / "a.mp4")
        plan = detect.scan_tree(tmp_path, ad_bank)
        detections = [d for d in plan.detections if d.verdict is Verdict.REVIEW]
        if not detections:
            pytest.skip("该样本没进 REVIEW 档")
        clusters = detect.cluster_unknown(plan, signatures={})
        assert all(cluster.note for cluster in clusters)
