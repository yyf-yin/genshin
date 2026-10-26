"""游戏名词表：纠正 AI 字幕的同音错字，把抽取结果里的名字归一成知识库 id。

名词来自两处：知识库（角色、别名、配装里的武器和圣遗物）和 data/bili/glossary.yaml（知识库还没录入的角色、
武器、圣遗物，以及常见错字）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from ..kb import KnowledgeBase

try:  # 可选依赖：pip install -e ".[bili]"
    from pypinyin import lazy_pinyin
except ImportError:  # pragma: no cover
    lazy_pinyin = None

_HAN = re.compile(r"[一-鿿]")


@dataclass
class Term:
    id: str
    zh: str
    kind: str  # character / weapon / artifact / term
    aliases: list[str] = field(default_factory=list)
    # 两个字的词默认不做同音纠错（“甘雨”和“干预”同音），要纠就在 glossary.yaml 里写 homophone: true
    homophone: bool | None = None
    since: date | None = None  # 上线日期：投票时判断旧视频能不能提到这个角色


@dataclass
class Glossary:
    terms: list[Term]
    typos: dict[str, str]
    reworks: dict[str, list[tuple[date, str]]] = field(default_factory=dict)  # 角色 id → [(加强日期, 版本)]
    playstyle_aliases: dict[str, str] = field(default_factory=dict)  # 玩法别名 → 标准名（超导 → 星超导）
    playstyle_parents: dict[str, list[str]] = field(default_factory=dict)  # 统称 → 具体玩法（星反应 → 星超导、星扩散）
    _index: dict[str, Term] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for t in self.terms:
            for name in (t.id, t.zh, *t.aliases):
                self._index.setdefault(name, t)

    def releases(self) -> dict[str, date]:
        return {t.id: t.since for t in self.terms if t.kind == "character" and t.since}

    def names(self) -> dict[str, str]:
        return {t.id: t.zh for t in self.terms}

    def resolve(self, name: str, kind: str | None = None) -> Term | None:
        t = self._index.get(str(name).strip())
        return t if t and (kind is None or t.kind == kind) else None

    def for_prompt(self, kinds=("character",)) -> str:
        """给大模型看的名词表：id、中文名、别名。"""
        rows = [f"{t.id}: {t.zh}" + (f"（{'、'.join(t.aliases)}）" if t.aliases else "")
                for t in self.terms if t.kind in kinds]
        return "\n".join(rows)

    # ── 纠错 ──
    def fix_typos(self, text: str, counter: Counter | None = None) -> str:
        for wrong, right in self.typos.items():
            if wrong in text:
                if counter is not None:
                    counter[(wrong, right)] += text.count(wrong)
                text = text.replace(wrong, right)
        return text

    def _homophone_targets(self) -> list[str]:
        out = []
        for t in self.terms:
            for name in (t.zh, *t.aliases):
                if not name or not all(_HAN.match(c) for c in name):
                    continue
                if len(name) >= 3 or (t.homophone and len(name) >= 2):
                    out.append(name)
        return sorted(set(out), key=len, reverse=True)

    def fix_homophones(self, text: str, counter: Counter | None = None) -> str:
        """把读音和某个名词完全相同、但字不同的片段换成正确写法（不看声调）。没装 pypinyin 就跳过。"""
        if lazy_pinyin is None or not _HAN.search(text):
            return text
        targets = getattr(self, "_targets", None)
        if targets is None:
            targets = self._targets = [(n, tuple(lazy_pinyin(n))) for n in self._homophone_targets()]
        chars = list(text)
        py = [lazy_pinyin(c)[0] if _HAN.match(c) else None for c in chars]
        i = 0
        out: list[str] = []
        while i < len(chars):
            for name, sound in targets:
                n = len(name)
                if tuple(py[i:i + n]) == sound:
                    window = "".join(chars[i:i + n])
                    if window != name and counter is not None:
                        counter[(window, name)] += 1
                    out.append(name)
                    i += n
                    break
            else:
                out.append(chars[i])
                i += 1
        return "".join(out)

    def correct(self, text: str, counter: Counter | None = None, asr: bool = True) -> str:
        text = self.fix_typos(text, counter)
        return self.fix_homophones(text, counter) if asr else text


def _date(v) -> date | None:
    return v if isinstance(v, date) or v is None else date.fromisoformat(str(v))


def load_glossary(kb: KnowledgeBase, path: Path | None, releases_path: Path | None = None) -> Glossary:
    """releases_path（data/releases.yaml）：全部角色的上线日期和加强版本，补进名词表。"""
    terms: list[Term] = [Term(c.id, c.zh, "character", list(c.aliases)) for c in kb.characters.values()]
    for b in kb.builds:
        terms += [Term(w.id, w.zh, "weapon") for w in b.weapons]
        terms += [Term(a.set, a.zh, "artifact") for combo in b.artifact_sets for a in combo]
    typos: dict[str, str] = {}
    ps_aliases: dict[str, str] = {}
    ps_parents: dict[str, list[str]] = {}
    if path and path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for kind in ("character", "weapon", "artifact", "term"):
            for t in raw.get(kind + "s") or []:
                since = t.get("since")
                terms.append(Term(t["id"], t["zh"], kind, list(t.get("aliases") or []), t.get("homophone"),
                                  _date(since)))
        typos = {str(k): str(v) for k, v in (raw.get("typos") or {}).items()}
        ps = raw.get("playstyles") or {}
        ps_aliases = {str(k): str(v) for k, v in (ps.get("aliases") or {}).items()}
        ps_parents = {str(k): [str(x) for x in v] for k, v in (ps.get("parents") or {}).items()}
    reworks: dict[str, list[tuple[date, str]]] = {}
    if releases_path and releases_path.exists():
        rel = yaml.safe_load(releases_path.read_text(encoding="utf-8")) or {}
        for c in rel.get("characters") or []:
            terms.append(Term(c["id"], c["zh"], "character", list(c.get("aliases") or []), since=_date(c.get("since"))))
        for r in rel.get("reworks") or []:
            for cid in r.get("characters") or []:
                reworks.setdefault(cid, []).append((_date(r["date"]), str(r.get("version", ""))))
    # 知识库优先：同一个 id 只留第一次出现的（上线日期只写在 glossary.yaml 里，合并过来）
    seen: dict[str, Term] = {}
    uniq = []
    for t in terms:
        if t.id not in seen:
            seen[t.id] = t
            uniq.append(t)
        else:
            if t.since and not seen[t.id].since:
                seen[t.id].since = t.since
            seen[t.id].aliases += [a for a in t.aliases if a not in seen[t.id].aliases]
    return Glossary(uniq, typos, reworks, ps_aliases, ps_parents)
