"""把取材、抽取、投票、对照、出草稿串起来。"""

from __future__ import annotations

import json
from pathlib import Path

from ..bili.harvest import Material, Target, load_materials
from ..kb import KnowledgeBase
from .compare import annotate, kb_not_mentioned
from .draft import write_draft
from .extractor import ExtractContext, extract_video
from .glossary import Glossary
from .llm import JSONModel
from .schema import VideoClaims
from .vote import aggregate


def core_id(kb: KnowledgeBase, target: Target) -> str:
    for name in (target.id, *target.names()):
        try:
            return kb.resolve(name).id
        except KeyError:
            continue
    return target.id


def extract_target(kb: KnowledgeBase, glossary: Glossary, target: Target, model: JSONModel, work: Path,
                   refresh: bool = False, log=print) -> tuple[Path, Path] | None:
    bili_dir = work / "bili"
    materials = load_materials(bili_dir, target.id)
    if not materials:
        log(f"  {target.zh} 还没有取材，先跑 bili harvest {target.zh}")
        return None
    cid = core_id(kb, target)
    ctx = ExtractContext(kb, glossary, cid, target.zh)
    videos: list[VideoClaims] = []
    usable: list[Material] = []
    for m in materials:
        if not m.subtitles and not m.desc.strip() and not m.pinned:
            log(f"  {m.bvid} 没有字幕、简介和置顶评论，跳过")
            continue
        usable.append(m)
        cache = bili_dir / target.id / f"{m.bvid}.claims.json"
        if cache.exists() and not refresh:
            vc = VideoClaims(**json.loads(cache.read_text(encoding="utf-8")))
        else:
            vc = extract_video(m, ctx, model)
            cache.write_text(vc.model_dump_json(indent=1), encoding="utf-8")
        log(f"  {m.bvid} 保留 {len(vc.claims)} 条，丢弃 {len(vc.rejected)} 条：{m.title}")
        videos.append(vc)
    findings = aggregate(videos)
    annotate(findings, kb, cid)
    return write_draft(work / "drafts", target.id, target.zh, usable, videos, findings,
                       kb_not_mentioned(findings, kb, cid))
