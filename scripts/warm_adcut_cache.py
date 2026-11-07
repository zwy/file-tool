"""预热 ad-cut 的指纹缓存（把片头窗口从 200s 扩到 300s）。

一次性脚本，不属于产品功能：模板长度改了以后，片头窗口必须够长
才装得下模板，而重算指纹要解码 280 个视频的前 300 秒（约 40 分钟），
单独跑一遍比每次 scan 都重算快。

用法::

    PYTHONPATH=. .venv/bin/python scripts/warm_adcut_cache.py /Volumes/P7000Z/R
"""
from __future__ import annotations

import concurrent.futures
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_tool.adcut import cache, detect, ffmpeg  # noqa: E402


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/Volumes/P7000Z/R")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else detect.DEFAULT_HEAD_SECONDS
    videos = detect.find_videos(root)
    print(f"共 {len(videos)} 个视频，窗口 {seconds:.0f}s", flush=True)

    done = 0
    started = time.time()
    errors = 0

    def work(path: Path) -> str:
        try:
            cache.get_or_compute(path, seconds, fps=1.0, size=ffmpeg.FRAME_SIZE)
        except Exception as exc:  # 坏文件（无 moov atom、0 字节…）
            return f"{path.name}：{exc}"
        return path.name

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for name in pool.map(work, videos):
            done += 1
            if done % 20 == 0:
                elapsed = time.time() - started
                print(f"  {done}/{len(videos)}  {elapsed:.0f}s", flush=True)

    print(f"完成 {done} 个，耗时 {time.time() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
