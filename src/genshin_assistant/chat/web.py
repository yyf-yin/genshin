"""本机网页：和派蒙对话。只监听 127.0.0.1，自用。
网页不设「我的角色」：按全都有算，四星的 0 命和 6 命并排给。

接口：
    GET  /             页面
    GET  /api/state    模型名、派蒙改写和派蒙语音服务状态、开场白
    GET  /api/greeting.wav  预先合成好的开场白语音（还没合成好时 404）
    POST /api/ask      {"question": "...", "last": 上一轮的 query 或 null} → 回答
    POST /api/speak    {"text": "一段话"} → wav（派蒙语音没开或不可用时 503）
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .assistant import Assistant, Query
from .voice import PaimonVoice

PAGE = Path(__file__).with_name("static") / "index.html"

GREETING = ("哇，旅行者，你终于来啦！派蒙等你好久了～这次想带谁去冒险？告诉派蒙伙伴的名字，派蒙都帮你安排得明明白白！"
            "要是哪位伙伴还没来到你身边，也别担心，派蒙帮你找个靠谱的替补！")


class Greeting:
    """开场白语音：启动时在后台合成一次存进 cache_dir，网页打开就直接播文件，不用等 GPT-SoVITS。"""

    def __init__(self, voice: PaimonVoice, cache_dir: Path, text: str = GREETING, mood: str = "兴奋说话"):
        self.voice, self.cache_dir, self.text, self.mood = voice, Path(cache_dir), text, mood
        self.path: Path | None = None

    def prepare(self, tries: int = 120, wait: float = 5.0) -> Path | None:
        """缓存里有就直接用；没有就等语音服务起来再合成（最多等 tries × wait 秒）。"""
        for i in range(tries):
            self.path = self.voice.cached(self.text, self.cache_dir, self.mood)
            if self.path or i == tries - 1:
                break
            time.sleep(wait)
        return self.path


def make_handler(assistant: Assistant, voice: PaimonVoice | None = None, greeting: Greeting | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 不刷屏
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                style = assistant.style.health() if assistant.style else "off"
                self._json({"model": getattr(assistant.model, "model", ""), "paimon": style,
                            "voice": voice.health() if voice else "off",
                            "greeting": greeting.text if greeting else GREETING,
                            "greeting_audio": bool(greeting and greeting.path and greeting.path.exists())})
            elif self.path == "/api/greeting.wav":
                if greeting and greeting.path and greeting.path.exists():
                    self._send(200, greeting.path.read_bytes(), "audio/wav")
                else:
                    self._send(404, b"not ready", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                body = self._body()
                if self.path == "/api/ask":
                    question = str(body.get("question") or "").strip()
                    if not question:
                        self._json({"error": "问题是空的"}, 400)
                        return
                    last = body.get("last")
                    names = {f.name for f in fields(Query)}
                    q = Query(**{k: v for k, v in last.items() if k in names}) if isinstance(last, dict) else None
                    self._json(assistant.ask(question, q).to_dict())
                elif self.path == "/api/speak":
                    wav = voice.speak(str(body.get("text") or "")) if voice else None
                    if wav:
                        self._send(200, wav, "audio/wav")
                    else:
                        self._json({"error": "派蒙语音不可用"}, 503)
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:  # 页面上显示错误，不让服务挂掉
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}：{e}"}, 500)

    return Handler


def serve(assistant: Assistant, port: int = 8765, open_browser: bool = True, voice: PaimonVoice | None = None,
          cache_dir: Path | None = None) -> None:
    greeting = Greeting(voice, cache_dir) if voice and cache_dir else None
    if greeting:
        threading.Thread(target=greeting.prepare, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(assistant, voice, greeting))
    url = f"http://127.0.0.1:{port}/"
    print(f"原神配队助手：{url}（Ctrl+C 退出）")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
