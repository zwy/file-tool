"""指纹层测试。

这里测的是**算法的性质**，不是具体某个视频：

- 同样的画面必须算出同样的哈希
- 轻微扰动（模拟重新编码）不能破坏匹配
- 完全不同的画面不能匹配上
- 帧间差异曲线要在画面突变处出现尖峰
"""
from __future__ import annotations

from file_tool.adcut import fingerprint


def _frame(value: int, size: int = 32) -> bytes:
    """造一帧纯色图。"""
    return bytes([value]) * (size * size)


def _gradient_frame(size: int = 32, reverse: bool = False) -> bytes:
    """造一帧横向渐变图 —— 相邻像素有明确大小关系。"""
    data = bytearray()
    for _ in range(size):
        for x in range(size):
            value = (x * 255) // (size - 1)
            if reverse:
                value = 255 - value
            data.append(value)
    return bytes(data)


class TestDhash:
    def test_same_frame_same_hash(self):
        assert fingerprint.dhash(_gradient_frame()) == fingerprint.dhash(_gradient_frame())

    def test_different_content_different_hash(self):
        a = fingerprint.dhash(_gradient_frame())
        b = fingerprint.dhash(_gradient_frame(reverse=True))
        assert a != b

    def test_uniform_frame_is_zero_hash(self):
        """纯色帧没有任何相邻像素差异，哈希应为 0。

        这也正是**必须补平均亮度**的原因 —— 纯红卡和纯蓝卡的
        dHash 都是 0，光靠哈希分不开。
        """
        assert fingerprint.dhash(_frame(10)) == 0
        assert fingerprint.dhash(_frame(200)) == 0

    def test_gradient_is_deterministic_and_nonzero(self):
        """渐变图有明确的左右大小关系，哈希不该是 0。"""
        value = fingerprint.dhash(_gradient_frame())
        assert value != 0
        assert value == fingerprint.dhash(_gradient_frame())


class TestHamming:
    def test_counts_differing_bits(self):
        assert fingerprint.hamming(0b1010, 0b1010) == 0
        assert fingerprint.hamming(0b1010, 0b1011) == 1
        assert fingerprint.hamming(0b0000, 0b1111) == 4


class TestMatch:
    def test_identical_sequences_match_fully(self):
        seq = [fingerprint.dhash(_gradient_frame()) + i for i in range(10)]
        result = fingerprint.match(seq, list(seq))
        assert result.matched
        assert result.prefix_len == 10
        assert result.coverage == 1.0
        assert result.score >= 0.9

    def test_different_content_does_not_match(self):
        a = [fingerprint.dhash(_gradient_frame()) for _ in range(10)]
        b = [fingerprint.dhash(_gradient_frame(reverse=True)) for _ in range(10)]
        result = fingerprint.match(a, b)
        assert not result.matched
        assert result.prefix_len == 0

    def test_uniform_frames_distinguished_by_brightness(self):
        """两段纯色画面：dHash 都是 0，靠平均亮度才能分开。

        没有亮度分量的话，「纯红片头」会匹配上「纯蓝片头」。
        """
        a = [fingerprint.dhash(_frame(20)) for _ in range(10)]
        b = [fingerprint.dhash(_frame(220)) for _ in range(10)]
        assert a == b  # 哈希确实分不开

        result = fingerprint.match(
            a, b,
            template_brightness=[20.0] * 10,
            video_brightness=[220.0] * 10,
        )
        assert not result.matched

    def test_brightness_within_tolerance_still_matches(self):
        """重新编码让平均亮度差个两三个灰阶 —— 仍然要匹配上。"""
        a = [fingerprint.dhash(_gradient_frame()) for _ in range(10)]
        b = list(a)
        result = fingerprint.match(
            a, b,
            template_brightness=[100.0] * 10,
            video_brightness=[103.0] * 10,
        )
        assert result.matched
        assert result.prefix_len == 10

    def test_tolerates_reencoding_noise(self):
        """模拟重新编码：每帧翻转 3 个比特，仍然要能匹配上。

        这是感知哈希相对于精确哈希的全部意义 —— 实测同族视频
        逐像素 md5 第一帧就不同，dHash 却能连对 137 帧。
        """
        base = [fingerprint.dhash(_gradient_frame()) for _ in range(12)]
        noisy = []
        for index, value in enumerate(base):
            noisy.append(value ^ (0b111 << (index % 50)))
        result = fingerprint.match(base, noisy)
        assert result.matched
        assert result.prefix_len == 12

    def test_short_match_is_not_accepted(self):
        """只对上两三帧不算命中 —— 否则一张静止黑帧能匹配任何模板。"""
        base = [fingerprint.dhash(_gradient_frame()) for _ in range(10)]
        result = fingerprint.match(base, base[:2])
        assert not result.matched

    def test_offset_is_found(self):
        """广告前有 2 帧别的东西时，也要能对上。"""
        base = [fingerprint.dhash(_gradient_frame()) for _ in range(10)]
        padded = [fingerprint.dhash(_frame(90)), fingerprint.dhash(_frame(91))] + list(base)
        result = fingerprint.match(base, padded)
        assert result.matched
        assert result.offset == 2
        assert result.prefix_len == 10

    def test_empty_input(self):
        assert not fingerprint.match([], []).matched
        assert not fingerprint.match([1, 2], []).matched


class TestScore:
    def test_full_match_scores_high(self):
        result = fingerprint.MatchResult(
            matched=True, prefix_len=40, coverage=1.0, template_len=40,
        )
        assert fingerprint.score_of(result) >= 0.9

    def test_partial_match_scores_lower(self):
        full = fingerprint.MatchResult(
            matched=True, prefix_len=40, coverage=1.0, template_len=40,
        )
        partial = fingerprint.MatchResult(
            matched=True, prefix_len=10, coverage=0.25, template_len=40,
        )
        assert fingerprint.score_of(partial) < fingerprint.score_of(full)

    def test_empty_template_scores_zero(self):
        assert fingerprint.score_of(fingerprint.MatchResult(matched=False)) == 0.0


class TestMad:
    def test_mad_is_zero_for_identical_frames(self):
        raw = _gradient_frame() * 3
        signature = fingerprint.signature_from_raw(raw, size=32)
        assert signature.mad[0] == 0.0
        assert signature.mad[1] == 0.0

    def test_mad_spikes_at_change(self):
        """从黑到亮的一次突变，帧间差异应该有明显尖峰。"""
        raw = _frame(10) * 2 + _frame(240) * 2
        signature = fingerprint.signature_from_raw(raw, size=32)
        assert signature.mad[2] > 100

    def test_spikes_detected(self):
        raw = _frame(10) * 2 + _frame(240) * 2
        signature = fingerprint.signature_from_raw(raw, size=32)
        spikes = fingerprint.mad_spikes(signature.mad, threshold=12.0)
        assert 2 in spikes


class TestSignature:
    def test_segment_slices_correctly(self):
        raw = _gradient_frame() * 5
        signature = fingerprint.signature_from_raw(raw, size=32)
        piece = signature.segment(1, 3)
        assert len(piece) == 3
        assert piece.hashes == signature.hashes[1:4]

    def test_segment_out_of_range_is_empty(self):
        raw = _gradient_frame() * 3
        signature = fingerprint.signature_from_raw(raw, size=32)
        assert len(signature.segment(5, 9)) == 0
