"""读取 data/game/（导入脚本生成的游戏数据）和 data/mechanics/（机制知识）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..kb import DEFAULT_DATA_DIR


@dataclass
class GameData:
    characters: dict[str, dict] = field(default_factory=dict)
    weapons: dict[str, dict] = field(default_factory=dict)
    artifact_sets: dict[str, dict] = field(default_factory=dict)
    terms: dict[str, dict] = field(default_factory=dict)
    mechanics: dict[str, dict] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    talent_multipliers: dict[str, dict] = field(default_factory=dict)  # 角色 id → 天赋倍率表（伤害计算用）
    level_multipliers: dict[int, float] = field(default_factory=dict)

    def level_multiplier(self, level: int) -> float:
        if level not in self.level_multipliers:
            raise KeyError(f"没有 {level} 级的等级系数")
        return self.level_multipliers[level]

    def _find(self, table: dict[str, dict], name: str, kind: str) -> dict:
        key = name.strip().lower()
        for row in table.values():
            if key in (row["id"], row["zh"].lower(), row.get("en", "").lower()):
                return row
        raise KeyError(f"游戏数据里找不到{kind}：{name}")

    def character(self, name: str) -> dict:
        return self._find(self.characters, name, "角色")

    def weapon(self, name: str) -> dict:
        return self._find(self.weapons, name, "武器")

    def artifact_set(self, name: str) -> dict:
        return self._find(self.artifact_sets, name, "圣遗物套装")

    def find(self, name: str) -> tuple[str, dict]:
        """按角色、武器、圣遗物、名词的顺序查，返回 (类别, 条目)。"""
        for kind, getter in (("character", self.character), ("weapon", self.weapon), ("artifact_set", self.artifact_set)):
            try:
                return kind, getter(name)
            except KeyError:
                pass
        if name in self.terms:
            return "term", self.terms[name]
        raise KeyError(f"游戏数据里找不到：{name}")

    def has_character_zh(self, zh: str) -> bool:
        return any(c["zh"] == zh for c in self.characters.values())

    def identity_tags(self, zh: str) -> list[tuple[str, str]]:
        """从游戏数据直接读出的身份标签，返回 [(标签, 依据)]：能开启的特殊反应、魔导、月兆、夜魂。"""
        row = next((c for c in self.characters.values() if c["zh"] == zh), None)
        if row is None:
            return []
        out = [(f"enables:{r}", "游戏数据：能开启该反应") for r in row.get("enables") or []]
        if row.get("witch_quest"):
            out.append(("witch_arcana", "游戏数据：有魔女的课业（魔导·秘仪）"))
        passives = row.get("passives") or []
        if any(p["name"].startswith("月兆祝赐") for p in passives):
            out.append(("moonsign", "游戏数据：有「月兆祝赐」天赋"))
        talents = (row.get("talents") or {}).values()
        if any("夜魂" in t["description"] for t in talents):
            out.append(("nightsoul", "游戏数据：技能有夜魂加持"))
        return out


def _read(path: Path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_gamedata(data_dir: Path | str = DEFAULT_DATA_DIR) -> GameData:
    data_dir = Path(data_dir)
    game = data_dir / "game"
    gd = GameData()
    if not game.exists():
        return gd
    for path in sorted((game / "characters").glob("*.yaml")):
        row = _read(path)
        gd.characters[row["id"]] = row
    gd.weapons = {w["id"]: w for w in _read(game / "weapons.yaml")["weapons"]}
    gd.artifact_sets = {a["id"]: a for a in _read(game / "artifacts.yaml")["artifact_sets"]}
    gd.terms = {t["name"]: t for t in _read(game / "glossary.yaml")["terms"]}
    gd.meta = _read(game / "meta.yaml")
    if (game / "talent_multipliers.yaml").exists():
        gd.talent_multipliers = _read(game / "talent_multipliers.yaml")
        gd.level_multipliers = {int(k): v for k, v in _read(game / "level_multiplier.yaml")["level_multiplier"].items()}
    mech = data_dir / "mechanics"
    if mech.exists():
        gd.mechanics = {p.stem: _read(p) for p in sorted(mech.glob("*.yaml"))}
    return gd
