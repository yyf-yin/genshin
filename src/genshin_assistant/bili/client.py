"""B 站只读客户端：凭据、WBI 签名、节流、每日读预算、风控熔断、元信息缓存。

移植自 dsh-bili-whale（MIT，Copyright (c) 2026 ALTERSSSS）的 tools/lib/bili-api.mjs、
tools/lib/cred.mjs 和各命令里重复的 WBI 签名，见 THIRD_PARTY_NOTICES.md。
只保留读接口；评论、私信、动态等写操作一概不做。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
API = "https://api.bilibili.com"

MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19,
             29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
             22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

# 撞到就熔断的风控码（HTTP 200 但 code 是这些）
BAN_CODES = {-412, -352, -799, -509, -10403}
CODE_HINTS = {
    -101: "账号未登录：SESSDATA 失效了，重新复制一次 cookie",
    -352: "风控拦截：请求太快或太像机器人，等几分钟再来",
    -412: "请求被拦截：本机 IP 被 B 站限流",
    -799: "请求过于频繁，退避一会儿",
}

# 只缓存静态元信息；字幕地址带分钟级 auth_key，评论会变，都不能缓存
VIEW_TTL = 24 * 3600
NAV_TTL = 10 * 60

Transport = Callable[[str, dict[str, str]], tuple[int, bytes]]


class BiliError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class RateLimited(BiliError):
    """撞到风控或用完当日预算。上层应该停下，而不是换个接口继续打。"""


@dataclass
class Credentials:
    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""

    def cookie(self) -> str:
        parts = [f"SESSDATA={self.sessdata}", f"bili_jct={self.bili_jct}", f"buvid3={self.buvid3}"]
        return "; ".join(p for p in parts if not p.endswith("="))

    @property
    def logged_in(self) -> bool:
        return bool(self.sessdata)


REPO_ROOT = Path(__file__).resolve().parents[3]


def credential_paths() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")
    return [REPO_ROOT / "bili-credentials.json",  # 仓库根目录，已在 .gitignore 里
            home / ".genshin-assistant" / "bili-credentials.json",
            home / ".bili-ai" / "credentials.json"]  # 和 dsh-bili-whale 共用同一份


def load_credentials() -> Credentials:
    """环境变量优先（BILI_SESSDATA / BILI_JCT / BILI_BUVID3），其次凭据文件。"""
    clean = lambda v: str(v or "").strip().strip("\"'")  # noqa: E731  粘贴时常带引号和换行
    if os.environ.get("BILI_SESSDATA"):
        return Credentials(clean(os.environ.get("BILI_SESSDATA")), clean(os.environ.get("BILI_JCT")),
                           clean(os.environ.get("BILI_BUVID3")))
    for p in credential_paths():
        if p.is_file():
            j = json.loads(p.read_text(encoding="utf-8-sig"))
            return Credentials(clean(j.get("sessdata")), clean(j.get("bili_jct")), clean(j.get("buvid3")))
    return Credentials()


def sign_wbi(params: dict, img_url: str, sub_url: str, now: float | None = None) -> str:
    key_of = lambda u: u.rsplit("/", 1)[-1].split(".")[0]  # noqa: E731
    raw = key_of(img_url) + key_of(sub_url)
    if len(raw) != 64:
        raise BiliError("WBI 签名密钥缺失，请稍后重试或检查登录状态")
    mixin = "".join(raw[i] for i in MIXIN_TAB)[:32]
    values = {k: v for k, v in params.items() if k != "w_rid"}
    values["wts"] = int(now if now is not None else time.time())
    query = "&".join(
        f"{urllib.parse.quote(str(k), safe='')}="
        f"{urllib.parse.quote(''.join(c for c in str(values[k]) if c not in '!()*' + chr(39)), safe='')}"
        for k in sorted(values))
    return f"{query}&w_rid={hashlib.md5((query + mixin).encode()).hexdigest()}"


def _decompress(body: bytes) -> bytes:
    """B 站网页有时不管请求头直接回 gzip / deflate。"""
    if body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    if body[:1] == b"\x78":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return body
    return body


def _urllib_transport(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, _decompress(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, TimeoutError) as e:
        raise BiliError(f"连不上 B 站：{getattr(e, 'reason', e)}") from None


class BiliClient:
    """所有请求都过这里：节流（默认两次请求间隔 1.5 秒）、每日读预算（默认 3000）、撞风控熔断。

    默认值取自 dsh-bili-whale 给小号的保守配置。状态存在 state_dir，跨进程生效。
    """

    def __init__(self, creds: Credentials | None = None, state_dir: Path | str | None = None,
                 min_gap: float = 1.5, daily_budget: int = 3000,
                 transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.creds = creds if creds is not None else load_credentials()
        default_dir = Path(os.environ.get("HOME", ".")) / ".genshin-assistant" / "bili"
        self.state_dir = Path(state_dir) if state_dir else default_dir
        self.min_gap = min_gap
        self.daily_budget = daily_budget
        self.transport = transport or _urllib_transport
        self.sleep = sleep
        self.clock = clock
        self._last = 0.0
        self.requests_made = 0

    # ── 状态文件 ──
    def _read(self, name: str) -> dict:
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, value: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def status(self) -> dict:
        s = self._read("state.json")
        reads = s.get("reads", 0) if s.get("day") == date.today().isoformat() else 0
        ban = max(0.0, s.get("ban_until", 0) - self.clock())
        return {"logged_in": self.creds.logged_in, "reads_today": reads, "daily_budget": self.daily_budget,
                "ban_minutes_left": round(ban / 60, 1), "ban_why": s.get("ban_why") if ban else None}

    def _ban(self, why: str) -> int:
        s = self._read("state.json")
        streak = s.get("streak", 0) + 1
        mins = min(30, 5 * 2 ** (streak - 1))  # 5 → 10 → 20 → 30 分钟
        s.update(ban_until=self.clock() + mins * 60, ban_why=why, streak=streak)
        self._write("state.json", s)
        return mins

    def _before_request(self) -> None:
        s = self._read("state.json")
        left = s.get("ban_until", 0) - self.clock()
        if left > 0:
            raise RateLimited(f"B 站风控熔断中（{s.get('ban_why')}），约 {max(1, round(left / 60))} 分钟后再试")
        today = date.today().isoformat()
        if s.get("day") != today:
            s.update(day=today, reads=0)
        if s["reads"] >= self.daily_budget:
            raise RateLimited(f"今天的读预算 {self.daily_budget} 次已用完，明天再跑")
        s["reads"] += 1
        self._write("state.json", s)
        gap = self.clock() - self._last
        if gap < self.min_gap:
            self.sleep(self.min_gap - gap)
        self._last = self.clock()
        self.requests_made += 1

    # ── 请求 ──
    def headers(self, referer: str = "https://www.bilibili.com/") -> dict[str, str]:
        h = {"user-agent": UA, "referer": referer}
        if self.creds.cookie():
            h["cookie"] = self.creds.cookie()
        return h

    def fetch(self, url: str, referer: str = "https://www.bilibili.com/", cookie: bool = True) -> bytes:
        """原始 GET（字幕 CDN、视频直链也走这里，一样计入预算和节流）。"""
        self._before_request()
        h = self.headers(referer)
        if not cookie:
            h.pop("cookie", None)
        status, body = self.transport(url, h)
        if status in (412, 429):
            mins = self._ban(f"HTTP {status}")
            raise RateLimited(f"HTTP {status}（B 站风控），已熔断 {mins} 分钟，别再打了", code=-412)
        if status >= 400:
            raise BiliError(f"HTTP {status}：{url.split('?')[0]}")
        return body

    def get_json(self, path: str, params: dict | None = None, wbi: bool = False,
                 referer: str = "https://www.bilibili.com/", ttl: int = 0) -> dict:
        """调 api.bilibili.com，返回 data 字段。code != 0 抛错；风控码会熔断。"""
        params = dict(params or {})
        cache_key = path + "?" + urllib.parse.urlencode(sorted(params.items()))
        if ttl:
            hit = self._read("cache.json").get(cache_key)
            if hit and self.clock() - hit["at"] < ttl:
                return hit["data"]
        if wbi:
            nav = self.nav()
            img = nav.get("wbi_img") or {}
            query = sign_wbi(params, img.get("img_url", ""), img.get("sub_url", ""), self.clock())
        else:
            query = urllib.parse.urlencode(params)
        body = self.fetch(f"{API}{path}?{query}" if query else f"{API}{path}", referer)
        try:
            j = json.loads(body)
        except ValueError:
            raise BiliError(f"非 JSON 响应：{path}，可能是风控或接口变更") from None
        code = int(j.get("code", 0))
        if code in BAN_CODES:
            mins = self._ban(f"code={code}")
            raise RateLimited(f"{CODE_HINTS.get(code, j.get('message', ''))}（code={code}），已熔断 {mins} 分钟",
                              code=code)
        if code != 0:
            raise BiliError(f"{path} 失败 code={code} {CODE_HINTS.get(code, j.get('message', ''))}", code=code)
        s = self._read("state.json")
        if s.get("streak"):
            s["streak"] = 0
            self._write("state.json", s)
        data = j.get("data") or {}
        if ttl:
            cache = self._read("cache.json")
            cache[cache_key] = {"at": self.clock(), "data": data}
            if len(cache) > 400:
                for k in sorted(cache, key=lambda k: cache[k]["at"])[: len(cache) - 400]:
                    del cache[k]
            self._write("cache.json", cache)
        return data

    def nav(self) -> dict:
        """nav 接口未登录时 code=-101，但仍会返回 WBI 密钥，所以单独处理。"""
        hit = self._read("cache.json").get("nav")
        if hit and self.clock() - hit["at"] < NAV_TTL:
            return hit["data"]
        body = self.fetch(f"{API}/x/web-interface/nav")
        data = (json.loads(body).get("data") or {})
        if not data.get("wbi_img"):
            raise BiliError("nav 没有返回 WBI 密钥")
        cache = self._read("cache.json")
        cache["nav"] = {"at": self.clock(), "data": {"wbi_img": data["wbi_img"]}}
        self._write("cache.json", cache)
        return cache["nav"]["data"]
