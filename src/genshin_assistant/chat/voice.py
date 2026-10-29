"""派蒙语音：回答逐段交给本机的 GPT-SoVITS（api_v2.py）合成，网页上按段播放。

模型用 B 站「白菜工厂1145号员工」训练的派蒙 GPT-SoVITS 模型（不可商用，自用）。
下载后放进一个目录，每种情绪一个子目录，原样保留文件名：
    paimon-voice/
        派蒙-平静/      派蒙-平静-e10.ckpt   派蒙-平静_e25_s11925.pth   <参考音频台词>.wav
        派蒙-兴奋说话/  ...
参考音频的文件名就是它的台词，合成时当 prompt_text 用。

只加载一套权重（默认「平静」），情绪靠换参考音频：句末是「！」用兴奋的参考，「？」用疑惑的参考。
切权重要几秒，逐段换不划算。

GPT-SoVITS 服务（默认 http://127.0.0.1:9880）：
    GET /set_gpt_weights?weights_path=...    GET /set_sovits_weights?weights_path=...
    POST /tts {"text", "text_lang", "ref_audio_path", "prompt_text", "prompt_lang", ...} → wav，出错 400 + json
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:9880"
BASE = "平静"
# 句子语气 → 用哪个情绪目录的参考音频（目录不在就退回 BASE）
MOODS = {"!": ["兴奋说话", "很激动"], "?": ["疑惑"]}


@dataclass
class Voice:
    mood: str
    gpt: Path | None
    sovits: Path | None
    ref: Path
    prompt: str


def scan(model_dir: Path) -> dict[str, Voice]:
    """读模型目录：{情绪: Voice}。没有参考音频的子目录跳过。"""
    voices = {}
    model_dir = Path(model_dir)
    for d in (sorted(p for p in model_dir.iterdir() if p.is_dir()) if model_dir.is_dir() else []):
        wavs = sorted(d.glob("*.wav"))
        if not wavs:
            continue
        mood = d.name.split("-", 1)[-1]
        ckpt, pth = sorted(d.glob("*.ckpt")), sorted(d.glob("*.pth"))
        voices[mood] = Voice(mood, ckpt[0] if ckpt else None, pth[0] if pth else None, wavs[0], wavs[0].stem)
    return voices


def speakable(text: str) -> str:
    """去掉 Markdown 记号和表情，留下念得出来的字。"""
    text = re.sub(r"[*`#>_~|]+", "", text)
    text = re.sub(r"^\s*(?:[-•]|\d+[.、])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿️]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def mood_of(text: str) -> str:
    end = text.rstrip(" 」』）)~～…。.")[-1:]
    return {"！": "!", "!": "!", "？": "?", "?": "?"}.get(end, "")


class PaimonVoice:
    def __init__(self, model_dir: Path, url: str = DEFAULT_URL):
        self.url = url.rstrip("/")
        self.model_dir = Path(model_dir)
        self.voices = scan(self.model_dir)
        self.base = self.voices.get(BASE) or next(iter(self.voices.values()), None)
        self._loaded = False
        self._lock = threading.Lock()

    def health(self) -> str:
        """ready / offline / nomodel / error。服务第一次连上时把派蒙的权重换上。"""
        if self.base is None:
            return "nomodel"
        try:  # api_v2 没有健康检查接口：根路径回 404 也说明服务起来了
            urllib.request.urlopen(f"{self.url}/", timeout=2).close()
        except urllib.error.HTTPError:
            pass
        except OSError:
            self._loaded = False  # 服务重启过的话要重新换权重
            return "offline"
        with self._lock:
            if not self._loaded:
                self._loaded = self._load_weights()
        return "ready" if self._loaded else "error"

    def _load_weights(self) -> bool:
        for endpoint, path in (("set_gpt_weights", self.base.gpt), ("set_sovits_weights", self.base.sovits)):
            if path is None:
                continue
            q = urllib.parse.urlencode({"weights_path": str(path)})
            try:
                urllib.request.urlopen(f"{self.url}/{endpoint}?{q}", timeout=120).close()
            except OSError:
                return False
        return True

    def speak(self, text: str, mood: str | None = None) -> bytes | None:
        """合成一段话，返回 wav；没东西可念或服务不可用返回 None。mood 指定参考音频（情绪目录名），不给就按句末语气选。"""
        text = speakable(text)
        if not text or self.health() != "ready":
            return None
        v = self._voice(text, mood)
        body = json.dumps({
            "text": text, "text_lang": "zh",
            "ref_audio_path": str(v.ref), "prompt_text": v.prompt, "prompt_lang": "zh",
            "text_split_method": "cut5", "batch_size": 1, "media_type": "wav", "streaming_mode": False,
        }, ensure_ascii=False).encode()
        req = urllib.request.Request(f"{self.url}/tts", body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except OSError:
            return None


    def _voice(self, text: str, mood: str | None) -> Voice:
        if mood in self.voices:
            return self.voices[mood]
        return next((self.voices[m] for m in MOODS.get(mood_of(text), []) if m in self.voices), self.base)

    def save(self, text: str, path: Path, mood: str | None = None) -> bool:
        """合成一段话存成 wav 文件（先写临时文件再替换，合成失败不会留下半个文件）。"""
        wav = self.speak(text, mood)
        if not wav:
            return False
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(wav)
        tmp.replace(path)
        return True


def find_gpt_sovits(*roots: Path) -> Path | None:
    """在这些目录里找 GPT-SoVITS 整合包（含 api_v2.py 的 GPT-SoVITS* 目录）。"""
    for root in roots:
        if (root / "api_v2.py").exists():
            return root
        for d in (sorted(root.glob("GPT-SoVITS*"), reverse=True) if root.is_dir() else []):
            if (d / "api_v2.py").exists():
                return d
    return None


def start_service(gsv_dir: Path, port: int = 9880):
    """启动 GPT-SoVITS 的 api_v2.py，返回 Popen。

    不开新窗口，跟网页共用一个控制台：关掉黑窗口两个进程一起退出。本进程正常退出时也会 terminate 它。
    """
    import atexit
    import subprocess
    import sys

    python = gsv_dir / "runtime" / "python.exe"  # Windows 整合包自带的 Python
    exe = str(python) if python.exists() else sys.executable
    try:
        proc = subprocess.Popen([exe, "api_v2.py", "-a", "127.0.0.1", "-p", str(port)], cwd=gsv_dir,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    atexit.register(proc.terminate)
    return proc
