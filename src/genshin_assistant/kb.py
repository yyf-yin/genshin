"""加载并校验 data/ 下的知识库。"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Build, Character, Element, EquipmentBuff, Tag, TagGrant, TeamTemplate

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


ELEMENT_ZH = {"pyro": "火", "hydro": "水", "electro": "雷", "cryo": "冰", "anemo": "风", "geo": "岩", "dendro": "草"}
assert set(ELEMENT_ZH) == {e.value for e in Element}
# buff:<乘区> 的乘区，对应 mechanics/damage.yaml
ZONE_ZH = {"atk_flat": "固定攻击力", "atk_pct": "百分比攻击力", "hp_pct": "百分比生命值", "flat_dmg": "倍率附加",
           "dmg_bonus": "百分比增伤", "crit_rate": "暴击率", "crit_dmg": "暴击伤害", "elevate": "擢升", "em": "元素精通"}


class KnowledgeBaseError(ValueError):
    pass


@dataclass
class KnowledgeBase:
    tags: dict[str, Tag] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)
    teams: dict[str, TeamTemplate] = field(default_factory=dict)
    builds: list[Build] = field(default_factory=list)
    reactions: dict[str, str] = field(default_factory=dict)  # id → 中文名，来自 mechanics/reactions.yaml
    equipment: list[EquipmentBuff] = field(default_factory=list)

    def equipment_buff(self, kind: str, zh: str, pieces: int = 4) -> EquipmentBuff | None:
        for e in self.equipment:
            if e.kind == kind and e.zh == zh and (kind == "weapon" or pieces >= e.pieces):
                return e
        return None

    def tag_zh(self, tag: str) -> str:
        """element:cryo → 元素：冰 这类带参数的标签也能显示。"""
        base, _, param = tag.partition(":")
        t = self.tags.get(base)
        if t is None:
            return tag
        if not param:
            return t.zh
        if param == "*":
            return f"任意{t.zh}"
        return f"{t.zh}：{self.reactions.get(param) or ELEMENT_ZH.get(param) or ZONE_ZH.get(param, param)}"

    def tag_error(self, tag: str) -> str | None:
        base, sep, param = tag.partition(":")
        if param == "*" and base in self.tags and self.tags[base].param:
            return None  # 通配：同类标签任意一个
        t = self.tags.get(base)
        if t is None:
            return f"未定义的标签 {tag}"
        if bool(sep) != bool(t.param):
            return f"标签 {base} {'需要' if t.param else '不带'}参数：{tag}"
        if t.param == "element" and param not in ELEMENT_ZH:
            return f"未知元素 {param}：{tag}"
        if t.param == "zone" and param not in ZONE_ZH:
            return f"未知乘区 {param}（可选：{'、'.join(ZONE_ZH)}）：{tag}"
        if t.param == "reaction" and self.reactions and param not in self.reactions:
            return f"未知反应 {param}（见 mechanics/reactions.yaml）：{tag}"
        return None

    def resolve(self, name: str) -> Character:
        """按 id、中文名或别名找角色。"""
        for c in self.characters.values():
            if name in (c.id, c.zh) or name in c.aliases:
                return c
        raise KeyError(f"找不到角色：{name}")

    def teams_for(self, char_id: str) -> list[TeamTemplate]:
        return [t for t in self.teams.values() if t.core.char == char_id]

    def validate(self, game=None) -> None:
        """game 是 gamedata.GameData；给了就顺便核对角色、武器、圣遗物的中文名是否和游戏一致。"""
        errors: list[str] = []

        def tag_ok(tag: str, where: str) -> None:
            if err := self.tag_error(tag):
                errors.append(f"{where}：{err}")

        def when_ok(expr: str | None, where: str) -> None:
            if expr:
                try:
                    ast.parse(expr, mode="eval")
                except SyntaxError:
                    errors.append(f"{where}：条件语法错误 {expr!r}")

        def ref_ok(char: str, playstyle: str, where: str) -> None:
            c = self.characters.get(char)
            if c is None:
                errors.append(f"{where}：未定义的角色 {char}")
            elif playstyle not in {p.id for p in c.playstyles}:
                errors.append(f"{where}：{char} 没有玩法 {playstyle}")

        for c in self.characters.values():
            for p in c.playstyles:
                where = f"角色 {c.id}/{p.id}"
                when_ok(p.when, where)
                for g in [*p.provides, *p.traits]:
                    tag_ok(g.tag, where)
                    when_ok(g.when, where)
                for n in [*p.needs, *p.avoids]:
                    tag_ok(n.tag, where)
                    when_ok(n.when, where)
                for r in p.team_rules:
                    when_ok(r.rule, f"{where} 规则 {r.id}")
                    when_ok(r.when, f"{where} 规则 {r.id}")

        for t in self.teams.values():
            ref_ok(t.core.char, t.core.playstyle, f"队伍 {t.id} 核心")
            for s in t.slots:
                where = f"队伍 {t.id} 槽位 {s.id}"
                if s.fixed:
                    ref_ok(s.fixed.char, s.fixed.playstyle, where)
                for f in s.functions:
                    tag_ok(f.tag, where)
                for cand in s.candidates:
                    ref_ok(cand.char, cand.playstyle, where)
            for k in t.constraints:
                when_ok(k.rule, f"队伍 {t.id} 限制 {k.id}")
            for o in t.substitutions:
                for ch in (o.original, o.substitute):
                    if ch not in self.characters:
                        errors.append(f"队伍 {t.id} 替补覆盖：未定义的角色 {ch}")

        for b in self.builds:
            ref_ok(b.char, b.playstyle, f"配装 {b.char}/{b.playstyle}")

        for e in self.equipment:
            for g in e.provides:
                tag_ok(g.tag, f"装备增益 {e.zh}")

        if game is not None and game.characters:
            errors += _check_game_names(self, game)

        if errors:
            raise KnowledgeBaseError("知识库校验失败：\n" + "\n".join(errors))


def _check_game_names(kb: KnowledgeBase, game) -> list[str]:
    errors = []
    char_names = {c["zh"] for c in game.characters.values()}
    weapon_names = {w["zh"] for w in game.weapons.values()}
    set_names = {a["zh"] for a in game.artifact_sets.values()}
    for e in kb.equipment:
        if e.zh not in (weapon_names if e.kind == "weapon" else set_names):
            errors.append(f"装备增益：「{e.zh}」不是游戏里的正式名")
    for c in kb.characters.values():
        if c.zh not in char_names:
            errors.append(f"角色 {c.id}：中文名「{c.zh}」不是游戏里的正式名")
    for b in kb.builds:
        for w in b.weapons:
            if w.zh not in weapon_names:
                errors.append(f"配装 {b.char}：武器「{w.zh}」不是游戏里的正式名")
        for combo in b.artifact_sets:
            for a in combo:
                if a.zh not in set_names:
                    errors.append(f"配装 {b.char}：圣遗物「{a.zh}」不是游戏里的正式名")
    return errors


def _load_yaml_docs(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if d]


def load(data_dir: Path | str = DEFAULT_DATA_DIR, validate: bool = True) -> KnowledgeBase:
    data_dir = Path(data_dir)
    kb = KnowledgeBase()
    for raw in _load_yaml_docs(data_dir / "tags.yaml")[0]["tags"]:
        tag = Tag(**raw)
        kb.tags[tag.id] = tag
    reactions = data_dir / "mechanics" / "reactions.yaml"
    if reactions.exists():
        kb.reactions = {r["id"]: r["zh"] for r in _load_yaml_docs(reactions)[0]["reactions"]}
    equipment = data_dir / "equipment_buffs.yaml"
    if equipment.exists():
        kb.equipment = [EquipmentBuff(**raw) for raw in _load_yaml_docs(equipment)[0]["equipment"]]
    for path in sorted((data_dir / "characters").glob("*.yaml")):
        for raw in _load_yaml_docs(path):
            c = Character(**raw)
            kb.characters[c.id] = c
    for path in sorted((data_dir / "teams").glob("*.yaml")):
        for raw in _load_yaml_docs(path):
            t = TeamTemplate(**raw)
            kb.teams[t.id] = t
    for path in sorted((data_dir / "builds").glob("*.yaml")):
        kb.builds.extend(Build(**raw) for raw in _load_yaml_docs(path))
    from .gamedata import load_gamedata

    game = load_gamedata(data_dir)
    _add_identity_tags(kb, game)
    if validate:
        kb.validate(game)
    return kb


def _add_identity_tags(kb: KnowledgeBase, game) -> None:
    """enables、魔导、月兆、夜魂由游戏数据决定，不用手写；手写了的以手写为准。"""
    for c in kb.characters.values():
        tags = game.identity_tags(c.zh)
        for ps in c.playstyles:
            have = {g.tag for g in ps.provides}
            ps.provides.extend(TagGrant(tag=t, evidence=e, source="game") for t, e in tags if t not in have)
