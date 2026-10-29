"""命令行入口。

例：
    python -m genshin_assistant recommend 水龙 --cons 0
    python -m genshin_assistant recommend 那维莱特 --own 芙宁娜,砂糖,白术 --json
    python -m genshin_assistant validate
    python -m genshin_assistant build 阿罗夏 --cons 1      # 养成方案
    python -m genshin_assistant bili run 那维莱特        # B 站取材 + DeepSeek 抽取 → work/drafts/
    python -m genshin_assistant game 桑多涅
    python -m genshin_assistant web                      # 本机网页问答（DeepSeek 解析问题、解释引擎结果）
    python -m genshin_assistant chat                     # 同上，命令行版
    python -m genshin_assistant damage examples/damage/neuvillette.yaml   # 伤害计算
游戏数据相关的命令见 gamedata/cli.py，伤害计算见 damage/cli.py。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Engine, Owned, Recommendation
from .damage import cli as damage_cli
from .gamedata import cli as gamedata_cli
from .kb import DEFAULT_DATA_DIR, KnowledgeBase, load


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
        print("   成员：" + "、".join(m.zh + (f"（{m.cons} 命）" if m.cons else "") for m in t.members))
        if t.damage:
            d = t.damage
            print(f"   全队一轮期望伤害 {d['total']:,}（标签分 {t.tag_score}）：" + "，".join(
                f"{m['zh']} {m['share']:.0%}" for m in d["members"]))
            if d.get("refine"):
                print("   武器精炼：" + "、".join(d["refine"]))
            for v in t.variants:
                total = f"全队伤害 {v['total']:,}，" if v["total"] else ""
                print(f"   换成{v['label']}：{total}得分 {v['score']}")
            if d["choices"]:
                print("   几选一：" + "；".join(d["choices"]))
            if d["missing"]:
                print("   还没有伤害数据（按 0 算）：" + "、".join(d["missing"]))
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
                sets = " / ".join("+".join(_piece(a) for a in combo) for combo in m.build["artifact_sets"])
                print(f"   养成：{m.zh} 武器 {w}，圣遗物 {sets}")
    for x in rec.invalid:
        print(f"\n不成立：{x.template_zh}：{x.reason}")
    for e in rec.excluded:
        print(f"排除：{e.char}（槽位 {e.slot}）：{e.reason}")


def _show_build(kb, args) -> None:
    from .kb import load_draft_builds

    try:
        c = kb.resolve(args.character)
        b = Engine(kb).build(c.id, args.playstyle, args.cons)
        name, draft = c.zh, False
    except KeyError:
        from .gamedata import load_gamedata

        row = load_gamedata(args.data).character(args.character)  # 不在知识库里：去 drafts/builds/ 找
        name, draft = row["zh"], True
        kb2 = KnowledgeBase(builds=[x for x in load_draft_builds(args.data) if x.char == row["id"]])
        b = Engine(kb2).build(row["id"], args.playstyle, args.cons)
    if b is None:
        sys.exit(f"{name} 在 {args.cons} 命、玩法 {args.playstyle} 下还没有配装")
    if args.json:
        print(json.dumps(b, ensure_ascii=False, indent=2))
        return
    flag = "  [待审草稿，drafts/builds/]" if draft else ("" if b["verified"] else "  [草稿，未审核]")
    print(f"{name}  {b['zh']}（{b['cons']} 命）{flag}")
    ranks: dict[int, list[str]] = {}
    for w in b["weapons"]:
        ranks.setdefault(w["rank"], []).append(w["zh"] + (f"（{w['note']}）" if w["note"] else ""))
    print("武器：")
    for r, ws in sorted(ranks.items()):
        print(f"  {r}. " + "；".join(ws))
    print("圣遗物：")
    for i, combo in enumerate(b["artifact_sets"], 1):
        notes = "；".join(a["note"] for a in combo if a.get("note"))
        print(f"  {i}. " + " + ".join(_piece(a) for a in combo) + (f"（{notes}）" if notes else ""))
    ms = b["main_stats"]
    print("主词条：" + "，".join(f"{_SLOT_ZH.get(k, k)} {' / '.join(_STAT_ZH.get(x, x) for x in v)}" for k, v in ms.items()))
    print("副词条：" + " > ".join(_STAT_ZH.get(x, x) for x in b["substats"]))
    if b["energy_recharge"]:
        print(f"充能：{b['energy_recharge'][0]}%~{b['energy_recharge'][1]}%")
    for n in b["energy_recharge_notes"]:
        print(f"  - {n}")
    if b["stat_targets"]:
        print("面板目标：" + "；".join(b["stat_targets"]))
    t = b["talents"]
    if t["level"]:
        print("天赋要点：" + "、".join(_TALENT_ZH.get(x, x) for x in t["level"]))
    if t["skip"]:
        print("天赋不用点：" + "、".join(_TALENT_ZH.get(x, x) for x in t["skip"]))
    for n in b["notes"]:
        print(f"备注：{n}")


def _piece(a: dict) -> str:
    return f"{a['zh']}（散件 {a['pieces']}）" if a["set"] == "mixed" else f"{a['zh']}{a['pieces']}"


_SLOT_ZH = {"sands": "沙漏", "goblet": "杯", "circlet": "头"}
_STAT_ZH = {"atk%": "攻击力%", "hp%": "生命值%", "def%": "防御力%", "hp": "小生命", "em": "元素精通",
            "energy_recharge": "充能", "crit_rate": "暴击率", "crit_dmg": "暴击伤害", "healing_bonus": "治疗加成"}
_TALENT_ZH = {"normal": "普攻", "skill": "元素战技", "burst": "元素爆发"}


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
    b = sub.add_parser("build", help="查看一个角色的养成方案（武器、圣遗物、词条、充能、天赋）")
    b.add_argument("character")
    b.add_argument("--cons", type=int, default=0)
    b.add_argument("--playstyle", default="default")
    b.add_argument("--json", action="store_true")
    for name, helptext in (("web", "打开本机网页，用中文提问配队和养成"), ("chat", "命令行里用中文提问配队和养成")):
        x = sub.add_parser(name, help=helptext)
        x.add_argument("--key-file", help="DeepSeek key 文件（默认先看 DEEPSEEK_API_KEY，再找仓库和上一级目录的 ds_key.txt）")
        x.add_argument("--paimon-url", default=None, help="派蒙改写模型服务（默认 PAIMON_URL 环境变量或 http://127.0.0.1:8000）")
        x.add_argument("--no-paimon", action="store_true", help="不做派蒙改写，直接显示 DeepSeek 的回答")
        if name == "chat":
            x.add_argument("--roster", help="「我的角色」存哪（默认 work/roster.json）")
        if name == "web":
            x.add_argument("--port", type=int, default=8765)
            x.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
            x.add_argument("--no-voice", action="store_true", help="不开派蒙语音")
            x.add_argument("--voice-dir", help="派蒙语音模型目录（默认 PAIMON_VOICE_DIR 环境变量或仓库上一级的 paimon-voice）")
            x.add_argument("--voice-url", default=None, help="GPT-SoVITS 服务（默认 GPT_SOVITS_URL 环境变量或 http://127.0.0.1:9880）")
    _add_bili_parser(sub)
    gamedata_cli.add_subcommands(sub)
    damage_cli.add_subcommands(sub)
    args = p.parse_args(argv)

    if args.cmd == "import-gamedata":
        gamedata_cli.run(args)
        return
    kb = load(args.data)
    if args.cmd == "bili":
        _bili(args, kb)
        return
    if args.cmd in gamedata_cli.COMMANDS:
        gamedata_cli.run(args, kb)
        return
    if args.cmd in damage_cli.COMMANDS:
        damage_cli.run(args, kb)
        return
    if args.cmd == "validate":
        from .gamedata import load_gamedata
        from .kb import check_build_names, load_draft_builds

        drafts = load_draft_builds(args.data)
        errors = check_build_names(drafts, load_gamedata(args.data))
        if errors:
            sys.exit("drafts/builds 校验失败：\n" + "\n".join(errors))
        print(f"知识库校验通过：{len(kb.characters)} 个角色，{len(kb.teams)} 个队伍模板，{len(kb.builds)} 套配装"
              f"（另有 {len(drafts)} 套待审配装在 drafts/builds/）")
        return
    if args.cmd == "build":
        _show_build(kb, args)
        return
    if args.cmd in ("web", "chat"):
        _chat(args, kb)
        return
    roster = _roster(kb, args.own, args.missing, args.character, args.cons)
    rec = Engine(kb).recommend(args.character, roster, args.playstyle)
    if args.json:
        print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print(rec)


def _deepseek_key(key_file: str | None, data_dir: Path) -> str | None:
    import os

    if key_file:
        return Path(key_file).read_text(encoding="utf-8").strip()
    if os.environ.get("DEEPSEEK_API_KEY"):
        return None  # DeepSeek 自己读环境变量
    for d in (data_dir.parent, data_dir.parent.parent):
        if (d / "ds_key.txt").exists():
            return (d / "ds_key.txt").read_text(encoding="utf-8").strip()
    return None


def _chat(args, kb) -> None:
    from .chat import Assistant, Roster
    from .extract.llm import DeepSeek, LLMError

    data_dir = Path(args.data)
    work = data_dir.parent / "work"
    try:
        model = DeepSeek(api_key=_deepseek_key(args.key_file, data_dir), cache_dir=work / "llm-cache" / "chat")
    except LLMError as e:
        sys.exit(f"{e}，或者把 key 放进仓库上一级目录的 ds_key.txt，或用 --key-file 指定")
    if args.cmd == "web":  # 网页没有「我的角色」，旧的 roster.json 也不读：按全都有算
        roster = Roster(None)
    else:
        roster = Roster(Path(args.roster) if args.roster else work / "roster.json")
    assistant = Assistant(kb, model, data_dir, roster)
    service = None  # noqa: F841  自动拉起的派蒙改写服务，跟本进程同生共死
    if not args.no_paimon:
        import os

        from .chat.paimon import DEFAULT_MODEL_DIR, DEFAULT_URL, PaimonStyle, game_terms, start_service
        url = args.paimon_url or os.environ.get("PAIMON_URL") or DEFAULT_URL
        assistant.style = PaimonStyle(game_terms(assistant.game), url)
        state = assistant.style.health()
        # 本机默认地址上没有服务就在 WSL 里拉起来。service 要一直引用着：它的 stdin 一关，服务就退出
        if state == "offline" and url == DEFAULT_URL:
            service = start_service(os.environ.get("PAIMON_MODEL_DIR") or DEFAULT_MODEL_DIR)
            if service:
                state = "starting"
        print(f"派蒙改写：{url} " + {
            "ready": "已就绪",
            "starting": "正在 WSL 里启动模型，约 15 秒后可用，之前的回答先显示原文",
        }.get(state, f"暂不可用（{state}），先显示原回答，服务就绪后自动启用"))
    if args.cmd == "web":
        from .chat.web import serve
        voice = None if args.no_voice else _voice(args, data_dir)
        serve(assistant, args.port, not args.no_browser, voice, data_dir / "voice")
        return
    print("问配队或养成，例如「我有沃雅妮莎，怎么配队」。空行退出。")
    last = None
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        reply = assistant.ask(q, last)
        last = reply.query
        print(reply.answer if reply.explained_by_llm else "[大模型解释失败，下面是引擎原始结果]\n" + reply.answer)


def _voice(args, data_dir: Path):
    """派蒙语音：有模型目录才开；本机默认地址上没有 GPT-SoVITS 服务就找整合包拉起来。"""
    import os

    from .chat.voice import DEFAULT_URL, PaimonVoice, find_gpt_sovits, start_service
    home = data_dir.parent.parent  # 仓库上一级，ds_key.txt 也在这
    model_dir = Path(args.voice_dir or os.environ.get("PAIMON_VOICE_DIR") or home / "paimon-voice")
    voice = PaimonVoice(model_dir, args.voice_url or os.environ.get("GPT_SOVITS_URL") or DEFAULT_URL)
    if voice.base is None:
        print(f"派蒙语音：{model_dir} 里没有派蒙模型，不开语音（下载方法见 README）")
        return None
    state = voice.health()
    if state == "offline" and voice.url == DEFAULT_URL:
        env = os.environ.get("GPT_SOVITS_DIR")
        gsv = find_gpt_sovits(Path(env)) if env else find_gpt_sovits(home, home.parent)
        if gsv and start_service(gsv):
            state = "starting"
    print(f"派蒙语音：{voice.url} " + {
        "ready": f"已就绪（{voice.base.mood}）",
        "starting": "正在启动 GPT-SoVITS，加载完（约半分钟）网页上就能听",
    }.get(state, f"暂不可用（{state}），服务就绪后自动启用"))
    return voice


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
    c = bs.add_parser("cores", help="把所有角色攻略里讲到的队伍按主 C 汇总（离线，不请求 B 站）")
    c.add_argument("characters", nargs="*", help="只看这些主 C（id 或中文名）；不填就全部")
    c.add_argument("--all", action="store_true", help="sources.yaml 里所有角色的攻略都算（默认只算 pilot: true 的）")
    f = bs.add_parser("frames", help="在字幕提到配装的时间点抽帧拼图（给人看配装面板）")
    f.add_argument("bvid")
    f.add_argument("--at", help="手动指定时间点，逗号分隔，如 1:20,3:05")


def _bili(args, kb) -> None:
    from .bili import BiliClient, BiliError, RateLimited
    from .bili.api import search_videos, subtitles, video_info
    from .bili.frames import build_moments, download_video, frame_sheet
    from .bili.harvest import harvest, is_off_topic, load_sources
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
        if cmd == "cores":
            from .extract.cores import by_core, write_cores_report
            glossary = load_glossary(kb, data_dir / "bili" / "glossary.yaml", data_dir / "releases.yaml")
            only = [getattr(glossary.resolve(n, "character"), "id", n) for n in args.characters] or None
            targets = [t for t in cfg.targets.values() if args.all or t.id in cfg.pilot]  # 默认只汇总当前试点
            report = by_core(kb, glossary, targets, work / "bili", only)
            print(write_cores_report(work / "drafts", report, glossary.names()))
            return
        if args.characters:
            targets = []
            for name in args.characters:
                t = cfg.find(name)
                if t is None:
                    sys.exit(f"{name} 不在 {sources_path} 里，先加进去（id、中文名、上线日期）")
                targets.append(t)
        else:
            targets = [t for t in cfg.targets.values() if t.id in cfg.pilot]
        glossary = load_glossary(kb, data_dir / "bili" / "glossary.yaml", data_dir / "releases.yaml")
        model = None
        if cmd in ("extract", "run"):
            model = DeepSeek(cache_dir=work / "llm-cache")
        for t in targets:
            print(f"== {t.zh}")
            if cmd in ("harvest", "run"):
                chosen = harvest(client, t, cfg, work / "bili", refresh=args.refresh, explain=args.explain)
            if cmd in ("extract", "run"):
                out = extract_target(kb, glossary, t, model, work, refresh=args.refresh,
                                     whitelist=cfg.whitelist)
                for _ in range(3):  # 补进来的视频也可能不是攻略，最多补三轮
                    if cmd != "run" or not any(is_off_topic(work / "bili", t.id, m.bvid) for m in chosen):
                        break
                    print("  有视频被判断不是攻略，补选后重新出草稿")
                    chosen = harvest(client, t, cfg, work / "bili", explain=args.explain)
                    out = extract_target(kb, glossary, t, model, work, whitelist=cfg.whitelist)
                if out:
                    print(f"  草稿：{out[0]}\n  报告：{out[1]}")
        print(f"本次 B 站请求 {client.requests_made} 次")
    except RateLimited as e:
        sys.exit(f"停下：{e}")
    except (BiliError, LLMError) as e:
        sys.exit(str(e))
