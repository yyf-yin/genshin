"""命令行入口。

例：
    python -m genshin_assistant recommend 水龙 --cons 0
    python -m genshin_assistant recommend 那维莱特 --own 芙宁娜,砂糖,白术 --json
    python -m genshin_assistant validate
    python -m genshin_assistant bili run 那维莱特        # B 站取材 + DeepSeek 抽取 → work/drafts/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Engine, Owned, Recommendation
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
    _add_bili_parser(sub)
    args = p.parse_args(argv)

    kb = load(args.data)
    if args.cmd == "bili":
        _bili(args, kb)
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


def _add_bili_parser(sub) -> None:
    b = sub.add_parser("bili", help="从 B 站攻略视频提取知识库草稿")
    b.add_argument("--work", help="工作目录（默认仓库下的 work/，已在 .gitignore 里）")
    b.add_argument("--sources", help="角色和搜索词配置（默认 data/bili/sources.yaml）")
    bs = b.add_subparsers(dest="bili_cmd", required=True)
    bs.add_parser("status", help="检查登录态、今日读预算、是否在风控熔断中")
    s = bs.add_parser("search", help="搜索视频")
    s.add_argument("keyword")
    s.add_argument("--order", default="totalrank", choices=["totalrank", "pubdate", "click"])
    for name, helptext in (("harvest", "搜索并取材（简介、置顶评论、字幕）"),
                           ("extract", "用 DeepSeek 抽取、跨视频投票、和知识库对照，输出草稿"),
                           ("run", "harvest + extract")):
        x = bs.add_parser(name, help=helptext)
        x.add_argument("characters", nargs="*", help="角色名；不填就跑 sources.yaml 里 pilot: true 的角色")
        x.add_argument("--refresh", action="store_true", help="忽略本地缓存重新取材 / 重新抽取")
        x.add_argument("--explain", action="store_true", help="列出被筛掉的视频和原因")
    f = bs.add_parser("frames", help="在字幕提到配装的时间点抽帧拼图（给人看配装面板）")
    f.add_argument("bvid")
    f.add_argument("--at", help="手动指定时间点，逗号分隔，如 1:20,3:05")


def _bili(args, kb) -> None:
    from .bili import BiliClient, BiliError, RateLimited
    from .bili.api import search_videos, subtitles, video_info
    from .bili.frames import build_moments, download_video, frame_sheet
    from .bili.harvest import harvest, load_sources
    from .extract.glossary import load_glossary
    from .extract.llm import DeepSeek, LLMError
    from .extract.pipeline import extract_target

    data_dir = Path(args.data)
    work = Path(args.work) if args.work else data_dir.parent / "work"
    sources_path = Path(args.sources) if args.sources else data_dir / "bili" / "sources.yaml"
    client = BiliClient(state_dir=work / ".bili-state")
    cmd = args.bili_cmd
    try:
        if cmd == "status":
            st = client.status()
            print(f"登录：{'是' if st['logged_in'] else '否（拉不到 AI 字幕）'}")
            print(f"今日已读：{st['reads_today']}/{st['daily_budget']}")
            if st["ban_minutes_left"]:
                print(f"风控熔断中：还剩 {st['ban_minutes_left']} 分钟（{st['ban_why']}）")
            return
        if cmd == "search":
            for h in search_videos(client, args.keyword, order=args.order):
                print(f"{h.bvid}  {h.title}\n    UP {h.author}  播放 {h.play}  {h.duration // 60} 分钟")
            return
        if cmd == "frames":
            info = video_info(client, args.bvid)
            if args.at:
                times = [sum(int(x) * 60 ** i for i, x in enumerate(reversed(t.split(":")))) for t in args.at.split(",")]
            else:
                times = build_moments(subtitles(client, info)[1])
            video = download_video(client, info, work / "video" / f"{args.bvid}.mp4")
            print(frame_sheet(video, times, work / "frames" / f"{args.bvid}.jpg"))
            return

        cfg = load_sources(sources_path)
        if args.characters:
            targets = []
            for name in args.characters:
                t = cfg.find(name)
                if t is None:
                    sys.exit(f"{name} 不在 {sources_path} 里，先加进去（id、中文名、上线日期）")
                targets.append(t)
        else:
            targets = [t for t in cfg.targets.values() if t.id in cfg.pilot]
        glossary = load_glossary(kb, data_dir / "bili" / "glossary.yaml")
        model = None
        if cmd in ("extract", "run"):
            model = DeepSeek(cache_dir=work / "llm-cache")
        for t in targets:
            print(f"== {t.zh}")
            if cmd in ("harvest", "run"):
                harvest(client, t, cfg, work / "bili", refresh=args.refresh, explain=args.explain)
            if cmd in ("extract", "run"):
                out = extract_target(kb, glossary, t, model, work, refresh=args.refresh,
                                     whitelist=cfg.whitelist)
                if out:
                    print(f"  草稿：{out[0]}\n  报告：{out[1]}")
        print(f"本次 B 站请求 {client.requests_made} 次")
    except RateLimited as e:
        sys.exit(f"停下：{e}")
    except (BiliError, LLMError) as e:
        sys.exit(str(e))
