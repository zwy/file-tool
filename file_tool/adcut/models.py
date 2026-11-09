"""片头广告的数据模型。

分成四层，和命令一一对应：

- :class:`AdTemplate` —— 模板库里的一条广告（指纹 + 元信息）
- :class:`Detection` —— 对**一个视频**的判定结果
- :class:`AdPlan` —— 对**一批视频**的完整计划（``scan`` / ``cut`` 的产物）
- :class:`CutOutcome` —— 一次裁剪的执行结果
- :class:`AdBoundary` / :class:`BoundaryBook` —— 人工核验过的逐文件广告边界
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    """单个视频的判定结论。"""

    CUT = "cut"
    """认出广告了，可以裁。"""
    REVIEW = "review"
    """像是有广告但证据不够硬 —— 建议人工看一眼。"""
    NONE = "none"
    """判定开头没有广告。"""
    SKIP = "skip"
    """文件坏了 / 读不出来 —— 既不是有广告也不是没有。"""


class TemplateStatus(str, Enum):
    """模板的成熟度。"""

    CANDIDATE = "candidate"
    """只见过一两次，证据不足 —— 命中了也不自动裁。"""
    CONFIRMED = "confirmed"
    """被人工确认过 —— 命中就自动裁。"""


class AdTemplate(BaseModel):
    """一支广告的模板。

    ``hashes`` 是这支广告**从第一帧开始**的逐帧 dHash 序列。
    长度决定广告时长（``len(hashes) / fps``）。

    ``sample`` 记着它是从哪个视频学来的 —— 万一模板库被改坏了，
    这行是唯一的溯源线索。
    """

    id: str
    label: str = ""
    hashes: List[int] = Field(default_factory=list)
    brightness: List[float] = Field(default_factory=list)
    """逐帧平均亮度 —— 用来区分「纯红卡」和「纯蓝卡」。

    dHash 只比较相邻像素，纯色画面的哈希恒为 0，光靠它分不开。
    """
    fps: float = 1.0
    sample: str = ""
    """来源视频路径（学模板时的那个）。"""
    samples: int = 1
    """被确认过几次 —— 越高越可信。"""
    status: TemplateStatus = TemplateStatus.CANDIDATE
    first_seen: str = ""
    last_seen: str = ""
    note: str = ""

    @property
    def duration(self) -> float:
        """广告时长（秒）。"""
        return len(self.hashes) / self.fps if self.fps else 0.0

    @property
    def seconds_label(self) -> str:
        return f"{self.duration:.1f}s"


class NegativeTemplate(BaseModel):
    """负样本：人眼判定「这段没有广告」的片段。

    负样本的作用是**抑制误切**。片头本来就是静止画面时
    （黑场、站点 logo 动画），dHash 有可能撞上某个模板；
    负样本让这类误报在下一轮被挡下来。
    """

    id: str
    hashes: List[int] = Field(default_factory=list)
    brightness: List[float] = Field(default_factory=list)
    fps: float = 1.0
    sample: str = ""
    reason: str = ""
    created: str = ""


class TemplateBank(BaseModel):
    """整个模板库。"""

    version: int = 1
    description: str = ""
    templates: List[AdTemplate] = Field(default_factory=list)
    negatives: List[NegativeTemplate] = Field(default_factory=list)

    def get(self, template_id: str) -> Optional[AdTemplate]:
        for template in self.templates:
            if template.id == template_id:
                return template
        return None

    def next_id(self) -> str:
        """生成下一个模板编号（``tpl-0007`` 这种）。"""
        highest = 0
        for template in self.templates:
            if template.id.startswith("tpl-"):
                try:
                    highest = max(highest, int(template.id[4:]))
                except ValueError:
                    continue
        return f"tpl-{highest + 1:04d}"


class Detection(BaseModel):
    """对一个视频的判定结果。"""

    path: str
    duration: float = 0.0
    size: int = 0
    verdict: Verdict = Verdict.NONE
    template_id: str = ""
    template_label: str = ""
    cut_at: float = 0.0
    """裁剪点（秒）—— 从视频开头算起要切掉多少。"""
    confidence: float = 0.0
    matched_frames: int = 0
    """连续匹配上的帧数。"""
    template_frames: int = 0
    """模板长度（帧）。"""
    coverage: float = 0.0
    """整条模板被命中的比例。"""
    static_ratio: float = 0.0
    """片头里「几乎不动」的画面占比。

    真实内容一直在动，片头卡是静止的 —— 这个比值能挑出
    「可能是广告但还没认出来」的视频，把它们从 200 多个
    「无广告」里拎出来，省得人工去逐个看。
    """
    boundary_delta: float = 0.0
    """画面突变点与匹配终点的偏差（帧）—— 0 表示两者吻合。"""
    mad_value: float = 0.0
    """裁剪点处的帧间差异幅值 —— 越大说明那里真的是画面硬切。"""
    reason: str = ""
    """人类可读的判定依据。"""
    note: str = ""
    """附带提醒（比如检测到外挂字幕）。"""

    @property
    def cuttable(self) -> bool:
        return self.verdict is Verdict.CUT

    @property
    def suspected(self) -> bool:
        """是不是「疑似有广告但没认出来」。"""
        return self.verdict is Verdict.NONE and self.static_ratio >= 0.6


class Cluster(BaseModel):
    """一组共享同一段未知片头的视频。

    这是「抛开人工」的关键结构：人工只需要给**一个**成员划一刀，
    这一组就都能自动裁了。
    """

    id: str
    members: List[str] = Field(default_factory=list)
    frames: int = 0
    """聚类依据的公共前缀帧数。"""
    representative: str = ""
    """建议拿它去学模板的视频。"""
    note: str = ""
    """这一组是什么情况（比如「某模板的变体，前 30 帧相同」）。"""

    @property
    def size(self) -> int:
        return len(self.members)


class AdPlan(BaseModel):
    """一批视频的完整判定/裁剪计划。"""

    root: str = ""
    detections: List[Detection] = Field(default_factory=list)
    clusters: List[Cluster] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    def summary(self) -> Dict[str, int]:
        return {
            "total": len(self.detections),
            "cut": len(self.cut_items),
            "review": len(self.review_items),
            "none": len(self.none_items),
            "skip": len(self.skip_items),
            "suspected": len(self.suspected_items),
            "clusters": len(self.clusters),
        }

    @property
    def cut_items(self) -> List[Detection]:
        return [d for d in self.detections if d.verdict is Verdict.CUT]

    @property
    def review_items(self) -> List[Detection]:
        return [d for d in self.detections if d.verdict is Verdict.REVIEW]

    @property
    def none_items(self) -> List[Detection]:
        return [d for d in self.detections if d.verdict is Verdict.NONE]

    @property
    def skip_items(self) -> List[Detection]:
        return [d for d in self.detections if d.verdict is Verdict.SKIP]

    @property
    def suspected_items(self) -> List[Detection]:
        """没认出来、但开头几乎静止 —— 可能是没见过的片头卡。"""
        return [d for d in self.detections if d.suspected]


class CutOutcome(BaseModel):
    """一次裁剪的执行结果。"""
    path: str
    ok: bool = False
    mode: str = ""
    """``copy``（流拷贝，无损）或 ``reencode``（重编码，帧精确）。"""
    cut_at: float = 0.0
    """实际裁掉的秒数（吸附到关键帧后可能是它）。"""
    snapped: float = 0.0
    """吸附到关键帧带来的偏移（秒）。"""
    before: float = 0.0
    after: float = 0.0
    saved_bytes: int = 0
    reason: str = ""


class AdBoundary(BaseModel):
    """一个文件的人工核验广告边界。

    模板长度是「一支广告有多长」，但站方推广片是多卡片轮播，
    **每支片子插的卡片数不一样**，广告总长也就跟着不一样。
    这条记录把某个具体文件的真实终点钉死，覆盖模板长度的推断值。
    """

    path: str
    """相对库根目录的 POSIX 路径。"""
    cut_at: float = 0.0
    """从片头算起要切掉的秒数 —— 也就是站方广告的终点。"""
    site_ad_end: float = 0.0
    """站方推广片本身结束的位置。

    和 ``cut_at`` 可能不同：中间可能还夹着厂牌片头 / 作品说明卡
    （那部分要不要裁由使用者决定，记在这里备查）。
    """
    verified: str = ""
    """核验日期（YYYY-MM-DD）。"""
    note: str = ""


class BoundaryBook(BaseModel):
    """逐文件人工核验边界表。"""

    version: int = 1
    description: str = ""
    boundaries: List[AdBoundary] = Field(default_factory=list)

    def get(self, path: str) -> Optional[AdBoundary]:
        for item in self.boundaries:
            if item.path == path:
                return item
        return None
