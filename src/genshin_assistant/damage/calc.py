"""按场景文件算伤害：面板 → 每一下伤害的各个乘区 → 不暴击 / 暴击 / 期望。

场景文件写法见 examples/damage/ 和 README「伤害计算」一节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..gamedata.store import GameData
from . import formulas as f
from .stats import (CATEGORIES, ELEMENT_ZH, FLOWER_HP, LUNAR_REACTIONS, MAIN_STATS, PLUME_ATK, REACTION_ZH,
                    SPECIAL_REACTIONS, SUBSTAT_ROLL, Stats, character_stats, parse_value, set_2pc_stats, stat_key, weapon_stats)
from .talents import SLOT_ZH, Talent, find_talent, load_talents

SLOT_KEYS = {"普攻": "normal_attack", "普通攻击": "normal_attack", "a": "normal_attack", "normal_attack": "normal_attack",
             "战技": "skill", "元素战技": "skill", "e": "skill", "skill": "skill",
             "爆发": "burst", "元素爆发": "burst", "大招": "burst", "q": "burst", "burst": "burst"}
# 特殊反应伤害的元素（抗性按这个算）。星扩散的「星辉风旋」爆炸和角色技能里的星扩散伤害都是冰伤
SPECIAL_ELEMENT = {"lunar_charged": "electro", "lunar_bloom": "dendro", "lunar_crystallize": "geo",
                   "stellar_superconduct": "cryo", "stellar_swirl": "cryo"}
TRANSFORMATIVE_ELEMENT = {"overloaded": "pyro", "superconduct": "cryo", "electro_charged": "electro",
                          "shattered": "physical", "bloom": "dendro", "hyperbloom": "dendro", "burgeoning": "dendro",
                          "burning": "pyro"}
REACTION_KEYS = {v: k for k, v in REACTION_ZH.items()}


@dataclass
class Zone:
    name: str
    value: float
    note: str = ""


@dataclass
class HitResult:
    label: str
    element: str
    zones: list[Zone]
    non_crit: float
    crit: float
    average: float
    times: int = 1
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    character: str
    panel: dict[str, float]
    sources: list[tuple[str, str, float]]
    hits: list[HitResult]
    warnings: list[str]

    @property
    def total_average(self) -> float:
        return sum(h.average * h.times for h in self.hits)


@dataclass
class Enemy:
    level: int = 100
    res: dict[str, float] = field(default_factory=dict)  # 元素 → 抗性，没写的按 default_res
    default_res: float = 0.10

    def resistance(self, element: str) -> float:
        return self.res.get(element, self.default_res)


def _reaction_key(name: str | None) -> str | None:
    if not name:
        return None
    key = REACTION_KEYS.get(name, name)
    if key not in REACTION_ZH:
        raise KeyError(f"不认识的反应：{name}")
    return key


def _enemy(spec: dict | None) -> tuple[Enemy, Stats]:
    """敌人等级、抗性；敌人这一节里也可以直接写减抗、减防（算进全局增益）。"""
    spec = dict(spec or {})
    enemy = Enemy(level=int(spec.pop("level", spec.pop("等级", 100))))
    res = spec.pop("res", spec.pop("抗性", 10))
    if isinstance(res, dict):
        for e, v in res.items():
            e = next((k for k, z in ELEMENT_ZH.items() if e in (k, z, f"{z}元素")), e)
            enemy.res[e] = parse_value("res", v)
    else:
        enemy.default_res = parse_value("res", res)
    debuffs = Stats()
    debuffs.add_mapping(spec, "敌人")
    return enemy, debuffs


def build_panel(gd: GameData, sc: dict, warnings: list[str]) -> Stats:
    char = gd.character(sc["character"])
    buff_sources = " ".join(str(b.get("source", "")) for b in sc.get("buffs") or [])
    st = character_stats(char)
    w = sc.get("weapon")
    if w:
        w = {"name": w} if isinstance(w, str) else w
        row = gd.weapon(w["name"])
        if row["type"] != char["weapon"]:
            warnings.append(f"{row['zh']} 是{row['type']}，{char['zh']}用的是{char['weapon']}")
        st = st.merged(weapon_stats(row))
        if row.get("r1") and row["zh"] not in buff_sources:
            warnings.append(f"武器特效没有自动计算，按需要写进 buffs：{row['zh']}「{row.get('passive', '')}」R1：{row['r1']}")
    art = sc.get("artifacts") or {}
    if art:
        st.add("hp_flat", FLOWER_HP, "生之花")
        st.add("atk_flat", PLUME_ATK, "死之羽")
        for slot, zh in (("sands", "时之沙"), ("goblet", "空之杯"), ("circlet", "理之冠")):
            name = art.get(slot) or art.get(zh)
            if name:
                key = stat_key(name)
                if key not in MAIN_STATS:
                    raise KeyError(f"{zh}没有{name}这个主词条")
                st.add(key, MAIN_STATS[key], f"{zh}主词条")
        st.add_mapping(art.get("substats") or art.get("副词条"), "副词条")
        for name, n in (art.get("substat_rolls") or art.get("副词条次数") or {}).items():
            key = stat_key(name)
            st.add(key, SUBSTAT_ROLL[key] * float(n), f"副词条×{n}")
        for s in art.get("sets") or art.get("套装") or []:
            name, pieces = (s["name"], int(s.get("pieces", 4))) if isinstance(s, dict) else (str(s).rstrip("24"), int(str(s)[-1]) if str(s)[-1] in "24" else 4)
            row = gd.artifact_set(name)
            bonus = set_2pc_stats(row.get("2pc", ""))
            if bonus is None:
                warnings.append(f"{row['zh']} 2 件套有条件，没有自动计算：{row.get('2pc')}")
            else:
                for k, v in bonus.items():
                    st.add(k, v, f"{row['zh']}2件套")
            if pieces >= 4 and row.get("4pc") and row["zh"] not in buff_sources:
                warnings.append(f"{row['zh']} 4 件套没有自动计算，按需要写进 buffs：{row['4pc']}")
    for b in sc.get("buffs") or []:
        st.add_mapping(b.get("stats"), b.get("source", "增益"))
    return st


def _category(t: Talent, override: str | None) -> str:
    if override:
        return next((k for k, v in CATEGORIES.items() if override in (k, v)), override)
    if t.slot == "skill":
        return "skill"
    if t.slot == "burst":
        return "burst"
    if "重击" in t.name:
        return "charged"
    if "坠" in t.name or "下落" in t.name:
        return "plunge"
    return "normal"


def _element(char: dict, t: Talent | None, hit_element: str | None, override: str | None) -> str:
    if override:
        return next((k for k, z in ELEMENT_ZH.items() if override in (k, z, f"{z}元素")), override)
    if hit_element:
        return hit_element
    if t is not None and t.slot == "normal_attack" and char["weapon"] != "catalyst":
        return "physical"
    return char["element"]


def _res_zone(enemy: Enemy, st: Stats, element: str) -> Zone:
    res = enemy.resistance(element) - st.get("res_shred") - st.get(f"{element}_res_shred")
    return Zone("抗性区", f.resistance_multiplier(res), f"{ELEMENT_ZH[element]}抗 {res:.0%}")


def _finish(label: str, element: str, zones: list[Zone], cr: float, cd: float, times: int, crit: bool = True) -> HitResult:
    non_crit = 1.0
    for z in zones:
        non_crit *= z.value
    if not crit:
        return HitResult(label, element, zones, non_crit, non_crit, non_crit, times)
    crit_mult, avg_mult = f.crit_multipliers(cr, cd)
    zones.append(Zone("暴击区", avg_mult, f"暴击率 {min(cr, 1):.1%}，暴击伤害 {cd:.1%}，表中为期望"))
    return HitResult(label, element, zones, non_crit, non_crit * crit_mult, non_crit * avg_mult, times)


def talent_hit(gd: GameData, char: dict, talent: Talent, level: int, spec: dict, st: Stats, enemy: Enemy,
               char_level: int) -> list[HitResult]:
    variant = int(spec.get("variant", 0))
    hits = talent.hits(level, variant)
    if not hits:
        raise ValueError(f"「{talent.name}」不是伤害倍率：{talent.template}")
    category = _category(talent, spec.get("category"))
    reaction = _reaction_key(spec.get("reaction"))
    special = _reaction_key(spec.get("special") or spec.get("lunar") or spec.get("stellar")) or next(
        (r for r in LUNAR_REACTIONS if f"{REACTION_ZH[r]}伤害" in talent.name), None)  # 「中间段月感电伤害」
    lv_mult = gd.level_multiplier(char_level)
    out = []
    for i, hit in enumerate(hits):
        label = f"{SLOT_ZH[talent.slot]}·{talent.name}" + (f"（第{i + 1}段）" if len(hits) > 1 else "")
        element = _element(char, talent, hit.element, spec.get("element"))
        talent_part = sum(coef * st.scaling_stat(stat) for stat, coef in hit.scaling.items())
        desc = " + ".join(f"{coef:.2%}×{_stat_zh(stat)} {st.scaling_stat(stat):.0f}" for stat, coef in hit.scaling.items())
        base_mult = float(spec.get("base_multiplier", 1.0))
        cr = st.get("cr") + st.get(f"{category}_cr")
        cd = st.get("cd") + st.get(f"{category}_cd")
        times = int(spec.get("times", 1))
        if special:
            coef = f.SPECIAL[special]["direct"]
            element = SPECIAL_ELEMENT[special]
            zones = [Zone("基础伤害", talent_part * base_mult + hit.flat, desc),
                     Zone(f"{REACTION_ZH[special]}系数", coef, "角色技能直接造成的反应伤害")]
            zones += _special_zones(special, st, enemy)
            out.append(_finish(label + f"（{REACTION_ZH[special]}）", element, zones, cr, cd, times))
            continue
        base = talent_part * base_mult + hit.flat + st.get("flat_dmg") + st.get(f"{category}_flat_dmg")
        zones = [Zone("基础伤害", base, desc + (f"，倍率×{base_mult}" if base_mult != 1 else ""))]
        if reaction in f.ADDITIVE:
            extra = f.additive_damage(reaction, lv_mult, st.get("em"), st.get(f"{reaction}_bonus"))
            zones[0] = Zone("基础伤害", base + extra, zones[0].note + f"，{REACTION_ZH[reaction]}额外 {extra:.0f}")
        dmg = st.get("dmg") + st.get(f"{element}_dmg") + st.get(f"{category}_dmg")
        zones += [
            Zone("增伤区", 1 + dmg),
            Zone("防御区", f.defense_multiplier(char_level, enemy.level, st.get("def_reduction"), st.get("def_ignore")),
                 f"角色 {char_level} 级，敌人 {enemy.level} 级"),
            _res_zone(enemy, st, element),
        ]
        if reaction in ("vaporize", "melt"):
            zones.append(Zone("增幅反应区", f.amplifying_multiplier(reaction, element, st.get("em"), st.get(f"{reaction}_bonus")),
                              REACTION_ZH[reaction]))
        elif reaction and reaction not in f.ADDITIVE:
            raise ValueError(f"{REACTION_ZH[reaction]} 不是乘在技能上的反应；剧变/月曜反应单独写一条 reaction")
        if st.get("elevate"):
            zones.append(Zone("擢升区", 1 + st.get("elevate")))
        suffix = f"（{REACTION_ZH[reaction]}）" if reaction else ""
        out.append(_finish(label + suffix, element, zones, cr, cd, times))
    return out


def _special_zones(reaction: str, st: Stats, enemy: Enemy) -> list[Zone]:
    """月曜/星烁共用：基础伤害提升、反应加成（精通 + 加成）、擢升、抗性。不吃防御和普通增伤。"""
    family = "lunar" if reaction in LUNAR_REACTIONS else "stellar"
    family_zh = "月曜" if family == "lunar" else "星烁"
    em = st.get("em")
    return [
        Zone(f"{family_zh}基础伤害提升", 1 + st.get(f"{family}_base_bonus") + st.get(f"{reaction}_base_bonus")),
        Zone(f"{family_zh}反应加成", 1 + f.em_bonus(em, "lunar") + st.get(f"{family}_bonus") + st.get(f"{reaction}_bonus"),
             f"精通 {em:.0f}"),
        Zone("擢升区", 1 + st.get("elevate")),
        _res_zone(enemy, st, SPECIAL_ELEMENT[reaction]),
    ]


def reaction_hit(gd: GameData, spec: dict, st: Stats, enemy: Enemy, char_level: int) -> HitResult:
    """单独算一次剧变/月曜反应的伤害（按这个角色的等级和面板）。"""
    reaction = _reaction_key(spec["reaction"])
    lv_mult = gd.level_multiplier(char_level)
    times = int(spec.get("times", 1))
    em = st.get("em")
    if reaction in f.SPECIAL:
        coef = spec.get("coefficient") or f.SPECIAL[reaction]["reaction"]
        if coef is None:
            raise NotImplementedError(f"{REACTION_ZH[reaction]}本身不直接造成伤害（或系数未知），可以用 coefficient 指定")
        zones = [Zone("基础伤害", float(coef) * lv_mult, f"系数 {coef} × 等级系数 {lv_mult:.1f}")]
        zones += _special_zones(reaction, st, enemy)
        return _finish(f"{REACTION_ZH[reaction]}反应", SPECIAL_ELEMENT[reaction], zones, st.get("cr"), st.get("cd"), times)
    if reaction not in f.TRANSFORMATIVE:
        raise ValueError(f"{REACTION_ZH[reaction]} 要写在技能那一条上（reaction: {REACTION_ZH[reaction]}）")
    element = TRANSFORMATIVE_ELEMENT.get(reaction) or _element({}, None, None, spec.get("element"))
    if reaction == "swirl" and not spec.get("element"):
        raise ValueError("扩散要写 element：被扩散的元素")
    zones = [
        Zone("基础伤害", f.TRANSFORMATIVE[reaction] * lv_mult,
             f"系数 {f.TRANSFORMATIVE[reaction]} × 等级系数 {lv_mult:.1f}"),
        Zone("剧变反应加成", 1 + f.em_bonus(em, "transformative") + st.get(f"{reaction}_bonus"), f"精通 {em:.0f}"),
        _res_zone(enemy, st, element),
    ]
    if st.get("elevate"):
        zones.append(Zone("擢升区", 1 + st.get("elevate")))
    return _finish(f"{REACTION_ZH[reaction]}反应", element, zones, st.get("cr"), st.get("cd"), times,
                   crit=bool(spec.get("can_crit")))


def _stat_zh(stat: str) -> str:
    return {"atk": "攻击力", "hp": "生命值", "def": "防御力", "em": "元素精通"}[stat]


def calculate(gd: GameData, sc: dict) -> Report:
    warnings: list[str] = []
    char = gd.character(sc["character"])
    char_level = int(sc.get("level", 90))
    if char_level != 90:
        warnings.append("角色白值只有 90 级的数据；等级只影响防御区和反应的等级系数")
    panel = build_panel(gd, sc, warnings)
    enemy, debuffs = _enemy(sc.get("enemy"))
    panel = panel.merged(debuffs)
    levels = {SLOT_KEYS.get(str(k).lower(), k): int(v) for k, v in (sc.get("talents") or {}).items()}
    talents = load_talents(gd.talent_multipliers.get(char["id"], {}))
    results: list[HitResult] = []
    for spec in sc.get("hits") or []:
        st = panel
        if spec.get("buffs"):
            extra = Stats()
            extra.add_mapping(spec["buffs"], "这一下的增益")
            st = panel.merged(extra)
        if spec.get("talent"):
            t = find_talent(talents, spec["talent"])
            level = int(spec.get("level", levels.get(t.slot, 10)))
            results += talent_hit(gd, char, t, level, spec, st, enemy, char_level)
        elif spec.get("reaction"):
            results.append(reaction_hit(gd, spec, st, enemy, char_level))
        else:
            raise ValueError("每一条 hits 要写 talent（技能）或 reaction（单独的反应）")
    shown = {"生命值": panel.hp, "攻击力": panel.atk, "防御力": panel.defense, "元素精通": panel.get("em"),
             "暴击率": panel.get("cr"), "暴击伤害": panel.get("cd"), "元素充能效率": 1 + panel.get("er")}
    return Report(char["zh"], shown, panel.sources, results, warnings)


def load_scenario(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
