"""推荐引擎：命座展开 → 填槽打分 → 整队限制 → 替补差异 → 养成方案。

全部是确定性代码，大模型只调用这里的结果并负责解释。
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace

from .conditions import check, evaluate
from .kb import KnowledgeBase
from .models import Avoid, Build, EquipmentBuff, Level, Need, Slot, TeamRule, TeamTemplate, WeaponOption

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
    # 以下给「增益装备一队只能有一件」用：provides 先按首选配装算，组队时可能换成后面的选项
    base_provides: dict[str, float] = field(default_factory=dict)  # 不含装备
    artifact_options: list[list[EquipmentBuff]] = field(default_factory=list)  # 每种圣遗物组合里的增益装备
    weapon_options: list[tuple[WeaponOption, EquipmentBuff | None]] = field(default_factory=list)  # 按档次
    artifact_choice: int = 0
    weapon_choice: str | None = None

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
    artifact_choice: int = 0  # 第几套圣遗物组合（为了不和队友撞增益装备，可能不是首选）
    weapon_choice: str | None = None


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
    # 有伤害数据时：score = 总伤害 / 本次最高总伤害 - penalty；tag_score 是只看标签时的分数
    tag_score: float = 0.0
    penalty: float = 0.0
    damage: dict | None = None


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
        arts: list[list[EquipmentBuff]] = []
        weapons: list[tuple[WeaponOption, EquipmentBuff | None]] = []
        b = self._find_build(c.id, p.id, cons)
        if b is not None:
            arts = [[e for a in combo if (e := self.kb.equipment_buff("artifact_set", a.zh, a.pieces))]
                    for combo in b.artifact_sets]
            weapons = [(w, self.kb.equipment_buff("weapon", w.zh)) for w in sorted(b.weapons, key=lambda w: w.rank)]
        k = Kit(c.id, c.zh, c.element.value, p.id, cons, dict(provides), needs, avoids, rules, p.field, traits,
                base_provides=provides, artifact_options=arts, weapon_options=weapons,
                weapon_choice=weapons[0][0].zh if weapons else None)
        return self._equip(k, 0, k.weapon_choice)

    def _equip(self, k: Kit, artifact_choice: int, weapon_choice: str | None) -> Kit:
        """按选定的圣遗物组合和武器，把装备给队友的加成算进 provides。"""
        provides = dict(k.base_provides)
        gear = list(k.artifact_options[artifact_choice]) if k.artifact_options else []
        gear += [e for w, e in k.weapon_options if w.zh == weapon_choice and e]
        ctx = {"cons": k.cons}
        for e in gear:
            for g in e.provides:
                if check(g.when, ctx):
                    provides[g.tag] = max(provides.get(g.tag, 0.0), g.score)
        return replace(k, provides=provides, gear=[e.zh for e in gear],
                       artifact_choice=artifact_choice, weapon_choice=weapon_choice)

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

        self._rank_by_damage(results)
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
                m.build = self.build(m.char, m.playstyle, m.cons, self._weapons(m.char, roster),
                                     m.artifact_choice, m.weapon_choice)
        return Recommendation(core_char.id, core_char.zh, core_cons, top, invalid, list(excluded.values()))

    def _rank_by_damage(self, results: list[TeamResult]) -> None:
        """有伤害数据的队伍按全队期望总伤害排：总伤害 / 最高总伤害，再减去没满足的需求、冲突的扣分。"""
        best = max((r.damage["total"] for r in results if r.damage), default=0)
        if best <= 0:
            return
        for r in results:
            if r.damage:
                r.score = round(r.damage["total"] / best - r.penalty, 3)

    def _team_damage(self, kits: list[Kit]) -> dict | None:
        """全队一轮的期望总伤害（damage/team.py）。主C没有循环数据时不算。"""
        from .damage.team import MemberSpec, team_damage

        profiles = self.kb.damage_profiles
        if not profiles or self.kb.game is None or (kits[0].char, kits[0].playstyle) not in profiles:
            return None
        members = [
            MemberSpec(k.char, k.zh, k.element, k.playstyle, k.cons, set(k.provides), k.position,
                       self._find_build(k.char, k.playstyle, k.cons), k.weapon_choice, k.artifact_choice,
                       [e for e in self.kb.equipment if e.zh in k.gear])
            for k in kits
        ]
        if members[0].position is None:
            members[0].position = "on_field"
        return team_damage(self.kb.game, members, profiles).to_dict()

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
        gear_reasons: dict[str, str] = {}
        # 每槽候选通常不超过十几个，直接穷举；数据变多后再换成束搜索
        for combo in itertools.product(*per_slot):
            chars = [k.char for k, _ in combo]
            if len(set(chars)) != len(chars):
                continue
            pairs = [(s.id, k.char) for s, (k, _) in zip(t.slots, combo) if not s.fixed]
            kits, gear_conflict = self._assign_gear([k for k, _ in combo], roster)
            if gear_conflict:
                gear_reasons[gear_conflict] = gear_conflict
                for p in pairs:
                    constraint_hits[p][gear_conflict] += 1
                continue
            combo = tuple((k, base) for k, (_, base) in zip(kits, combo))
            lacking = [k.zh for s, k in zip(t.slots, kits) if any(k.has(x) <= 0 for x in s.core_tags())]
            failed = [x.id for x in self._failed_constraints(t, kits)]
            failed += [r.id for r in self._failed_rules(kits) if r.hard]
            if lacking:
                why = f"{'、'.join(lacking)}让出增益装备后缺少槽位核心功能"
                gear_reasons[why] = why
                failed.insert(0, why)
            if failed:
                for p in pairs:
                    constraint_hits[p][failed[0]] += 1
                continue
            in_valid_team.update(pairs)
            teams.append(self._score(t, combo, roster))

        reasons = {k.id: k.reason for k in t.constraints}
        reasons.update({r.id: r.reason for s in per_slot for k, _ in s for r in k.rules})
        reasons.update(gear_reasons)
        for pair, hits in constraint_hits.items():
            if pair not in in_valid_team:
                exclusions.append(Exclusion(pair[0], pair[1], "违反整队限制：" + reasons[hits.most_common(1)[0][0]]))
        if not teams:
            return [], "没有满足整队限制的组合", exclusions
        return teams, None, exclusions

    def _assign_gear(self, kits: list[Kit], roster: Roster) -> tuple[list[Kit], str | None]:
        """增益装备（equipment_buffs.yaml 里的套装和武器）同一件一队只生效一次，所以一队只带一件：
        每人按配装顺序挑第一个不和队友撞的选项，选项序号之和最小的方案胜出。
        有人所有选项都撞时队伍不成立，返回原因。"""
        def pick(options: list[list], label: str) -> tuple[list[int], str | None]:
            best: tuple[int, ...] | None = None
            for choice in itertools.product(*(range(len(o)) for o in options)):
                names = [n for o, i in zip(options, choice) for n in o[i]]
                if len(names) == len(set(names)) and (best is None or sum(choice) < sum(best)):
                    best = choice
            if best is not None:
                return list(best), None
            firsts = Counter(n for o in options for n in o[0])
            dup = next((n for n, c in firsts.items() if c > 1), "")
            who = [k.zh for k, o in zip(kits, options) if dup in o[0]]
            return [], f"{'、'.join(who)}都要带{dup}，同一件增益{label}一队只能有一件"

        arts = [[[e.zh for e in combo] for combo in k.artifact_options] or [[]] for k in kits]
        held = []  # 每人拥有的武器（没限制武器时是全部），按档次
        for k in kits:
            owned = self._weapons(k.char, roster)
            ws = [w for w, _ in k.weapon_options if owned is None or w.id in owned or w.source == "craftable"]
            held.append(ws or [w for w, _ in k.weapon_options[:1]])
        buff = {k.char: {w.zh: e for w, e in k.weapon_options} for k in kits}
        weapons = [[[buff[k.char][w.zh].zh] if buff[k.char][w.zh] else [] for w in ws] or [[]] for k, ws in zip(kits, held)]
        a_choice, why = pick(arts, "圣遗物")
        if why:
            return kits, why
        w_choice, why = pick(weapons, "武器")
        if why:
            return kits, why
        return [self._equip(k, a, ws[w].zh if ws else None)
                for k, a, w, ws in zip(kits, a_choice, w_choice, held)], None

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
            members.append(Member(s.id, k.char, k.zh, k.playstyle, k.cons, round(score, 3), None,
                                  k.artifact_choice, k.weapon_choice))

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
        return TeamResult(t.id, t.zh, round(score, 3), members, needs, subs, t.verified, conflicts,
                          tag_score=round(score, 3), penalty=round(penalty_total, 3), damage=self._team_damage(kits))

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

    # ---------- 辅助找主C ----------

    def is_support(self, char_id: str, cons: int = 0) -> bool:
        """这个命座下没有任何玩法提供主C。"""
        return not any((k := self.kit(char_id, pid, cons)) and k.has("main_dps") > 0
                       for pid in self.available_playstyles(char_id, cons))

    def carries_for(self, support_id: str, cons: int = 0, roster: Roster = None) -> tuple[list[dict], list[dict], bool]:
        """辅助可以搭配的主C。返回 (合适的, 不合适的及原因, 是否万用)。
        合适 = 主C的队伍模板候选里有这个辅助（模板已经按整队配好，优先于两人规则），
        或者辅助自己的整队规则在「辅助 + 主C」两人时已经满足，且主C不排斥辅助的功能。
        万用 = 辅助没有整队规则（例如只要吃攻击力就行），这时只能请玩家直接选。"""
        kits = [k for pid in self.available_playstyles(support_id, cons) if (k := self.kit(support_id, pid, cons))]
        universal = not any(k.rules for k in kits)
        fit: list[dict] = []
        unfit: list[dict] = []
        for c in self.kb.characters.values():
            if c.id == support_id or not self._owns(c.id, roster):
                continue
            ccons = self._cons(c.id, roster)
            in_template = any(cand.char == support_id for t in self.kb.teams_for(c.id)
                              for sl in t.slots for cand in sl.candidates)
            for pid in self.available_playstyles(c.id, ccons):
                carry = self.kit(c.id, pid, ccons)
                if carry is None or (carry.has("main_dps") <= 0 and not in_template):
                    continue
                best: dict | None = None
                for k in kits:
                    pair = [k, carry]
                    funcs = self._team_funcs(pair)
                    broken = [r.reason for r in k.rules if not evaluate(r.rule, {}, funcs)]
                    avoided = [f"{c.zh}排斥{self._tag_zh(a.tag)}：{a.reason}" for a in carry.avoids if k.has(a.tag) > 0]
                    helps = [self._tag_zh(n.tag) for n in carry.needs if k.has(n.tag) > 0]
                    row = {"char": c.id, "zh": c.zh, "element": c.element.value, "support_playstyle": k.playstyle,
                           "meets_needs": helps, "has_team_template": bool(self.kb.teams_for(c.id)),
                           "in_template": in_template,
                           # 模板里已有这个辅助：两人时的整队规则不作数，只留主C明确排斥的功能
                           "problems": avoided if in_template else broken + avoided}
                    if best is None or len(row["problems"]) < len(best["problems"]):
                        best = row
                if best is not None:
                    (unfit if best["problems"] else fit).append(best)
                break  # 一个主C只算一次
        fit.sort(key=lambda r: (not r["in_template"], -len(r["meets_needs"]), not r["has_team_template"], r["zh"]))
        return fit, unfit, universal

    # ---------- 养成方案 ----------

    def build(self, char_id: str, playstyle: str, cons: int, owned_weapons: set[str] | None = None,
              artifact_choice: int = 0, weapon_choice: str | None = None) -> dict | None:
        """artifact_choice / weapon_choice：组队时为了不和队友撞增益装备选的那一套，排到最前面。"""
        b = self._find_build(char_id, playstyle, cons)
        if b is None:
            return None
        weapons = [
            {**w.model_dump(), "owned": owned_weapons is None or w.id in owned_weapons or w.source == "craftable"}
            for w in sorted(b.weapons, key=lambda w: w.rank)
        ]
        first_owned = next((w for w in weapons if w["owned"]), None)
        best = next((w for w in weapons if w["zh"] == weapon_choice), None) or first_owned
        sets = [[a.model_dump() for a in combo] for combo in b.artifact_sets]
        if 0 < artifact_choice < len(sets):
            sets.insert(0, sets.pop(artifact_choice))
        return {
            "char": char_id,
            "playstyle": playstyle,
            "cons": b.cons,
            "zh": b.zh,
            "recommended_weapon": best,
            "weapons": weapons,
            "artifact_sets": sets,
            "gear_swapped": artifact_choice > 0 or best is not first_owned,  # 让出了增益装备
            "main_stats": b.main_stats,
            "substats": b.substats,
            "energy_recharge": b.energy_recharge,
            "energy_recharge_notes": b.energy_recharge_notes,
            "stat_targets": b.stat_targets,
            "talents": b.talents.model_dump(),
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
