"""用「跨片共享」反推广告的真实长度。

## 依据

这套工具的整个前提是：**片头广告是站方投放的共享素材**，
而正片是每个发行版独有的。所以拿两个视频逐帧比，
广告区间会**逐帧对得上**，正片区间会对不上。

于是有一个不需要人眼、不需要模板的边界信号：

    对每个视频 V 和每个帧号 i，
    如果存在另一个视频 W，使得 V[i] 和 W[i] 逐帧相似，
    那么 i 大概率还在广告里。

取「整段连续共享」的最右端，就是这支视频广告的真实长度。

## 为什么不用帧间突变

实测站方推广片内部就有五六个硬切（26.7s / 30.3s / 33.7s / 57.4s …），
任何「找最大突变点」的启发式都会切在广告中间。共享性不依赖画面内容，
因此不受这个问题影响。

## 用法

    PYTHONPATH=. .venv/bin/python scripts/find_ad_boundaries.py /Volumes/P7000Z/R 300
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, detect, ffmpeg  # noqa: E402

# 判定「这一帧是共享的」时的严格程度
TOLERANCE = 4
"""汉明距离上限。比匹配模板用的 6 更严 —— 这里是「同一个画面」
而不是「同一个广告」，宁可漏判共享，也不要把两个不同的画面
（尤其是纯黑标题卡）当成共享。"""

BRIGHTNESS_TOLERANCE = 8.0
"""平均亮度差上限。纯色画面 dHash 恒为 0，只靠 dHash 会把
「不同视频的纯黑标题卡」判成共享 —— 亮度把它们分开。"""

MIN_SHARED_RUN = 10
"""连续共享至少这么长才算一段广告。零星一两帧撞上不算数。"""


def shared_mask(
    sigs: List[cache.FrameSignature],
    tolerance: int = TOLERANCE,
    brightness_tolerance: float = BRIGHTNESS_TOLERANCE,
) -> List[List[bool]]:
    """算出每一帧号上，哪些视频之间「这一帧是共享的」。"""
    count = len(sigs)
    # 注意：坏文件（无 moov atom、0 字节）取不到任何帧，它们的指纹是空的。
    # 不能用 ``min(长度)`` 当帧数上限 —— 一个坏文件就把整个矩阵压成空的，
    # 结果是所有视频都判成「没有共享前缀」。
    frames = max((len(s.hashes) for s in sigs), default=0)
    mask = [[False] * count for _ in range(frames)]

    for i in range(frames):
        hashes = [s.hashes[i] if i < len(s.hashes) else None for s in sigs]
        brights = [
            (s.brightness[i] if i < len(s.brightness) else 0.0) if s.brightness else 0.0
            for s in sigs
        ]
        for a in range(count):
            ha = hashes[a]
            if ha is None:
                continue
            ba = brights[a]
            for b in range(a + 1, count):
                hb = hashes[b]
                if hb is None:
                    continue
                if (ha ^ hb).bit_count() > tolerance:
                    continue
                if abs(ba - brights[b]) > brightness_tolerance:
                    continue
                mask[i][a] = True
                mask[i][b] = True
    return mask


def ad_end_from_mask(
    mask: List[List[bool]],
    index: int,
    min_run: int = MIN_SHARED_RUN,
    max_gap: int = 2,
) -> int:
    """从共享掩码里取出「这段视频的广告有多长」（帧数）。

    广告是**片头**，所以共享段必须从第 0 帧就开始 ——
    中间冒出来的一小段共享（比如两支片子都用了同一首 BGM 的画面）
    不算广告。

    允许段内出现 ``max_gap`` 帧不共享：转场帧、码率突变的某一帧
    都可能对不上，一次失配不该把整条边界截断。
    """
    flags = [mask[i][index] for i in range(len(mask))]

    # 从第 0 帧**向前**扫：广告是片头，所以共享段从一开始就该在。
    # 遇到连续 ``max_gap`` 帧不共享才停 —— 中间夹一两帧对不上
    # （转场帧、码率突变）不该把边界截断。
    #
    # 注意不能从「最后一个共享帧」往回找：正片里可能有一小段
    # 偶然撞上别的片子的画面（片名卡尤其容易撞 —— 不同片子的
    # 黑底片名卡长得都差不多），从那里往回找会被整段判成广告。
    end = 0
    gap = 0
    for i in range(len(flags)):
        if flags[i]:
            end = i + 1
            gap = 0
            continue
        gap += 1
        if gap > max_gap:
            break

    if end < min_run:
        return 0
    return end


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/P7000Z/R")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else detect.DEFAULT_HEAD_SECONDS
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/ad_boundaries.json")

    videos = detect.find_videos(root)
    print(f"共 {len(videos)} 个视频，窗口 {seconds:.0f}s", flush=True)

    sigs = []
    for path in videos:
        try:
            sigs.append(cache.get_or_compute(path, seconds, fps=1.0, size=ffmpeg.FRAME_SIZE))
        except Exception as exc:  # 坏文件
            print(f"  跳过 {path.name}：{exc}", flush=True)
            sigs.append(cache.FrameSignature(hashes=[], mad=[], brightness=[], fps=1.0))

    print("计算跨片共享矩阵……", flush=True)
    mask = shared_mask(sigs)

    result: Dict[str, int] = {}
    for index, path in enumerate(videos):
        end = ad_end_from_mask(mask, index)
        result[str(path.relative_to(root))] = end

    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {out}", flush=True)

    lengths: Dict[int, int] = {}
    for value in result.values():
        lengths[value] = lengths.get(value, 0) + 1
    print("\n按广告长度分组（帧 → 个数）：", flush=True)
    for length in sorted(lengths):
        if length == 0:
            continue
        print(f"  {length:4d} 帧 ({length:.0f}s)  {lengths[length]:3d} 个", flush=True)
    print(f"  0 帧（没有共享前缀，判为无广告）  {lengths.get(0, 0):3d} 个", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
