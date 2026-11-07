"""用「跨片共享 + 指纹跳变」给一支视频定广告终点。

判据（三条同时成立才算广告卡）：

1. 这一帧被**别的视频共享**（share >= MIN_SHARE）—— 站方通用推广片
   同毫秒出现在几十个不相关作品里；
2. 这一帧**不是退化帧**（平均亮度 >= MIN_BRIGHT）—— 纯黑/纯色画面的
   dHash 恒为常量，会和所有黑场互相误匹配；
3. 它的**下一帧不是**同族的延续（亮度或 MAD 发生跳变）—— 广告卡结束。

用法::

    PYTHONPATH=. .venv/bin/python scripts/boundary.py <共享剖面json> <视频相对路径> [...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, ffmpeg  # noqa: E402

MIN_SHARE = 10
MIN_BRIGHT = 25.0
JUMP = 30.0
"""亮度或 MAD 的跳变阈值 —— 超过它就算「换了一张卡 / 进了正片」。"""


def signature(path: Path, seconds: float = 300.0):
    sig = cache.load(path, fps=1.0, seconds=seconds, size=ffmpeg.FRAME_SIZE)
    if sig is None:
        sig = cache.get_or_compute(path, seconds, fps=1.0, size=ffmpeg.FRAME_SIZE)
    return sig


def analyse(profiles: dict, rel: str) -> None:
    share = profiles.get(rel)
    sig = signature(Path("/Volumes/P7000Z/R") / rel)
    if share is None or sig is None:
        print(f"{rel}: 缺少共享剖面或指纹")
        return
    n = min(len(share), len(sig.hashes))
    bright = sig.brightness or [0.0] * n
    mad = sig.mad or [0.0] * n

    hits = []
    for i in range(n):
        if share[i] < MIN_SHARE or bright[i] < MIN_BRIGHT:
            continue
        # 下一帧若亮度 / MAD 发生跳变，说明广告卡到这儿为止
        nxt_b = bright[i + 1] if i + 1 < n else 0.0
        nxt_m = mad[i + 1] if i + 1 < n else 0.0
        jump = abs(nxt_b - bright[i]) > JUMP or nxt_m > JUMP
        hits.append((i, share[i], round(bright[i], 1), round(nxt_b, 1), round(nxt_m, 1), jump))
    tail = hits[-6:]
    print(f"{rel}")
    for i, s, b, nb, nm, jump in tail:
        print(f"    {i:4d}s share={s:4d} bright={b:6.1f} → next bright={nb:6.1f} mad={nm:6.1f}"
              f"  {'← 跳变' if jump else ''}")
    print(f"    ⇒ 候选广告终点 {tail[-1][0] if tail else '?'}，建议裁剪点 {(tail[-1][0] + 1) if tail else '?'}")


def main() -> int:
    profiles = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for rel in sys.argv[2:]:
        analyse(profiles, rel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
