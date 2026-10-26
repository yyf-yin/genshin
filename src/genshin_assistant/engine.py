"""推荐引擎：命座展开 → 填槽打分 → 整队限制 → 替补差异 → 养成方案。

全部是确定性代码，大模型只调用这里的结果并负责解释。
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field

from .conditions import check, evaluate
from .kb import KnowledgeBase
from .models import Avoid, Build, Level, Need, Slot, TeamRule, TeamTemplate

# 候选名单之外、由引擎自动找到的角色的基础分
AUTO_CANDIDATE_SCORE = 0.5
# 每多一名必须站场（field: on_field）的角色扣的分：两个站场角色会互相抢场上时间
FIELD_CONFLICT_PENALTY = 0.3


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
    avoids: list[Avoid] = field(default_factory=list)
    rules: list[TeamRule] = field(default_factory=list)
    position: str | None = None  # 玩法的 field：on_field / off_field / flex
    traits: dict[str, float] = field(default_factory=dict)
    gear: list[str] = field(default_factory=list)  # 配装里会给队友加成的装备，见 equipment_buffs.yaml

    def has(self, tag: str) -> float:
        """功能强度。「buff:*」这种通配写法取同类标签里最高的，槽位可以写「任何增益」。"""
        if tag.endswith(":*"):
            prefix = tag[:-1]
            return max((v for t, v in self.provides.items() if t.startswith(prefix)), default=0.0)
        return self.provides.get(tag, 0.0)


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
class Conflict:
    """扣分项：角色排斥的队友功能、没满足的角色软规则、站场冲突。"""

    kind: str  # avoid / rule / field
    owner: str
    reason: str
    involved: list[str]
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
    conflicts: list[Conflict] = field(default_factory=list)


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
        # 元素是每个角色都有的「功能」，让 needs 可以写 element:hydro
        provides: dict[str, float] = {f"element:{c.element.value}": 1.0}
        for g in p.provides:
            if check(g.when, ctx):
                provides[g.tag] = max(provides.get(g.tag, 0.0), g.score)
        needs = [n for n in p.needs if check(n.when, ctx)]
        avoids = [a for a in p.avoids if check(a.when, ctx)]
        rules = [r for r in p.team_rules if check(r.when, ctx)]
        traits = {t.tag: t.score for t in p.traits if check(t.when, ctx)}
        gear: list[str] = []
        b = self._find_build(c.id, p.id, cons)
        if b is not None:
            # 只看首选配装：首选武器、首选圣遗物组合
            picks = [("weapon", w.zh, 1) for w in sorted(b.weapons, key=lambda w: w.rank)[:1]]
            picks += [("artifact_set", a.zh, a.pieces) for a in (b.artifact_sets[0] if b.artifact_sets else [])]
            for kind, zh, pieces in picks:
                e = self.kb.equipment_buff(kind, zh, pieces)
                if e is None:
                    continue
                gear.append(e.zh)
                for g in e.provides:
                    if check(g.when, ctx):
                        provides[g.tag] = max(provides.get(g.tag, 0.0), g.score)
        return Kit(c.id, c.zh, c.element.value, p.id, cons, provides, needs, avoids, rules, p.field, traits, gear)

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
            missing = [t for t in core_tags if k.has(t) <= 0]
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
            failed += [r for r in self._failed_rules([k for k, _ in combo]) if r.hard]
            pairs = [(s.id, k.char) for s, (k, _) in zip(t.slots, combo) if not s.fixed]
            if failed:
                for p in pairs:
                    constraint_hits[p][failed[0].id] += 1
                continue
            in_valid_team.update(pairs)
            teams.append(self._score(t, combo, roster))

        reasons = {k.id: k.reason for k in t.constraints}
        reasons.update({r.id: r.reason for s in per_slot for k, _ in s for r in k.rules})
        for pair, hits in constraint_hits.items():
            if pair not in in_valid_team:
                exclusions.append(Exclusion(pair[0], pair[1], "违反整队限制：" + reasons[hits.most_common(1)[0][0]]))
        if not teams:
            return [], "没有满足整队限制的组合", exclusions
        return teams, None, exclusions

    @staticmethod
    def _team_funcs(kits: list[Kit]) -> dict:
        elements = Counter(k.element for k in kits)
        return {
            "count": lambda e: elements.get(e, 0),
            "count_any": lambda es: sum(elements.get(e, 0) for e in es),
            "max_count": lambda es: max((elements.get(e, 0) for e in es), default=0),
            "has_tag": lambda tag: any(k.has(tag) > 0 for k in kits),
            "has_char": lambda c: any(k.char == c for k in kits),
        }

    def _failed_constraints(self, t: TeamTemplate, kits: list[Kit]):
        funcs = self._team_funcs(kits)
        return [k for k in t.constraints if not evaluate(k.rule, {}, funcs)]

    def _failed_rules(self, kits: list[Kit]) -> list[TeamRule]:
        """队员自带的整队规则里没满足的那些。"""
        funcs = self._team_funcs(kits)
        return [r for k in kits for r in k.rules if not evaluate(r.rule, {}, funcs)]

    def _score(self, t: TeamTemplate, combo, roster: Roster) -> TeamResult:
        members: list[Member] = []
        slot_scores: list[float] = []
        for s, (k, base) in zip(t.slots, combo):
            extra = [f for f in s.functions if f.level != Level.core]
            total = sum(f.weight for f in extra)
            covered = sum(f.weight * k.has(f.tag) for f in extra)
            score = base * (1 + covered) / (1 + total)
            slot_scores.append(score)
            members.append(Member(s.id, k.char, k.zh, k.playstyle, k.cons, round(score, 3)))

        kits = [k for k, _ in combo]
        needs: list[NeedStatus] = []
        penalty_total = 0.0
        for owner in kits:
            mates = [k for k in kits if k is not owner]
            for n in owner.needs:
                if owner.traits.get(n.tag, 0) > 0:
                    continue  # 自己就有，例如自带抗打断
                providers = [k for k in mates if k.has(n.tag) > 0]
                top = sorted((k.has(n.tag) for k in providers), reverse=True)[: n.count]
                penalty = n.weight * (1 - sum(top) / n.count)
                penalty_total += penalty
                needs.append(NeedStatus(owner.zh, n.tag, self._tag_zh(n.tag), n.reason, [k.zh for k in providers], round(penalty, 3)))

        conflicts: list[Conflict] = []
        for owner in kits:
            for a in owner.avoids:
                offenders = [k for k in kits if k is not owner and k.has(a.tag) > 0]
                if offenders:
                    penalty = a.weight * max(k.has(a.tag) for k in offenders)
                    conflicts.append(Conflict("avoid", owner.zh, f"排斥{self._tag_zh(a.tag)}：{a.reason}",
                                              [k.zh for k in offenders], round(penalty, 3)))
        failed = set(map(id, self._failed_rules(kits)))
        for owner in kits:
            for r in owner.rules:
                if not r.hard and id(r) in failed:
                    conflicts.append(Conflict("rule", owner.zh, r.reason, [owner.zh], r.weight))
        on_field = [k.zh for k in kits if k.position == "on_field"]
        if len(on_field) > 1:
            conflicts.append(Conflict("field", "、".join(on_field), "多名角色都需要站场，会互相抢场上时间", on_field,
                                      round(FIELD_CONFLICT_PENALTY * (len(on_field) - 1), 3)))
        penalty_total += sum(c.penalty for c in conflicts)

        score = sum(slot_scores) / len(slot_scores) - penalty_total
        subs = self._diffs(t, combo, roster)
        return TeamResult(t.id, t.zh, round(score, 3), members, needs, subs, t.verified, conflicts)

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
            # 元素差异另有整队规则把关，这里只比功能
            pref_tags = {x for x in (pref_kit.provides if pref_kit else (f.tag for f in s.functions)) if not x.startswith("element:")}
            kept = sorted(tag for tag in pref_tags if k.has(tag) > 0)
            lost = sorted(tag for tag in pref_tags if k.has(tag) <= 0)
            gained = sorted(tag for tag in k.provides if tag not in pref_tags and not tag.startswith("element:"))
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
            "zh": b.zh,
            "recommended_weapon": best,
            "weapons": weapons,
            "artifact_sets": [[a.model_dump() for a in combo] for combo in b.artifact_sets],
            "main_stats": b.main_stats,
            "substats": b.substats,
            "energy_recharge": b.energy_recharge,
            "energy_recharge_notes": b.energy_recharge_notes,
            "stat_targets": b.stat_targets,
            "talents": b.talents,
            "notes": b.notes,
            "evidence": b.evidence,
            "verified": b.verified,
        }

    def _find_build(self, char_id: str, playstyle: str, cons: int) -> Build | None:
        for b in self.kb.builds:
            lo, hi = b.cons_range()
            if b.char == char_id and b.playstyle == playstyle and lo <= cons <= hi:
                return b
        return None

    def _tag_zh(self, tag: str) -> str:
        return self.kb.tag_zh(tag)
