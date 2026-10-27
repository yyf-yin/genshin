"""问答层测试。用假的大模型，验证「解析 → 引擎 → 解释」的接线和兜底，不测大模型本身答得好不好。"""

import json
import threading
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
    r = make(kb, {"intent": "team", "character": "沃雅妮莎"}).ask("我有沃雅妮莎，怎么配队")
    assert r.facts["status"] == "no_team_template"
    assert r.facts["build"]["weapons"]
    assert isinstance(r.facts["appears_in"], list)
    assert "元素减抗：水" in r.facts["kit"][0]["provides"]


def test_character_outside_kb_uses_draft_build(kb):
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
        assert state["owned"] is None and any(c["zh"] == "芙宁娜" and c["in_kb"] for c in state["characters"])
        post("/api/roster", {"owned": {"芙宁娜": 1}})
        assert a.roster.load() == {"芙宁娜": 1}
        reply = post("/api/ask", {"question": "水龙怎么配", "last": {"intent": "team", "character": "那维莱特", "junk": 1}})
        assert reply["facts"]["status"] == "team" and reply["query"]["character"] == "那维莱特"
        post("/api/roster", {"owned": None})
        assert a.roster.load() is None
    finally:
        server.shutdown()


def test_switching_character_drops_stale_missing(kb):
    a = make(kb, {"intent": "build", "character": "阿罗夏", "missing": ["芙宁娜"]})
    r = a.ask("阿罗夏 1 命怎么养成", Query("team", "那维莱特", 0, ["芙宁娜"]))
    assert r.query.missing == []
    a.model.parsed = {"intent": "team", "character": "那维莱特", "missing": ["芙宁娜", "枫原万叶"]}
    assert a.ask("那万叶也没有呢", Query("team", "那维莱特", 0, ["芙宁娜"])).query.missing == ["芙宁娜", "枫原万叶"]
