"""队伍伤害：一轮标准循环里全队的期望总伤害，给配队排序用。

每个角色在 data/damage/profiles/ 里有一份「循环」：一轮放哪些技能、各打几下、触发什么反应，
以及给自己和队友的增益（带生效条件）。会给队友加成的装备沿用 data/equipment_buffs.yaml 的 value。
面板按 builds/ 的首选配装（组队时让出增益装备后的那一套）和统一的副词条估算。

只用来比较「同一个主C换不同队友」谁更高，绝对数值不代表实战伤害。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..conditions import evaluate
from ..gamedata.store import GameData
from . import formulas as f
from .calc import Enemy, HitResult, SLOT_KEYS, reaction_hit, talent_hit
from .stats import Stats, character_stats, parse_value, set_2pc_stats, stat_key, weapon_stats
from .stats import FLOWER_HP, MAIN_STATS, PLUME_ATK, SUBSTAT_ROLL
from .talents import Talent, find_talent, load_talents

ROTATION_SECONDS = 20
# 副词条：每个副词条固定 2 次，再按配装的副词条优先级分 20 次有效词条（[7, 7, 4, 2]）
FIXED_ROLLS = 2
LIQUID_ROLLS = [7, 7, 4, 2]
ALL_SUBSTATS = ["hp_flat", "atk_flat", "def_flat", "hp_pct", "atk_pct", "def_pct", "em", "er", "cr", "cd"]
CRIT_RATE_CAP = 1.0

# 「会给队友加成的装备」的标签 → 属性键
TAG_STATS = {"buff:atk_pct": "atk_pct", "buff:hp_pct": "hp_pct", "buff:def_pct": "def_pct", "buff:em": "em",
             "buff:dmg_bonus": "dmg", "buff:crit_rate": "cr", "buff:crit_dmg": "cd", "buff:elevate": "elevate",
             "universal_shred": "res_shred", "def_shred": "def_reduction"}
# 一个反应需要哪些元素（队里同时有这些元素才算能触发）
REACTION_ELEMENTS = {
    "overloaded": [["pyro"], ["electro"]], "superconduct": [["cryo"], ["electro"]],
    "electro_charged": [["electro"], ["hydro"]], "lunar_charged": [["electro"], ["hydro"]],
    "vaporize": [["hydro"], ["pyro"]], "melt": [["pyro"], ["cryo"]], "frozen": [["cryo"], ["hydro"]],
    "bloom": [["dendro"], ["hydro"]], "quicken": [["dendro"], ["electro"]], "burning": [["dendro"], ["pyro"]],
    "swirl": [["anemo"], ["pyro", "hydro", "electro", "cryo"]],
    "crystallize": [["geo"], ["pyro", "hydro", "electro", "cryo"]],
}


@dataclass
class Effect:
    """一条增益。to：self / all / others / on_field（站场角色）/ off_field / tag:标签（例如 tag:witch_arcana）。
    group + option：同一名角色身上同一个 group 的增益每次只取一个 option（例如被扩散的元素：风套减抗和
    砂糖的精通分享都跟着它走），引擎把每个 option 都试一遍，取全队总伤害最高的。"""

    source: str
    stats: dict[str, float] = field(default_factory=dict)
    to: str = "self"
    when: str | None = None
    from_stat: dict | None = None  # {stat: em, ratio: 0.2, cap: 200, key: em}：数值按提供者的面板算
    group: str | None = None
    option: str | None = None
    uptime: float = 1.0

    @classmethod
    def parse(cls, raw: dict, default_to: str = "self") -> "Effect":
        stats = {}
        for name, v in (raw.get("stats") or {}).items():
            key = stat_key(name)
            stats[key] = parse_value(key, v)
        fs = raw.get("from_stat")
        if fs:
            fs = dict(fs)
            fs["key"] = stat_key(fs["key"])
        return cls(raw.get("source", ""), stats, raw.get("to", default_to), raw.get("when"), fs,
                   raw.get("group"), raw.get("option"), float(raw.get("uptime", 1.0)))


@dataclass
class Profile:
    char: str
    playstyle: str = "default"
    talents: dict[str, int] = field(default_factory=dict)
    hits: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    on_field_seconds: float = 0.0
    notes: str = ""
    verified: bool = False


def load_profiles(data_dir: Path | str) -> dict[tuple[str, str], Profile]:
    out = {}
    for path in sorted((Path(data_dir) / "damage" / "profiles").glob("*.yaml")):
        with path.open(encoding="utf-8") as fh:
            for raw in yaml.safe_load_all(fh):
                if not raw:
                    continue
                p = Profile(
                    char=raw["char"], playstyle=raw.get("playstyle", "default"),
                    talents={SLOT_KEYS.get(str(k).lower(), k): int(v) for k, v in (raw.get("talents") or {}).items()},
                    hits=raw.get("hits") or [], reactions=raw.get("reactions") or [],
                    effects=[Effect.parse(e) for e in raw.get("buffs") or []],
                    on_field_seconds=float(raw.get("on_field_seconds", 0)), notes=raw.get("notes", ""),
                    verified=bool(raw.get("verified", False)),
                )
                out[(p.char, p.playstyle)] = p
    return out


def equipment_effects(equipment) -> list[Effect]:
    """equipment_buffs.yaml 的一件装备 → 增益。带 choose_one 的几条互斥（风套）。"""
    out = []
    for g in equipment.provides:
        if g.value is None:
            continue
        base, _, param = g.tag.partition(":")
        if g.tag in TAG_STATS:
            key = TAG_STATS[g.tag]
        elif base == "shred":
            key = f"{param}_res_shred"
        elif base == "element_dmg_buff":
            key = f"{param}_dmg"
        elif base == "reaction_buff":
            key = f"{param}_bonus"
        else:
            continue
        # 风套这类「几条只生效一条」：按扩散到的元素分组，和砂糖固有天赋等同组增益一起选
        group, option = ("swirl", param) if equipment.choose_one else (None, None)
        out.append(Effect(equipment.zh, {key: float(g.value)}, equipment.to, g.when, None, group, option))
    return out


@dataclass
class MemberSpec:
    """参与计算的一名队员。build 是 models.Build，可以为空（没有配装时只用白值）。"""

    char: str
    zh: str
    element: str
    playstyle: str
    cons: int
    tags: set[str]
    position: str | None
    build: object | None = None
    weapon: str | None = None  # 组队时选定的武器中文名
    artifact_choice: int = 0
    equipment: list = field(default_factory=list)  # 这名队员身上会给队友加成的装备（EquipmentBuff）


@dataclass
class MemberDamage:
    char: str
    zh: str
    total: float
    hits: list[tuple[str, float]]  # (说明, 这一行一轮的期望伤害)
    has_profile: bool


@dataclass
class TeamDamage:
    total: float
    members: list[MemberDamage]
    choices: list[str]  # 几选一的增益选了哪条
    buffs: list[str]  # 实际生效的增益（来源 → 对象）
    missing: list[str]  # 没有循环数据的队员
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": round(self.total),
            "members": [{"zh": m.zh, "total": round(m.total), "share": round(m.total / self.total, 3) if self.total else 0,
                         "top_hits": [(h, round(v)) for h, v in sorted(m.hits, key=lambda x: -x[1])[:3]]}
                        for m in self.members],
            "choices": self.choices,
            "buffs": self.buffs,
            "missing": self.missing,
        }


def member_panel(gd: GameData, m: MemberSpec) -> Stats:
    char = gd.character(m.char)
    st = character_stats(char)
    b = m.build
    if b is None:
        return st
    weapon = m.weapon or (sorted(b.weapons, key=lambda w: w.rank)[0].zh if b.weapons else None)
    if weapon:
        try:
            st = st.merged(weapon_stats(gd.weapon(weapon)))
        except KeyError:
            pass
    st.add("hp_flat", FLOWER_HP, "生之花")
    st.add("atk_flat", PLUME_ATK, "死之羽")
    for slot in ("sands", "goblet", "circlet"):
        for name in b.main_stats.get(slot, [])[:1]:
            key = _main_key(name)
            if key in MAIN_STATS:
                st.add(key, MAIN_STATS[key], f"{slot}主词条")
    for key in ALL_SUBSTATS:
        st.add(key, SUBSTAT_ROLL[key] * FIXED_ROLLS, "副词条（固定）")
    prio = [k for name in b.substats if (k := _main_key(name)) in SUBSTAT_ROLL]
    for key, n in zip(prio, LIQUID_ROLLS):
        st.add(key, SUBSTAT_ROLL[key] * n, "副词条（有效）")
    if st.get("cr") > CRIT_RATE_CAP:  # 溢出的暴击率按 1:2 换成暴击伤害
        extra = st.get("cr") - CRIT_RATE_CAP
        st.add("cr", -extra, "暴击率溢出")
        st.add("cd", extra * 2, "暴击率溢出")
    combos = b.artifact_sets
    if combos:
        combo = combos[m.artifact_choice] if m.artifact_choice < len(combos) else combos[0]
        for piece in combo:
            text = None
            if piece.set == "mixed":
                text = piece.zh
            else:
                try:
                    text = gd.artifact_set(piece.zh).get("2pc", "")
                except KeyError:
                    pass
            bonus = set_2pc_stats(text or "") if piece.set != "mixed" else _mixed(text)
            for k, v in (bonus or {}).items():
                st.add(k, v, f"{piece.zh}2件套")
    return st


def _main_key(name: str) -> str:
    try:
        return stat_key(name)
    except KeyError:
        return name


def _mixed(text: str | None) -> dict[str, float] | None:
    """散搭写成「攻击力+18%」。"""
    if not text or "+" not in text:
        return None
    name, _, value = text.partition("+")
    try:
        key = stat_key(name + "%" if value.endswith("%") and not name.endswith("%") and "伤害" not in name else name)
    except KeyError:
        return None
    return {key: parse_value(key, value)}


def _funcs(members: list[MemberSpec]) -> dict:
    elements = [m.element for m in members]

    def can(r: str) -> bool:
        need = REACTION_ELEMENTS.get(r)
        if need is None:  # 特殊反应：看有没有能开启它的角色
            return any(f"enables:{r}" in m.tags for m in members) and can(
                {"stellar_superconduct": "superconduct", "stellar_swirl": "swirl", "lunar_charged": "electro_charged",
                 "lunar_bloom": "bloom", "lunar_crystallize": "crystallize"}[r])
        return all(any(e in elements for e in group) for group in need)

    return {
        "count": lambda e: elements.count(e),
        "count_any": lambda es: sum(elements.count(e) for e in es),
        "max_count": lambda es: max((elements.count(e) for e in es), default=0),
        "has_tag": lambda t: any(t in m.tags for m in members),
        "count_tag": lambda t: sum(t in m.tags for m in members),
        "has_char": lambda c: any(m.char == c for m in members),
        "reaction": can,
    }


def _targets(e: Effect, provider: int, members: list[MemberSpec]) -> list[int]:
    idx = range(len(members))
    if e.to == "self":
        return [provider]
    if e.to == "all":
        return list(idx)
    if e.to == "others":
        return [i for i in idx if i != provider]
    if e.to.startswith("tag:"):
        return [i for i in idx if e.to[4:] in members[i].tags]
    on_field = [i for i in idx if members[i].position == "on_field"] or [0]
    if e.to == "on_field":
        return on_field
    if e.to == "off_field":
        return [i for i in idx if i not in on_field]
    raise ValueError(f"不认识的增益对象：{e.to}")


def team_damage(gd: GameData, members: list[MemberSpec], profiles: dict[tuple[str, str], Profile],
                enemy: Enemy | None = None) -> TeamDamage:
    """members[0] 当作主C。几选一的增益逐个试，取全队总伤害最高的组合。"""
    enemy = enemy or Enemy()
    funcs = _funcs(members)
    panels = [member_panel(gd, m) for m in members]
    # 收集所有增益：(提供者序号, Effect)
    effects: list[tuple[int, Effect]] = []
    for i, m in enumerate(members):
        p = profiles.get((m.char, m.playstyle))
        if p:
            effects += [(i, e) for e in p.effects]
        for eq in m.equipment:
            effects += [(i, e) for e in equipment_effects(eq)]
    active = [(i, e) for i, e in effects if not e.when or evaluate(e.when, {"cons": members[i].cons}, funcs)]
    groups: dict[tuple[int, str], dict[str, list[tuple[int, Effect]]]] = {}
    fixed = []
    for i, e in active:
        if e.group:
            groups.setdefault((i, e.group), {}).setdefault(e.option or e.source, []).append((i, e))
        else:
            fixed.append((i, e))
    keys = list(groups)
    best: TeamDamage | None = None
    for pick in itertools.product(*(list(groups[k]) for k in keys)):
        chosen = fixed + [x for k, opt in zip(keys, pick) for x in groups[k][opt]]
        result = _evaluate(gd, members, profiles, panels, chosen, enemy)
        result.choices = [f"{members[k[0]].zh}·{k[1]}：{opt}" for k, opt in zip(keys, pick)]
        if best is None or result.total > best.total:
            best = result
    return best


def _evaluate(gd, members, profiles, panels, chosen, enemy) -> TeamDamage:
    buffs = [Stats() for _ in members]
    applied = []
    for i, e in chosen:
        values = dict(e.stats)
        if e.from_stat:
            fs = e.from_stat
            v = max(0.0, panels[i].scaling_stat(fs["stat"]) + float(fs.get("offset", 0))) * float(fs["ratio"])
            if "cap" in fs:
                v = min(v, float(fs["cap"]))
            values[fs["key"]] = values.get(fs["key"], 0.0) + v
        targets = _targets(e, i, members)
        for t in targets:
            for k, v in values.items():
                buffs[t].add(k, v * e.uptime, e.source)
        who = "、".join(members[t].zh for t in targets)
        applied.append(f"{members[i].zh}·{e.source} → {who}：{_stats_zh(values)}")
    out = []
    missing = []
    for i, m in enumerate(members):
        p = profiles.get((m.char, m.playstyle))
        if p is None:
            missing.append(m.zh)
            out.append(MemberDamage(m.char, m.zh, 0.0, [], False))
            continue
        out.append(_member_damage(gd, m, p, panels[i].merged(buffs[i]), enemy, panels, buffs, members))
    return TeamDamage(sum(x.total for x in out), out, [], applied, missing)


def _member_damage(gd: GameData, m: MemberSpec, p: Profile, st: Stats, enemy: Enemy,
                   panels: list[Stats], buffs: list[Stats], members: list[MemberSpec]) -> MemberDamage:
    char = gd.character(m.char)
    talents = load_talents(gd.talent_multipliers.get(m.char, {}))
    levels = _talent_levels(m, p)
    rows: list[tuple[str, float]] = []
    for spec in p.hits:
        if spec.get("when") and not evaluate(spec["when"], {"cons": m.cons}, _funcs(members)):
            continue
        t = _custom_talent(spec) if "scaling" in spec else find_talent(talents, spec["talent"])
        level = int(spec.get("level", levels.get(t.slot, 9)))
        spec = dict(spec)
        if "base_multiplier_from" in spec:  # 例：莱欧斯利强化斥逐拳 = 普攻伤害 × 战技倍率
            src = find_talent(talents, spec.pop("base_multiplier_from"))
            values = next(iter(src.params.values()))
            spec["base_multiplier"] = values[min(levels.get(src.slot, 9), len(values)) - 1] * float(
                spec.get("base_multiplier", 1.0))
        count = float(spec.pop("count", 1))
        hits = talent_hit(gd, char, t, level, spec, st, enemy, 90)
        rows += [(h.label, h.average * count) for h in hits]
    for spec in p.reactions:
        if spec.get("when") and not evaluate(spec["when"], {"cons": m.cons}, _funcs(members)):
            continue
        count = float(spec.get("count", 1))
        who = st
        if spec.get("by") == "on_field":  # 反应由站场角色触发，按站场角色的面板算，伤害记在提供元素的人头上
            j = next((k for k, x in enumerate(members) if x.position == "on_field"), 0)
            who = panels[j].merged(buffs[j])
        h: HitResult = reaction_hit(gd, spec, who, enemy, 90)
        rows.append((h.label, h.average * count))
    return MemberDamage(m.char, m.zh, sum(v for _, v in rows), rows, True)


_STAT_SUFFIX = {"atk": "攻击力", "hp": "生命值上限", "def": "防御力", "em": "元素精通"}


def _custom_talent(spec: dict) -> Talent:
    """天赋表里没有的伤害（固有天赋、命座），直接写倍率：scaling: {atk: 0.8}。"""
    parts, params = [], {}
    for n, (stat, v) in enumerate(spec["scaling"].items(), 1):
        parts.append(f"{{param{n}:P}}{_STAT_SUFFIX[stat]}")
        params[f"param{n}"] = [float(v)] * 15
    return Talent(spec.get("slot", "skill"), spec.get("name", "额外伤害"), "+".join(parts), params)


def _talent_levels(m: MemberSpec, p: Profile) -> dict[str, int]:
    """配装里「要点」的天赋按 10 级，「不用点」按 6 级，其余 9 级；循环数据里写了的以循环为准。"""
    out = {"normal_attack": 9, "skill": 9, "burst": 9}
    if m.build is not None:
        slot = {"normal": "normal_attack", "skill": "skill", "burst": "burst"}
        for x in m.build.talents.level:
            out[slot[x]] = 10
        for x in m.build.talents.skip:
            out[slot[x]] = 6
    out.update(p.talents)
    return out


def _stats_zh(values: dict[str, float]) -> str:
    from .stats import FLAT_KEYS

    return "，".join(f"{k} +{v:.0f}" if k in FLAT_KEYS else f"{k} +{v:.1%}" for k, v in values.items())
