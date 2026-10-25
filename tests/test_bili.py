"""B 站客户端测试：全部用假的网络层，不会真的请求 B 站。"""

import json
from datetime import date

import pytest

from genshin_assistant.bili.api import pinned_comments, search_videos, subtitles, video_info
from genshin_assistant.bili.client import BiliClient, BiliError, Credentials, RateLimited, sign_wbi
from genshin_assistant.bili.frames import build_moments
from genshin_assistant.bili.api import SubtitleLine, VideoHit
from genshin_assistant.bili.harvest import Target, select_videos

IMG = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
SUB = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"
NAV = {"code": -101, "data": {"wbi_img": {"img_url": IMG, "sub_url": SUB}}}


def test_wbi_signature_matches_original_js():
    # 期望值用原插件 lib/bili-api.mjs 的 signWbi 在 Node 里算出
    q = sign_wbi({"search_type": "video", "keyword": "那维莱特 配队 (C0)!~'", "page": 1}, IMG, SUB, 1700000000)
    assert q == ("keyword=%E9%82%A3%E7%BB%B4%E8%8E%B1%E7%89%B9%20%E9%85%8D%E9%98%9F%20C0~&page=1"
                 "&search_type=video&wts=1700000000&w_rid=6fe04ef558c714159e6fb6bc544331a5")


