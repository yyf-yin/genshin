"""伤害计算：面板 × 倍率 × 各乘区（增伤、暴击、防御、抗性、反应、擢升）。"""

from .calc import Report, calculate, load_scenario

__all__ = ["Report", "calculate", "load_scenario"]
