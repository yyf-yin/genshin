"""从天赋、命座原文里找定位线索，给录入 data/characters/ 的人参考。

只是关键词匹配，结果是「建议」而不是结论：每条都带原文片段，需要人看过再写进知识库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (标签, 正则)。标签必须在 data/tags.yaml 里。
RULES: list[tuple[str, re.Pattern]] = [
    ("shield", re.compile(r"(创造|生成|提供|获得)[^。]{0,12}护盾")),
    ("healer", re.compile(r"(恢复|治疗)[^。]{0,20}生命值|生命值[^。]{0,6}(恢复|治疗)")),
    ("elemental_shred", re.compile(r"元素抗性降低|抗性降低")),
    ("team_dmg_buff", re.compile(r"提升队伍中[^。]{0,30}(伤害|攻击力)|队伍中[^。]{0,24}角色[^。]{0,40}(伤害提升|伤害加成|攻击力提升|攻击力提高|擢升)")),
    ("em_share", re.compile(r"队伍中[^。]{0,24}角色[^。]{0,24}元素精通提升")),
    ("energy_battery", re.compile(r"(为|使)?队伍中[^。]{0,30}恢复[^。]{0,8}元素能量")),
    ("grouping", re.compile(r"牵引|聚集")),
    ("interruption_res", re.compile(r"抗打断")),
]
OFF_FIELD = re.compile(r"(处于|位于)队伍后台时[^。]{0,8}(依然|也能|也会|仍)")
CONS_LEVEL = re.compile(r"^c(\d)$")


@dataclass
class Hint:
    tag: str
    source: str  # 例：元素战技「事象数式·游衍解析」、固有天赋「淑女的行事准则」、2 命
    when: str | None
    evidence: str


def _snippet(text: str, m: re.Match, width: int = 40) -> str:
    start = max(0, text.rfind("。", 0, m.start()) + 1, m.start() - width)
    end = min(len(text), m.end() + width)
    stop = text.find("。", m.end())
    if stop != -1:
        end = min(end, stop + 1)
    return text[start:end].replace("\n", " ").replace("**", "").strip()


def _texts(char: dict):
    names = {"normal_attack": "普通攻击", "skill": "元素战技", "special": "特殊技能", "burst": "元素爆发"}
    for key, t in char.get("talents", {}).items():
        yield f"{names.get(key, key)}「{t['name']}」", None, t["description"]
    for p in char.get("passives", []):
        yield f"固有天赋「{p['name']}」", None, p["description"]
    for key, c in char.get("constellations", {}).items():
        n = int(CONS_LEVEL.match(key).group(1))
        yield f"{n} 命「{c['name']}」", f"cons >= {n}", c["description"]
    if char.get("witch_quest"):
        yield "魔女的课业（魔导·秘仪）", None, char["witch_quest"]


def suggest(char: dict) -> list[Hint]:
    """每个标签只保留最早出现的那条（天赋优先于命座）。"""
    found: dict[str, Hint] = {}
    off_field: Hint | None = None
    for source, when, text in _texts(char):
        for tag, pattern in RULES:
            if tag in found:
                continue
            for m in pattern.finditer(text):
                # 「队伍中每有一位冰元素角色，薇斯纳的攻击力提升」是给自己加，不算全队增益
                if tag == "team_dmg_buff" and char["zh"] in m.group(0):
                    continue
                found[tag] = Hint(tag, source, when, _snippet(text, m))
                break
        if off_field is None and when is None and (m := OFF_FIELD.search(text)):
            off_field = Hint("off_field", source, None, _snippet(text, m))
    hints = list(found.values())
    if off_field:
        hints.append(off_field)
    return hints
