"""派蒙改写测试：用假的改写服务，验证逐段检查和各种兜底，不测小模型本身改得好不好。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from genshin_assistant.chat import Assistant
from genshin_assistant.chat.paimon import PaimonStyle

from test_chat import FakeModel, _data_dir

TERMS = ["那维莱特", "芙宁娜", "钟离", "万世流涌大典", "绝缘之旗印"]


@pytest.fixture
def service():
    """假服务：rewrites 里有的段落按表改写，其他段落加个「！」；state 可改。"""
    box = {"state": "ready", "rewrites": {}, "seen": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._json({"state": box["state"]})

        def do_POST(self):
            text = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["text"]
            box["seen"].append(text)
            new = box["rewrites"].get(text, text.rstrip("。") + "！")
            self._json({"results": {"tuned": {"text": new, "stopped_at_eos": True}}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    box["url"] = f"http://127.0.0.1:{server.server_port}"
    yield box
    server.shutdown()


@pytest.mark.parametrize("orig, new, ok", [
    ("那维莱特带万世流涌大典。", "那维莱特就带万世流涌大典啦！", True),
    ("那维莱特是主力输出，芙宁娜是重要输出。", "维莱特是主力输出，芙宁娜是重要输出！", False),  # 人名截短
    ("那维莱特是主力输出，芙宁娜是重要输出。", "那维莱特是主力输出，重要输出也有！", False),  # 丢了人名
    ("钟离护盾很稳。", "钟离护盾很稳，芙宁娜也行！", False),  # 多了人名
    ("单水从两百五降到两百一。", "单水从25降到21！", False),  # 数字被改
    ("零命那维莱特容易被打断。", "那维莱特容易被打断！", False),  # 命座丢了
    ("生命百分比不是废词条。", "生命可不是废词条！", True),  # 「百分比」的百不算数字
    ("绝缘之旗印四件套。", "绝缘之旗印四件套！一套给夜兰，一套给别人，先看清楚目标再选思路，别搞混啦！", False),  # 凭空加句子
])
def test_faithful(orig, new, ok):
    assert PaimonStyle(TERMS).faithful(orig, new) is ok


def test_rewrite_keeps_original_for_bad_paragraphs(service):
    service["rewrites"] = {"那维莱特是主力输出。": "维莱特是主力输出！"}
    style = PaimonStyle(TERMS, service["url"])
    out = style.rewrite("那维莱特是主力输出。\n\n钟离负责护盾。")
    assert out == "那维莱特是主力输出。\n\n钟离负责护盾！"
    assert service["seen"] == ["那维莱特是主力输出。", "钟离负责护盾。"]  # 空行不发


def test_rewrite_needs_ready_service(service):
    service["state"] = "loading"
    assert PaimonStyle(TERMS, service["url"]).rewrite("钟离负责护盾。") is None
    assert PaimonStyle(TERMS, "http://127.0.0.1:9").health() == "offline"


def test_assistant_shows_stylized_answer(service):
    from genshin_assistant import load

    model = FakeModel({"intent": "other"})
    model.complete_json = lambda system, user: (
        {"intent": "other"} if "解析器" in system else {"answer": "钟离负责护盾。"})
    a = Assistant(load(), model, _data_dir(), style=PaimonStyle(TERMS, service["url"]))
    r = a.ask("你好")
    assert r.answer == "钟离负责护盾！" and r.stylized and r.to_dict()["stylized"]
    service["state"] = "error"
    r = a.ask("你好")
    assert r.answer == "钟离负责护盾。" and not r.stylized
