"""从上游数据生成 data/game/ 和 data/mechanics/official/。

生成的文件不要手改，每个版本重跑一次：
    python -m genshin_assistant import-gamedata
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import yaml

from . import sources
from .text import LINK_IDS, clean, render_talent_label

LANG = "ChineseSimplified"

ELEMENTS = {
    "ELEMENT_PYRO": "pyro", "ELEMENT_HYDRO": "hydro", "ELEMENT_ELECTRO": "electro", "ELEMENT_CRYO": "cryo",
    "ELEMENT_ANEMO": "anemo", "ELEMENT_GEO": "geo", "ELEMENT_DENDRO": "dendro", "ELEMENT_NONE": None,
}
WEAPONS = {
    "WEAPON_SWORD_ONE_HAND": "sword", "WEAPON_CLAYMORE": "claymore", "WEAPON_POLE": "polearm",
    "WEAPON_BOW": "bow", "WEAPON_CATALYST": "catalyst",
}
# 不带百分号的属性
FLAT_PROPS = {"FIGHT_PROP_ELEMENT_MASTERY", "FIGHT_PROP_BASE_ATTACK", "FIGHT_PROP_HP", "FIGHT_PROP_ATTACK", "FIGHT_PROP_DEFENSE"}
TALENT_SLOTS = [("combat1", "normal_attack"), ("combat2", "skill"), ("combatsp", "special"), ("combat3", "burst")]
SCALING_LEVELS = (10, 13)

RESONANCE_ELEMENTS = {"fire": "pyro", "water": "hydro", "wind": "anemo", "electric": "electro",
                      "grass": "dendro", "ice": "cryo", "rock": "geo"}

# 天赋里写「……将转为触发星超导/星扩散反应」的角色能为队伍开启这些特殊反应
SPECIAL_REACTIONS = {"月感电": "lunar_charged", "月绽放": "lunar_bloom", "月结晶": "lunar_crystallize",
                     "星超导": "stellar_superconduct", "星扩散": "stellar_swirl"}
_ENABLES = re.compile(r"将转为触发((?:%s)(?:/(?:%s))*)反应" % (("|".join(SPECIAL_REACTIONS),) * 2))

# 元素反应相关的游戏内教程，按主题归类。id 来自 TutorialDetailExcelConfigData。
TUTORIALS = {
    "elemental_aura": [30301, 30401, 30501, 23701],
    "frozen": [30701],
    "vaporize": [30901, 31101],
    "melt": [31001, 31201],
    "electro_charged": [31301, 31401],
    "overloaded": [31601],
    "superconduct": [31701],
    "swirl": [31901],
    "crystallize": [33901, 33902],
    "quicken": [38001, 38002],
    "bloom": [38101, 38103],
    "elemental_shield": [32401],
    "energy": [32501, 32601],
    "shield": [32801],
    "moonsign": [38701],
    "lunar_charged": [38501],
    "lunar_bloom": [38601, 38801, 38802],
    "lunar_crystallize": [38901, 38902],
    "stellar_superconduct": [39001, 39002],
    "stellar_swirl": [39101, 39102],
}


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


class _Flow(list):
    """写成一行 [a, b, c] 的列表（天赋参数表）。"""


_Dumper.add_representer(_Flow, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, flow_style=True))


def dump(data, path: Path, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=10_000)
    path.write_text(header + body, encoding="utf-8")


def _stat_value(prop: str, value: float) -> str:
    if prop in FLAT_PROPS:
        return f"{value:.0f}"
    return f"{value * 100:.1f}%"


class Importer:
    def __init__(self, gdb: dict, agd: dict, gdb_version: str):
        self.gdb = gdb
        self.zh = gdb["data"][LANG]
        self.en = gdb["data"]["English"]
        self.version = gdb["version"]
        self.gdb_version = gdb_version
        self.textmap = {**agd["textmap_medium"], **agd["textmap"]}
        self.proud = {p["proudSkillId"]: p["paramList"] for p in agd["proud_skills"]}
        self.hyperlinks = {h["id"]: h for h in agd["hyperlinks"]}
        self.tutorials = {t["id"]: t for t in agd["tutorials"]}
        self.resonance = agd["resonance"]
        self.element_coeff = agd.get("element_coeff", [])
        self._intros = self._find_intros()
        self._witch = self._find_witch_quests()

    def _text(self, h: int | str) -> str:
        return self.textmap.get(str(h), "")

    def _clean(self, s: str) -> str:
        return clean(s, proud=self.proud)

    # ---------- 名词解释 ----------

    def glossary_entry(self, link_id: int) -> dict | None:
        h = self.hyperlinks.get(link_id)
        if not h:
            return None
        name = self._text(h["nameTextMapHash"])
        desc = clean(self._text(h["descTextMapHash"]), params=h["descParamList"], proud=self.proud)
        return {"id": link_id, "name": name, "text": desc} if name else None

    def glossary(self) -> list[dict]:
        return [e for i in sorted(self.hyperlinks) if (e := self.glossary_entry(i))]

    # ---------- 角色 ----------

    def _find_intros(self) -> dict[str, str]:
        """官方一句话定位，例如「桑多涅是一位……的冰元素输出型角色」。只有较新的角色有。"""
        names = {c["name"] for c in self.zh["characters"].values()}
        intros: dict[str, str] = {}
        for text in self.textmap.values():
            head, sep, _ = text.partition("是一位")
            if sep and head in names and "角色" in text[:150] and "角色试用" not in text:
                if len(text) > len(intros.get(head, "")):
                    intros[head] = text
        return {k: self._clean(v) for k, v in intros.items()}

    def _find_witch_quests(self) -> dict[str, str]:
        """「魔女的课业」：完成后角色成为魔导角色，队伍有 2 名以上魔导角色时触发「魔导·秘仪」。
        genshin-db 的天赋数据不含这部分，从文本表里按句式找。"""
        pattern = re.compile(r"^完成「?魔女的课业·[^」后]*」?后，(\S+?)将成为魔导角色。队伍中编入")
        out: dict[str, str] = {}
        for text in self.textmap.values():
            m = pattern.match(self._clean(text))
            if m:
                out[m.group(1)] = self._clean(text)
        return out

    def _base_stats(self, key: str) -> dict:
        st = self.gdb["stats"]["characters"][key]
        curve = self.gdb["curve"]["characters"]["90"]
        top = st["promotion"][-1]
        spec = top["specialized"]
        if st["specialized"] == "FIGHT_PROP_CRITICAL":
            spec += st["base"]["critrate"]
        elif st["specialized"] == "FIGHT_PROP_CRITICAL_HURT":
            spec += st["base"]["critdmg"]
        return {
            "hp": round(st["base"]["hp"] * curve[st["curve"]["hp"]] + top["hp"]),
            "atk": round(st["base"]["attack"] * curve[st["curve"]["attack"]] + top["attack"]),
            "def": round(st["base"]["defense"] * curve[st["curve"]["defense"]] + top["defense"]),
            "ascension_stat": self.zh["characters"][key]["substatText"],
            "ascension_value": _stat_value(st["specialized"], spec),
        }

    def _talents(self, key: str, links: set[int]) -> tuple[dict, list[dict]]:
        t = self.zh["talents"][key]
        params = self.gdb["stats"]["talents"].get(key, {})
        talents = {}
        for src, dst in TALENT_SLOTS:
            if src not in t:
                continue
            raw = t[src]
            links.update(int(x) for x in LINK_IDS.findall(raw.get("descriptionRaw", "")))
            entry = {"name": raw["name"], "description": raw["description"]}
            labels = raw.get("attributes", {}).get("labels", [])
            if labels and src in params:
                # 「10级 / 13级」，两者相同（冷却、能量）时只写一个
                scaling = {}
                for label in labels:
                    values = [render_talent_label(label, params[src], lv) for lv in SCALING_LEVELS]
                    shown = dict.fromkeys(v for _, v in values)
                    scaling[values[0][0]] = " / ".join(shown)
                entry["scaling"] = scaling
            talents[dst] = entry
        passives = []
        for i in range(1, 5):
            p = t.get(f"passive{i}")
            if p:
                links.update(int(x) for x in LINK_IDS.findall(p.get("descriptionRaw", "")))
                passives.append({"name": p["name"], "description": p["description"]})
        return talents, passives

    def _constellations(self, key: str, links: set[int]) -> dict:
        c = self.zh["constellations"].get(key, {})
        out = {}
        for i in range(1, 7):
            x = c.get(f"c{i}")
            if x:
                links.update(int(n) for n in LINK_IDS.findall(x.get("descriptionRaw", "")))
                out[f"c{i}"] = {"name": x["name"], "description": x["description"]}
        return out

    def character_keys(self) -> list[str]:
        """全部角色，加上各元素的旅行者（genshin-db 里只有天赋和命座，没有单独的角色条目）。"""
        return sorted(set(self.zh["characters"]) | set(self.zh["talents"]))

    def character(self, key: str) -> dict:
        traveler = key.startswith("traveler")
        base = "aether" if traveler else key
        c = self.zh["characters"][base]
        links: set[int] = set()
        talents, passives = self._talents(key, links) if key in self.zh["talents"] else ({}, [])
        cons = self._constellations(key, links)
        out = {
            "id": key,
            "game_id": c["id"],
            "zh": self.zh["talents"][key]["name"].replace(" ", "") if traveler else c["name"],
            "en": self.en["talents"][key]["name"] if traveler else self.en["characters"][key]["name"],
            "title": c.get("title") or None,
            "element": key.removeprefix("traveler") if traveler else ELEMENTS.get(c["elementType"]),
            "weapon": WEAPONS[c["weaponType"]],
            "rarity": c["rarity"],
            "region": c.get("region") or None,
            "affiliation": c.get("affiliation") or None,
            "version": None if traveler else self.version["characters"].get(key) or None,
            "intro": None if traveler else self._intros.get(c["name"]),
            "witch_quest": None if traveler else self._witch.get(c["name"]),
            "enables": _enabled_reactions(passives),
            "stats_lv90": self._base_stats(base) if base in self.gdb["stats"]["characters"] else None,
            "talents": talents,
            "passives": passives,
            "constellations": cons,
            "terms": [e for i in sorted(links) if (e := self.glossary_entry(i))],
        }
        return {k: v for k, v in out.items() if v not in (None, [], {})}

    # ---------- 武器、圣遗物 ----------

    def weapon(self, key: str) -> dict:
        w = self.zh["weapons"][key]
        out = {
            "id": key,
            "zh": w["name"],
            "en": self.en["weapons"][key]["name"],
            "type": WEAPONS[w["weaponType"]],
            "rarity": w["rarity"],
            "version": self.version["weapons"].get(key) or None,
        }
        st = self.gdb["stats"]["weapons"].get(key)
        if st:
            top = st["promotion"][-1]
            curve = self.gdb["curve"]["weapons"][str(top["maxlevel"])]
            out["max_level"] = top["maxlevel"]
            out["base_atk"] = round(st["base"]["attack"] * curve[st["curve"]["attack"]] + top["attack"])
            if w.get("mainStatType"):
                value = st["base"]["specialized"] * curve[st["curve"]["specialized"]]
                out["substat"] = w["mainStatText"]
                out["substat_value"] = _stat_value(w["mainStatType"], value)
        if w.get("effectName"):
            out["passive"] = w["effectName"]
            out["r1"] = w["r1"]["description"]
            if "r5" in w:
                out["r5"] = w["r5"]["description"]
        return {k: v for k, v in out.items() if v is not None}

    def artifact_set(self, key: str) -> dict:
        a = self.zh["artifacts"][key]
        out = {
            "id": key,
            "zh": a["name"],
            "en": self.en["artifacts"][key]["name"],
            "rarity": max(a.get("rarityList") or [0]),
            "version": self.version["artifacts"].get(key) or None,
        }
        for n in ("1", "2", "4"):
            if a.get(f"effect{n}Pc"):
                out[f"{n}pc"] = a[f"effect{n}Pc"]
        return {k: v for k, v in out.items() if v is not None}

    # ---------- 伤害计算用的数值表 ----------

    def talent_multipliers(self, key: str) -> dict:
        """天赋每一行的模板和 1~15 级参数，给伤害计算用（damage/talents.py 负责解析）。"""
        return talent_multipliers(self.zh["talents"].get(key, {}), self.gdb["stats"]["talents"].get(key, {}))

    def level_multipliers(self) -> dict[int, float]:
        return level_multipliers(self.element_coeff)

    # ---------- 教程 ----------

    def tutorials_by_topic(self) -> dict[str, list[str]]:
        out = {}
        for topic, ids in TUTORIALS.items():
            texts = [self._clean(self._text(self.tutorials[i]["descriptTextMapHash"])) for i in ids if i in self.tutorials]
            if any(texts):
                out[topic] = [t for t in texts if t]
        return out

    def resonances(self) -> list[dict]:
        out = []
        for r in self.resonance:
            counts = {RESONANCE_ELEMENTS[k[:-len("AvatarCount")]]: v for k, v in r.items() if k.endswith("AvatarCount")}
            out.append({
                "zh": self._text(r["nameTextMapHash"]),
                "requires": counts or "4 种不同元素",
                "effect": self._clean(self._text(r["descTextMapHash"])),
            })
        return out

    # ---------- 写盘 ----------

    def write(self, data_dir: Path) -> dict[str, int]:
        stamp = dt.date.today().isoformat()
        header = (
            f"# 自动生成，不要手改。来源：genshin-db {self.gdb_version}、AnimeGameData2；生成日期 {stamp}。\n"
            "# 重新生成：python -m genshin_assistant import-gamedata\n"
        )
        char_header = header + (
            "# stats_lv90 是 90 级突破后的白值，暴击率/暴击伤害已含基础 5%/50%。\n"
            "# scaling 写成「10级 / 13级」，两个等级相同时只写一个。\n"
        )
        game = data_dir / "game"
        char_dir = game / "characters"
        if char_dir.exists():
            for old in char_dir.glob("*.yaml"):
                old.unlink()
        keys = self.character_keys()
        for key in keys:
            dump(self.character(key), char_dir / f"{key}.yaml", char_header)
        weapons = [self.weapon(k) for k in sorted(self.zh["weapons"], key=lambda k: (-self.zh["weapons"][k]["rarity"], k))]
        dump({"weapons": weapons}, game / "weapons.yaml", header)
        sets = [self.artifact_set(k) for k in sorted(self.zh["artifacts"])]
        dump({"artifact_sets": sets}, game / "artifacts.yaml", header)
        glossary = self.glossary()
        dump({"terms": glossary}, game / "glossary.yaml", header)
        dump({"source_version": self.gdb_version, "generated": stamp,
              "latest_game_version": max(self.version["characters"].values(), key=_version_key)},
             game / "meta.yaml", header)
        write_damage_tables(game, header, {k: self.talent_multipliers(k) for k in keys}, self.level_multipliers())
        tutorials = self.tutorials_by_topic()
        dump({"tutorials": tutorials}, data_dir / "mechanics" / "official_tutorials.yaml", header)
        dump({"resonances": self.resonances()}, data_dir / "mechanics" / "official_resonance.yaml", header)
        return {"characters": len(keys), "weapons": len(weapons), "artifact_sets": len(sets),
                "terms": len(glossary), "tutorial_topics": len(tutorials)}


def talent_multipliers(talent: dict, params: dict) -> dict:
    """{天赋槽: [{name, template, params: {paramN: [1~15级]}}]}，只留带百分比参数（倍率）的行。"""
    out = {}
    for src, dst in TALENT_SLOTS:
        labels = talent.get(src, {}).get("attributes", {}).get("labels", [])
        if not labels or src not in params:
            continue
        rows = []
        for label in labels:
            name, _, template = label.partition("|")
            used = _PARAM_REF.findall(template)
            if not any(fmt.endswith("P") for _, fmt in used):
                continue
            rows.append({
                "name": name,
                "template": template,
                "params": {f"param{n}": _Flow(round(v, 6) for v in params[src].get(f"param{n}", [])) for n, _ in used},
            })
        if rows:
            out[dst] = rows
    return out


def level_multipliers(element_coeff: list[dict]) -> dict[int, float]:
    """角色等级 → 等级系数（ElementCoeffExcelConfigData 的 playerElementLevelCo，第 0 行是占位，第 n 行是 n 级）。"""
    return {lv: round(row["playerElementLevelCo"], 4) for lv, row in enumerate(element_coeff) if 1 <= lv <= 100}


def write_damage_tables(game: Path, header: str, multipliers: dict[str, dict], levels: dict[int, float]) -> None:
    dump({k: v for k, v in sorted(multipliers.items()) if v}, game / "talent_multipliers.yaml", header + (
        "# 天赋倍率：template 是游戏里的显示模板，params 是 1~15 级的原始数值（百分比按小数存）。\n"
        "# 解析见 src/genshin_assistant/damage/talents.py。\n"))
    dump({"level_multiplier": levels}, game / "level_multiplier.yaml", header + (
        "# 等级系数（剧变、月曜反应和激化的基础值），按触发角色的等级。\n"))


_PARAM_REF = re.compile(r"\{param(\d+):([A-Z0-9]+)\}")


def _enabled_reactions(passives: list[dict]) -> list[str]:
    found: list[str] = []
    for p in passives:
        for m in _ENABLES.finditer(p["description"]):
            found += [SPECIAL_REACTIONS[x] for x in m.group(1).split("/") if SPECIAL_REACTIONS[x] not in found]
    return found


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def run(data_dir: Path, gdb_version: str | None = None, refresh: bool = False) -> dict[str, int]:
    gdb, version = sources.genshin_db(gdb_version, refresh=refresh)
    agd = sources.animegamedata(refresh=refresh)
    return Importer(gdb, agd, version).write(data_dir)
