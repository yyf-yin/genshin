"""按角色挑攻略视频并取材：简介、置顶评论、带时间戳的字幕。结果存成 work/bili/<角色>/<BV>.json。"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .api import Comment, SubtitleLine, VideoHit, pinned_comments, search_videos, subtitles, video_info
from .client import BiliClient, BiliError, RateLimited

DEFAULT_QUERIES = ["{zh} 配队", "{zh} 养成 攻略", "{zh} 替代 平替"]
# 标题里有这些词的多半不是配队养成攻略（试点里选进过“法尔伽幻化武器攻略”）
DEFAULT_EXCLUDE = ["幻化", "外观", "剧情", "抽卡", "抽取记录", "爆料", "测试服", "前瞻", "手书", "MMD", "cos", "鬼畜"]


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
    exclude_words: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    require_words: list[str] = field(default_factory=list)

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
                        [c["id"] for c in raw.get("characters") or [] if c.get("pilot")],
                        list(d.get("exclude_words") if d.get("exclude_words") is not None else DEFAULT_EXCLUDE),
                        list(d.get("require_words") or []))


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


def select_videos(hits: list[VideoHit], target: Target, whitelist: list[str], min_duration: int = 120,
                  exclude_words: list[str] = (), rejected: list[tuple[VideoHit, str]] | None = None,
                  require_words: list[str] = ()) -> list[VideoHit]:
    """过滤并排序：标题或简介里要有角色名；标题、简介或标签里要有 require_words 之一（如“原神”）；
    标题不含排除词；上线日期之后发布；不要短视频；
    白名单 UP 主优先，其余按播放量。传入 rejected 列表会收集被筛掉的视频和原因。"""
    since_ts = datetime(target.since.year, target.since.month, target.since.day,
                        tzinfo=timezone.utc).timestamp() if target.since else 0
    wl = set(whitelist)
    seen, kept = set(), []
    for h in hits:
        if h.bvid in seen:
            continue
        seen.add(h.bvid)
        why = ""
        if not any(n in h.title or n in h.description for n in target.names()):
            why = "标题和简介里没有角色名"
        elif require_words and not any(w in f"{h.title} {h.description} {h.tags}" for w in require_words):
            why = f"标题、简介、标签里没有「{'/'.join(require_words)}」"
        elif bad := next((w for w in exclude_words if w.lower() in h.title.lower()), None):
            why = f"标题含排除词「{bad}」"
        elif h.pubdate < since_ts:
            why = f"发布于上线日期 {target.since} 之前"
        elif h.duration < min_duration:
            why = f"时长 {h.duration} 秒，太短"
        if why:
            if rejected is not None:
                rejected.append((h, why))
            continue
        kept.append(h)
    kept.sort(key=lambda h: (not (h.author in wl or str(h.mid) in wl), -h.play))
    return kept[: target.max_videos]


def whitelist_names(whitelist: list[str]) -> list[str]:
    """白名单里的 UP 主名字（纯数字的是 mid，不能拿来搜）。"""
    return [w for w in whitelist if not w.isdigit()]


def harvest(client: BiliClient, target: Target, cfg: SourceConfig, out_dir: Path,
            refresh: bool = False, explain: bool = False, log=print) -> list[Material]:
    """搜索、挑选、取材。已取过的视频直接读本地文件，不再请求。撞到风控立刻停，已取到的照样返回。

    每个搜索词按综合排序和最新发布各搜一页：新角色的攻略在综合排序里常被老视频挤掉。
    """
    hits: list[VideoHit] = []
    searches = [(q.format(zh=target.zh), order) for q in target.queries for order in ("totalrank", "pubdate")]
    # 白名单 UP 主单独搜一次“UP 名 + 角色名”：泛搜第一页常常排不到他们
    searches += [(f"{up} {target.zh}", "totalrank") for up in whitelist_names(cfg.whitelist)]
    for kw, order in searches:
        try:
            found = search_videos(client, kw, order=order)
        except RateLimited:
            raise
        except BiliError as e:
            log(f"  搜索「{kw}」({order}) 失败：{e}")
            continue
        log(f"  搜索「{kw}」({order})：{len(found)} 条")
        hits += found
    rejected: list[tuple[VideoHit, str]] = []
    chosen = select_videos(hits, target, cfg.whitelist, cfg.min_duration, cfg.exclude_words, rejected,
                           cfg.require_words)
    reasons = Counter(why.split("「")[0].split(" ")[0] for _, why in rejected)
    log(f"  选中 {len(chosen)} 个视频；筛掉 {len(rejected)} 个：" + "，".join(f"{k} {n}" for k, n in reasons.items()))
    if explain:
        for h, why in rejected:
            published = datetime.fromtimestamp(h.pubdate, timezone.utc).date() if h.pubdate else "?"
            log(f"    ✗ {h.bvid} {published} {h.duration // 60}分 {why}：{h.title}")
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
