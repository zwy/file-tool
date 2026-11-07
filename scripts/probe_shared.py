"""验证「共享出现在任何位置」+「共享者数量」能不能定出广告边界。

前面的同下标版本有两个硬伤：

- 变体独有的卡片（实测 CLT-013C 的 148~185 秒）会让共享段断开，
  断点被误当成边界；
- 不同片子的黑底片名卡长得都差不多，会被误判成共享，
  把边界推到片名卡之后。

所以这里同时看两个信号：

1. 这一帧**出现在任何位置**都能在别的视频里找到（不要求下标对齐）
2. 有多少个**别的视频**包含它 —— 站方通用卡被几十个片子共用，
   片名卡最多两三个片子共用

用法::

    PYTHONPATH=. .venv/bin/python scripts/probe_shared.py /Volumes/P7000Z/R ABF-094/ABF-094.mp4
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, detect, ffmpeg  # noqa: E402

TOLERANCE = 4
BRIGHTNESS_TOLERANCE = 8.0
MAX_BUCKET = 4000
"""单个 LSH 桶超过这么大就跳过 —— 纯黑帧这类低熵哈希会挤爆桶。"""


def build_index(sigs):
    """建两个索引：精确哈希表 + 四段 16 位 LSH 桶。"""
    exact: Dict[int, List[Tuple[int, int]]] = {}
    lsh: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for v, sig in enumerate(sigs):
        for i, h in enumerate(sig.hashes):
            exact.setdefault(h, []).append((v, i))
            for shift in (0, 16, 32, 48):
                key = (shift, (h >> shift) & 0xFFFF)
                bucket = lsh.setdefault(key, [])
                if len(bucket) < MAX_BUCKET:
                    bucket.append((v, i))
    return exact, lsh


def candidates(exact, lsh, h: int):
    seen = set()
    for item in exact.get(h, ()):
        seen.add(item)
    for shift in (0, 16, 32, 48):
        for item in lsh.get((shift, (h >> shift) & 0xFFFF), ()):
            seen.add(item)
    return seen


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/P7000Z/R")
    targets = sys.argv[2:] or ["AKID-103/AKID-103.mp4"]
    seconds = 300.0

    videos = detect.find_videos(root)
    sigs = []
    for path in videos:
        try:
            sigs.append(cache.get_or_compute(path, seconds, fps=1.0, size=ffmpeg.FRAME_SIZE))
        except Exception:
            sigs.append(cache.FrameSignature(hashes=[], mad=[], brightness=[], fps=1.0))

    print("建索引……", flush=True)
    exact, lsh = build_index(sigs)

    for rel in targets:
        index = next(i for i, v in enumerate(videos) if str(v).endswith(rel))
        sig = sigs[index]
        print(f"\n=== {rel} ===")
        print("  帧    同下标共享数   全库共享视频数   亮度")
        for f in range(0, min(len(sig.hashes), 300)):
            h = sig.hashes[f]
            b = sig.brightness[f] if sig.brightness else 0.0
            same_index = 0
            others = set()
            for v, j in candidates(exact, lsh, h):
                if v == index:
                    continue
                other = sigs[v]
                if j >= len(other.hashes):
                    continue
                if (h ^ other.hashes[j]).bit_count() > TOLERANCE:
                    continue
                if other.brightness and abs(b - other.brightness[j]) > BRIGHTNESS_TOLERANCE:
                    continue
                others.add(v)
                if j == f:
                    same_index += 1
            if same_index or len(others) >= 3:
                print(f"  {f:4d}  {same_index:8d}   {len(others):10d}   {b:6.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
