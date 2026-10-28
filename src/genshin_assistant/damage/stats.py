"""面板：角色白值 + 武器 + 圣遗物 + 各种增益，算出最终属性。

属性统一用英文键，百分比存小数。写场景文件时可以用中文名（见 ALIASES），
百分比属性直接写数字（20 表示 20%）或者带百分号的字符串（"20%"）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ELEMENTS = ["pyro", "hydro", "electro", "cryo", "anemo", "geo", "dendro", "physical"]
ELEMENT_ZH = {"pyro": "火", "hydro": "水", "electro": "雷", "cryo": "冰", "anemo": "风", "geo": "岩", "dendro": "草",
              "physical": "物理"}
CATEGORIES = {"normal": "普通攻击", "charged": "重击", "plunge": "下落攻击", "skill": "元素战技", "burst": "元素爆发"}
REACTION_ZH = {
    "vaporize": "蒸发", "melt": "融化", "overloaded": "超载", "superconduct": "超导", "electro_charged": "感电",
    "swirl": "扩散", "shattered": "碎冰", "bloom": "绽放", "hyperbloom": "超绽放", "burgeoning": "烈绽放", "burning": "燃烧",
    "aggravate": "超激化", "spread": "蔓激化", "lunar_charged": "月感电", "lunar_bloom": "月绽放",
    "lunar_crystallize": "月结晶", "stellar_superconduct": "星超导", "stellar_swirl": "星扩散",
}
LUNAR_REACTIONS = ["lunar_charged", "lunar_bloom", "lunar_crystallize"]
STELLAR_REACTIONS = ["stellar_superconduct", "stellar_swirl"]
SPECIAL_REACTIONS = LUNAR_REACTIONS + STELLAR_REACTIONS

# 不是百分比的属性
FLAT_KEYS = {"hp_flat", "atk_flat", "def_flat", "em", "flat_dmg"} | {f"{c}_flat_dmg" for c in CATEGORIES}


def _aliases() -> dict[str, str]:
    a = {
        "生命值": "hp_flat", "小生命": "hp_flat", "生命值%": "hp_pct", "大生命": "hp_pct", "生命值上限%": "hp_pct",
        "攻击力": "atk_flat", "小攻击": "atk_flat", "攻击力%": "atk_pct", "大攻击": "atk_pct",
        "防御力": "def_flat", "小防御": "def_flat", "防御力%": "def_pct", "大防御": "def_pct",
        "元素精通": "em", "精通": "em", "元素充能效率": "er", "充能": "er",
        "暴击率": "cr", "暴击伤害": "cd", "暴伤": "cd",
        "伤害加成": "dmg", "增伤": "dmg", "造成的伤害提升": "dmg",
        "擢升": "elevate", "减防": "def_reduction", "无视防御": "def_ignore",
        "减抗": "res_shred", "全元素减抗": "res_shred",
        "固定伤害": "flat_dmg", "伤害值提高": "flat_dmg",
        "治疗加成": "heal", "护盾强效": "shield",
        "月曜基础伤害提升": "lunar_base_bonus", "月曜反应伤害提升": "lunar_bonus",
        "星烁基础伤害提升": "stellar_base_bonus", "星烁反应伤害提升": "stellar_bonus",
        "月曜暴击伤害": "lunar_cd", "星烁暴击伤害": "stellar_cd",  # 只对月曜/星烁反应伤害
        # builds/ 里主词条、副词条的写法
        "atk%": "atk_pct", "hp%": "hp_pct", "def%": "def_pct", "em": "em", "energy_recharge": "er",
        "crit_rate": "cr", "crit_dmg": "cd", "healing_bonus": "heal",
    }
    for e, zh in ELEMENT_ZH.items():
        a[f"{zh}伤"] = a[f"{zh}元素伤害加成"] = a[f"{zh}伤害加成"] = f"{e}_dmg"
        a[f"{zh}减抗"] = a[f"{zh}元素减抗"] = a[f"{zh}抗性降低"] = f"{e}_res_shred"
        a[f"{zh}元素暴击伤害"] = a[f"{zh}伤暴击伤害"] = f"{e}_cd"  # 只对这个元素的伤害生效（珐露珊 6 命）
    a["物理伤害加成"] = "physical_dmg"
    for c, zh in CATEGORIES.items():
        short = {"normal": "普攻", "skill": "战技", "burst": "爆发", "charged": "重击", "plunge": "下落"}[c]
        for name in {zh, short}:
            a[f"{name}增伤"] = a[f"{name}伤害提升"] = f"{c}_dmg"
            a[f"{name}暴击率"] = f"{c}_cr"
            a[f"{name}暴击伤害"] = f"{c}_cd"
            a[f"{name}固定伤害"] = a[f"{name}伤害值提高"] = f"{c}_flat_dmg"
    for r, zh in REACTION_ZH.items():
        a[f"{zh}加成"] = a[f"{zh}反应加成"] = a[f"{zh}伤害提升"] = f"{r}_bonus"
    for r in SPECIAL_REACTIONS:
        a[f"{REACTION_ZH[r]}基础伤害提升"] = f"{r}_base_bonus"
    return a


ALIASES = _aliases()

# 5 星 +20 圣遗物主词条
MAIN_STATS = {"hp_pct": 0.466, "atk_pct": 0.466, "def_pct": 0.583, "em": 186.5, "er": 0.518, "cr": 0.311, "cd": 0.622,
              "heal": 0.359, "physical_dmg": 0.583, **{f"{e}_dmg": 0.466 for e in ELEMENTS if e != "physical"}}
FLOWER_HP, PLUME_ATK = 4780, 311
# 5 星副词条单次提升的平均值（四档 70%/80%/90%/100% 的平均）
SUBSTAT_ROLL = {"hp_flat": 253.94, "atk_flat": 16.54, "def_flat": 19.68, "hp_pct": 0.0496, "atk_pct": 0.0496,
                "def_pct": 0.062, "em": 19.82, "er": 0.0551, "cr": 0.0331, "cd": 0.0662}


def stat_key(name: str) -> str:
    key = ALIASES.get(str(name).strip(), str(name).strip())
    if not _known(key):
        raise KeyError(f"不认识的属性：{name}")
    return key


def _known(key: str) -> bool:
    base = {"hp_base", "atk_base", "def_base", "hp_pct", "atk_pct", "def_pct", "er", "cr", "cd", "dmg", "elevate",
            "def_reduction", "def_ignore", "res_shred", "heal", "shield", "lunar_base_bonus", "lunar_bonus",
            "stellar_base_bonus", "stellar_bonus"} | FLAT_KEYS
    if key in base:
        return True
    head, _, tail = key.rpartition("_")
    if key.endswith("_res_shred"):
        return key.removesuffix("_res_shred") in ELEMENTS
    if key.endswith("_flat_dmg"):
        return key.removesuffix("_flat_dmg") in CATEGORIES
    if key.endswith("_base_bonus"):
        return key.removesuffix("_base_bonus") in SPECIAL_REACTIONS
    if tail == "dmg":
        return head in ELEMENTS or head in CATEGORIES
    if tail in ("cr", "cd"):
        return head in CATEGORIES or (tail == "cd" and (head in ELEMENTS or head in ("lunar", "stellar")))
    if tail == "bonus":
        return head in REACTION_ZH
    return False


def parse_value(key: str, value) -> float:
    """「20%」→ 0.2；百分比属性的纯数字也按百分数理解（20 → 0.2）；固定值原样。"""
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("%"):
            return float(s[:-1]) / 100
        value = float(s)
    return float(value) if key in FLAT_KEYS else float(value) / 100


@dataclass
class Stats:
    """一组属性的累加器。"""

    values: dict[str, float] = field(default_factory=dict)
    sources: list[tuple[str, str, float]] = field(default_factory=list)  # (来源, 属性, 数值)，给输出核对用

    def add(self, key: str, value: float, source: str = "") -> None:
        self.values[key] = self.values.get(key, 0.0) + value
        if source:
            self.sources.append((source, key, value))

    def add_mapping(self, mapping: dict | None, source: str) -> None:
        for name, v in (mapping or {}).items():
            key = stat_key(name)
            self.add(key, parse_value(key, v), source)

    def get(self, key: str) -> float:
        return self.values.get(key, 0.0)

    def merged(self, other: "Stats") -> "Stats":
        out = Stats(dict(self.values), list(self.sources))
        for k, v in other.values.items():
            out.values[k] = out.values.get(k, 0.0) + v
        out.sources += other.sources
        return out

    # 最终属性
    @property
    def hp(self) -> float:
        return self.get("hp_base") * (1 + self.get("hp_pct")) + self.get("hp_flat")

    @property
    def atk(self) -> float:
        return self.get("atk_base") * (1 + self.get("atk_pct")) + self.get("atk_flat")

    @property
    def defense(self) -> float:
        return self.get("def_base") * (1 + self.get("def_pct")) + self.get("def_flat")

    def scaling_stat(self, name: str) -> float:
        return {"hp": self.hp, "atk": self.atk, "def": self.defense, "em": self.get("em"), "er": 1 + self.get("er"),
                "base_atk": self.get("atk_base")}[name]


# 角色突破属性、武器副属性的中文名 → 属性键（这里的「攻击力」「生命值」「防御力」是百分比）
ASCENSION_STAT = {"攻击力": "atk_pct", "生命值": "hp_pct", "防御力": "def_pct", "元素精通": "em",
                  "元素充能效率": "er", "暴击率": "cr", "暴击伤害": "cd", "治疗加成": "heal",
                  "物理伤害加成": "physical_dmg",
                  **{f"{zh}元素伤害加成": f"{e}_dmg" for e, zh in ELEMENT_ZH.items() if e != "physical"}}


def _num(s: str) -> float:
    return float(str(s).rstrip("%"))


def character_stats(row: dict) -> Stats:
    """90 级突破后的白值；暴击率/暴击伤害带基础 5%/50%（突破属性是暴击时游戏数据里已经含了）。"""
    st = Stats()
    base = row["stats_lv90"]
    name = row["zh"]
    st.add("hp_base", base["hp"], f"{name}白值")
    st.add("atk_base", base["atk"], f"{name}白值")
    st.add("def_base", base["def"], f"{name}白值")
    key = ASCENSION_STAT[base["ascension_stat"]]
    value = _num(base["ascension_value"])
    st.add(key, value if key == "em" else value / 100, f"{name}突破")
    if key != "cr":
        st.add("cr", 0.05, "基础暴击率")
    if key != "cd":
        st.add("cd", 0.5, "基础暴击伤害")
    return st


def weapon_stats(row: dict) -> Stats:
    st = Stats()
    st.add("atk_base", row["base_atk"], f"{row['zh']}基础攻击")
    if row.get("substat"):
        key = ASCENSION_STAT[row["substat"]]
        value = _num(row["substat_value"])
        st.add(key, value if key == "em" else value / 100, f"{row['zh']}副属性")
    return st


_SET_2PC = [
    (re.compile(r"^获得15%(.)元素伤害加成。$"), lambda m: {f"{_elem(m.group(1))}_dmg": 0.15}),
    (re.compile(r"^攻击力提高(\d+)%。$"), lambda m: {"atk_pct": int(m.group(1)) / 100}),
    (re.compile(r"^生命值提升(\d+)%。$"), lambda m: {"hp_pct": int(m.group(1)) / 100}),
    (re.compile(r"^防御力提高(\d+)%。$"), lambda m: {"def_pct": int(m.group(1)) / 100}),
    (re.compile(r"^元素精通提高(\d+)点。$"), lambda m: {"em": int(m.group(1))}),
    (re.compile(r"^元素充能效率提高(\d+)%。$"), lambda m: {"er": int(m.group(1)) / 100}),
    (re.compile(r"^暴击率提高(\d+)%。$"), lambda m: {"cr": int(m.group(1)) / 100}),
    (re.compile(r"^造成的物理伤害提高(\d+)%。$"), lambda m: {"physical_dmg": int(m.group(1)) / 100}),
    (re.compile(r"^元素战技造成的伤害提升(\d+)%。$"), lambda m: {"skill_dmg": int(m.group(1)) / 100}),
    (re.compile(r"^元素爆发造成的伤害提升(\d+)%。$"), lambda m: {"burst_dmg": int(m.group(1)) / 100}),
    (re.compile(r"^下落攻击造成的伤害提升(\d+)%。$"), lambda m: {"plunge_dmg": int(m.group(1)) / 100}),
    (re.compile(r"^普通攻击与重击造成的伤害提高(\d+)%。$"),
     lambda m: {"normal_dmg": int(m.group(1)) / 100, "charged_dmg": int(m.group(1)) / 100}),
    (re.compile(r"^治疗加成提高(\d+)%。$"), lambda m: {"heal": int(m.group(1)) / 100}),
]


def _elem(zh: str) -> str:
    return next(e for e, z in ELEMENT_ZH.items() if z == zh)


def set_2pc_stats(text: str) -> dict[str, float] | None:
    """能直接算进面板的 2 件套效果；有条件的（夜魂、抗性）返回 None。"""
    for pattern, fn in _SET_2PC:
        m = pattern.match(text.strip())
        if m:
            return fn(m)
    return None
