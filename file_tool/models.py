"""数据模型：改名规则与改名计划。

规则分三层，串成一条流水线：

1. ``PreprocessRule``（预处理/剥离）
   对原始目录名做**与番号知识无关**的噪声剥离，例如去掉水印域名 ``aavv38.xyz@``、
   去掉重复副本标记 ``(1)`` / `` 2`` / ``-2``。被剥离的文本用命名分组捕获，
   例如 ``(?P<domain>...)``、``(?P<dup>...)``，供后续合成阶段回填。

2. ``MatchRule``（番号匹配）
   在剥离后的核心串上匹配番号形态。``pattern`` 用命名分组表达，
   例如 ``(?P<studio>[A-Z]{2,6})-(?P<num>\\d{3})``。

3. 合成阶段
   用 ``template`` 把匹配分组回填成规范名，``{raw}`` 代表剥离后的核心串；
   重复副本标记按 ``dup_suffix`` 回贴（例如 ``" {dup}"``），避免改名后互相覆盖。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# 合成阶段可用的全局占位符
RAW = "raw"
DUP = "dup"


class Action(str, Enum):
    """单个目录的处置动作。"""

    RENAME = "rename"      # 会改名
    KEEP = "keep"          # 已是目标名，不动
    SKIP = "skip"          # 没匹配到任何规则，跳过
    CONFLICT = "conflict"  # 目标名已被占用或批次内重名，跳过


class PreprocessRule(BaseModel):
    """噪声剥离规则。

    ``pattern`` 是正则，``replacement`` 是替换串（默认空串即删除匹配到的文本）。
    ``capture`` 指明把被剥离的文本存进哪个命名分组（该组必须存在于 pattern 中）。

    ``conditional=True`` 表示这条规则**只在剥离后仍能匹配到番号规则时才生效**。
    这是为了区分两种横杠：
    ``MAAN-961`` 的横杠属于番号本身，不能剥；而 ``DOW24072205-2`` 的 ``-2``
    是重复副本标记，该剥。两者字形相同，只能靠「剥完是否还是合法番号」来判断。
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1)
    pattern: str
    replacement: str = ""
    capture: str = ""
    description: str = ""
    conditional: bool = False

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"无效正则 {v!r}: {exc}") from exc
        return v

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


class MatchRule(BaseModel):
    """番号匹配规则。

    ``pattern`` 建议使用命名分组；``template`` 用 ``{分组名}`` 回填，
    ``{raw}`` 代表剥离后的核心串。``dup_suffix`` 控制重复副本标记如何回贴，
    例如 ``" {dup}"`` 把 ``SDMM030AC(1)`` 还原成 ``SDMM030AC (1)``。
    ``None`` 表示不回贴（标记被丢弃）。
    """

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1)
    pattern: str
    template: str = "{raw}"
    dup_suffix: Optional[str] = None
    description: str = ""
    priority: int = 0

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"无效正则 {v!r}: {exc}") from exc
        return v

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


class RuleSet(BaseModel):
    """一整套改名规则。"""

    model_config = {"extra": "forbid"}

    version: int = 1
    description: str = ""
    preprocess: List[PreprocessRule] = Field(default_factory=list)
    matches: List[MatchRule] = Field(default_factory=list)
    exclude: List[str] = Field(default_factory=list)
    """永不视为番号的名称形态（正则）。

    匹配对象是**剥掉水印域名之后的核心串**。例如 ``FLUP-1131428``、
    ``FLUP113730`` —— FLUP 是国产内容的编号，不是日本番号。

    命中排除的名字一律不当番号处理。两个直接后果：

    - ``rename`` 不会去改它们（国产内容不该套番号规范）
    - ``isolate`` 会把它们判为「不符合番号规范」而挪到国产文件夹，
      番号库里就不留国产内容了

    排除是「整类形态」的判断，必须写在规则文件里而不是代码里 ——
    哪个前缀属于国产是领域知识，会随资料来源变化。

    **匹配一律忽略大小写**（见 :meth:`is_excluded`）。国产编号的写法
    本身就没有大小写规范：``xfxfz14468`` 和 ``XFXFZ14468`` 是同一个站
    的同一种编号。如果排除是大小写敏感的，那么统一大写之后，
    国产内容会因为「变成了大写形态」而不再命中排除名单 ——
    国产内容就这么混进番号库了。
    """
    dup_sources: List[str] = Field(default_factory=lambda: ["dup", "wdup"])
    """回贴标记的取值优先级：按顺序取第一个存在的捕获组，填入 ``{dup}`` 槽位。"""

    uppercase: bool = True
    """规范名是否统一大写。

    **为什么必须统一**：整理出来的库最终是要放到 exFAT 上的，而
    exFAT / Windows NTFS / macOS APFS（默认）全都大小写不敏感 ——
    在它们上面 ``club-810`` 与 ``CLUB-810`` 是同一个名字。
    在大小写敏感的卷（Linux ext4）上整理出来的库，两个只差大小写的
    目录能共存；拷到 exFAT 上就撞了。统一大写把这个问题从根上消掉，
    而不是留给「拷贝时报错」。

    **大写只作用于规范名本身**（厂牌 + 序号 + 版本后缀）：

    - 扩展名原样保留（``.mp4`` 不动）—— 刮削器和播放器对扩展名大小写
      敏感的地方不少，改它收益为零、风险不小
    - 噪声后缀原样保留（``_en`` / ``[4K]`` 这类标记是有意义的信息）
    - 中文标题文件一律不动 —— 那些名字是内容标识，不是番号
    """

    @field_validator("exclude")
    @classmethod
    def _check_exclude(cls, v: List[str]) -> List[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"无效排除正则 {pattern!r}: {exc}") from exc
        return v

    def is_excluded(self, core: str) -> bool:
        """核心串是否命中排除名单。

        **忽略大小写**。国产编号的写法本身就没有大小写规范，
        大写化之后如果排除名单还是大小写敏感的，国产内容会因为
        「变成了大写形态」而漏掉排除 —— 正好和统一大写的目标相反。
        """
        return any(re.search(pattern, core, re.IGNORECASE) for pattern in self.exclude)

    def ordered_matches(self) -> List[MatchRule]:
        """按 priority 降序返回匹配规则（同优先级保持文件内顺序）。"""
        return sorted(self.matches, key=lambda r: -r.priority)


class PlanItem(BaseModel):
    """一个待改名目录的完整决策记录。"""

    original: str
    stripped: str = ""
    target: str = ""
    base: str = ""
    """不含重复副本标记的基础名 —— 「同一作品」的判定键，merge 阶段按它分组。"""
    action: Action = Action.SKIP
    rule: str = ""
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.action is Action.RENAME


class Plan(BaseModel):
    """一次改名任务的完整计划。"""

    root: str
    items: List[PlanItem] = Field(default_factory=list)

    @property
    def rename_items(self) -> List[PlanItem]:
        return [i for i in self.items if i.action is Action.RENAME]

    @property
    def keep_items(self) -> List[PlanItem]:
        return [i for i in self.items if i.action is Action.KEEP]

    @property
    def skip_items(self) -> List[PlanItem]:
        return [i for i in self.items if i.action is Action.SKIP]

    @property
    def conflict_items(self) -> List[PlanItem]:
        return [i for i in self.items if i.action is Action.CONFLICT]

    def summary(self) -> Dict[str, int]:
        return {
            "total": len(self.items),
            "rename": len(self.rename_items),
            "keep": len(self.keep_items),
            "skip": len(self.skip_items),
            "conflict": len(self.conflict_items),
        }
