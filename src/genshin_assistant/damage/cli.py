"""伤害计算的子命令。

    python -m genshin_assistant damage examples/damage/neuvillette.yaml   # 按场景文件算
    python -m genshin_assistant damage examples/damage/neuvillette.yaml --json
    python -m genshin_assistant multipliers 那维莱特 --level 10             # 列出天赋倍率，写场景时查名字
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ..gamedata.store import load_gamedata
from .calc import Report, calculate, load_scenario
from .talents import SLOT_ZH, load_talents

COMMANDS = {"damage", "multipliers"}


def add_subcommands(sub: argparse._SubParsersAction) -> None:
    d = sub.add_parser("damage", help="按场景文件（角色、武器、圣遗物、增益、敌人）计算伤害，逐个乘区列出来")
    d.add_argument("scenario", help="场景 YAML，写法见 examples/damage/")
    d.add_argument("--json", action="store_true")
    m = sub.add_parser("multipliers", help="列出角色的天赋倍率（写场景文件时查天赋名）")
    m.add_argument("character")
    m.add_argument("--level", type=int, default=10, help="天赋等级，默认 10")
    m.add_argument("--all", action="store_true", help="连治疗、护盾等非伤害的行一起列")


def run(args: argparse.Namespace, kb=None) -> None:
    gd = load_gamedata(args.data)
    if args.cmd == "multipliers":
        name = args.character
        if kb is not None:
            try:
                name = kb.resolve(name).zh
            except KeyError:
                pass
        char = gd.character(name)
        for t in load_talents(gd.talent_multipliers.get(char["id"], {})):
            if not (t.is_damage or args.all):
                continue
            shown = []
            for i in range(len(t.variants())):
                hits = t.hits(args.level, i)
                shown.append(" + ".join(_hit_text(h) for h in hits) or "（不是倍率）")
            print(f"{SLOT_ZH[t.slot]}:{t.name}  " + "  /  ".join(shown))
        return
    report = calculate(gd, load_scenario(Path(args.scenario)))
    if args.json:
        print(json.dumps(asdict(report) | {"total_average": report.total_average}, ensure_ascii=False, indent=2))
    else:
        print_report(report)


def _hit_text(h) -> str:
    zh = {"atk": "攻击力", "hp": "生命值", "def": "防御力", "em": "元素精通"}
    parts = [f"{v:.2%}{zh[k]}" for k, v in h.scaling.items()]
    if h.flat:
        parts.append(f"{h.flat:.0f}")
    return "+".join(parts) + (f"[{h.element}]" if h.element else "")


def print_report(r: Report) -> None:
    print(f"{r.character} 面板：" + "，".join(
        f"{k} {v:.1%}" if k in ("暴击率", "暴击伤害", "元素充能效率") else f"{k} {v:.0f}" for k, v in r.panel.items()))
    for h in r.hits:
        times = f" ×{h.times}" if h.times > 1 else ""
        print(f"\n{h.label}{times}  [{h.element}]")
        for z in h.zones:
            value = f"{z.value:.0f}" if z.name == "基础伤害" else f"×{z.value:.4f}"
            print(f"  {z.name:<10}{value:>12}  {z.note}")
        print(f"  不暴击 {h.non_crit:.0f}，暴击 {h.crit:.0f}，期望 {h.average:.0f}")
    if len(r.hits) > 1:
        print(f"\n合计期望：{r.total_average:.0f}")
    for w in r.warnings:
        print(f"\n注意：{w}")
