"""问答层：大模型把问题解析成查询 → 推荐引擎算结果 → 大模型照着结果用中文解释。

大模型不决定配队和配装，只做两件事：听懂问题、把引擎结果讲清楚。
界面（网页、命令行）只调用 Assistant.ask，不碰这里的细节。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..engine import Engine, Owned
from ..extract.llm import JSONModel, LLMError
from ..kb import KnowledgeBase, load_draft_builds

INTENTS = ("team", "build", "other")

PARSE_SYSTEM = """你是原神配队助手的问题解析器。把玩家的问题解析成 JSON，不要回答问题本身。
字段：
- intent："team"（配队、队友、替补、缺某个角色怎么办）、"build"（武器、圣遗物、词条、充能、天赋）、"other"（其他）
- character：问题围绕的那个角色，写正式中文名；问题里没提就沿用上一轮的角色；都没有就是 null
- cons：这个角色的命座（整数 0-6），没说就是 null。「满命」是 6，「零命」「0命」是 0
- missing：玩家说自己没有的角色（正式中文名列表），没说就是 []
- own：只有玩家完整列出「我只有这些角色」时才填，否则是 []
- playstyle：玩家指定的玩法 id，一般是 null
- carry：问的是辅助角色、并且玩家说了想搭配的主C时，填主C的正式中文名，否则是 null
追问（如「那没有芙宁娜呢」）时，沿用上一轮的 character、cons，并把上一轮的 missing 合并进来。
换了角色时，不要沿用上一轮的 cons 和 missing，除非本轮又说了。
上一轮 awaiting_carry 为 true 时，上一轮是在问玩家「辅助想搭配哪个主C」：本轮说出的角色是主C，填到 carry，character 保持上一轮的辅助。
已知角色（正式名：别名）：
{names}
只输出 JSON 对象。"""

EXPLAIN_SYSTEM = """你是原神配队与养成助手，用中文回答玩家。
下面的「引擎结果」是推荐引擎按知识库算出来的，是你唯一的依据：
- 只推荐引擎结果里出现的角色、武器、圣遗物，不要凭记忆添加或改动；引擎没给的就直说知识库里还没有
- 队伍按引擎的顺序讲，先给结论（队伍成员），再讲为什么：每个位置起什么作用、满足了谁的需求、扣分项
- 有替补时，讲清替补保留了什么、失去了什么，有攻略结论（override_reason）时优先讲它，没有就不提
- 数据标了未审核（verified=false 或 draft）时，在结尾提醒一句「这部分数据还是草稿，未审核」
- 命座会改变结论时要点出来
- status 是 need_carry 时，问的是辅助角色，先不要给队伍：问玩家想搭配哪个主C。universal 为 false 时列出 candidates 里的主C和一句理由（meets_needs、in_template、元素），unfit 里的可以简短说为什么不合适；universal 为 true 时说明这个辅助几乎哪个队都能用，直接问想搭配哪个主C，candidates 只作为可选举例
- 有 carry 时，队伍是以 carry 为核心、包含所问辅助的队伍，先说明这一点；support_missing_reason 有值时说明这个主C的现有队伍里放不进该辅助及原因
- status 是 no_team_template 时，说明还没有现成队伍，再按 kit（这个角色提供的功能、对队友的需求、整队规则）讲适合什么样的队友，但不要点名具体角色，除非 appears_in 里有
- 理由只能来自引擎结果：保留/失去的功能照 kept、lost 原样讲，扣分只讲 needs、conflicts 里的，不要自己补强度评价或扣分原因
- 队伍有 damage 时，排序依据是全队一轮的期望总伤害：讲每人的伤害占比（share）和主要伤害来源（top_hits），队友对比时用它说明谁更强、强在哪；choices 是引擎为风套等「几选一」增益选的元素；missing 里的角色还没有伤害数据，伤害按 0 算，要点明
- 队员 cons 不为 0 时，名字后面带上命座；玩家没说命座时四星按 0 命、6 命各算一份，cons_variants 是同一队换成另一种命座的全队伤害，要讲出差多少
- 武器按 rank 讲，rank 相同的写成并列
- 队员 gear_swapped 为 true 表示首选的增益装备（如翠绿之影、讨龙英杰谭、炉火）已由队友带着，同一件一队只生效一次，所以他换成了列出的次选，要点明
- 不要在回答里出现英文字段名（如 override_reason、kept、verified）
- 简洁，用短段落和列表，不要寒暄
输出 JSON：{{"answer": "Markdown 格式的回答"}}"""


@dataclass
class Query:
    intent: str = "other"
    character: str | None = None
    cons: int | None = None
    missing: list[str] = field(default_factory=list)
    own: list[str] = field(default_factory=list)
    playstyle: str | None = None
    carry: str | None = None  # 问辅助时，玩家选的主C
    awaiting_carry: bool = False  # 上一轮在问玩家「搭配哪个主C」，由程序设置

    @classmethod
    def from_llm(cls, raw: dict) -> "Query":
        cons = raw.get("cons")
        try:
            cons = None if cons is None else max(0, min(6, int(cons)))
        except (TypeError, ValueError):
            cons = None
        intent = raw.get("intent") if raw.get("intent") in INTENTS else "other"
        names = lambda k: [str(x).strip() for x in raw.get(k) or [] if str(x).strip()]  # noqa: E731
        return cls(intent, raw.get("character") or None, cons, names("missing"), names("own"),
                   raw.get("playstyle") or None, raw.get("carry") or None)


@dataclass
class Reply:
    answer: str
    query: Query
    facts: dict
    explained_by_llm: bool = True

    def to_dict(self) -> dict:
        return {"answer": self.answer, "query": asdict(self.query), "facts": self.facts,
                "explained_by_llm": self.explained_by_llm}


class Roster:
    """玩家拥有的角色，按中文名存（知识库和游戏数据的 id 不完全一样，中文名两边都认）。
    文件不存在表示没设置过，按「全都有、0 命」算。"""

    def __init__(self, path: Path | None):
        self.path = path

    def load(self) -> dict[str, int] | None:
        if not self.path or not self.path.exists():
            return None
        return {k: int(v) for k, v in json.loads(self.path.read_text(encoding="utf-8")).get("owned", {}).items()}

    def save(self, owned: dict[str, int] | None) -> None:
        if not self.path:
            return
        if owned is None:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"owned": owned}, ensure_ascii=False, indent=1), encoding="utf-8")


class Assistant:
    def __init__(self, kb: KnowledgeBase, model: JSONModel, data_dir: Path, roster: Roster | None = None, game=None):
        self.kb = kb
        self.model = model
        self.data_dir = Path(data_dir)
        self.engine = Engine(kb)
        self.roster = roster or Roster(None)
        if game is None:
            from ..gamedata import load_gamedata
            game = load_gamedata(self.data_dir)
        self.game = game

    # ---------- 对外 ----------

    def ask(self, question: str, last: Query | None = None) -> Reply:
        q = self.parse(question, last)
        facts = self.facts(q)
        q.awaiting_carry = facts.get("status") == "need_carry"
        try:
            answer = self.explain(question, facts)
            return Reply(answer, q, facts)
        except LLMError:
            return Reply(render_plain(facts), q, facts, explained_by_llm=False)

    def parse(self, question: str, last: Query | None = None) -> Query:
        names = "\n".join(f"{c.zh}：{'、'.join(c.aliases)}" if c.aliases else c.zh for c in self.kb.characters.values())
        user = question if last is None else f"上一轮解析结果：{json.dumps(asdict(last), ensure_ascii=False)}\n本轮问题：{question}"
        q = Query.from_llm(self.model.complete_json(PARSE_SYSTEM.format(names=names), user))
        if last and last.awaiting_carry and not q.carry and q.character and q.character != last.character:
            # 上一轮在问主C，模型却把主C填进了 character：挪回来
            q.carry, q.character = q.character, last.character
            q.intent = "team"
            if q.cons is None:
                q.cons = last.cons
        if last and q.character != last.character:
            # 换了角色：上一轮的「没有谁」只在本轮又提到时才算（模型偶尔会照搬）
            q.missing = [n for n in q.missing if n in question]
        return q

    def explain(self, question: str, facts: dict) -> str:
        user = f"玩家问题：{question}\n\n引擎结果：\n{json.dumps(facts, ensure_ascii=False)}"
        raw = self.model.complete_json(EXPLAIN_SYSTEM.format(), user)
        answer = raw.get("answer") if isinstance(raw, dict) else None
        if not isinstance(answer, str) or not answer.strip():
            raise LLMError("模型没给出 answer")
        return answer.strip()

    # ---------- 查引擎 ----------

    def facts(self, q: Query) -> dict:
        if q.intent == "other" or not q.character:
            return {"status": "unsupported", "note": "目前只回答某个角色的配队和养成，问题里要带上角色名"}
        try:
            core = self.kb.resolve(q.character)
        except KeyError:
            return self._outside_kb(q)
        roster, own_cons, unknown = self._roster(q, core.id)
        cons = q.cons if q.cons is not None else own_cons
        out: dict = {"character": core.zh, "cons": cons}
        if unknown:
            out["unknown_names"] = unknown
        if q.intent == "build":
            return {**out, "status": "build", "build": _trim_build(self.engine.build(core.id, q.playstyle or "default", cons))}
        if q.carry or self.engine.is_support(core.id, cons):
            return {**out, **self._support_facts(q, core, roster, cons)}
        if not self.kb.teams_for(core.id):
            return {**out, "status": "no_team_template",
                    "note": f"知识库里还没有以{core.zh}为核心的队伍模板",
                    "appears_in": self._appears_in(core.id),
                    "kit": self._kit(core.id, q.playstyle, cons),
                    "build": _trim_build(self.engine.build(core.id, q.playstyle or "default", cons))}
        rec = self.engine.recommend(core.id, roster, q.playstyle)
        return {**out, "status": "team", "missing": q.missing, **_trim_rec(rec.to_dict())}

    def _support_facts(self, q: Query, support, roster: dict[str, Owned], cons: int) -> dict:
        """问辅助：没说主C就先列可搭配的主C让玩家选；说了就按主C的体系找包含这个辅助的队伍。"""
        if not q.carry:
            fit, unfit, universal = self.engine.carries_for(support.id, cons, roster)
            return {"status": "need_carry", "universal": universal,
                    "candidates": [_carry_row(r) for r in fit], "unfit": [_carry_row(r) for r in unfit],
                    "note": f"{support.zh}是辅助，先确定搭配哪个主C"}
        try:
            carry = self.kb.resolve(q.carry)
        except KeyError:
            return {"status": "unknown_character", "name": q.carry, "note": "找不到这个主C，或者还没录入知识库"}
        saved = self.roster.load() or {}
        roster = {**roster, carry.id: roster.get(carry.id) or Owned(cons=saved.get(carry.zh, 0))}  # 选的主C当作拥有
        out = {"status": "team", "support": support.zh, "carry": carry.zh, "missing": q.missing}
        if not self.kb.teams_for(carry.id):
            return {**out, "status": "carry_no_template",
                    "note": f"知识库里还没有以{carry.zh}为核心的队伍模板",
                    "carry_kit": self._kit(carry.id, None, roster[carry.id].cons or 0),
                    "support_kit": self._kit(support.id, q.playstyle, cons),
                    "build": _trim_build(self.engine.build(support.id, q.playstyle or "default", cons))}
        rec = self.engine.recommend(carry.id, roster, top_n=50).to_dict()
        with_support = [t for t in rec["teams"] if any(m["char"] == support.id for m in t["members"])]
        trimmed = _trim_rec({**rec, "teams": with_support[:3] or rec["teams"][:3]})
        if not with_support:
            why = [e["reason"] for e in rec["excluded"] if e["char"] == support.id]
            out["support_missing_reason"] = "；".join(why) or f"{carry.zh}的队伍模板里没有{support.zh}能放的位置"
        return {**out, **trimmed}

    def _roster(self, q: Query, core_id: str) -> tuple[dict[str, Owned], int, list[str]]:
        """存的「我的角色」+ 本轮说的有/没有 → 引擎用的 roster。返回 (roster, 核心命座, 认不出的名字)。"""
        unknown: list[str] = []

        def ids(names: list[str]) -> set[str]:
            out = set()
            for n in names:
                try:
                    out.add(self.kb.resolve(n).id)
                except KeyError:
                    if not self.game.has_character_zh(n):
                        unknown.append(n)  # 游戏里有但知识库没有的角色，本来就进不了队，不用提示
            return out

        saved = self.roster.load()
        by_zh = {c.zh: c.id for c in self.kb.characters.values()}
        if q.own:
            roster = {cid: Owned() for cid in ids(q.own)}
        elif saved is not None:
            roster = {by_zh[zh]: Owned(cons=n) for zh, n in saved.items() if zh in by_zh}
        else:
            roster = {cid: Owned() for cid in self.kb.characters}
        for cid in ids(q.missing):
            roster.pop(cid, None)
        own_cons = (roster[core_id].cons or 0) if core_id in roster else 0
        cons = q.cons if q.cons is not None else own_cons
        roster[core_id] = Owned(cons=cons)  # 问到的角色当作拥有
        return roster, own_cons, unknown

    def _kit(self, char_id: str, playstyle: str | None, cons: int) -> list[dict]:
        """没有队伍模板时，至少讲清这个角色自己能提供什么、想要什么样的队友。"""
        out = []
        for pid in [playstyle] if playstyle else self.engine.available_playstyles(char_id, cons):
            k = self.engine.kit(char_id, pid, cons)
            if k is None:
                continue
            p = self.kb.characters[char_id].playstyle(pid)
            out.append({
                "playstyle": p.zh or pid, "field": k.position,
                "provides": {self.kb.tag_zh(t): v for t, v in k.provides.items() if not t.startswith("element:")},
                "needs": [{"tag": self.kb.tag_zh(n.tag), "reason": n.reason} for n in k.needs],
                "avoids": [{"tag": self.kb.tag_zh(a.tag), "reason": a.reason} for a in k.avoids],
                "team_rules": [r.reason for r in k.rules],
            })
        return out

    def _appears_in(self, char_id: str) -> list[dict]:
        """这个角色在别人的队伍模板里能当什么位置。"""
        out = []
        for t in self.kb.teams.values():
            for s in t.slots:
                for c in s.candidates:
                    if c.char == char_id:
                        out.append({"team": t.zh, "slot": s.zh or s.id, "score": c.score, "verified": t.verified})
        return out

    def _outside_kb(self, q: Query) -> dict:
        try:
            row = self.game.character(q.character)
        except KeyError:
            return {"status": "unknown_character", "name": q.character,
                    "note": "知识库和游戏数据里都找不到这个角色，可能是名字写错了"}
        drafts = KnowledgeBase(builds=[b for b in load_draft_builds(self.data_dir) if b.char == row["id"]])
        b = Engine(drafts).build(row["id"], q.playstyle or "default", q.cons or 0)
        out = {"status": "not_in_kb", "character": row["zh"], "cons": q.cons or 0,
               "note": f"{row['zh']}还没有录入知识库，没有配队数据"}
        if b:
            out["build"] = {**_trim_build(b), "draft": True}
        return out


def _carry_row(r: dict) -> dict:
    return {k: r[k] for k in ("zh", "element", "meets_needs", "in_template", "has_team_template", "problems") if r[k]}


def _trim_build(b: dict | None) -> dict | None:
    """给大模型看的配装：去掉出处原文这类长字段。"""
    if b is None:
        return None
    return {
        "zh": b["zh"], "cons": b["cons"], "verified": b["verified"],
        "weapons": [{"rank": w["rank"], "zh": w["zh"], "note": w.get("note"), "owned": w["owned"]} for w in b["weapons"]],
        "artifact_sets": [" + ".join(_piece(a) for a in combo) for combo in b["artifact_sets"]],
        "main_stats": b["main_stats"], "substats": b["substats"],
        "energy_recharge": b["energy_recharge"], "energy_recharge_notes": b["energy_recharge_notes"],
        "stat_targets": b["stat_targets"], "talents": b["talents"], "notes": b["notes"],
    }


def _piece(a: dict) -> str:
    s = f"{a['zh']}（散件 {a['pieces']}）" if a["set"] == "mixed" else f"{a['zh']}{a['pieces']}"
    return s + (f"（{a['note']}）" if a.get("note") else "")


def _trim_rec(r: dict) -> dict:
    def member(m: dict) -> dict:
        b = m["build"] or {}
        w = b.get("recommended_weapon")
        return {"slot": m["slot"], "zh": m["zh"], "cons": m["cons"], "weapon": w["zh"] if w else None,
                "artifacts": [" + ".join(_piece(a) for a in c) for c in b.get("artifact_sets", [])][:2],
                "gear_swapped": b.get("gear_swapped", False)}

    teams = []
    for t in r["teams"]:
        teams.append({
            "template": t["template_zh"], "score": t["score"], "verified": t["verified"],
            "members": [member(m) for m in t["members"]],
            "needs": t["needs"], "conflicts": t["conflicts"], "substitutions": t["substitutions"],
            "damage": _trim_damage(t.get("damage")),
            "cons_variants": t.get("cons_variants") or [],
        })
    return {"teams": teams, "invalid": r["invalid"], "excluded": r["excluded"]}


def _trim_damage(d: dict | None) -> dict | None:
    if not d:
        return None
    return {"total": d["total"], "members": [{"zh": m["zh"], "share": m["share"], "top_hits": [h for h, _ in m["top_hits"]]}
                                             for m in d["members"]],
            "choices": d["choices"], "missing": d["missing"]}


def _member_label(m: dict) -> str:
    """队员名字；四星按命座分开算，非 0 命时标出来。"""
    return m["zh"] + (f"（{m['cons']} 命）" if m.get("cons") else "")


def render_plain(facts: dict) -> str:
    """大模型解释失败时的兜底：直接把引擎结果排成文字。"""
    st = facts.get("status")
    lines: list[str] = []
    if facts.get("note"):
        lines.append(facts["note"])
    if st == "team":
        lines.append(f"**{facts['character']}（{facts['cons']} 命）**")
        for i, t in enumerate(facts["teams"], 1):
            lines.append(f"\n{i}. {'、'.join(_member_label(m) for m in t['members'])}（{t['template']}，得分 {t['score']}"
                         f"{'' if t['verified'] else '，草稿未审核'}）")
            if t.get("damage"):
                dmg = t["damage"]
                lines.append("   - 伤害占比：" + "，".join(f"{m['zh']} {m['share']:.0%}" for m in dmg["members"])
                             + (f"（{'、'.join(dmg['missing'])}还没有伤害数据）" if dmg["missing"] else ""))
            for v in t.get("cons_variants") or []:
                lines.append(f"   - 换成{v['label']}：得分 {v['score']}")
            for d in t["substitutions"]:
                why = d["override_reason"] or f"保留 {'、'.join(d['kept']) or '无'}；失去 {'、'.join(d['lost']) or '无'}"
                lines.append(f"   - {d['chosen']} 替 {d['preferred']}：{why}")
            for c in t["conflicts"]:
                lines.append(f"   - 扣分：{c['owner']}，{c['reason']}")
        for x in facts["invalid"]:
            lines.append(f"\n不成立：{x['template_zh']}：{x['reason']}")
        if not facts["teams"] and not facts["invalid"]:
            lines.append("没有找到可用的队伍。")
    if st == "need_carry":
        lines.append("想搭配哪个主C？" + ("这个辅助几乎哪队都能用，举几个：" if facts["universal"] else "合适的有："))
        lines.extend(f"- {c['zh']}" + (f"（满足{'、'.join(c['meets_needs'])}的需求）" if c.get("meets_needs") else "")
                     for c in facts["candidates"])
        lines.extend(f"- 不太合适：{c['zh']}，{'；'.join(c['problems'])}" for c in facts["unfit"])
    if facts.get("support_missing_reason"):
        lines.append(f"{facts['carry']}的现有队伍里放不进{facts['support']}：{facts['support_missing_reason']}")
    for k in facts.get("kit", []) + facts.get("carry_kit", []) + facts.get("support_kit", []):
        lines.append(f"- {k['playstyle']}：提供 {'、'.join(k['provides']) or '无'}")
        lines.extend(f"  - 整队规则：{r}" for r in k["team_rules"])
        lines.extend(f"  - 需要{n['tag']}：{n['reason']}" for n in k["needs"])
    for a in facts.get("appears_in", []):
        lines.append(f"- 可以在「{a['team']}」里当{a['slot']}")
    b = facts.get("build")
    if b:
        lines.append(f"\n**养成：{b['zh']}（{b['cons']} 命）**{'' if b['verified'] and not b.get('draft') else '（草稿，未审核）'}")
        lines.append("武器：" + "；".join(f"{w['rank']}. {w['zh']}" for w in b["weapons"]))
        lines.append("圣遗物：" + "；".join(b["artifact_sets"]))
        if b["energy_recharge"]:
            lines.append(f"充能：{b['energy_recharge'][0]}%~{b['energy_recharge'][1]}%")
        lines.extend(f"备注：{n}" for n in b["notes"])
    elif st in ("build", "no_team_template"):
        lines.append("知识库里还没有这个角色在这个命座下的配装。")
    return "\n".join(lines)
