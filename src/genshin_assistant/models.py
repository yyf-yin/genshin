"""知识库的数据结构。YAML 文件加载后按这里的模型校验。"""

from __future__ import annotations

from enum import Enum

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
    id: str
    zh: str
    desc: str = ""


class TagGrant(BaseModel):
    """角色（某个玩法下）能提供的功能。"""

    tag: str
    score: float = Field(1.0, ge=0, le=1)
    when: str | None = None


class Need(BaseModel):
    """角色对队友的需求。队里没人提供时，按 weight 扣分。"""

    tag: str
    weight: float = Field(ge=0, le=1)
    when: str | None = None
    reason: str = ""


class Playstyle(BaseModel):
    id: str = "default"
    zh: str = ""
    when: str | None = None
    provides: list[TagGrant] = []
    needs: list[Need] = []


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
    set: str
    zh: str
    pieces: int


class Build(BaseModel):
    char: str
    playstyle: str = "default"
    cons: str = "0-6"
    weapons: list[WeaponOption]
    artifact_sets: list[list[ArtifactSet]]  # 每个内层列表是一种组合，按优先级排列
    main_stats: dict[str, list[str]]  # sands / goblet / circlet
    substats: list[str]
    energy_recharge: tuple[int, int] | None = None
    talents: list[str] = []
    verified: bool = False

    def cons_range(self) -> tuple[int, int]:
        lo, _, hi = self.cons.partition("-")
        return int(lo), int(hi or lo)
