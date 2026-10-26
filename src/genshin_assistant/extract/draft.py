"""输出待审草稿：一份 YAML（逐条填 review: accept / edit / reject）和一份方便阅读的 Markdown 报告。

草稿只放在 work/drafts/ 下，永远不直接写进 data/ 知识库。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..bili.harvest import Material
from .schema import KIND_ZH, VideoClaims
from .vote import LEVEL_ZH, Finding


def _fmt_t(t: float | None) -> str:
    return "" if t is None else f"{int(t // 60):02d}:{int(t % 60):02d}"


def _support(f: Finding, total: int) -> str:
    if f.eligible_note:
        return f"{f.eligible_support}/{f.eligible}（{f.eligible_note}）"
    return f"{f.support}/{total}"


def describe(f: Finding) -> str:
    d = f.data
    cons = f"［{f.cons}］" if f.cons else ""
    style = f"［{f.playstyle}］" if f.playstyle else ""
    no = "不" if f.value == "against" else ""
    if f.kind == "teammate":
        fn = f"（{'、'.join(d.get('functions') or [])}）" if d.get("functions") else ""
        return f"{f.subject}{cons}{style} 队{no}推荐 {d.get('char')}{fn}"
    if f.kind == "slot_function":
        note = f"，{d['slot_note']}" if d.get("slot_note") else ""
        return f"{f.subject}{cons}{style} 队需要 {d.get('tag')}：{LEVEL_ZH.get(f.value, f.value)}{note}"
    if f.kind == "team_constraint":
        rule = f"  规则：{d['rule']}" if d.get("rule") else ""
        return f"{f.subject}{cons} 队整队限制：{d.get('description', '')}{rule}"
    if f.kind == "substitution":
        why = "；".join(d.get("reasons") or [])
        return f"{f.subject}{cons} 队：没有 {d.get('original')} 时{'不能' if no else '可'}用 {d.get('substitute')}" + (f"（{why}）" if why else "")
    if f.kind == "cons_effect":
        return f"{f.subject} {f.cons or ''}：{d.get('effect', '')}"
    if f.kind == "playstyle":
        return f"{f.subject} 玩法「{d.get('name')}」" + (f"（{d['when']}）" if d.get("when") else "") + f"：{d.get('description', '')}"
    rank = f" 第 {d['rank']} 选择" if d.get("rank") else ""
    return f"{f.subject}{cons}{style} {d.get('field')}：{d.get('value')}{rank}"


def write_draft(out_dir: Path, target_id: str, target_zh: str, materials: list[Material],
                videos: list[VideoClaims], findings: list[Finding], not_mentioned: list[str]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    yml, md = out_dir / f"{target_id}-{stamp}.yaml", out_dir / f"{target_id}-{stamp}.md"
    flagged = sum(1 for f in findings if f.review_flags)
    kb_counts = Counter(f.kb_status.split("（")[0] for f in findings)
    doc = {
        "character": target_id,
        "zh": target_zh,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "AI 草稿，未经审核。逐条把 review 改成 accept / edit / reject，再手工合入 data/。",
        "summary": {
            "videos": len(materials),
            "claims_kept": sum(len(v.claims) for v in videos),
            "claims_rejected": sum(len(v.rejected) for v in videos),
            "findings": len(findings),
            "need_review": flagged,
            "vs_kb": dict(kb_counts),
        },
        "videos": [{"bvid": m.bvid, "title": m.title, "up": m.up, "published": m.published,
                    "subtitle": m.subtitle_lan or "无", "whitelisted": m.whitelisted, "url": m.url}
                   for m in materials],
        "findings": [{
            "id": f"f{i:03d}",
            "review": "pending",
            "kind": f.kind,
            "summary": describe(f),
            "subject": f.subject,
            "cons": f.cons,
            "playstyle": f.playstyle,
            "data": f.data,
            "majority": f.value,
            "support": _support(f, len(materials)),
            "published": "~".join(dict.fromkeys(f.dates)) if f.dates[0] else "",
            "votes": f.votes,
            "kb": f.kb_status,
            "flags": f.review_flags,
            "evidence": [{"bvid": e.bvid, "source": e.source, "t": _fmt_t(e.t), "link": e.link(), "quote": e.quote}
                         for e in f.evidence],
        } for i, f in enumerate(findings, 1)],
        "kb_not_mentioned": not_mentioned,
        "corrections": dict(sum((Counter(v.corrections) for v in videos), Counter()).most_common()),
        "rejected": [{"bvid": v.bvid, **r} for v in videos for r in v.rejected],
    }
    yml.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")

    lines = [f"# {target_zh} 攻略提取草稿（待审核）", "",
             f"{len(materials)} 个视频，{len(findings)} 条结论，其中 {flagged} 条需要重点审核。"
             f"和知识库对照：{'，'.join(f'{k} {n}' for k, n in kb_counts.items()) or '无'}。", ""]
    for kind, zh in KIND_ZH.items():
        rows = [(i, f) for i, f in enumerate(findings, 1) if f.kind == kind]
        if not rows:
            continue
        lines += [f"## {zh}", ""]
        for i, f in rows:
            mark = "⚠️ " if f.review_flags else ""
            when = f"（{'~'.join(dict.fromkeys(f.dates))}）" if f.dates[0] else ""
            lines.append(f"- **f{i:03d}** {mark}{describe(f)}  ·  {_support(f, len(materials))} 个视频{when}  ·  知识库：{f.kb_status}")
            for flag in f.review_flags:
                lines.append(f"  - {flag}")
            for e in f.evidence:
                where = f"[{e.bvid} {_fmt_t(e.t)}]({e.link()})" if e.t is not None else f"[{e.bvid} {e.source}]({e.link()})"
                lines.append(f"  - {where}「{e.quote}」")
        lines.append("")
    if not_mentioned:
        lines += ["## 知识库有、视频没提到", ""] + [f"- {x}" for x in not_mentioned] + [""]
    lines += ["## 视频", ""] + [f"- [{m.title}]({m.url})  {m.up}  {m.published}  字幕：{m.subtitle_lan or '无'}"
                                 for m in materials]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return yml, md
