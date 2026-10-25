"""按角色挑攻略视频并取材：简介、置顶评论、带时间戳的字幕。结果存成 work/bili/<角色>/<BV>.json。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .api import Comment, SubtitleLine, VideoHit, pinned_comments, search_videos, subtitles, video_info
from .client import BiliClient, BiliError, RateLimited

DEFAULT_QUERIES = ["{zh} 配队", "{zh} 养成 攻略", "{zh} 替代 平替"]


@dataclass
class Target:
    """一个要提取的角色（可以还不在知识库里，例如新角色）。"""

    id: str
    zh: str
    aliases: list[str] = field(default_factory=list)
    since: date | None = None  # 只要这天之后发布的视频（角色上线日期）
    queries: list[str] = field(default_factory=list)
    max_videos: int = 8

    def names(self) -> list[str]:
        return [self.zh, *self.aliases]


@dataclass
class SourceConfig:
    targets: dict[str, Target]
    whitelist: list[str]  # 信任的 UP 主，名字或 mid
    min_duration: int = 120
    pilot: list[str] = field(default_factory=list)  # 不指定角色时默认跑的

    def find(self, name: str) -> Target | None:
        for t in self.targets.values():
            if name == t.id or name in t.names():
                return t
        return None


def load_sources(path: Path) -> SourceConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    d = raw.get("defaults") or {}
    targets = {}
    for c in raw.get("characters") or []:
        since = c.get("since")
        targets[c["id"]] = Target(
            id=c["id"], zh=c["zh"], aliases=list(c.get("aliases") or []),
            since=since if isinstance(since, date) else (date.fromisoformat(since) if since else None),
            queries=list(c.get("queries") or d.get("queries") or DEFAULT_QUERIES),
            max_videos=int(c.get("max_videos") or d.get("max_videos") or 8))
    return SourceConfig(targets, [str(w) for w in raw.get("whitelist_up") or []],
                        int(d.get("min_duration") or 120),
                        [c["id"] for c in raw.get("characters") or [] if c.get("pilot")])


@dataclass
class Material:
    bvid: str
    title: str
    up: str
    up_mid: int
    pubdate: int
    duration: int
    desc: str
    pinned: list[Comment]
    subtitle_lan: str
    subtitles: list[SubtitleLine]
    whitelisted: bool = False
    fetched_at: str = ""

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}/"

    @property
    def published(self) -> str:
        return datetime.fromtimestamp(self.pubdate, timezone.utc).date().isoformat() if self.pubdate else ""

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Material":
        d = json.loads(path.read_text(encoding="utf-8"))
        d["pinned"] = [Comment(**c) for c in d["pinned"]]
        d["subtitles"] = [SubtitleLine(**s) for s in d["subtitles"]]
        return cls(**d)


def select_videos(hits: list[VideoHit], target: Target, whitelist: list[str], min_duration: int = 120) -> list[VideoHit]:
    """过滤并排序：标题里要有角色名；上线日期之后发布；不要短视频；白名单 UP 主优先，其余按播放量。"""
    since_ts = datetime(target.since.year, target.since.month, target.since.day,
                        tzinfo=timezone.utc).timestamp() if target.since else 0
    wl = set(whitelist)
    seen, kept = set(), []
    for h in hits:
        if h.bvid in seen:
            continue
        seen.add(h.bvid)
        if not any(n in h.title for n in target.names()):
            continue
        if h.pubdate < since_ts or h.duration < min_duration:
            continue
        kept.append(h)
    kept.sort(key=lambda h: (not (h.author in wl or str(h.mid) in wl), -h.play))
    return kept[: target.max_videos]


def harvest(client: BiliClient, target: Target, cfg: SourceConfig, out_dir: Path,
            refresh: bool = False, log=print) -> list[Material]:
    """搜索、挑选、取材。已取过的视频直接读本地文件，不再请求。撞到风控立刻停，已取到的照样返回。"""
    hits: list[VideoHit] = []
    for q in target.queries:
        kw = q.format(zh=target.zh)
        try:
            found = search_videos(client, kw)
        except RateLimited:
            raise
        except BiliError as e:
            log(f"  搜索「{kw}」失败：{e}")
            continue
        log(f"  搜索「{kw}」：{len(found)} 条")
        hits += found
    chosen = select_videos(hits, target, cfg.whitelist, cfg.min_duration)
    log(f"  选中 {len(chosen)} 个视频")
    wl = set(cfg.whitelist)
    results = []
    for h in chosen:
        path = out_dir / target.id / f"{h.bvid}.json"
        if path.exists() and not refresh:
            results.append(Material.load(path))
            log(f"  {h.bvid} 已有，跳过：{h.title}")
            continue
        try:
            info = video_info(client, h.bvid)
            pinned = pinned_comments(client, info)
            lan, lines = subtitles(client, info)
        except RateLimited as e:
            log(f"  停下：{e}")
            break
        except BiliError as e:
            log(f"  {h.bvid} 取材失败：{e}")
            continue
        m = Material(bvid=h.bvid, title=info.title, up=info.owner, up_mid=info.owner_mid, pubdate=info.pubdate,
                     duration=info.duration, desc=info.desc, pinned=pinned, subtitle_lan=lan, subtitles=lines,
                     whitelisted=info.owner in wl or str(info.owner_mid) in wl,
                     fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        m.save(path)
        results.append(m)
        log(f"  {h.bvid} 字幕 {len(lines)} 行（{lan or '无字幕'}），置顶 {len(pinned)} 条：{info.title}")
    return results


def load_materials(out_dir: Path, target_id: str) -> list[Material]:
    return [Material.load(p) for p in sorted((out_dir / target_id).glob("BV*.json")) if not p.name.endswith(".claims.json")]
