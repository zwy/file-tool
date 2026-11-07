"""用「任意位置的跨片共享」定出每支推广片的真实长度。

## 为什么不能要求下标对齐

站方推广片是**卡片轮播**：每张卡片 4~8 秒，不同发行版用的卡片组合不同，
同一张卡在两个片子里出现的位置也不一样。

所以「这一帧在别的视频的**同一秒**出现过吗」这个判据会失效 ——
实测 `PAKX-008` 的推广片在 67~87 秒这一段，同下标共享数是 **0**
（别的片子里这张卡在别的位置），但「任意位置」的共享数是 18~48。

## 判据

```
count[i] = 有多少个**别的**视频里存在一帧和第 i 帧几乎一样（不限位置）
```

站方通用卡被几十上百个片子共用（实测 174 个），
片子自己的正片则是每个发行版独有的（count = 0）。
黑底片名卡是个中间态（实测 4 个）—— 靠「共享者数量」和正片区分得开。

边界 = 从第 0 帧起，连续满足 ``count >= 阈值`` 的那一段的末尾。

## 用法

    PYTHONPATH=. .venv/bin/python scripts/find_ad_boundaries2.py /Volumes/P7000Z/R 300
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, detect, ffmpeg  # noqa: E402

TOLERANCE = 4
BRIGHTNESS_TOLERANCE = 8.0
MAX_BUCKET = 3000
"""查询时桶超过这么大就跳过这个桶（纯黑帧这类低熵哈希会挤爆桶）。

**注意是查询时跳过，不是插入时丢弃** —— 早先在插入时就丢弃，
结果桶一满，后面的帧全都查不到（表现为「某段之后的共享数全是 0」）。
"""

# 分块数。8 段 8 位：64 位哈希切成 8 段，每段 8 位。
#
# 用多索引 LSH 找近似匹配：两帧汉明距离 ≤ 4 时，至少有 4 段是干净的，
# 所以一定能在某个桶里撞上。段数越多越不容易漏，桶也越小
# （8 位桶的平均大小是 16 位桶的 1/256）。
CHUNKS = 8
CHUNK_BITS = 8


def build_index(sigs):
    exact: Dict[int, List] = {}
    lsh: Dict[tuple, List] = {}
    for v, sig in enumerate(sigs):
        for i, h in enumerate(sig.hashes):
            exact.setdefault(h, []).append((v, i))
            for chunk in range(CHUNKS):
                key = (chunk, (h >> (chunk * CHUNK_BITS)) & 0xFF)
                lsh.setdefault(key, []).append((v, i))
    return exact, lsh


def _candidates(exact, lsh, h):
    seen = set(exact.get(h, ()))
    for chunk in range(CHUNKS):
        key = (chunk, (h >> (chunk * CHUNK_BITS)) & 0xFF)
        bucket = lsh.get(key)
        if not bucket or len(bucket) > MAX_BUCKET:
            continue
        seen.update(bucket)
    return seen


def counts_for(index, exact, lsh, sigs, index_of, frames=300):
    """算出每一帧被多少个别的视频共享（不限位置）。"""
    sig = sigs[index_of]
    out = []
    for f in range(min(frames, len(sig.hashes))):
        h = sig.hashes[f]
        b = sig.brightness[f] if sig.brightness else 0.0
        peers = set()
        for v, j in _candidates(exact, lsh, h):
            if v == index_of:
                continue
            other = sigs[v]
            if j >= len(other.hashes):
                continue
            if (h ^ other.hashes[j]).bit_count() > TOLERANCE:
                continue
            if other.brightness and abs(b - other.brightness[j]) > BRIGHTNESS_TOLERANCE:
                continue
            peers.add(v)
        out.append(len(peers))
    return out


SHARE_THRESHOLD = 3
"""至少被这么多个别的视频共享，才算「站方素材」。"""

MAX_GAP = 3
"""连续这么多帧不共享就认为推广片结束了。"""


def boundary_from_counts(counts, threshold=SHARE_THRESHOLD, max_gap=MAX_GAP):
    end = 0
    gap = 0
    for i, value in enumerate(counts):
        if value >= threshold:
            end = i + 1
            gap = 0
            continue
        gap += 1
        if gap > max_gap:
            break
    return end


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/P7000Z/R")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/ad_boundaries2.json")

    videos = detect.find_videos(root)
    sigs = []
    for path in videos:
        try:
            sigs.append(cache.get_or_compute(path, seconds, fps=1.0, size=ffmpeg.FRAME_SIZE))
        except Exception:
            sigs.append(cache.FrameSignature(hashes=[], mad=[], brightness=[], fps=1.0))

    print("建索引……", flush=True)
    exact, lsh = build_index(sigs)

    result: Dict[str, int] = {}
    profiles: Dict[str, List[int]] = {}
    for index, path in enumerate(videos):
        if not sigs[index].hashes:
            continue
        counts = counts_for(index, exact, lsh, sigs, index)
        result[str(path.relative_to(root))] = boundary_from_counts(counts)
        profiles[str(path.relative_to(root))] = counts

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("/tmp/ad_share_profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False), encoding="utf-8"
    )
    print(f"已写出 {out_path}")

    lengths: Dict[int, int] = {}
    for value in result.values():
        lengths[value] = lengths.get(value, 0) + 1
    print("\n按广告长度分组（帧 → 个数）：")
    for length in sorted(lengths):
        print(f"  {length:4d} 帧 ({length:.0f}s)  {lengths[length]:3d} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
