"""大模型抽取结果的结构。每条结论（claim）都要带原文引用，定位不到原文的结论直接丢弃。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["teammate", "slot_function", "team_constraint", "substitution", "cons_effect", "build", "playstyle"]

KIND_ZH = {
    "teammate": "推荐队友",
    "slot_function": "槽位功能分级",
    "team_constraint": "整队限制",
    "substitution": "替补",
    "cons_effect": "命座影响",
    "build": "养成",
    "playstyle": "玩法",
}

# 这两类是视频里很少直说、最容易推断错的，一律要人审（见方案文档“主要风险”）
ALWAYS_REVIEW = {"slot_function", "team_constraint"}

BUILD_FIELDS = ("weapon", "artifact_set", "sands", "goblet", "circlet", "substat", "energy_recharge", "talent")


class Evidence(BaseModel):
    source: Literal["subtitle", "desc", "pinned"] = "subtitle"
    quote: str
    bvid: str = ""
    t: float | None = None  # 字幕里的秒数，由定位步骤填，不信任模型给的时间

    def link(self) -> str:
        base = f"https://www.bilibili.com/video/{self.bvid}/"
        return base + (f"?t={int(self.t)}" if self.t is not None else "")


class Claim(BaseModel):
    kind: Kind
    subject: str  # 核心角色（队伍类结论）或被描述的角色（养成、命座、玩法）
    cons: str | None = None  # 生效条件，如 "cons >= 1"；不分命座就留空
    playstyle: str | None = None
    stance: Literal["for", "against"] = "for"  # against：视频明确说不推荐 / 不行
    inferred: bool = False  # 模型自己推断的（视频没直说），要人审
    data: dict = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    bvid: str = ""
    flags: list[str] = Field(default_factory=list)

    def key(self) -> tuple:
        """跨视频投票用的键：同一个键视为同一条结论。stance 和分级不进键，用来判断“有争议”。"""
        d = self.data
        k: tuple
        if self.kind == "teammate":
            k = (d.get("char"),)
        elif self.kind == "slot_function":
            k = (d.get("tag"),)
        elif self.kind == "team_constraint":
            k = (_squash(d.get("rule") or d.get("description") or ""),)
        elif self.kind == "substitution":
            k = (d.get("original"), d.get("substitute"))
        elif self.kind == "cons_effect":
            k = (d.get("tag") or _squash(d.get("effect") or ""),)
        elif self.kind == "build":
            k = (d.get("field"), _squash(str(d.get("value") or "")))
        else:
            k = (_squash(d.get("name") or ""),)
        return (self.kind, self.subject, self.playstyle or "", _squash(self.cons or ""), *k)

    def value(self) -> str:
        """同一个键下可能不一致的取值。"""
        if self.kind == "slot_function":
            return str(self.data.get("level"))
        return self.stance


class VideoClaims(BaseModel):
    bvid: str
    title: str = ""
    up: str = ""
    published: str = ""
    model: str = ""
    claims: list[Claim] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)  # 丢掉的结论和原因，方便排查
    corrections: dict[str, int] = Field(default_factory=dict)  # 字幕纠错 “错→对”: 次数


def _squash(s: str) -> str:
    return "".join(str(s).split()).lower().replace('"', "'")
