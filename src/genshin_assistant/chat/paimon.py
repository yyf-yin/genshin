"""派蒙风格改写：DeepSeek 写好的回答，交给本机的派蒙微调模型逐段改写，网页上显示改写后的话。

模型服务是 D:\\projects\\model 里的 scripts/client-server.py（Qwen3-1.7B + QLoRA），只监听 127.0.0.1：
    GET  /api/health    {"state": "ready" | "loading" | "error", ...}
    POST /api/generate  {"text": "...", "mode": "tuned"} → {"results": {"tuned": {"text", "stopped_at_eos", ...}}}

小模型会截断人名、改数字、加原文没有的话，所以每段改写都要过 faithful 检查，不过关的段落保留原文。
服务没开、没加载好或者超时，整段回答都用原文。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable

DEFAULT_URL = "http://127.0.0.1:8000"

# 数量和档位：命座、件数、精炼、阿拉伯数字、带十百千的中文数（「两百五」「七十五」）。
# 「一部分」「两把」这类普通量词不算，改写时换说法很正常
_NUMBER = re.compile(
    r"[零一二两三四五六七八九\d]+(?:命|件|星)|精[一二三四五1-5]|\d+(?:\.\d+)?%?"
    r"|[零一二两三四五六七八九]*[十百千万][零一二两三四五六七八九十百千万]*"
)


class PaimonStyle:
    def __init__(self, terms: Iterable[str], url: str = DEFAULT_URL, budget: float = 120.0):
        """terms：要原样保留的专有名词（角色、武器、圣遗物套装）；budget：一个回答最多花多少秒改写。"""
        self.url = url.rstrip("/")
        self.budget = budget
        names = sorted({t for t in terms if len(t) >= 2}, key=len, reverse=True)
        self._terms = re.compile("|".join(map(re.escape, names))) if names else None
        self._names = names

    def health(self) -> str:
        """服务状态：ready / loading / error / offline。"""
        try:
            with urllib.request.urlopen(f"{self.url}/api/health", timeout=2) as r:
                return str(json.loads(r.read()).get("state") or "error")
        except (OSError, ValueError):
            return "offline"

    def rewrite(self, text: str) -> str | None:
        """逐段改写。服务不可用时返回 None；可用时没过检查的段落保留原文。"""
        if self.health() != "ready":
            return None
        deadline = time.monotonic() + self.budget
        out = []
        for line in text.split("\n"):
            left = deadline - time.monotonic()
            if not line.strip() or left <= 1:
                out.append(line)
                continue
            new = self._generate(line.strip(), left)
            out.append(new if new is not None and self.faithful(line, new) else line)
        return "\n".join(out)

    def _generate(self, text: str, timeout: float) -> str | None:
        body = json.dumps({"text": text, "mode": "tuned", "max_new_tokens": 512}).encode()
        req = urllib.request.Request(f"{self.url}/api/generate", body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read())["results"]["tuned"]
        except (OSError, ValueError, KeyError, TypeError):  # 超时、原文过长（400）、服务出错
            return None
        new = str(res.get("text") or "").strip()
        return new if new and res.get("stopped_at_eos") else None  # 没停在结束符就是被截断了

    def faithful(self, orig: str, new: str) -> bool:
        """改写有没有保住事实：专有名词一个不少也不多、没被截短，数字和命座不变，篇幅没有暴涨。"""
        if len(new) > len(orig) * 1.5 + 20:  # 凭空加句子
            return False
        if _numbers(orig) != _numbers(new):
            return False
        if self._terms is None:
            return True
        before, after = Counter(self._terms.findall(orig)), Counter(self._terms.findall(new))
        if set(before) != set(after):
            return False
        # 「那维莱特」写成「维莱特」：去掉首字的残名比全名出现得多
        return all(new.count(t[1:]) == after[t] for t in after if len(t) >= 3 and t[1:] not in self._names)


def _numbers(text: str) -> set[str]:
    # 单个「百」「千」「万」是「百分比」「千岩」「万叶」里的字，不是数
    return {n for n in _NUMBER.findall(text) if len(n) > 1 or n.isdigit()}


def game_terms(game) -> list[str]:
    """游戏数据里所有角色、武器、圣遗物套装的中文名。"""
    return [row["zh"] for table in (game.characters, game.weapons, game.artifact_sets) for row in table.values()]


DEFAULT_MODEL_DIR = r"D:\projects\model"
WSL_DISTRO = "Paimon-Train"


def start_service(model_dir: str = DEFAULT_MODEL_DIR, port: int = 8000):
    """在 WSL 里启动改写服务，返回 Popen；模型项目不在就返回 None。

    服务进程在 WSL 里自成一个会话，关掉 wsl.exe 杀不掉它，所以让 bash 守着 stdin：
    本进程退出（包括直接关窗口）时管道断开，cat 读到 EOF，就把服务也关掉。
    """
    import subprocess
    from pathlib import Path

    if not (Path(model_dir) / "scripts" / "client-server.py").exists():
        return None
    serve = f"bash scripts/run-python.sh -u scripts/client-server.py --host 127.0.0.1 --port {port}"
    watch = f"{serve} & pid=$!; cat >/dev/null; kill $pid"
    try:
        return subprocess.Popen(["wsl.exe", "-d", WSL_DISTRO, "-u", "paimon", "--cd", model_dir, "--", "bash", "-c", watch],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:  # 没装 WSL
        return None
