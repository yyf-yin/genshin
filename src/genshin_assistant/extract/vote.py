"""跨视频汇总：同一结论按支持它的视频数计票，矛盾的标为“有争议”，不同命座、玩法的结论分开。

时效：原神常常先出主 C，后面几个版本才补齐最佳队友。妮可比法尔伽晚上线，法尔伽上线期的攻略不可能推荐她，
所以推荐某个队友的票数只拿“这个队友上线之后发布的视频”当分母；只有旧视频支持、而之后又出了被推荐的新队友的，
标“可能已过时”交给人审。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

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
    eligible: int = 0  # 能提到这条的视频数（队友上线之后发布的）；0 表示没算
    eligible_note: str = ""  # 例如“只算 妮可 上线后的视频”
    dates: tuple[str, str] = ("", "")  # 持多数意见的视频，最早和最晚发布日期

    @property
    def disputed(self) -> bool:
        return len(self.votes) > 1


def _newcomer(c: Claim | Finding) -> str | None:
    """这条结论里“晚上线就不可能被旧视频提到”的角色。"""
    if c.kind == "teammate":
        return c.data.get("char")
    if c.kind == "substitution":
        return c.data.get("substitute")
    return None


def aggregate(videos: list[VideoClaims], per_video_evidence: int = 2,
              releases: dict[str, date] | None = None, names: dict[str, str] | None = None) -> list[Finding]:
    """releases：角色 id → 上线日期（不知道的不写）；names：id → 中文名，只用于提示文字。"""
    releases, names = releases or {}, names or {}
    published = {v.bvid: v.published for v in videos}
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
        days = sorted(published.get(b, "") for b in votes[value] if published.get(b))
        f.dates = (days[0], days[-1]) if days else ("", "")
        who = _newcomer(f)
        if who and who in releases:
            since = releases[who].isoformat()
            f.eligible = sum(1 for d in published.values() if d and d >= since)
            if f.eligible < len(videos):
                f.eligible_note = f"只算 {names.get(who, who)} 上线（{since}）后的视频"
        f.review_flags = flags
        findings.append(f)
    _flag_outdated(findings, releases, names)
    findings.sort(key=lambda f: (KIND_ORDER.index(f.kind), f.subject, -f.support, str(f.data)))
    return findings


def _flag_outdated(findings: list[Finding], releases: dict[str, date], names: dict[str, str]) -> None:
    """有视频推荐了某个后出的队友，而另一条推荐只来自那个队友上线之前的视频：多半是当时的替代，标出来人审。"""
    newer = sorted(((releases[c].isoformat(), c) for f in findings
                    if f.value == "for" and (c := _newcomer(f)) in releases and f.dates[1] >= releases[c].isoformat()),
                   reverse=True)
    if not newer:
        return
    for f in findings:
        if f.value != "for" or not _newcomer(f) or not f.dates[1]:
            continue
        hit = next(((d, c) for d, c in newer if c != _newcomer(f) and f.dates[1] < d), None)
        if hit:
            d, c = hit
            f.review_flags.insert(0, f"可能已过时：支持它的视频都发布于 {names.get(c, c)} 上线（{d}）之前，"
                                     f"而之后的视频推荐了 {names.get(c, c)}")
