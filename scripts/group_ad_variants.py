"""用「组内共享」定出每支推广片的真实长度。

## 为什么需要它

「这帧在别的视频里出现过吗」这个信号有个硬伤：
**变体独有的卡片**只在这一支推广片里出现过，跨片共享检测不到，
共享段会在那里断掉 —— 实测 CLT-013C 的推广片有 38 秒独有的卡片，
检测器把边界截在 147 秒，而真值是 193 秒。

## 组内共享为什么更准

同一支推广片的不同发行版，内容**逐帧完全一致**（只是重新编码过）。
所以对同一组里的两个视频来说：

- 推广片区间 → 每一帧都对得上
- 正片区间 → 每个片子都不一样，对不上

于是「组内最后一个还对得上的帧号」就是推广片的终点 ——
哪怕那张卡只在这一支推广片里出现过，它在对上的时候也是**组内**对得上。

## 分组口径

必须是「整条推广片逐帧一致」，不能用「前 30 帧碰巧一样」——
实测不同变体最多共享 115 帧才分叉，前 30 帧根本区分不出来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, detect, fingerprint, ffmpeg  # noqa: E402

TOLERANCE = 4
"""组内判「同一画面」的汉明距离上限（比模板匹配的 6 更严）。"""

BRIGHTNESS_TOLERANCE = 8.0
"""组内判「同一画面」的亮度差上限。"""

LENGTH_TOLERANCE = 3
"""长度差这么多帧算同一组。"""

MAX_GAP = 2
"""连续这么多帧对不上就认为推广片结束了。"""

COMPARE_FRAMES = 180
"""判定「同一支推广片」时比对多少帧。

**不能用检测出来的长度** —— 那个长度在推广片尾部是变体独有时会被截断
（实测截到 147 秒，而真值是 193 秒）。拿它当比对长度，
两支尾部不同的推广片会被误判成同一支。

固定比 180 帧：实测不同变体最多共享 115 帧就分叉，
180 帧足够把它们分开；而同一支推广片的发行版是整条一致的。
"""


def frames_match(a, b, ai, bi, sig_a, sig_b) -> bool:
    if (sig_a.hashes[ai] ^ sig_b.hashes[bi]).bit_count() > TOLERANCE:
        return False
    if sig_a.brightness and sig_b.brightness:
        if abs(sig_a.brightness[ai] - sig_b.brightness[bi]) > BRIGHTNESS_TOLERANCE:
            return False
    return True


def same_reel(sig_a, sig_b, length: int) -> bool:
    """两支推广片是不是同一支（逐帧一致，允许末尾差一两帧）。"""
    usable = min(length, len(sig_a.hashes), len(sig_b.hashes))
    if usable < 10:
        return False
    matched = 0
    for f in range(usable):
        if frames_match(sig_a, sig_b, f, f, sig_a, sig_b):
            matched += 1
    return matched >= usable - 2


def same_reel_fixed(sig_a, sig_b) -> bool:
    """固定比对 ``COMPARE_FRAMES`` 帧，判定是否同一支推广片。"""
    usable = min(COMPARE_FRAMES, len(sig_a.hashes), len(sig_b.hashes))
    if usable < 10:
        return False
    matched = 0
    for f in range(usable):
        if frames_match(sig_a, sig_b, f, f, sig_a, sig_b):
            matched += 1
    return matched >= usable - 2


def group_end(sig_a, sig_b, limit: int) -> int:
    """从第 0 帧往前扫，直到连续 ``MAX_GAP`` 帧对不上。"""
    end = 0
    gap = 0
    for f in range(limit):
        if frames_match(sig_a, sig_b, f, f, sig_a, sig_b):
            end = f + 1
            gap = 0
            continue
        gap += 1
        if gap > MAX_GAP:
            break
    return end


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/P7000Z/R")
    boundaries_path = Path(sys.argv[2] if len(sys.argv) > 2 else Path("/tmp/ad_boundaries.json"))
    out_path = Path(sys.argv[3] if len(sys.argv) > 3 else Path("/tmp/ad_variants.json"))

    boundaries: Dict[str, int] = json.loads(boundaries_path.read_text(encoding="utf-8"))

    videos = detect.find_videos(root)
    sigs = {}
    for path in videos:
        try:
            sigs[str(path.relative_to(root))] = cache.get_or_compute(
                path, 300.0, fps=1.0, size=ffmpeg.FRAME_SIZE
            )
        except Exception:
            continue

    candidates = sorted(
        (rel for rel, length in boundaries.items() if length > 0),
        key=lambda rel: boundaries[rel],
    )

    # ---- 按「整条推广片一致」分组 ----
    #
    # 长度只用来做粗筛（避免把差了几十秒的两支推广片凑到一起细算），
    # 真正的判据是固定 180 帧的逐帧一致性。
    clusters: List[dict] = []
    for rel in candidates:
        length = boundaries[rel]
        sig = sigs.get(rel)
        if sig is None:
            continue
        placed = False
        for cluster in clusters:
            if abs(cluster["length"] - length) > LENGTH_TOLERANCE:
                continue
            if same_reel_fixed(sig, sigs[cluster["representative"]]):
                cluster["members"].append(rel)
                placed = True
                break
        if not placed:
            clusters.append({"length": length, "members": [rel], "representative": rel})

    # ---- 组内共享段 = 推广片终点 ----
    for cluster in clusters:
        members = cluster["members"]
        rep = sigs[cluster["representative"]]
        limit = min(len(rep.hashes), 300)
        if len(members) >= 2:
            end = 0
            gap = 0
            for f in range(limit):
                count = 0
                for rel in members:
                    other = sigs[rel]
                    if f >= len(other.hashes):
                        continue
                    if frames_match(rep, other, f, f, rep, other):
                        count += 1
                if count >= 2:
                    end = f + 1
                    gap = 0
                    continue
                gap += 1
                if gap > MAX_GAP:
                    break
            cluster["group_end"] = end
            cluster["boundary_source"] = "group" if end else "global"
        else:
            cluster["group_end"] = 0
            cluster["boundary_source"] = "global"
        cluster["length"] = max(cluster["group_end"], cluster["length"])

    clusters.sort(key=lambda c: (-len(c["members"]), c["length"]))
    for index, cluster in enumerate(clusters, start=1):
        cluster["id"] = f"var-{index:03d}"

    out_path.write_text(json.dumps(clusters, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(clusters)} 个变体 → {out_path}\n")
    print(f"{'组':10s} {'长度':>6s} {'来源':>8s} {'成员':>5s}  代表")
    for cluster in clusters:
        print(f"{cluster['id']:10s} {cluster['length']:6d} {cluster['boundary_source']:>8s} "
              f"{len(cluster['members']):5d}  {cluster['representative']}")
    print(f"\n覆盖 {sum(len(c['members']) for c in clusters)} 个视频")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
