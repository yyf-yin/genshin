"""跨视频汇总：同一结论按支持它的视频数计票，矛盾的标为“有争议”，不同命座、玩法的结论分开。

时效：原神常常先出主 C，后面几个版本才补齐最佳队友。妮可比法尔伽晚上线，法尔伽上线期的攻略不可能推荐她，
所以推荐某个队友的票只在“这个队友上线前一个版本之后发布的视频”里数（攻略常提前讲下个版本的角色）。
A 只有旧视频推荐，而之后出了和 A 功能重叠的新队友 B，B 上线后的视频（至少 MIN_LATER 个）推荐 B、都不再推荐 A，
才标“可能已被 B 取代”。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .schema import ALWAYS_REVIEW, Claim, Evidence, VideoClaims

PREVIEW = timedelta(days=42)  # 攻略提前一个版本讲新角色
MIN_LATER = 3  # 新队友上线后至少要有这么多视频，才判断旧推荐是否被取代
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
    eligible: int = 0  # 能提到这条的视频数（队友上线前一个版本之后发布的）；0 表示没算
    eligible_support: int = 0  # 这些视频里持多数意见的
    eligible_note: str = ""  # 例如“妮可 2026-05-14 上线，只算之后的视频”
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
              releases: dict[str, date] | None = None, names: dict[str, str] | None = None,
              reworks: dict[str, list[tuple[date, str]]] | None = None) -> list[Finding]:
    """releases：角色 id → 上线日期（不知道的不写）；names：id → 中文名，只用于提示文字；
    reworks：角色 id → [(加强日期, 版本)]。"""
    releases, names, reworks = releases or {}, names or {}, reworks or {}
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
            window = (releases[who] - PREVIEW).isoformat()
            in_window = {b for b, d in published.items() if d and d >= window}
            if len(in_window) < len(videos):
                f.eligible = len(in_window)
                f.eligible_support = len(in_window & set(votes[value]))
                f.eligible_note = f"{names.get(who, who)} {releases[who]} 上线，只算之后（含前一个版本）的视频"
        f.review_flags = flags
        findings.append(f)
    _flag_outdated(findings, releases, names, published)
    _flag_before_rework(findings, names, reworks)
    findings.sort(key=lambda f: (KIND_ORDER.index(f.kind), f.subject, -f.support, str(f.data)))
    return findings


def _flag_outdated(findings: list[Finding], releases: dict[str, date], names: dict[str, str],
                   published: dict[str, str]) -> None:
    """A 只有旧视频推荐；之后出的 B 能提供 A 的全部功能，B 上线后的视频推荐 B、没有一个再推荐 A（任何命座、玩法下）：
    A 可能已被 B 取代。只要求功能有交集会误判：温迪和妮可都能充能，但温迪的价值是聚怪。"""
    mates = [f for f in findings if f.kind == "teammate" and f.value == "for"]
    last_for: dict[str, str] = {}  # 角色 → 推荐它的最新视频日期（各命座、玩法合并）
    for f in mates:
        c = f.data.get("char")
        last_for[c] = max(last_for.get(c, ""), f.dates[1])
    for a in mates:
        aid, need = a.data.get("char"), set(a.data.get("functions") or [])
        if not need or not last_for.get(aid):
            continue
        for b in sorted(mates, key=lambda x: releases.get(x.data.get("char"), date.min), reverse=True):
            bid = b.data.get("char")
            if bid == aid or bid not in releases or last_for[aid] >= releases[bid].isoformat():
                continue
            later = {v for v, d in published.items() if d and d >= releases[bid].isoformat()}
            if need <= set(b.data.get("functions") or []) and len(later) >= MIN_LATER and set(b.votes["for"]) & later:
                a.review_flags.insert(0, f"可能已被 {names.get(bid, bid)} 取代：推荐它的视频都早于 {names.get(bid, bid)} 上线"
                                         f"（{releases[bid]}），之后的 {len(later)} 个视频都没再推荐它，"
                                         f"而 {names.get(bid, bid)} 也能提供 {'、'.join(sorted(need))}")
                break


def _flag_before_rework(findings: list[Finding], names: dict[str, str], reworks: dict[str, list[tuple[date, str]]]) -> None:
    """主角色或被推荐的队友后来加强过，而支持这条的视频都在加强之前：当时的评价可能不准了。"""
    for f in findings:
        if not f.dates[1]:
            continue
        for who in dict.fromkeys(x for x in (f.subject, _newcomer(f)) if x):
            later = [(d, v) for d, v in reworks.get(who, []) if f.dates[1] < d.isoformat()]
            if later:
                d, v = max(later)
                f.review_flags.insert(0, f"可能已过时：支持它的视频都早于 {names.get(who, who)} 的加强（{v}，{d}）")
