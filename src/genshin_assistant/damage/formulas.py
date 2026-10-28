"""伤害公式的各个乘区，纯函数。公式说明和出处见 data/mechanics/damage.yaml、reactions.yaml。

约定：百分比一律用小数（20% 写 0.2）。
"""

from __future__ import annotations

# 增幅反应倍率：(反应, 触发元素) → 倍率
AMPLIFYING = {
    ("vaporize", "hydro"): 2.0, ("vaporize", "pyro"): 1.5,
    ("melt", "pyro"): 2.0, ("melt", "cryo"): 1.5,
}
# 剧变反应系数（乘在等级系数上）
TRANSFORMATIVE = {
    "overloaded": 2.75, "superconduct": 1.5, "electro_charged": 2.0, "swirl": 0.6, "shattered": 3.0,
    "bloom": 2.0, "hyperbloom": 3.0, "burgeoning": 3.0, "burning": 0.25,
}
# 激化：额外的基础伤害 = 系数 × 等级系数 × (1 + 精通加成 + 反应加成)
ADDITIVE = {"aggravate": 1.15, "spread": 1.25}
# 月曜反应。reaction：反应本身（如月感电落雷）乘在等级系数上的系数；
# direct：角色技能「造成月xx伤害」时乘在 倍率×属性 上的系数。
# 来源是社区实测，见 reactions.yaml；None 表示还不知道。
LUNAR = {
    "lunar_charged": {"reaction": 1.8, "direct": 3.0},
    "lunar_bloom": {"reaction": None, "direct": 1.0},
    "lunar_crystallize": {"reaction": 0.96, "direct": 1.6},
}
# 星烁反应（7.0 起）。星超导系数随极星辉域层数从 1.0 涨到 2.0，首轮叠满后一直是 2.0，按 2.0 算（Cliff 2026-10-27）。
# 星扩散直伤系数 1.0（社区资料）；星扩散反应本身的系数未知。
STELLAR = {
    "stellar_superconduct": {"reaction": 2.0, "direct": 2.0},
    "stellar_swirl": {"reaction": None, "direct": 1.0},
}
# 月曜、星烁统称「特殊反应」：不吃防御区和普通增伤区，精通公式相同
SPECIAL = {**LUNAR, **STELLAR}


def em_bonus(em: float, kind: str) -> float:
    """元素精通带来的反应加成。kind：amplifying / transformative / additive / lunar。"""
    if kind == "amplifying":
        return 2.78 * em / (em + 1400)
    if kind == "transformative":
        return 16 * em / (em + 2000)
    if kind == "additive":
        return 5 * em / (em + 1200)
    if kind == "lunar":
        return 6 * em / (em + 2000)
    raise ValueError(kind)


def defense_multiplier(char_level: int, enemy_level: int, def_reduction: float = 0.0, def_ignore: float = 0.0) -> float:
    """防御区：(角色等级+100) / [(角色等级+100) + (敌人等级+100)×(1-减防)×(1-无视防御)]。"""
    k = (enemy_level + 100) * (1 - def_reduction) * (1 - def_ignore)
    return (char_level + 100) / ((char_level + 100) + k)


def resistance_multiplier(res: float) -> float:
    """抗性区，res 为减抗之后的抗性。"""
    if res < 0:
        return 1 - res / 2
    if res < 0.75:
        return 1 - res
    return 1 / (1 + 4 * res)


def crit_multipliers(crit_rate: float, crit_dmg: float) -> tuple[float, float]:
    """返回 (暴击时的乘数, 期望乘数)。暴击率按 0~100% 截断。"""
    rate = min(max(crit_rate, 0.0), 1.0)
    return 1 + crit_dmg, 1 + rate * crit_dmg


def amplifying_multiplier(reaction: str, trigger_element: str, em: float, bonus: float = 0.0) -> float:
    try:
        base = AMPLIFYING[(reaction, trigger_element)]
    except KeyError:
        raise ValueError(f"{trigger_element} 元素不能触发 {reaction}") from None
    return base * (1 + em_bonus(em, "amplifying") + bonus)


def additive_damage(reaction: str, level_mult: float, em: float, bonus: float = 0.0) -> float:
    return ADDITIVE[reaction] * level_mult * (1 + em_bonus(em, "additive") + bonus)


def transformative_base(reaction: str, level_mult: float, em: float, bonus: float = 0.0) -> float:
    """剧变反应抗性前的伤害（不吃防御、增伤）。"""
    return TRANSFORMATIVE[reaction] * level_mult * (1 + em_bonus(em, "transformative") + bonus)


def lunar_team_total(contributions: list[float]) -> float:
    """月曜反应由多名角色共同施加元素时：每人按自己的面板算一份，最高的 ×1、第二 ×1/2、其余 ×1/12，再相加。"""
    weights = [1.0, 0.5]
    ordered = sorted(contributions, reverse=True)
    return sum(v * (weights[i] if i < 2 else 1 / 12) for i, v in enumerate(ordered))
