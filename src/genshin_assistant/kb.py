"""加载并校验 data/ 下的知识库。"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Build, Character, Tag, TeamTemplate

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class KnowledgeBaseError(ValueError):
    pass


@dataclass
class KnowledgeBase:
    tags: dict[str, Tag] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)
    teams: dict[str, TeamTemplate] = field(default_factory=dict)
    builds: list[Build] = field(default_factory=list)

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
            if tag not in self.tags:
                errors.append(f"{where}：未定义的标签 {tag}")

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
                for g in p.provides:
                    tag_ok(g.tag, where)
                    when_ok(g.when, where)
                for n in p.needs:
                    tag_ok(n.tag, where)
                    when_ok(n.when, where)

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

        if game is not None and game.characters:
            errors += _check_game_names(self, game)

        if errors:
            raise KnowledgeBaseError("知识库校验失败：\n" + "\n".join(errors))


def _check_game_names(kb: KnowledgeBase, game) -> list[str]:
    errors = []
    char_names = {c["zh"] for c in game.characters.values()}
    weapon_names = {w["zh"] for w in game.weapons.values()}
    set_names = {a["zh"] for a in game.artifact_sets.values()}
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
    if validate:
        from .gamedata import load_gamedata

        kb.validate(load_gamedata(data_dir))
    return kb
