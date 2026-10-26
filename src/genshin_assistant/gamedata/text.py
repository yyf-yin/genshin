"""游戏文本清洗：去掉颜色标签、超链接标记，填入参数。"""

from __future__ import annotations

import re

_COLOR = re.compile(r"</?color[^>]*>")
_LINK = re.compile(r"\{LINK#[A-Z]?\d+\}|\{/LINK\}")
_SPRITE = re.compile(r"\{SPRITE(?:_PRESET)?#[^}]*\}")
_INDEX = re.compile(r"\{(\d+)\}")
# {PARAM#P1192101|1S100}：天赋 P1192101 的第 1 个参数，乘 100 显示
_PARAM = re.compile(r"\{PARAM#P(\d+)\|(\d+)S(\d+)\}")
_TALENT_PARAM = re.compile(r"\{param(\d+):([A-Z0-9]+)\}")
LINK_IDS = re.compile(r"\{LINK#N(\d+)\}")


def clean(text: str, params: list[str] | None = None, proud: dict[int, list[float]] | None = None) -> str:
    """去掉富文本标记。params 填 {0}{1}…，proud 填 {PARAM#P…}，查不到的参数写成「?」。"""
    text = text.replace("\\n", "\n")
    text = _COLOR.sub("", text)
    text = _LINK.sub("", text)
    text = _SPRITE.sub("", text)
    if params is not None:
        text = _INDEX.sub(lambda m: _pick(params, int(m.group(1))), text)
    text = _PARAM.sub(lambda m: _proud_param(proud, m), text)
    return "\n".join(line.rstrip() for line in text.strip().split("\n"))


def _pick(params: list[str], i: int) -> str:
    return params[i] if i < len(params) and params[i] != "" else "?"


def _proud_param(proud: dict[int, list[float]] | None, m: re.Match) -> str:
    values = (proud or {}).get(int(m.group(1)))
    index, scale = int(m.group(2)), int(m.group(3))
    if not values or index > len(values):
        return "?"
    return _num(values[index - 1] * scale)


def _num(v: float, digits: int = 1) -> str:
    s = f"{v:.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def format_talent_param(fmt: str, value: float) -> str:
    """genshin-db 天赋倍率的格式：F1P=一位小数百分比，P=整数百分比，I=整数，F1=一位小数。"""
    percent = fmt.endswith("P")
    core = fmt[:-1] if percent else fmt
    v = value * 100 if percent else value
    if core == "I":
        s = str(round(v))
    elif core.startswith("F"):
        s = f"{v:.{int(core[1:] or 0)}f}"
    else:
        s = _num(v, 0) if percent else _num(v, 2)
    return s + ("%" if percent else "")


def render_talent_label(label: str, params: dict[str, list[float]], level: int) -> tuple[str, str]:
    """把「一段伤害|{param1:F1P}」渲染成 ("一段伤害", "152.8%")。level 从 1 开始。"""
    name, _, template = label.partition("|")

    def sub(m: re.Match) -> str:
        values = params.get(f"param{m.group(1)}")
        if not values:
            return "?"
        return format_talent_param(m.group(2), values[min(level, len(values)) - 1])

    return name, _TALENT_PARAM.sub(sub, template)
