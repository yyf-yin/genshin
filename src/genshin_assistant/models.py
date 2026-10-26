"""知识库的数据结构。YAML 文件加载后按这里的模型校验。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Element(str, Enum):
    pyro = "pyro"
    hydro = "hydro"
    electro = "electro"
    cryo = "cryo"
    anemo = "anemo"
    geo = "geo"
    dendro = "dendro"


class Level(str, Enum):
    """槽位功能的级别：core 缺了阵容不成立，major/minor 按权重计分。"""

    core = "core"
    major = "major"
    minor = "minor"


class Tag(BaseModel):
    """param 不为空时，用法是「id:参数」，例如 reaction_buff:stellar_swirl、element:cryo。"""

    id: str
    zh: str
    desc: str = ""
    param: Literal["reaction", "element", "zone"] | None = None


class TagGrant(BaseModel):
    """角色（某个玩法下）能提供的功能。"""

    tag: str
    score: float = Field(1.0, ge=0, le=1)
    when: str | None = None
    value: float | None = None  # 增益的大致数值：百分比写小数（0.2 = 20%），固定值写点数
    evidence: str = ""  # 天赋原文片段
    source: str = ""


class Need(BaseModel):
    """角色对队友的需求。队友里提供者不够 count 个时，按 weight 扣分。"""

    tag: str
    weight: float = Field(ge=0, le=1)
    count: int = Field(1, ge=1)
    when: str | None = None
    reason: str = ""
    evidence: str = ""
    source: str = ""


class Avoid(BaseModel):
    """角色排斥的队友功能，例如莱欧斯利排斥治疗。队友里有人提供时，按 weight 扣分。"""

    tag: str
    weight: float = Field(ge=0, le=1)
    when: str | None = None
    reason: str = ""
    evidence: str = ""
    source: str = ""


class TeamRule(BaseModel):
    """角色自带的整队规则，写法同模板的 constraints。hard 不满足时队伍不成立，否则按 weight 扣分。"""

    id: str
    rule: str
    hard: bool = False
    weight: float = Field(0.0, ge=0, le=1)
    when: str | None = None
    reason: str = ""
    evidence: str = ""
    source: str = ""


class Playstyle(BaseModel):
    id: str = "default"
    zh: str = ""
    when: str | None = None
    field: Literal["on_field", "off_field", "flex"] | None = None  # 站场 / 后台 / 都可以（不用 on/off：YAML 会读成布尔值）
    provides: list[TagGrant] = []
    traits: list[TagGrant] = []  # 只给自己的能力，例如自带抗打断；满足自己的同名需求
    needs: list[Need] = []
    avoids: list[Avoid] = []
    team_rules: list[TeamRule] = []


class Character(BaseModel):
    id: str
    zh: str
    aliases: list[str] = []
    element: Element
    weapon: str
    rarity: int
    playstyles: list[Playstyle]
    verified: bool = False

    def playstyle(self, pid: str) -> Playstyle:
        for p in self.playstyles:
            if p.id == pid:
                return p
        raise KeyError(f"{self.id} 没有玩法 {pid}")


class EquipmentBuff(BaseModel):
    """会给队友加成的圣遗物套装或武器。角色的配装选了它，引擎就把这些功能加到角色身上。"""

    kind: Literal["artifact_set", "weapon"]
    zh: str
    pieces: int = 4  # 套装件数，武器忽略
    provides: list[TagGrant]
    note: str = ""
    verified: bool = False


class CharRef(BaseModel):
    char: str
    playstyle: str = "default"


class Candidate(CharRef):
    score: float = Field(1.0, ge=0, le=1)


class SlotFunction(BaseModel):
    tag: str
    level: Level
    weight: float = Field(0.0, ge=0, le=1)


class Slot(BaseModel):
    id: str
    zh: str = ""
    fixed: CharRef | None = None
    functions: list[SlotFunction] = []
    candidates: list[Candidate] = []
    # 允许引擎自动考虑候选名单之外、满足全部 core 功能的角色
    auto_candidates: bool = True

    def core_tags(self) -> list[str]:
        return [f.tag for f in self.functions if f.level == Level.core]


class TeamConstraint(BaseModel):
    id: str
    rule: str
    reason: str


class SubstitutionOverride(BaseModel):
    """攻略里明确给出的替补结论，优先于自动推导。"""

    original: str
    substitute: str
    reason: str
    build_changes: list[str] = []
    source: str = ""


class TeamTemplate(BaseModel):
    id: str
    zh: str
    core: CharRef
    slots: list[Slot]
    constraints: list[TeamConstraint] = []
    substitutions: list[SubstitutionOverride] = []
    source: str = ""
    verified: bool = False


class WeaponOption(BaseModel):
    id: str
    zh: str
    rank: int
    source: str = ""  # limited_5star / standard_5star / gacha_4star / craftable / event / bp
    note: str = ""


class ArtifactSet(BaseModel):
    set: str  # mixed = 散搭（任意两件套），zh 写成「攻击力+18%」这样的效果
    zh: str
    pieces: int
    note: str = ""


class Build(BaseModel):
    char: str
    playstyle: str = "default"
    cons: str = "0-6"
    zh: str = ""  # 方案名，例如「星超导主C」
    weapons: list[WeaponOption]
    artifact_sets: list[list[ArtifactSet]]  # 每个内层列表是一种组合，按优先级排列
    main_stats: dict[str, list[str]]  # sands / goblet / circlet，按优先级排列
    substats: list[str]  # 按优先级排列
    energy_recharge: tuple[int, int] | None = None  # 常规队伍的充能区间（%）
    energy_recharge_notes: list[str] = []  # 充能随队伍、武器、命座的变化
    stat_targets: list[str] = []  # 面板目标，例如「暴击率 70%~75%」
    talents: list[str] = []  # 天赋优先级：normal / skill / burst
    notes: list[str] = []
    evidence: list[str] = []  # 出处：UP 主、BV 号、时间点、原话
    verified: bool = False

    def cons_range(self) -> tuple[int, int]:
        lo, _, hi = self.cons.partition("-")
        return int(lo), int(hi or lo)
