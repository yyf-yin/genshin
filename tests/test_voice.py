"""派蒙语音测试：用假的 GPT-SoVITS 服务，验证选模型、换权重、按语气选参考音频和网页接口，不测合成出来像不像。"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_chat import _data_dir, kb, make  # noqa: F401

from genshin_assistant.chat.voice import (
    PaimonVoice,
    find_gpt_sovits,
    mood_of,
    scan,
    speakable,
)
from genshin_assistant.chat.web import make_handler


@pytest.fixture
def models(tmp_path):
    """按下载下来的样子摆：每种情绪一个目录，参考音频文件名是台词。"""
    root = tmp_path / "paimon-voice"
    for mood, line in (("平静", "既然罗莎莉亚说足迹上有元素力"), ("疑惑", "这是什么呀"), ("兴奋说话", "太好啦")):
        d = root / f"派蒙-{mood}"
        d.mkdir(parents=True)
        (d / f"{line}.wav").write_bytes(b"RIFF")
        (d / f"派蒙-{mood}-e10.ckpt").write_bytes(b"")
        (d / f"派蒙-{mood}_e25_s11925.pth").write_bytes(b"")
    (root / "说明").mkdir()  # 没有参考音频的目录跳过
    return root


@pytest.fixture
def gsv():
    """假 GPT-SoVITS：记下换过的权重和收到的合成请求，/tts 回一段假 wav。"""
    box = {"weights": [], "tts": [], "fail": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path.startswith("/set_"):
                box["weights"].append((u.path, urllib.parse.parse_qs(u.query)["weights_path"][0]))
                self._send(200, b'"success"')
            else:
                self._send(404, b'{"detail": "Not Found"}')

        def do_POST(self):
            box["tts"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if box["fail"]:
                self._send(400, b'{"message": "tts failed"}')
            else:
                self._send(200, b"RIFF-fake-wav", "audio/wav")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    box["url"] = f"http://127.0.0.1:{server.server_port}"
    yield box
    server.shutdown()


def test_scan(models):
    voices = scan(models)
    assert set(voices) == {"平静", "疑惑", "兴奋说话"}
    v = voices["平静"]
    assert v.prompt == "既然罗莎莉亚说足迹上有元素力" and v.gpt.suffix == ".ckpt" and v.sovits.suffix == ".pth"
    assert scan(models / "没有这个目录") == {}


@pytest.mark.parametrize("text, spoken", [
    ("**那维莱特**是主力输出。", "那维莱特是主力输出。"),
    ("- 芙宁娜：重要输出", "芙宁娜：重要输出"),
    ("1. 先升大招 ✨", "先升大招"),
])
def test_speakable(text, spoken):
    assert speakable(text) == spoken


@pytest.mark.parametrize("text, mood", [("好耶！", "!"), ("你没有芙宁娜吗？」", "?"), ("就这样。", ""), ("", "")])
def test_mood_of(text, mood):
    assert mood_of(text) == mood


def test_speak_loads_weights_once_and_picks_reference(models, gsv):
    voice = PaimonVoice(models, gsv["url"])
    assert voice.speak("那维莱特是主力输出。") == b"RIFF-fake-wav"
    assert voice.speak("你有芙宁娜吗？") and voice.speak("**太棒啦！**")
    # 权重只换一次，换的是「平静」
    assert [w[0] for w in gsv["weights"]] == ["/set_gpt_weights", "/set_sovits_weights"]
    assert all("派蒙-平静" in w[1] for w in gsv["weights"])
    refs = [t["prompt_text"] for t in gsv["tts"]]
    assert refs == ["既然罗莎莉亚说足迹上有元素力", "这是什么呀", "太好啦"]
    assert gsv["tts"][2]["text"] == "太棒啦！" and gsv["tts"][0]["text_lang"] == "zh"


def test_speak_fallbacks(models, gsv, tmp_path):
    assert PaimonVoice(tmp_path / "空", gsv["url"]).health() == "nomodel"
    assert PaimonVoice(models, "http://127.0.0.1:9").speak("你好。") is None  # 服务没开
    voice = PaimonVoice(models, gsv["url"])
    assert voice.speak("**  **") is None and gsv["tts"] == []  # 没字可念不发请求
    gsv["fail"] = True
    assert voice.speak("你好。") is None


def test_find_gpt_sovits(tmp_path):
    pkg = tmp_path / "GPT-SoVITS-v2pro-20250604"
    pkg.mkdir()
    assert find_gpt_sovits(tmp_path) is None
    (pkg / "api_v2.py").write_text("")
    assert find_gpt_sovits(tmp_path / "nope", tmp_path) == pkg
    assert find_gpt_sovits(pkg) == pkg


def test_web_speak(kb, tmp_path, models, gsv):  # noqa: F811
    a = make(kb, {"intent": "team", "character": "那维莱特"}, tmp_path)
    for voice, code in ((PaimonVoice(models, gsv["url"]), 200), (None, 503)):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(a, voice))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            state = json.loads(urllib.request.urlopen(base + "/api/state").read())
            assert state["voice"] == ("ready" if voice else "off")
            req = urllib.request.Request(base + "/api/speak", json.dumps({"text": "你好！"}).encode(),
                                         {"Content-Type": "application/json"})
            try:
                r = urllib.request.urlopen(req)
                assert (r.status, r.headers["Content-Type"], r.read()) == (200, "audio/wav", b"RIFF-fake-wav")
            except urllib.error.HTTPError as e:
                assert e.code == code == 503
        finally:
            server.shutdown()
