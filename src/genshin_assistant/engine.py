"""推荐引擎：命座展开 → 填槽打分 → 整队限制 → 替补差异 → 养成方案。

全部是确定性代码，大模型只调用这里的结果并负责解释。
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

from .conditions import check, evaluate
from .kb import KnowledgeBase
from .models import Build, Level, Need, Slot, TeamTemplate

# 候选名单之外、由引擎自动找到的角色的基础分
AUTO_CANDIDATE_SCORE = 0.5


@dataclass
class Owned:
    cons: int = 0
    weapons: set[str] | None = None  # None 表示不限制武器


# 角色 id → 拥有情况；roster 为 None 表示假定拥有全部角色（0 命）
Roster = dict[str, Owned] | None


@dataclass
class Kit:
    """某角色在某玩法、某命座下实际生效的功能和需求。"""

    char: str
    zh: str
    element: str
    playstyle: str
    cons: int
    provides: dict[str, float]
    needs: list[Need]


@dataclass
class Member:
    slot: str
    char: str
    zh: str
    playstyle: str
    cons: int
    slot_score: float
    build: dict | None = None


@dataclass
class NeedStatus:
    owner: str
    tag: str
    tag_zh: str
    reason: str
    met_by: list[str]
    penalty: float


@dataclass
class SubstitutionDiff:
    slot: str
    preferred: str
    chosen: str
    preferred_owned: bool
    kept: list[str]
    lost: list[str]
    gained: list[str] = field(default_factory=list)
    override_reason: str | None = None
    build_changes: list[str] = field(default_factory=list)


@dataclass
class Exclusion:
    slot: str
    char: str
    reason: str


@dataclass
class TeamResult:
    template: str
    template_zh: str
    score: float
    members: list[Member]
    needs: list[NeedStatus]
    substitutions: list[SubstitutionDiff]
    verified: bool


@dataclass
class InvalidTemplate:
    template: str
    template_zh: str
    reason: str


@dataclass
class Recommendation:
    core: str
    core_zh: str
    cons: int
    teams: list[TeamResult]
    invalid: list[InvalidTemplate]
    excluded: list[Exclusion]

    def to_dict(self) -> dict:
        return asdict(self)


class Engine:
    def __init__(self, kb: KnowledgeBase):
        self.kb = kb

    # ---------- 命座展开 ----------

    def kit(self, char_id: str, playstyle: str, cons: int) -> Kit | None:
        """返回生效的功能与需求；玩法在该命座下不可用时返回 None。"""
        c = self.kb.characters[char_id]
        p = c.playstyle(playstyle)
        ctx = {"cons": cons}
        if not check(p.when, ctx):
            return None
        provides: dict[str, float] = {}
        for g in p.provides:
            if check(g.when, ctx):
                provides[g.tag] = max(provides.get(g.tag, 0.0), g.score)
        needs = [n for n in p.needs if check(n.when, ctx)]
        return Kit(c.id, c.zh, c.element.value, p.id, cons, provides, needs)

    def available_playstyles(self, char_id: str, cons: int) -> list[str]:
        c = self.kb.characters[char_id]
        return [p.id for p in c.playstyles if check(p.when, {"cons": cons})]

    # ---------- 推荐 ----------

    def recommend(self, core: str, roster: Roster = None, playstyle: str | None = None, top_n: int = 3) -> Recommendation:
        core_char = self.kb.resolve(core)
        core_cons = self._cons(core_char.id, roster)
        results: list[TeamResult] = []
        invalid: list[InvalidTemplate] = []
        excluded: dict[tuple[str, str], Exclusion] = {}

        for t in self.kb.teams_for(core_char.id):
            if playstyle and t.core.playstyle != playstyle:
                continue
            if self.kit(core_char.id, t.core.playstyle, core_cons) is None:
                invalid.append(InvalidTemplate(t.id, t.zh, f"{core_char.zh} 当前命座下不能使用该玩法"))
                continue
            teams, why_invalid, excl = self._fill(t, core_char.id, roster)
            results.extend(teams)
            if why_invalid:
                invalid.append(InvalidTemplate(t.id, t.zh, why_invalid))
            for e in excl:
                excluded.setdefault((e.slot, e.char), e)

        results.sort(key=lambda r: r.score, reverse=True)
        seen: set[frozenset[str]] = set()
        top: list[TeamResult] = []
        for r in results:
            key = frozenset(m.char for m in r.members)
            if key in seen:
                continue
            seen.add(key)
            top.append(r)
            if len(top) == top_n:
                break
        for r in top:
            for m in r.members:
                b = self.build(m.char, m.playstyle, m.cons, self._weapons(m.char, roster))
                m.build = b
        return Recommendation(core_char.id, core_char.zh, core_cons, top, invalid, list(excluded.values()))

    def _cons(self, char_id: str, roster: Roster) -> int:
        if roster is None or char_id not in roster:
            return 0
        return roster[char_id].cons

    def _weapons(self, char_id: str, roster: Roster) -> set[str] | None:
        if roster is None or char_id not in roster:
            return None
        return roster[char_id].weapons

    def _owns(self, char_id: str, roster: Roster) -> bool:
        return roster is None or char_id in roster

    def _slot_candidates(self, slot: Slot, core_id: str, roster: Roster) -> tuple[list[tuple[Kit, float]], list[Exclusion]]:
        if slot.fixed:
            k = self.kit(slot.fixed.char, slot.fixed.playstyle, self._cons(slot.fixed.char, roster))
            return ([(k, 1.0)] if k else []), []

        core_tags = slot.core_tags()
        out: list[tuple[Kit, float]] = []
        excl: list[Exclusion] = []
        listed: set[str] = set()

        def consider(char_id: str, pid: str, base: float, is_listed: bool) -> None:
            if char_id == core_id or not self._owns(char_id, roster):
                return
            k = self.kit(char_id, pid, self._cons(char_id, roster))
            if k is None:
                return
            missing = [t for t in core_tags if k.provides.get(t, 0) <= 0]
            if missing:
                if is_listed:
                    excl.append(Exclusion(slot.id, char_id, "缺少核心功能：" + "、".join(self._tag_zh(t) for t in missing)))
                return
            out.append((k, base))

        for cand in slot.candidates:
            listed.add(cand.char)
            consider(cand.char, cand.playstyle, cand.score, True)
        if slot.auto_candidates and core_tags:
            # 只有声明了核心功能的槽位才自动扩展，避免把无关角色塞进来
            for c in self.kb.characters.values():
                if c.id in listed:
                    continue
                for pid in self.available_playstyles(c.id, self._cons(c.id, roster)):
                    consider(c.id, pid, AUTO_CANDIDATE_SCORE, False)
        return out, excl

    def _fill(self, t: TeamTemplate, core_id: str, roster: Roster) -> tuple[list[TeamResult], str | None, list[Exclusion]]:
        per_slot: list[list[tuple[Kit, float]]] = []
        exclusions: list[Exclusion] = []
        for s in t.slots:
            cands, excl = self._slot_candidates(s, core_id, roster)
            exclusions.extend(excl)
            if not cands:
                core = "、".join(self._tag_zh(x) for x in s.core_tags()) or "（无）"
                return [], f"槽位「{s.zh or s.id}」没有可用角色，核心功能：{core}", exclusions
            per_slot.append(cands)

        teams: list[TeamResult] = []
        in_valid_team: set[tuple[str, str]] = set()
        constraint_hits: dict[tuple[str, str], Counter] = defaultdict(Counter)
        # 每槽候选通常不超过十几个，直接穷举；数据变多后再换成束搜索
        for combo in itertools.product(*per_slot):
            chars = [k.char for k, _ in combo]
            if len(set(chars)) != len(chars):
                continue
            failed = self._failed_constraints(t, [k for k, _ in combo])
            pairs = [(s.id, k.char) for s, (k, _) in zip(t.slots, combo) if not s.fixed]
            if failed:
                for p in pairs:
                    constraint_hits[p][failed[0].id] += 1
                continue
            in_valid_team.update(pairs)
            teams.append(self._score(t, combo, roster))

        reasons = {k.id: k.reason for k in t.constraints}
        for pair, hits in constraint_hits.items():
            if pair not in in_valid_team:
                exclusions.append(Exclusion(pair[0], pair[1], "违反整队限制：" + reasons[hits.most_common(1)[0][0]]))
        if not teams:
            return [], "没有满足整队限制的组合", exclusions
        return teams, None, exclusions

    def _failed_constraints(self, t: TeamTemplate, kits: list[Kit]):
        elements = Counter(k.element for k in kits)
        funcs = {
            "count": lambda e: elements.get(e, 0),
            "max_count": lambda es: max((elements.get(e, 0) for e in es), default=0),
            "has_tag": lambda tag: any(k.provides.get(tag, 0) > 0 for k in kits),
            "has_char": lambda c: any(k.char == c for k in kits),
        }
        return [k for k in t.constraints if not evaluate(k.rule, {}, funcs)]

    def _score(self, t: TeamTemplate, combo, roster: Roster) -> TeamResult:
        members: list[Member] = []
        slot_scores: list[float] = []
        for s, (k, base) in zip(t.slots, combo):
            extra = [f for f in s.functions if f.level != Level.core]
            total = sum(f.weight for f in extra)
            covered = sum(f.weight * k.provides.get(f.tag, 0) for f in extra)
            score = base * (1 + covered) / (1 + total)
            slot_scores.append(score)
            members.append(Member(s.id, k.char, k.zh, k.playstyle, k.cons, round(score, 3)))

        kits = [k for k, _ in combo]
        needs: list[NeedStatus] = []
        penalty_total = 0.0
        for owner in kits:
            for n in owner.needs:
                providers = [k for k in kits if k.provides.get(n.tag, 0) > 0]
                best = max((k.provides[n.tag] for k in providers), default=0.0)
                penalty = n.weight * (1 - best)
                penalty_total += penalty
                needs.append(NeedStatus(owner.zh, n.tag, self._tag_zh(n.tag), n.reason, [k.zh for k in providers], round(penalty, 3)))

        score = sum(slot_scores) / len(slot_scores) - penalty_total
        subs = self._diffs(t, combo, roster)
        return TeamResult(t.id, t.zh, round(score, 3), members, needs, subs, t.verified)

    def _diffs(self, t: TeamTemplate, combo, roster: Roster) -> list[SubstitutionDiff]:
        diffs: list[SubstitutionDiff] = []
        overrides = {(o.original, o.substitute): o for o in t.substitutions}
        for s, (k, _) in zip(t.slots, combo):
            if s.fixed or not s.candidates:
                continue
            pref = max(s.candidates, key=lambda c: c.score)
            if pref.char == k.char:
                continue
            pref_kit = self.kit(pref.char, pref.playstyle, self._cons(pref.char, roster))
            pref_tags = set(pref_kit.provides) if pref_kit else {f.tag for f in s.functions}
            kept = sorted(tag for tag in pref_tags if k.provides.get(tag, 0) > 0)
            lost = sorted(tag for tag in pref_tags if k.provides.get(tag, 0) <= 0)
            gained = sorted(tag for tag in k.provides if tag not in pref_tags)
            o = overrides.get((pref.char, k.char))
            diffs.append(SubstitutionDiff(
                slot=s.id,
                preferred=self.kb.characters[pref.char].zh,
                chosen=k.zh,
                preferred_owned=self._owns(pref.char, roster),
                kept=[self._tag_zh(x) for x in kept],
                lost=[self._tag_zh(x) for x in lost],
                gained=[self._tag_zh(x) for x in gained],
                override_reason=o.reason if o else None,
                build_changes=o.build_changes if o else [],
            ))
        return diffs

    # ---------- 养成方案 ----------

    def build(self, char_id: str, playstyle: str, cons: int, owned_weapons: set[str] | None = None) -> dict | None:
        b = self._find_build(char_id, playstyle, cons)
        if b is None:
            return None
        weapons = [
            {**w.model_dump(), "owned": owned_weapons is None or w.id in owned_weapons or w.source == "craftable"}
            for w in sorted(b.weapons, key=lambda w: w.rank)
        ]
        best = next((w for w in weapons if w["owned"]), None)
        return {
            "char": char_id,
            "playstyle": playstyle,
            "cons": b.cons,
            "recommended_weapon": best,
            "weapons": weapons,
            "artifact_sets": [[a.model_dump() for a in combo] for combo in b.artifact_sets],
            "main_stats": b.main_stats,
            "substats": b.substats,
            "energy_recharge": b.energy_recharge,
            "talents": b.talents,
            "verified": b.verified,
        }

    def _find_build(self, char_id: str, playstyle: str, cons: int) -> Build | None:
        for b in self.kb.builds:
            lo, hi = b.cons_range()
            if b.char == char_id and b.playstyle == playstyle and lo <= cons <= hi:
                return b
        return None

    def _tag_zh(self, tag: str) -> str:
        t = self.kb.tags.get(tag)
        return t.zh if t else tag
