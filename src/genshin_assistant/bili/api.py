"""搜索、视频信息、带时间戳的字幕、置顶评论。

接口用法和踩坑记录移植自 dsh-bili-whale（MIT）的 search.mjs、subtitles.mjs、comments.mjs。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field

from .client import BiliClient, BiliError


@dataclass
class VideoHit:
    bvid: str
    title: str
    author: str
    mid: int
    play: int
    pubdate: int  # unix 秒
    duration: int  # 秒
    description: str = ""
    tags: str = ""  # 搜索结果里的标签，逗号分隔

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SubtitleLine:
    start: float
    end: float
    text: str


@dataclass
class Comment:
    user: str
    mid: int
    text: str
    like: int = 0
    tag: str = ""  # 置顶 / UP主


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    cid: int
    title: str
    desc: str
    pubdate: int
    duration: int
    owner: str
    owner_mid: int
    tags: list[str] = field(default_factory=list)


_TAG_RE = re.compile(r"<[^>]+>")


def _strip(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", str(s or "")))


def _duration(v) -> int:
    """搜索结果里时长是 "12:34" 或 "1:02:03"。"""
    if isinstance(v, int):
        return v
    secs = 0
    for part in str(v or "0").split(":"):
        secs = secs * 60 + int(part or 0)
    return secs


def search_videos(client: BiliClient, keyword: str, page: int = 1, order: str = "totalrank") -> list[VideoHit]:
    """order: totalrank 综合 / pubdate 最新 / click 播放多。"""
    data = client.get_json("/x/web-interface/wbi/search/type",
                           {"search_type": "video", "keyword": keyword, "page": page, "order": order},
                           wbi=True, referer="https://search.bilibili.com/")
    hits = []
    for r in data.get("result") or []:
        if r.get("type", "video") != "video" or not r.get("bvid"):
            continue
        hits.append(VideoHit(
            bvid=r["bvid"], title=_strip(r.get("title")), author=str(r.get("author", "")),
            mid=int(r.get("mid") or 0), play=int(r.get("play") or 0), pubdate=int(r.get("pubdate") or 0),
            duration=_duration(r.get("duration")), description=_strip(r.get("description")),
            tags=_strip(r.get("tag"))))
    return hits


def video_info(client: BiliClient, bvid: str) -> VideoInfo:
    d = client.get_json("/x/web-interface/wbi/view", {"bvid": bvid}, wbi=True, ttl=24 * 3600,
                        referer=f"https://www.bilibili.com/video/{bvid}/")
    return VideoInfo(
        bvid=bvid, aid=int(d["aid"]), cid=int(d.get("cid") or d["pages"][0]["cid"]), title=d.get("title", ""),
        desc=d.get("desc", ""), pubdate=int(d.get("pubdate") or 0), duration=int(d.get("duration") or 0),
        owner=d.get("owner", {}).get("name", ""), owner_mid=int(d.get("owner", {}).get("mid") or 0))


def subtitles(client: BiliClient, info: VideoInfo, prefer: str = "ai-zh") -> tuple[str, list[SubtitleLine]]:
    """返回 (字幕轨道语言, 字幕行)。没有字幕返回 ("", [])。

    踩坑（来自原插件 2026-10-14 的实测）：
    1. 必须走 WBI 签名的 /x/player/wbi/v2，旧的 /x/player/v2 给的 subtitle_url 缺 auth_key，CDN 会 403；
    2. auth_key 分钟级有效，拿到就下载，URL 不能截断；
    3. 匿名拿不到 AI 字幕，必须带登录 cookie。
    """
    if not client.creds.logged_in:
        raise BiliError("拉 AI 字幕需要登录 cookie（建议用小号），见 README 的 B 站提取一节")
    ref = f"https://www.bilibili.com/video/{info.bvid}/"
    p1 = client.get_json("/x/player/wbi/v2", {"bvid": info.bvid, "cid": info.cid}, wbi=True, referer=ref)
    subs = (p1.get("subtitle") or {}).get("subtitles") or []
    if not subs:
        # 兜底：播放器实际调用的端点
        ext = json.dumps({"video_type": 1}, separators=(",", ":"))
        p2 = client.get_json("/x/v2/subtitle/web/view", {
            "oid": info.aid, "pid": info.aid, "context_ext": ext, "type": 1, "cur_production_type": 0,
            "preferred_language": prefer, "playlist_switch": 0}, referer=ref)
        subs = (p2.get("subtitle") or {}).get("subtitles") or []
    if not subs:
        return "", []
    pick = (next((s for s in subs if s.get("lan") == prefer), None)
            or next((s for s in subs if s.get("lan") == "zh-CN"), None)
            or next((s for s in subs if str(s.get("lan", "")).startswith("zh")), None)
            or subs[0])
    url = str(pick["subtitle_url"])
    url = url if url.startswith("http") else "https:" + url
    try:
        body = json.loads(client.fetch(url, referer=ref, cookie=False)).get("body") or []
    except ValueError:
        raise BiliError(f"{info.bvid} 字幕不是 JSON") from None
    lines = [SubtitleLine(float(b["from"]), float(b.get("to", b["from"])), str(b["content"]).strip())
             for b in body if str(b.get("content", "")).strip()]
    return str(pick.get("lan", "")), lines


def pinned_comments(client: BiliClient, info: VideoInfo, scan: int = 20) -> list[Comment]:
    """置顶评论（UP 主常在这里贴文字版总结），外加首页热评里 UP 主自己的评论。

    评论接口匿名可读，oid 是 aid 不是 BV 号；sort=2 是热度序。
    """
    d = client.get_json("/x/v2/reply", {"type": 1, "oid": info.aid, "sort": 2, "ps": scan, "pn": 1},
                        referer=f"https://www.bilibili.com/video/{info.bvid}/")

    def one(r: dict, tag: str) -> Comment:
        m = r.get("member") or {}
        return Comment(user=str(m.get("uname", "")), mid=int(m.get("mid") or 0),
                       text=str((r.get("content") or {}).get("message", "")).strip(),
                       like=int(r.get("like") or 0), tag=tag)

    found: list[Comment] = []
    top = d.get("top")
    if isinstance(top, list):
        found += [one(r, "置顶") for r in top if r]
    elif isinstance(top, dict):
        found += [one(r, "置顶") for r in (top.get("upper"), top.get("admin")) if r]
    upper_top = (d.get("upper") or {}).get("top")
    if upper_top:
        found.append(one(upper_top, "置顶"))
    for r in d.get("replies") or []:
        if int((r.get("member") or {}).get("mid") or 0) == info.owner_mid:
            found.append(one(r, "UP主"))
    seen, out = set(), []
    for c in found:
        if c.text and c.text not in seen:
            seen.add(c.text)
            out.append(c)
    return out
