"""游戏数据相关的子命令。主入口 cli.py 只负责把它们挂上去。

    python -m genshin_assistant import-gamedata            # 下载上游数据，重新生成 data/game/
    python -m genshin_assistant game 桑多涅                 # 查角色/武器/圣遗物/名词
    python -m genshin_assistant game 桑多涅 --section cons  # 只看命座
    python -m genshin_assistant enablers 星超导             # 哪些角色能开启这个反应
    python -m genshin_assistant hints 沃雅妮莎              # 从天赋原文找定位线索
    python -m genshin_assistant draft-tags                 # 给知识库里还没有的角色起草标签
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from . import importer
from .hints import draft, suggest
from .store import load_gamedata

SECTIONS = {"talents": "talents", "passives": "passives", "cons": "constellations", "terms": "terms"}
COMMANDS = {"import-gamedata", "game", "enablers", "hints", "draft-tags"}
DRAFT_HEADER = "# 自动起草（关键词匹配天赋原文），未经审核。审核后挪到 data/characters/ 才会参与配队。\n"


def add_subcommands(sub: argparse._SubParsersAction) -> None:
    i = sub.add_parser("import-gamedata", help="从 genshin-db 和 AnimeGameData2 重新生成 data/game/")
    i.add_argument("--version", help="genshin-db 版本号，默认最新")
    i.add_argument("--refresh", action="store_true", help="忽略缓存重新下载")
    g = sub.add_parser("game", help="查游戏数据：角色、武器、圣遗物套装、名词")
    g.add_argument("name")
    g.add_argument("--section", choices=sorted(SECTIONS), help="只看角色的某一部分")
    g.add_argument("--json", action="store_true")
    e = sub.add_parser("enablers", help="哪些角色能为队伍开启月曜/星烁反应")
    e.add_argument("reaction", help="例：月感电、星超导，或 lunar_charged")
    h = sub.add_parser("hints", help="从天赋、命座原文里找定位线索（关键词匹配，仅供参考）")
    h.add_argument("character")
    d = sub.add_parser("draft-tags", help="给知识库里还没有的角色自动起草标签，写到 data/drafts/characters/")
    d.add_argument("--force", action="store_true", help="覆盖已有的草稿")


def _game_name(name: str, kb) -> str:
    """先用知识库的别名（水龙、万叶）换成正式名。"""
    if kb is None:
        return name
    try:
        return kb.resolve(name).zh
    except KeyError:
        return name


def run(args: argparse.Namespace, kb=None) -> None:
    data_dir = Path(args.data)
    if args.cmd == "import-gamedata":
        counts = importer.run(data_dir, args.version, args.refresh)
        print("已生成：" + "，".join(f"{k} {v}" for k, v in counts.items()))
        return

    gd = load_gamedata(data_dir)
    if not gd.characters:
        raise SystemExit("data/game/ 为空，先运行 import-gamedata")

    if args.cmd == "game":
        kind, row = gd.find(_game_name(args.name, kb))
        if args.section:
            if kind != "character":
                raise SystemExit("--section 只适用于角色")
            row = {"zh": row["zh"], SECTIONS[args.section]: row.get(SECTIONS[args.section])}
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2))
        else:
            print(yaml.dump(row, Dumper=importer._Dumper, allow_unicode=True, sort_keys=False, width=10_000))
    elif args.cmd == "enablers":
        rid = importer.SPECIAL_REACTIONS.get(args.reaction, args.reaction)
        names = [c["zh"] for c in gd.characters.values() if rid in c.get("enables", [])]
        if not names:
            raise SystemExit(f"没有角色能开启 {args.reaction}（可选：{'、'.join(importer.SPECIAL_REACTIONS)}）")
        print(f"{args.reaction}：" + "、".join(names))
    elif args.cmd == "hints":
        char = gd.character(_game_name(args.character, kb))
        print(f"{char['zh']} 的定位线索（关键词匹配，需人工确认）：")
        if char.get("intro"):
            print("官方定位：" + char["intro"].split("\n")[0])
        if char.get("enables"):
            print("能开启：" + "、".join(char["enables"]))
        for hint in suggest(char):
            cond = f"（{hint.when}）" if hint.when else ""
            label = "后台生效" if hint.tag == "off_field" else hint.tag
            print(f"- {label}{cond} ← {hint.source}：{hint.evidence}")
    elif args.cmd == "draft-tags":
        have = {c.zh for c in kb.characters.values()} if kb else set()
        out = data_dir / "drafts" / "characters"
        out.mkdir(parents=True, exist_ok=True)
        written = 0
        for char in gd.characters.values():
            if char["zh"] in have or not char.get("element") or not char.get("talents"):
                continue
            path = out / f"{char['id']}.yaml"
            if path.exists() and not args.force:
                continue
            text = yaml.dump(draft(char), Dumper=importer._Dumper, allow_unicode=True, sort_keys=False, width=10_000)
            path.write_text(DRAFT_HEADER + text, encoding="utf-8")
            written += 1
        print(f"已起草 {written} 个角色，见 {out}")
