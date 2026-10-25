"""DeepSeek 调用（OpenAI 兼容接口，JSON 输出）。相同输入的结果缓存在本地，重跑不再花钱。

环境变量：DEEPSEEK_API_KEY（必填）、DEEPSEEK_MODEL（默认 deepseek-chat）、
DEEPSEEK_BASE_URL（默认 https://api.deepseek.com）。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Protocol


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """输出被 max_tokens 截断（finish_reason=length）。调用方应该把材料拆小再试。"""


class JSONModel(Protocol):
    model: str

    def complete_json(self, system: str, user: str) -> dict: ...


class DeepSeek:
    def __init__(self, api_key: str | None = None, model: str | None = None, base_url: str | None = None,
                 cache_dir: Path | None = None, post: Callable[[str, dict, dict], dict] | None = None):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.cache_dir = cache_dir
        self._post = post or _post_json
        if not self.api_key and post is None:
            raise LLMError("没有 DEEPSEEK_API_KEY 环境变量")

    def complete_json(self, system: str, user: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 8000,
        }
        key = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        cached = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if cached and cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        resp = self._post(f"{self.base_url}/chat/completions", body,
                          {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            choice = resp["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"DeepSeek 返回格式不对：{str(resp)[:300]}") from e
        reason = choice.get("finish_reason")
        if reason == "length":
            raise LLMTruncated(f"DeepSeek 输出被截断（finish_reason=length，已输出 {len(content)} 字）")
        try:
            result = json.loads(content)
        except ValueError as e:
            raise LLMError(f"DeepSeek 返回的不是合法 JSON（finish_reason={reason}）：…{content[-200:]}") from e
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result


def _post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"DeepSeek HTTP {e.code}：{e.read()[:300].decode(errors='replace')}") from None
