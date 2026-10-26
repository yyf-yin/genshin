"""命令行入口。

例：
    python -m genshin_assistant recommend 水龙 --cons 0
    python -m genshin_assistant recommend 那维莱特 --own 芙宁娜,砂糖,白术 --json
    python -m genshin_assistant validate
    python -m genshin_assistant game 桑多涅
游戏数据相关的命令见 gamedata/cli.py。
"""

from __future__ import annotations

import argparse
import json

from .engine import Engine, Owned, Recommendation
from .gamedata import cli as gamedata_cli
from .kb import DEFAULT_DATA_DIR, load


def _roster(kb, own: str | None, missing: str | None, core: str, cons: int):
    core_id = kb.resolve(core).id
    if own:
        roster = {kb.resolve(n.strip()).id: Owned() for n in own.split(",") if n.strip()}
    elif missing:
        lacking = {kb.resolve(n.strip()).id for n in missing.split(",") if n.strip()}
        roster = {cid: Owned() for cid in kb.characters if cid not in lacking}
    else:
        roster = {cid: Owned() for cid in kb.characters}
    roster[core_id] = Owned(cons=cons)
    return roster


def _print(rec: Recommendation) -> None:
    print(f"核心：{rec.core_zh}（{rec.cons} 命）")
    for i, t in enumerate(rec.teams, 1):
        flag = "" if t.verified else "  [草稿数据，未审核]"
        print(f"\n{i}. {t.template_zh}  得分 {t.score}{flag}")
        print("   成员：" + "、".join(m.zh for m in t.members))
        for n in t.needs:
            state = f"由{'、'.join(n.met_by)}满足" if n.met_by else f"未满足，扣 {n.penalty}"
            print(f"   需求：{n.owner}需要{n.tag_zh}（{n.reason}）→ {state}")
        for c in t.conflicts:
            print(f"   扣分：{c.owner}，{c.reason}（{'、'.join(c.involved)}），扣 {c.penalty}")
        for d in t.substitutions:
            why = d.override_reason or f"保留：{'、'.join(d.kept) or '无'}；失去：{'、'.join(d.lost) or '无'}；新增：{'、'.join(d.gained) or '无'}"
            print(f"   替补：{d.chosen} 替 {d.preferred}（{why}）")
        for m in t.members:
            if m.build and m.build["recommended_weapon"]:
                w = m.build["recommended_weapon"]["zh"]
                sets = " / ".join("+".join(f"{a['zh']}{a['pieces']}" for a in combo) for combo in m.build["artifact_sets"])
                print(f"   养成：{m.zh} 武器 {w}，圣遗物 {sets}")
    for x in rec.invalid:
        print(f"\n不成立：{x.template_zh}：{x.reason}")
    for e in rec.excluded:
        print(f"排除：{e.char}（槽位 {e.slot}）：{e.reason}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="genshin-assistant")
    p.add_argument("--data", default=str(DEFAULT_DATA_DIR))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recommend", help="围绕一个角色推荐队伍")
    r.add_argument("character")
    r.add_argument("--cons", type=int, default=0, help="核心角色命座")
    r.add_argument("--own", help="拥有的角色，逗号分隔（不填表示全有）")
    r.add_argument("--missing", help="没有的角色，逗号分隔")
    r.add_argument("--playstyle")
    r.add_argument("--json", action="store_true")
    sub.add_parser("validate", help="校验知识库")
    gamedata_cli.add_subcommands(sub)
    args = p.parse_args(argv)

    if args.cmd == "import-gamedata":
        gamedata_cli.run(args)
        return
    kb = load(args.data)
    if args.cmd in gamedata_cli.COMMANDS:
        gamedata_cli.run(args, kb)
        return
    if args.cmd == "validate":
        print(f"知识库校验通过：{len(kb.characters)} 个角色，{len(kb.teams)} 个队伍模板，{len(kb.builds)} 套配装")
        return
    roster = _roster(kb, args.own, args.missing, args.character, args.cons)
    rec = Engine(kb).recommend(args.character, roster, args.playstyle)
    if args.json:
        print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print(rec)
