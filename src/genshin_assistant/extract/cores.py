"""按主 C 汇总：把所有角色攻略里抽到的队伍类结论，按队伍的主 C 重新分组。

主 C 在队里的位置是固定的，能让老队伍变强的只有新辅助。所以一个主 C 当前的最优队，往往写在最新辅助的攻略里
（妮可的攻略会给出带妮可的法尔伽队，沃雅妮莎的攻略会讲绫华队和丝柯克队）。这里把这些散在各处的队伍收拢到主 C 名下，
每条标上来自谁的攻略、视频发布日期，越新的辅助越靠前。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..bili.harvest import Target
from ..kb import KnowledgeBase
from .compare import annotate
from .draft import _support, describe
from .glossary import Glossary
from .schema import KIND_ZH, VideoClaims
from .vote import aggregate

TEAM_KINDS = ("teammate", "slot_function", "team_constraint", "substitution")


def load_claims(bili_dir: Path, target_id: str) -> list[VideoClaims]:
    """这个角色最近一次选中的视频的抽取结果（还没抽的跳过）。"""
    d = bili_dir / target_id
    sel = d / "selection.json"
    bvids = json.loads(sel.read_text(encoding="utf-8")) if sel.exists() else [p.stem.split(".")[0] for p in d.glob("*.claims.json")]
    out = []
    for b in bvids:
        p = d / f"{b}.claims.json"
        if p.exists():
            out.append(VideoClaims(**json.loads(p.read_text(encoding="utf-8"))))
    return out


def by_core(kb: KnowledgeBase, glossary: Glossary, targets: list[Target], bili_dir: Path,
            only: list[str] | None = None) -> dict[str, list]:
    """主 C id → [(结论, 来自哪些角色的攻略)]。同一个视频被多个角色选中只算一次。"""
    seen: dict[str, VideoClaims] = {}
    source: dict[str, set[str]] = {}
    for t in targets:
        for vc in load_claims(bili_dir, t.id):
            seen.setdefault(vc.bvid, vc)
            source.setdefault(vc.bvid, set()).add(t.zh)
    cores = sorted({c.subject for vc in seen.values() for c in vc.claims if c.kind in TEAM_KINDS})
    if only:
        cores = [c for c in cores if c in only]
    out = {}
    for core in cores:
        videos = [vc.model_copy(update={"claims": [c for c in vc.claims if c.kind in TEAM_KINDS and c.subject == core]})
                  for vc in seen.values()]
        videos = [v for v in videos if v.claims]
        findings = aggregate(videos, releases=glossary.releases(), names=glossary.names(), reworks=glossary.reworks)
        annotate(findings, kb, core)
        out[core] = [(f, sorted({s for bs in f.votes.values() for b in bs for s in source.get(b, ())}), len(videos))
                     for f in findings]
    return out


def write_cores_report(out_dir: Path, report: dict[str, list], names: dict[str, str]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"cores-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.md"
    lines = ["# 按主 C 汇总的队伍（待审核）", "",
             "把所有角色攻略里讲到的队伍按主 C 收拢。新辅助的攻略常给出老主 C 当前的最优队，但也偏向推荐那个辅助本人，审的时候留意。", ""]
    order = sorted(report, key=lambda c: -max((n for _, _, n in report[c]), default=0))
    for core in order:
        rows = report[core]
        n = max((x for _, _, x in rows), default=0)
        lines += [f"## {names.get(core, core)}（{core}）  ·  {n} 个视频讲到", ""]
        for kind in TEAM_KINDS:
            items = [(f, src) for f, src, _ in rows if f.kind == kind]
            if not items:
                continue
            lines.append(f"### {KIND_ZH[kind]}")
            for f, src in sorted(items, key=lambda x: (-x[0].support, -int(x[0].dates[1].replace('-', '') or 0))):
                mark = "⚠️ " if f.review_flags else ""
                when = f"（{'~'.join(dict.fromkeys(f.dates))}）" if f.dates[0] else ""
                lines.append(f"- {mark}{describe(f)}  ·  {_support(f, n)} 个视频{when}  ·  来自：{'、'.join(src) or '?'} 的攻略"
                             f"  ·  知识库：{f.kb_status}")
                for flag in f.review_flags:
                    lines.append(f"  - {flag}")
                for e in f.evidence[:2]:
                    lines.append(f"  - [{e.bvid}]({e.link()})「{e.quote}」")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
