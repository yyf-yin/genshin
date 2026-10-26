"""从天赋、命座原文里找定位线索，给录入 data/characters/ 的人参考。

只是关键词匹配，结果是「建议」而不是结论：每条都带原文片段，需要人看过再写进知识库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ELEMENTS = {"火": "pyro", "水": "hydro", "雷": "electro", "冰": "cryo", "风": "anemo", "岩": "geo", "草": "dendro"}
_TEAM = r"队伍中[^。]{0,24}角色[^。]{0,40}"
# (标签, 正则)。标签必须能通过 data/tags.yaml 的校验；带 {el} 的从匹配到的元素名换成元素 id。
RULES: list[tuple[str, re.Pattern]] = [
    ("shield", re.compile(r"(创造|生成|提供|获得|形成|召唤)[^。]{0,12}护盾|\S{2,6}护盾的(伤害)?吸收量")),
    ("healer", re.compile(r"(恢复|治疗)[^。]{0,20}生命值|生命值[^。]{0,6}(恢复|治疗)")),
    ("universal_shred", re.compile(r"所有元素抗性[^。]{0,12}降低|元素抗性与物理抗性降低")),
    ("shred:{el}", re.compile(r"((?:[火水雷冰风岩草]元素抗性[与和、]?)+)降低")),
    ("def_shred", re.compile(r"防御力降低")),
    ("buff:crit_rate", re.compile(_TEAM + r"暴击率提升")),
    ("buff:crit_dmg", re.compile(_TEAM + r"暴击伤害提升")),
    ("buff:em", re.compile(_TEAM + r"元素精通提升")),
    ("buff:atk_flat", re.compile(r"攻击力提升[^。]{0,16}相当于[^。]{0,12}基础攻击力|基于[^。]{0,8}基础攻击力[^。]{0,16}攻击力加成")),
    ("buff:atk_pct", re.compile(r"提升队伍中[^。]{0,30}攻击力|" + _TEAM + r"攻击力(提升|提高)")),
    ("buff:elevate", re.compile(_TEAM + r"擢升")),
    ("buff:dmg_bonus", re.compile(r"提升队伍中[^。]{0,30}伤害|" + _TEAM + r"(伤害提升|伤害加成)")),
    ("energy_battery", re.compile(r"(为|使)?队伍中[^。]{0,30}恢复[^。]{0,8}元素能量")),
    ("grouping", re.compile(r"牵引|聚集")),
    ("interruption_res", re.compile(r"抗打断")),
    ("hp_drain", re.compile(r"(消耗|扣除)[^。]{0,16}(队伍中|附近)[^。]{0,16}生命值")),
]
# 只给自己加的不算全队增益：「队伍中每有一位冰元素角色，薇斯纳的攻击力提升」
_SELF_BUFF = {"buff:atk_flat", "buff:crit_rate", "buff:crit_dmg", "buff:em", "buff:atk_pct", "buff:elevate", "buff:dmg_bonus"}
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
                # 「基于班尼特的基础攻击力」说的是数值来源，不算给自己加
                if tag in _SELF_BUFF and char["zh"] in re.sub(f"(基于|相当于){char['zh']}", "", m.group(0)):
                    continue
                names = [tag.format(el=ELEMENTS[e]) for e in re.findall(r"([火水雷冰风岩草])元素", m.group(1))] if "{el}" in tag else [tag]
                for name in names:
                    if name not in found:
                        found[name] = Hint(name, source, when, _snippet(text, m))
                break
        if off_field is None and when is None and (m := OFF_FIELD.search(text)):
            off_field = Hint("off_field", source, None, _snippet(text, m))
    hints = list(found.values())
    if off_field:
        hints.append(off_field)
    return hints


def draft(char: dict) -> dict:
    """把线索整理成 data/characters/ 的格式，给人审核后挪进知识库。分数一律 0.4，审核时再调。"""
    hints = suggest(char)
    provides = []
    if "输出型" in (char.get("intro") or "").split("\n")[0]:
        provides.append({"tag": "main_dps", "score": 0.7, "evidence": "官方定位：" + char["intro"].split("\n")[0], "source": "hints"})
    for h in hints:
        if h.tag == "off_field":
            continue
        grant = {"tag": h.tag, "score": 0.4}
        if h.when:
            grant["when"] = h.when
        grant["evidence"] = f"{h.source}：{h.evidence}"
        grant["source"] = "hints"
        provides.append(grant)
    playstyle: dict = {"id": "default", "zh": "待定"}
    if any(h.tag == "off_field" for h in hints):
        playstyle["field"] = "off_field"
    playstyle["provides"] = provides
    return {
        "id": char["id"],
        "zh": char["zh"],
        "aliases": [char["en"]] if char.get("en") else [],
        "element": char["element"],
        "weapon": char["weapon"],
        "rarity": char["rarity"],
        "playstyles": [playstyle],
        "verified": False,
    }