class FakeNet:
    """按 URL 片段返回预设响应，并记录请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        for frag, resp in self.routes.items():
            if frag in url:
                resp = resp(url) if callable(resp) else resp
                status, body = resp if isinstance(resp, tuple) else (200, resp)
                return status, body if isinstance(body, bytes) else json.dumps(body).encode()
        raise AssertionError(f"没有预设响应：{url}")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def client(tmp_path, routes, logged_in=True, **kw):
    clock = Clock()
    net = FakeNet(routes)
    c = BiliClient(Credentials("sess", "jct", "buv") if logged_in else Credentials(), tmp_path,
                   transport=net, sleep=clock.sleep, clock=clock, **kw)
    return c, net, clock


def test_throttle_and_daily_budget(tmp_path):
    c, net, clock = client(tmp_path, {"/x/v2/reply": {"code": 0, "data": {}}}, min_gap=1.5, daily_budget=2)
    c.get_json("/x/v2/reply")
    c.get_json("/x/v2/reply")
    assert clock.slept == [1.5]
    with pytest.raises(RateLimited, match="读预算"):
        c.get_json("/x/v2/reply")
    assert len(net.calls) == 2


def test_risk_code_trips_breaker_and_blocks_further_requests(tmp_path):
    c, net, clock = client(tmp_path, {"/x/v2/reply": {"code": -352, "message": "风控校验失败"}})
    with pytest.raises(RateLimited, match="熔断 5 分钟"):
        c.get_json("/x/v2/reply")
    with pytest.raises(RateLimited, match="熔断中"):
        c.get_json("/x/v2/reply")
    assert len(net.calls) == 1
    clock.now += 6 * 60
    with pytest.raises(RateLimited, match="熔断 10 分钟"):  # 连续撞风控，退避翻倍
        c.get_json("/x/v2/reply")


def test_http_412_trips_breaker(tmp_path):
    c, _, _ = client(tmp_path, {"/x/v2/reply": (412, b"")})
    with pytest.raises(RateLimited):
        c.get_json("/x/v2/reply")
    assert c.status()["ban_minutes_left"] == 5


def test_view_is_cached(tmp_path):
    view = {"code": 0, "data": {"aid": 1, "cid": 2, "title": "t", "desc": "", "pubdate": 0, "duration": 600,
                                "owner": {"name": "up", "mid": 9}}}
    c, net, _ = client(tmp_path, {"/nav": NAV, "/wbi/view": view})
    video_info(c, "BV1")
    video_info(c, "BV1")
    assert sum("/wbi/view" in u for u, _ in net.calls) == 1


def test_search_parses_results(tmp_path):
    result = {"code": 0, "data": {"result": [
        {"type": "video", "bvid": "BV1a", "title": "<em class=\"keyword\">那维莱特</em>配队&amp;养成", "author": "up1",
         "mid": 11, "play": 5000, "pubdate": 1730000000, "duration": "12:34", "description": "d"},
        {"type": "ketang", "bvid": "", "title": "课程"},
    ]}}
    c, net, _ = client(tmp_path, {"/nav": NAV, "/search/type": result})
    hits = search_videos(c, "那维莱特 配队")
    assert [(h.bvid, h.title, h.duration) for h in hits] == [("BV1a", "那维莱特配队&养成", 754)]
    url = next(u for u, _ in net.calls if "/search/type" in u)
    assert "w_rid=" in url and "wts=" in url


def test_subtitles_uses_signed_player_endpoint_and_downloads_track(tmp_path):
    view = {"code": 0, "data": {"aid": 1, "cid": 2, "title": "t", "desc": "", "pubdate": 0, "duration": 600,
                                "owner": {"name": "up", "mid": 9}}}
    player = {"code": 0, "data": {"subtitle": {"subtitles": [
        {"lan": "ai-en", "subtitle_url": "//aisubtitle.hdslb.com/en.json?auth_key=1"},
        {"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/zh.json?auth_key=1-0-abc"},
    ]}}}
    track = {"body": [{"from": 1.0, "to": 3.0, "content": "那维莱特 配队"}, {"from": 3.0, "to": 4.0, "content": " "}]}
    c, net, _ = client(tmp_path, {"/nav": NAV, "/wbi/view": view, "/player/wbi/v2": player, "zh.json": track})
    lan, lines = subtitles(c, video_info(c, "BV1"))
    assert lan == "ai-zh" and [(s.start, s.text) for s in lines] == [(1.0, "那维莱特 配队")]
    sub_url, sub_headers = next((u, h) for u, h in net.calls if "zh.json" in u)
    assert sub_url.startswith("https://") and sub_url.endswith("auth_key=1-0-abc")  # 不能截断
    assert "cookie" not in sub_headers


def test_subtitles_requires_login(tmp_path):
    c, _, _ = client(tmp_path, {}, logged_in=False)
    with pytest.raises(BiliError, match="登录"):
        from genshin_assistant.bili.api import VideoInfo
        subtitles(c, VideoInfo("BV1", 1, 2, "t", "", 0, 0, "up", 9))


def test_pinned_comments_handles_both_shapes_and_up_replies(tmp_path):
    from genshin_assistant.bili.api import VideoInfo
    info = VideoInfo("BV1", 1, 2, "t", "", 0, 0, "up", 9)
    reply = lambda m, text: {"member": {"uname": "u", "mid": m}, "content": {"message": text}, "like": 1}  # noqa: E731
    data = {"code": 0, "data": {"upper": {"top": reply(9, "文字版总结：芙宁娜万叶钟离")},
                                "replies": [reply(5, "路人"), reply(9, "补充：没钟离用白术")]}}
    c, _, _ = client(tmp_path, {"/x/v2/reply": data})
    got = pinned_comments(c, info)
    assert [(x.tag, x.text) for x in got] == [("置顶", "文字版总结：芙宁娜万叶钟离"), ("UP主", "补充：没钟离用白术")]


def test_select_videos_filters_and_prefers_whitelist():
    t = Target("neuvillette", "那维莱特", ["水龙"], since=date(2023, 11, 8), max_videos=3)
    new = 1730000000
    hits = [
        VideoHit("BV1", "那维莱特配队", "路人", 1, 90000, new, 600),
        VideoHit("BV2", "水龙毕业面板", "信任UP", 2, 100, new, 600),
        VideoHit("BV3", "那维莱特爆料", "x", 3, 1, 1690000000, 600),  # 上线前
        VideoHit("BV4", "那维莱特30秒", "x", 4, 1, new, 30),  # 太短
        VideoHit("BV5", "芙宁娜配队", "x", 5, 1, new, 600),  # 标题没有角色名
        VideoHit("BV1", "那维莱特配队", "路人", 1, 90000, new, 600),  # 重复
    ]
    assert [h.bvid for h in select_videos(hits, t, ["信任UP"])] == ["BV2", "BV1"]


def test_build_moments_picks_spaced_build_mentions():
    lines = [SubtitleLine(10, 12, "先说圣遗物"), SubtitleLine(12, 14, "主词条生命"), SubtitleLine(40, 42, "武器选择"),
             SubtitleLine(60, 62, "聊聊剧情")]
    assert build_moments(lines) == [11.5, 41.5]


def test_select_videos_reports_why_videos_were_dropped():
    t = Target("varka", "法尔加", since=date(2026, 2, 19))
    new = 1775000000  # 2026-04
    hits = [
        VideoHit("BV1", "法尔加幻化武器超简单的攻略", "x", 1, 9, new, 300),
        VideoHit("BV2", "新角色配队思路", "x", 2, 9, new, 300, description="法尔加双风队"),  # 角色名只在简介里
        VideoHit("BV3", "法尔加配队", "x", 3, 9, 1760000000, 300),
    ]
    rejected = []
    kept = select_videos(hits, t, [], exclude_words=["幻化"], rejected=rejected)
    assert [h.bvid for h in kept] == ["BV2"]
    assert [(h.bvid, why) for h, why in rejected] == [
        ("BV1", "标题含排除词「幻化」"), ("BV3", "发布于上线日期 2026-02-19 之前")]
