"""把取材、抽取、投票、对照、出草稿串起来。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..bili.harvest import Material, Target, load_materials
from ..kb import KnowledgeBase
from .compare import annotate, kb_not_mentioned
from .draft import write_draft
from .extractor import SYSTEM_PROMPT, ExtractContext, extract_video, normalize_claim
from .glossary import Glossary
from .llm import JSONModel, LLMError
from .schema import VideoClaims
from .vote import aggregate


PROMPT_VERSION = hashlib.sha1(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]  # 提示词改了，旧的抽取结果也要作废


def core_id(kb: KnowledgeBase, target: Target) -> str:
    for name in (target.id, *target.names()):
        try:
            return kb.resolve(name).id
        except KeyError:
            continue
    return target.id


def extract_target(kb: KnowledgeBase, glossary: Glossary, target: Target, model: JSONModel, work: Path,
                   refresh: bool = False, log=print, whitelist: list[str] | None = None) -> tuple[Path, Path] | None:
    bili_dir = work / "bili"
    materials = load_materials(bili_dir, target.id)
    if whitelist is not None:  # 白名单可能是取材之后才加的，按当前配置重算
        wl = set(whitelist)
        for m in materials:
            m.whitelisted = m.up in wl or str(m.up_mid) in wl
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
        cache = bili_dir / target.id / f"{m.bvid}.claims.json"
        vc = None
        if cache.exists() and not refresh:
            vc = VideoClaims(**json.loads(cache.read_text(encoding="utf-8")))
            if vc.source_hash != f"{m.content_hash()}-{PROMPT_VERSION}":
                log(f"  {m.bvid} 字幕、简介或提示词变了，重新抽取")
                vc = None
        if vc is not None:
            # 名词表、词表改了以后不用重新调用模型：用当前名词表重新归一、重算标记
            vc.claims = [normalize_claim(c.model_copy(update={"flags": []}), ctx) for c in vc.claims]
        else:
            try:
                vc = extract_video(m, ctx, model)
                vc.source_hash = f"{m.content_hash()}-{PROMPT_VERSION}"
            except LLMError as e:
                log(f"  {m.bvid} 抽取失败，跳过（下次重跑会再试）：{e}")
                continue
            cache.write_text(vc.model_dump_json(indent=1), encoding="utf-8")
        usable.append(m)
        log(f"  {m.bvid} 保留 {len(vc.claims)} 条，丢弃 {len(vc.rejected)} 条：{m.title}")
        videos.append(vc)
    releases = glossary.releases()
    if target.since:
        releases.setdefault(cid, target.since)
    findings = aggregate(videos, releases=releases, names=glossary.names(), reworks=glossary.reworks)
    annotate(findings, kb, cid)
    return write_draft(work / "drafts", target.id, target.zh, usable, videos, findings,
                       kb_not_mentioned(findings, kb, cid))
