"""人工核验过的逐文件广告边界。

## 为什么需要这张表

模板库里的每条模板只记**一支广告的长度**，而 ``detect.py`` 直接拿
模板长度当裁剪点（``cut_at = offset + template_len``）。

这套设计的前提是「同一支广告在不同片子里长度一样」。实测**不成立**：

- 站方推广片是**多卡片轮播**（手机画面 → 二维码 → 票选前10大 → 游玩 →
  百家樂 …），每张卡片 4~8 秒；
- **每支片子插的卡片数量不一样**，广告总长也就跟着不一样。
  实测同一支 gamebb 推广片：DVMM-012 是 319 秒，dvdms-00300b 是 320 秒，
  sdmu-00877b 是 329 秒；
- 更麻烦的是同一模板下混进了不同长度的片子 ——
  ``tpl-0017`` 的四个成员实测是 291 / 291 / 291 / 291 秒。

一条模板只能记一个长度，于是这类文件必然切错：按模板长度裁，
短的会留下几十秒广告尾巴，长的会切进正片。

## 核验方法：逐帧精确抽帧

这张表里的每个秒数都是**逐帧抽帧**看出来的，不是估的：

    ffmpeg -ss <秒> -i <文件> -frames:v 1 -y out.png

**不要用** ``-ss X -t N -vf fps=1`` 的滤镜流水线来核验 ——
这些站点重压的片子 ``start_time`` 不是 0（是 0.023）且关键帧稀疏，
滤镜流水线的时间轴和源时间对不上，实测能差 10 秒。偏差还是逐文件不同的，
拼图看上去永远「合情合理」，发现不了。

## 口径：哪些卡该裁

- **带站点二维码 / 站点 logo 的推广卡** → 站方广告，裁掉；
- **黑底白字的作品简介 / 免责 / 厂牌 logo 卡** → 厂牌片头，保留；
- 「DOUJIN R18 成人向」分级卡带站点标识 → 算站方广告，裁掉。

所以 ``cut_at`` 记的是**从片头切掉多少秒**，也就是
「站方广告最后一帧 + 1」；它后面如果还有厂牌片头，那些帧会被保留。

## 这张表怎么用

``rules/ad_boundaries.yaml`` 里逐个文件记下**人工核验过的广告终点**，
``detect.py`` 命中表里有记录的文件时，用表里的秒数替代模板长度。

只有确实核过片的文件才写进来 —— 宁可没有，也不要写错的。

## 数据格式

和 ``rules/ad_templates.yaml`` 同构：人类可读、可 diff、可回滚。

    version: 1
    boundaries:
      - path: DVMM-012/DVMM-012.mp4
        cut_at: 319
        site_ad_end: 319
        verified: '2026-11-09'
        note: 站方推广片到 318 秒结束

``path`` 是相对库根目录的 POSIX 路径；``cut_at`` 是从片头算起要切掉的秒数。
``verified`` 是核验日期（字符串），用来标记这条是不是真的看过。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .models import AdBoundary, BoundaryBook


class BoundaryError(RuntimeError):
    """边界表读不了。"""


def load_boundaries(path: Path) -> BoundaryBook:
    """读边界表。文件不存在就返回空表（表是可选的）。"""
    path = Path(path)
    if not path.exists():
        return BoundaryBook()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BoundaryError(f"读不了边界表 {path}：{exc}") from exc
    if raw is None:
        return BoundaryBook()
    if not isinstance(raw, dict):
        raise BoundaryError(f"边界表格式不对（要 dict）：{path}")
    try:
        book = BoundaryBook(**raw)
    except Exception as exc:
        raise BoundaryError(f"边界表内容不合法（{path}）：{exc}") from exc
    for item in book.boundaries:
        if item.cut_at <= 0:
            raise BoundaryError(f"边界表里有非正的 cut_at：{item.path}")
    return book


def save_boundaries(book: BoundaryBook, path: Path) -> None:
    """写回边界表。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            book.model_dump(exclude_defaults=False),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def as_map(book: BoundaryBook) -> Dict[str, float]:
    """摊平成 ``相对路径 -> 裁剪秒数``。"""
    return {item.path: item.cut_at for item in book.boundaries}


def lookup(book: Optional[BoundaryBook], rel_path: str) -> Optional[AdBoundary]:
    """查一个文件的人工核验边界。"""
    if book is None:
        return None
    for item in book.boundaries:
        if item.path == rel_path:
            return item
    return None
