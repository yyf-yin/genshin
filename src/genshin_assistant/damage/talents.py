"""把天赋倍率模板解析成「每一段伤害吃哪些属性、各多少倍率」。

模板来自 data/game/talent_multipliers.yaml，例如：
    {param1:F1P}                        一段，攻击力倍率
    {param1:F2P}生命值上限               一段，生命值倍率
    {param1:F1P}+{param2:F1P}           两段（同一属性的 + 是分段）
    {param1:F1P}攻击力+{param2:F1P}元素精通   一段，同时吃两种属性
    {param1:F1P}*3                      同样的伤害打 3 次
    {param1:P}/{param2:P}               几种情况二选一（低空/高空坠地），用 variant 选
百分比参数（格式以 P 结尾）是倍率，其余（I、F1）是固定值，加在同一段伤害上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PARAM = re.compile(r"\{param(\d+):([A-Z0-9]+)\}")
_REPEAT = re.compile(r"^(.*?)\*(\d+)$")

STATS = {"攻击力": "atk", "生命值上限": "hp", "最大生命值": "hp", "防御力": "def", "元素精通": "em"}
ELEMENT_SUFFIX = {"物理": "physical", "火元素": "pyro", "水元素": "hydro", "雷元素": "electro", "冰元素": "cryo",
                  "风元素": "anemo", "岩元素": "geo", "草元素": "dendro"}


@dataclass
class Hit:
    """一段伤害：基础伤害 = Σ 倍率 × 属性 + 固定值。"""

    scaling: dict[str, float] = field(default_factory=dict)  # {"atk": 1.52, "em": 3.04}
    flat: float = 0.0
    element: str | None = None  # 模板里写明元素时（「冰元素+物理」）才有


@dataclass
class Talent:
    slot: str  # normal_attack / skill / burst / special
    name: str
    template: str
    params: dict[str, list[float]]

    @property
    def is_damage(self) -> bool:
        return "伤害" in self.name and not any(w in self.name for w in ("提升", "提高", "增加"))

    def variants(self) -> list[str]:
        """/ 分隔的几种情况。只有单位的部分（「/层」「/0.5秒」）不算一种情况；
        「A/B生命值上限」里 A 也按生命值算。"""
        pieces = [p for p in _split_top(self.template, "/") if any(f.endswith("P") for _, f in _PARAM.findall(p))]
        if not pieces:
            return [self.template]
        suffix = re.fullmatch(r".*\}(%s)" % "|".join(STATS), pieces[-1])
        if suffix:
            pieces = [p + suffix.group(1) if _PARAM.fullmatch(p) else p for p in pieces]
        return pieces

    def hits(self, level: int, variant: int = 0) -> list[Hit]:
        """level 1~15；variant 选 / 分隔的第几种情况（从 0 数）。不是伤害倍率（全是秒数、点数）时返回空列表。"""
        variants = self.variants()
        if not 0 <= variant < len(variants):
            raise ValueError(f"{self.name} 只有 {len(variants)} 种情况：{self.template}")
        return _parse_expr(variants[variant], self.params, level)


def _split_top(s: str, sep: str) -> list[str]:
    """按 sep 切分，括号里的不切。"""
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def _value(params: dict[str, list[float]], n: str, level: int) -> float:
    values = params[f"param{n}"]
    return values[min(level, len(values)) - 1]


def _parse_expr(expr: str, params: dict[str, list[float]], level: int) -> list[Hit]:
    expr = expr.strip()
    m = _REPEAT.match(expr)
    if m and not _PARAM.fullmatch(m.group(2)):
        inner, times = m.group(1).strip(), int(m.group(2))
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1]
        if "+" not in _strip_parens(inner) or inner != m.group(1).strip():
            return _parse_expr(inner, params, level) * times
    terms = [t.strip() for t in _split_top(expr, "+")]
    last = re.fullmatch(r".*\}(%s)" % "|".join(STATS), terms[-1])
    if len(terms) > 1 and last:  # 「A+B防御力」= A防御力 + B防御力
        terms = [t + last.group(1) if _PARAM.fullmatch(t) and _PARAM.fullmatch(t).group(2).endswith("P") else t
                 for t in terms]
    if len(terms) > 1:
        parsed = [_parse_term(t, params, level) for t in terms]
        if any(p is None for p in parsed):  # 某一项是 (…)*N 这类
            return [h for t in terms for h in _parse_expr(t, params, level)]
        scaled = [p for p in parsed if p.scaling]
        stats = [next(iter(p.scaling)) for p in scaled]
        elements = {p.element for p in scaled}
        # 同一属性（或不同元素）相加 = 分段；不同属性相加 = 同一段吃多个属性
        if len(set(stats)) == len(stats) and len(elements) <= 1:
            hit = Hit(element=scaled[0].element if scaled else None)
            for p in parsed:
                for k, v in p.scaling.items():
                    hit.scaling[k] = hit.scaling.get(k, 0.0) + v
                hit.flat += p.flat
            return [hit] if hit.scaling else []
        hits = [Hit(dict(p.scaling), p.flat, p.element) for p in scaled]
        flats = sum(p.flat for p in parsed if not p.scaling)
        if hits and flats:
            hits[0].flat += flats
        return hits
    hit = _parse_term(expr, params, level)
    if hit is None:
        return _parse_expr(expr.strip("()"), params, level) if expr.startswith("(") else []
    return [hit] if hit.scaling else []


def _strip_parens(s: str) -> str:
    return re.sub(r"\([^()]*\)", "", s)


def _parse_term(term: str, params: dict[str, list[float]], level: int) -> Hit | None:
    """单项：{param}后跟可选的属性/元素后缀。不认识的后缀（秒、点、层……）当作不是伤害。"""
    if "(" in term or _REPEAT.match(term):
        return None
    m = _PARAM.fullmatch(term) or re.fullmatch(r"(\{param(\d+):([A-Z0-9]+)\})(.*)", term)
    if not m:
        return Hit()
    if _PARAM.fullmatch(term):
        n, fmt, suffix = m.group(1), m.group(2), ""
    else:
        n, fmt, suffix = m.group(2), m.group(3), m.group(4)
    v = _value(params, n, level)
    if not fmt.endswith("P"):
        return Hit(flat=v) if suffix == "" else Hit()
    if suffix == "":
        return Hit({"atk": v})
    if suffix in STATS:
        return Hit({STATS[suffix]: v})
    if suffix in ELEMENT_SUFFIX:
        return Hit({"atk": v}, element=ELEMENT_SUFFIX[suffix])
    return Hit()


SLOT_ZH = {"normal_attack": "普通攻击", "skill": "元素战技", "burst": "元素爆发", "special": "特殊技能"}


def load_talents(rows: dict) -> list[Talent]:
    """rows 是 talent_multipliers.yaml 里一个角色的部分。"""
    return [Talent(slot, r["name"], r["template"], r["params"]) for slot, items in rows.items() for r in items]


def find_talent(talents: list[Talent], query: str) -> Talent:
    """按「槽位:名字」或名字查，名字可以只写一部分（衡平推裁）。"""
    slot = None
    if ":" in query or "：" in query:
        slot_q, query = re.split(r"[:：]", query, maxsplit=1)
        slot = next((k for k, v in SLOT_ZH.items() if slot_q in (k, v) or v.startswith(slot_q)), slot_q)
        if slot_q in ("普攻", "a", "na"):
            slot = "normal_attack"
        elif slot_q in ("e", "战技"):
            slot = "skill"
        elif slot_q in ("q", "大招", "爆发"):
            slot = "burst"
    pool = [t for t in talents if slot is None or t.slot == slot]
    found = [t for t in pool if t.name == query] or [t for t in pool if query in t.name]
    if len(found) == 1:
        return found[0]
    if not found:
        raise KeyError(f"找不到天赋倍率：{query}")
    names = "、".join(f"{SLOT_ZH.get(t.slot, t.slot)}:{t.name}" for t in found)
    raise KeyError(f"「{query}」对应多行，请写完整或加槽位前缀：{names}")
