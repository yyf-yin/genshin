"""问答层测试。用假的大模型，验证「解析 → 引擎 → 解释」的接线和兜底，不测大模型本身答得好不好。"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from genshin_assistant import load
from genshin_assistant.chat import Assistant, Query, Roster
from genshin_assistant.chat.assistant import PARSE_SYSTEM
from genshin_assistant.chat.web import make_handler
from genshin_assistant.extract.llm import LLMError


class FakeModel:
    """解析时返回给定的 query；解释时把收到的引擎结果原样塞进 answer，方便断言。"""

    model = "fake"

    def __init__(self, parsed: dict, explain_fails: bool = False):
        self.parsed = parsed
        self.explain_fails = explain_fails
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        if system.startswith(PARSE_SYSTEM[:20]):
            return self.parsed
        if self.explain_fails:
            raise LLMError("boom")
        return {"answer": user}


@pytest.fixture(scope="module")
def kb():
    return load()


def make(kb, parsed, tmp_path=None, **kw):
    roster = Roster(tmp_path / "roster.json" if tmp_path else None)
    return Assistant(kb, FakeModel(parsed, **kw), _data_dir(), roster)


def _data_dir():
    from genshin_assistant.kb import DEFAULT_DATA_DIR
    return DEFAULT_DATA_DIR


def members(facts):
    return [[m["zh"] for m in t["members"]] for t in facts["teams"]]


def test_team_with_missing_character(kb):
    a = make(kb, {"intent": "team", "character": "水龙", "cons": 0, "missing": ["芙宁娜"]})
    r = a.ask("我没有芙宁娜，水龙怎么配")
    assert r.facts["status"] == "team"
    assert r.facts["character"] == "那维莱特"
    assert r.facts["teams"] and all("芙宁娜" not in m for m in members(r.facts))
    assert r.facts["teams"][0]["substitutions"], "缺了首选应该有替补说明"
    assert r.explained_by_llm and "引擎结果" in r.answer
    # 解析提示里带上了知识库的别名，模型才能认出「水龙」
    assert "那维莱特：水龙" in a.model.calls[0][0]


def test_saved_roster_limits_teammates(kb, tmp_path):
    a = make(kb, {"intent": "team", "character": "那维莱特"}, tmp_path)
    a.roster.save({"芙宁娜": 2, "砂糖": 6, "钟离": 0})
    r = a.ask("水龙怎么配")
    used = {n for team in members(r.facts) for n in team}
    assert used <= {"那维莱特", "芙宁娜", "砂糖", "钟离"}
    furina = next(m for t in r.facts["teams"] for m in t["members"] if m["zh"] == "芙宁娜")
    assert furina["cons"] == 2


def test_cons_from_roster_and_question(kb, tmp_path):
    a = make(kb, {"intent": "build", "character": "阿罗夏"}, tmp_path)
    a.roster.save({"阿罗夏": 1})
    assert a.ask("阿罗夏怎么养").facts["build"]["cons"] == "1-6"
    a.model.parsed = {"intent": "build", "character": "阿罗夏", "cons": 0}
    assert a.ask("0 命阿罗夏怎么养").facts["build"]["cons"] == "0"


def test_core_without_team_template_still_gets_build(kb):
    # 2026-10-27 起五个新主C都有队伍模板了；法尔伽是剩下没有模板的主C（也还没有配装）
    r = make(kb, {"intent": "team", "character": "法尔伽"}).ask("法尔伽怎么配队")
    assert r.facts["status"] == "no_team_template"
    assert "build" in r.facts
    assert isinstance(r.facts["appears_in"], list)
    assert "主C" in r.facts["kit"][0]["provides"]


def test_support_asks_for_carry_first(kb):
    a = make(kb, {"intent": "team", "character": "沃雅妮莎"})
    r = a.ask("我有沃雅妮莎，怎么配队")
    assert r.facts["status"] == "need_carry" and r.facts["universal"] is False
    names = [c["zh"] for c in r.facts["candidates"]]
    assert "薇斯纳" in names and "那维莱特" in names  # 星扩散 / 水冰主C
    assert "法尔伽" not in names
    assert any(c["zh"] == "莱欧斯利" and "排斥治疗" in c["problems"][0] for c in r.facts["unfit"])
    assert r.query.awaiting_carry


def test_universal_support_just_asks(kb):
    r = make(kb, {"intent": "team", "character": "枫原万叶"}).ask("万叶怎么配队")
    assert r.facts["status"] == "need_carry" and r.facts["universal"] is True


def test_support_with_carry_returns_teams_containing_support(kb):
    a = make(kb, {"intent": "team", "character": "沃雅妮莎", "carry": "那维莱特"})
    r = a.ask("搭配那维莱特")
    assert r.facts["status"] == "team" and r.facts["carry"] == "那维莱特"
    assert r.facts["teams"] and all("沃雅妮莎" in m for m in members(r.facts))
    assert not r.query.awaiting_carry


def test_carry_answer_moved_out_of_character(kb):
    # 上一轮在问主C，模型把主C写进了 character：程序挪到 carry
    a = make(kb, {"intent": "team", "character": "那维莱特"})
    r = a.ask("那维莱特", Query("team", "沃雅妮莎", awaiting_carry=True))
    assert (r.query.character, r.query.carry) == ("沃雅妮莎", "那维莱特")
    assert r.facts["carry"] == "那维莱特"


def test_support_carries_from_team_templates(kb):
    # 主C的队伍模板里已有这个辅助时，算合适的主C，而且排在前面（2026-10-27 奥黛塔被列成「不适合桑多涅」）
    r = make(kb, {"intent": "team", "character": "奥黛塔"}).ask("奥黛塔怎么配队")
    assert r.facts["status"] == "need_carry"
    names = [c["zh"] for c in r.facts["candidates"]]
    assert {"桑多涅", "薇斯纳", "赛诺"} <= set(names[:6])
    assert "桑多涅" not in [c["zh"] for c in r.facts["unfit"]]


def test_carry_without_template(kb):
    r = make(kb, {"intent": "team", "character": "沃雅妮莎", "carry": "法尔伽"}).ask("配法尔伽")
    assert r.facts["status"] == "carry_no_template"
    assert r.facts["carry_kit"] and r.facts["support_kit"] and r.facts["build"]


def test_character_outside_kb_uses_draft_build(kb):
    from genshin_assistant.kb import load_draft_builds

    drafts = [b for b in load_draft_builds() if b.char not in kb.characters]
    if not drafts:
        pytest.skip("drafts/builds/ 里没有知识库之外角色的配装")
    r = make(kb, {"intent": "build", "character": "八重神子"}).ask("八重神子怎么养")
    assert r.facts["status"] == "not_in_kb"
    assert r.facts["build"]["draft"] is True


def test_unknown_and_unsupported(kb):
    assert make(kb, {"intent": "team", "character": "不存在的人"}).ask("?").facts["status"] == "unknown_character"
    assert make(kb, {"intent": "other"}).ask("今天天气").facts["status"] == "unsupported"


def test_follow_up_passes_last_query(kb):
    a = make(kb, {"intent": "team", "character": "那维莱特", "missing": ["芙宁娜", "枫原万叶"]})
    last = Query("team", "那维莱特", 1, ["芙宁娜"])
    a.ask("那万叶也没有呢", last)
    assert '"character": "那维莱特"' in a.model.calls[0][1]


def test_explain_failure_falls_back_to_plain_text(kb):
    r = make(kb, {"intent": "team", "character": "那维莱特"}, explain_fails=True).ask("水龙怎么配")
    assert not r.explained_by_llm
    assert "那维莱特（0 命）" in r.answer


def test_bad_llm_fields_are_sanitized():
    q = Query.from_llm({"intent": "dance", "cons": "满", "missing": None, "own": ["", " 钟离 "]})
    assert (q.intent, q.cons, q.missing, q.own) == ("other", None, [], ["钟离"])
    assert Query.from_llm({"cons": 9}).cons == 6


def test_web_endpoints(kb, tmp_path):
    a = make(kb, {"intent": "team", "character": "那维莱特"}, tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(a))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "原神配队助手" in page
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state == {"model": "fake", "paimon": "off"} and "我的角色" not in page
        reply = post("/api/ask", {"question": "水龙怎么配", "last": {"intent": "team", "character": "那维莱特", "junk": 1}})
        assert reply["facts"]["status"] == "team" and reply["query"]["character"] == "那维莱特"
        try:
            post("/api/roster", {"owned": {"芙宁娜": 1}})
            raise AssertionError("/api/roster should be gone")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()


def test_switching_character_drops_stale_missing(kb):
    a = make(kb, {"intent": "build", "character": "阿罗夏", "missing": ["芙宁娜"]})
    r = a.ask("阿罗夏 1 命怎么养成", Query("team", "那维莱特", 0, ["芙宁娜"]))
    assert r.query.missing == []
    a.model.parsed = {"intent": "team", "character": "那维莱特", "missing": ["芙宁娜", "枫原万叶"]}
    assert a.ask("那万叶也没有呢", Query("team", "那维莱特", 0, ["芙宁娜"])).query.missing == ["芙宁娜", "枫原万叶"]


def test_model_never_sees_damage_numbers(kb):
    a = make(kb, {"intent": "team", "character": "那维莱特"})
    r = a.ask("水龙怎么配")
    sent = a.model.calls[-1][1]
    for key in ('"total"', '"share"', '"score"'):
        assert key not in sent
    dmg = [t["damage"] for t in r.facts["teams"] if t.get("damage")]
    assert dmg, "那维莱特队应该有伤害数据"
    assert all(m["role"] in ("主力输出", "重要输出", "以辅助为主") for d in dmg for m in d["members"])
    assert all(isinstance(h, str) for d in dmg for m in d["members"] for h in m["top_hits"])
