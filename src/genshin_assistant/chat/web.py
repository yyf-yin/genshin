"""本机网页：聊天 + 「我的角色」。只监听 127.0.0.1，自用。

接口：
    GET  /             页面
    GET  /api/state    全部角色（游戏数据）、已存的我的角色
    POST /api/roster   {"owned": {"芙宁娜": 2, ...}} 保存；{"owned": null} 清空（= 全都有）
    POST /api/ask      {"question": "...", "last": 上一轮的 query 或 null} → 回答
"""

from __future__ import annotations

import json
import traceback
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .assistant import Assistant, Query

PAGE = Path(__file__).with_name("static") / "index.html"


def make_handler(assistant: Assistant):
    game = assistant.game
    in_kb = {c.zh for c in assistant.kb.characters.values()}
    characters = sorted(
        ({"zh": c["zh"], "element": c.get("element"), "rarity": c.get("rarity"), "in_kb": c["zh"] in in_kb}
         for c in game.characters.values() if c.get("element") and not c["id"].startswith("traveler")),
        key=lambda c: (not c["in_kb"], c["element"] or "", c["zh"]))

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
                self._json({"characters": characters, "owned": assistant.roster.load(),
                            "model": getattr(assistant.model, "model", "")})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                body = self._body()
                if self.path == "/api/roster":
                    owned = body.get("owned")
                    assistant.roster.save(None if owned is None else {str(k): int(v) for k, v in owned.items()})
                    self._json({"ok": True})
                elif self.path == "/api/ask":
                    question = str(body.get("question") or "").strip()
                    if not question:
                        self._json({"error": "问题是空的"}, 400)
                        return
                    last = body.get("last")
                    names = {f.name for f in fields(Query)}
                    q = Query(**{k: v for k, v in last.items() if k in names}) if isinstance(last, dict) else None
                    self._json(assistant.ask(question, q).to_dict())
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:  # 页面上显示错误，不让服务挂掉
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}：{e}"}, 500)

    return Handler


def serve(assistant: Assistant, port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(assistant))
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
