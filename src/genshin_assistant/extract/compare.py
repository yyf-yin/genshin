"""把草稿和手工录入的知识库对照：每条结论标“一致 / 冲突 / 知识库没有”，并列出知识库里有、视频没提到的内容。

试点（那维莱特、法尔伽、薇斯纳）就靠这份对照判断 AI 草稿靠不靠谱。
"""

from __future__ import annotations

from ..kb import KnowledgeBase
from .schema import _squash
from .vote import LEVEL_ZH, Finding

AGREE, CONFLICT, MISSING, NO_CHAR = "一致", "冲突", "知识库没有", "知识库没有该角色"


def _status(f: Finding, kb: KnowledgeBase, core: str) -> str:
    d = f.data
    team_core = f.subject or core  # 视频里顺带讲的别的主 C 的队伍，和那个主 C 的模板对照
    teams = kb.teams_for(team_core)
    against = f.value == "against"
    if f.kind in ("teammate", "slot_function", "team_constraint", "substitution") and not teams:
        return f"{MISSING}（没有以 {team_core} 为核心的队伍模板）"
    if f.kind == "teammate":
        names = {s.fixed.char for t in teams for s in t.slots if s.fixed}
        names |= {c.char for t in teams for s in t.slots for c in s.candidates}
        hit = d.get("char") in names
        if against:
            return f"{CONFLICT}（视频说不推荐，知识库列为候选）" if hit else AGREE
        return AGREE if hit else MISSING
    if f.kind == "slot_function":
        levels = {fn.level.value for t in teams for s in t.slots for fn in s.functions if fn.tag == d.get("tag")}
        if not levels:
            return MISSING
        if f.value in levels:
            return AGREE
        return f"{CONFLICT}（知识库是{'/'.join(LEVEL_ZH[x] for x in sorted(levels))}）"
    if f.kind == "team_constraint":
        rules = [k.rule for t in teams for k in t.constraints]
        if not rules:
            return MISSING
        if d.get("rule") and _squash(d["rule"]) in {_squash(r) for r in rules}:
            return AGREE
        return f"需对照（知识库已有：{'；'.join(rules)}）"
    if f.kind == "substitution":
        o, s = d.get("original"), d.get("substitute")
        if any(x.original == o and x.substitute == s for t in teams for x in t.substitutions):
            return AGREE
        for t in teams:
            for slot in t.slots:
                cands = {c.char for c in slot.candidates}
                if o in cands and s in cands:
                    return f"{AGREE}（同槽候选）"
        return MISSING
    char = kb.characters.get(f.subject)
    if char is None:
        return NO_CHAR
    if f.kind == "cons_effect":
        cons = _squash(f.cons or "")
        for p in char.playstyles:
            for x in [*p.provides, *p.needs]:
                if x.when and _squash(x.when) == cons and (not d.get("tag") or d["tag"] == x.tag):
                    return AGREE
        return MISSING
    if f.kind == "playstyle":
        name = _squash(d.get("name", ""))
        return AGREE if any(name in (_squash(p.id), _squash(p.zh)) for p in char.playstyles) else MISSING
    if f.kind == "build":
        builds = [b for b in kb.builds if b.char == f.subject]
        if not builds:
            return MISSING
        field, value = d.get("field"), str(d.get("value", ""))
        if field == "weapon":
            ranks = [w.rank for b in builds for w in b.weapons if d.get("id") == w.id or value == w.zh]
            if not ranks:
                return MISSING
            if d.get("rank") and int(d["rank"]) not in ranks:
                return f"{AGREE}（顺位不同：知识库第 {ranks[0]}）"
            return AGREE
        if field == "artifact_set":
            hit = any(d.get("id") == a.set or value == a.zh for b in builds for combo in b.artifact_sets for a in combo)
            return AGREE if hit else MISSING
        if field in ("sands", "goblet", "circlet"):
            stats = {s for b in builds for s in b.main_stats.get(field, [])}
            if not stats:
                return MISSING
            if d.get("id"):
                if d["id"] in stats:
                    return AGREE
                if d.get("rank") and int(d["rank"]) > 1:  # 视频说的是备选，不和知识库的首选冲突
                    return f"需对照（视频给的是备选，知识库：{'/'.join(sorted(stats))}）"
                return f"{CONFLICT}（知识库：{'/'.join(sorted(stats))}）"
            return f"需对照（知识库：{'/'.join(sorted(stats))}）"
        if field == "substat" and d.get("id"):
            subs = {s for b in builds for s in b.substats}
            if d["id"] == "crit":
                return AGREE if {"crit_rate", "crit_dmg"} & subs else MISSING
            return AGREE if d["id"] in subs else MISSING
        if field == "energy_recharge":
            ers = [b.energy_recharge for b in builds if b.energy_recharge]
            return f"需对照（知识库：{ers[0][0]}-{ers[0][1]}）" if ers else MISSING
        return "需对照"
    return MISSING


def annotate(findings: list[Finding], kb: KnowledgeBase, core: str) -> None:
    for f in findings:
        f.kb_status = _status(f, kb, core)


def kb_not_mentioned(findings: list[Finding], kb: KnowledgeBase, core: str) -> list[str]:
    """知识库里有、但没有任何视频提到的内容（看 AI 草稿漏了什么）。"""
    mentioned_chars = {f.data.get("char") for f in findings if f.kind == "teammate"}
    mentioned_chars |= {f.data.get("substitute") for f in findings if f.kind == "substitution"}
    mentioned_tags = {f.data.get("tag") for f in findings if f.kind == "slot_function"}
    mentioned_weapons = {f.data.get("id") or f.data.get("value") for f in findings
                         if f.kind == "build" and f.data.get("field") == "weapon"}
    out = []
    for t in kb.teams_for(core):
        for s in t.slots:
            for c in s.candidates:
                if c.char not in mentioned_chars:
                    out.append(f"队伍 {t.id} 槽位 {s.id} 的候选 {c.char}")
            for fn in s.functions:
                if fn.tag not in mentioned_tags:
                    out.append(f"队伍 {t.id} 槽位 {s.id} 的{LEVEL_ZH[fn.level.value]}功能 {fn.tag}")
        for k in t.constraints:
            out.append(f"队伍 {t.id} 的整队限制 {k.id}（只能人工对照）")
    for b in kb.builds:
        if b.char == core:
            out += [f"{core} 的武器 {w.zh}" for w in b.weapons if w.id not in mentioned_weapons and w.zh not in mentioned_weapons]
    return out
