"""跨视频汇总：同一结论按支持它的视频数计票，矛盾的标为“有争议”，不同命座、玩法的结论分开。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .schema import ALWAYS_REVIEW, Claim, Evidence, VideoClaims

KIND_ORDER = ["teammate", "slot_function", "team_constraint", "substitution", "cons_effect", "playstyle", "build"]
LEVEL_ZH = {"core": "核心", "major": "重要", "minor": "加分"}


@dataclass
class Finding:
    kind: str
    subject: str
    cons: str | None
    playstyle: str | None
    data: dict
    value: str  # 多数意见：slot_function 是分级，其他是 for / against
    support: int  # 持多数意见的视频数
    mentioned: int  # 提到这条的视频数（含反对意见）
    votes: dict[str, list[str]]  # 取值 → 视频 BV 号
    evidence: list[Evidence]
    review_flags: list[str] = field(default_factory=list)
    kb_status: str = ""

    @property
    def disputed(self) -> bool:
        return len(self.votes) > 1


def aggregate(videos: list[VideoClaims], per_video_evidence: int = 2) -> list[Finding]:
    groups: dict[tuple, list[Claim]] = defaultdict(list)
    for v in videos:
        for c in v.claims:
            groups[c.key()].append(c)
    findings = []
    for claims in groups.values():
        votes: dict[str, list[str]] = defaultdict(list)
        for c in claims:
            if c.bvid not in votes[c.value()]:
                votes[c.value()].append(c.bvid)
        value = max(votes, key=lambda k: (len(votes[k]), k == "for"))
        majority = [c for c in claims if c.value() == value]
        first = majority[0]
        data = dict(first.data)
        if first.kind == "teammate":
            data["functions"] = sorted({t for c in majority for t in c.data.get("functions") or []})
        if first.kind == "substitution":
            data["reasons"] = list(dict.fromkeys(c.data.get("reason") for c in majority if c.data.get("reason")))
            data.pop("reason", None)
        evidence: list[Evidence] = []
        per: dict[str, int] = defaultdict(int)
        for c in majority + [c for c in claims if c.value() != value]:
            for e in c.evidence:
                if per[c.bvid] < per_video_evidence:
                    per[c.bvid] += 1
                    evidence.append(e)
        flags = sorted({f for c in claims for f in c.flags})
        f = Finding(kind=first.kind, subject=first.subject, cons=first.cons, playstyle=first.playstyle, data=data,
                    value=value, support=len(votes[value]), mentioned=len({c.bvid for c in claims}),
                    votes=dict(votes), evidence=evidence)
        if f.kind in ALWAYS_REVIEW:
            flags.insert(0, "功能分级和整队限制最容易推断错，必须人审")
        if f.disputed:
            shown = "，".join(f"{LEVEL_ZH.get(k, {'for': '支持', 'against': '反对'}.get(k, k))} {len(b)} 个视频"
                             for k, b in votes.items())
            flags.insert(0, f"有争议：{shown}")
        if f.support == 1 and len(videos) > 1:
            flags.append("只有 1 个视频提到")
        f.review_flags = flags
        findings.append(f)
    findings.sort(key=lambda f: (KIND_ORDER.index(f.kind), f.subject, -f.support, str(f.data)))
    return findings
