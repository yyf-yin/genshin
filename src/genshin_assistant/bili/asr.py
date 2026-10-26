"""没有字幕的视频：下载音轨，用本机的 faster-whisper 转写成带时间戳的字幕。

可选功能，默认关闭（sources.yaml 的 asr）。需要 pip install -e ".[asr]"；faster-whisper 自带 PyAV 解码音频，
不需要单独装 ffmpeg。第一次运行会下载语音模型。
"""

from __future__ import annotations

from pathlib import Path

from .api import SubtitleLine, VideoInfo
from .client import BiliClient, BiliError

DEFAULT_MODEL = "small"  # tiny / base / small / medium / large-v3：越大越准越慢


def download_audio(client: BiliClient, info: VideoInfo, out: Path) -> Path:
    """取 dash 流里码率最低的音轨（几 MB），不下载画面。"""
    d = client.get_json("/x/player/wbi/playurl", {"bvid": info.bvid, "cid": info.cid, "fnval": 16, "fourk": 0},
                        wbi=True, referer=f"https://www.bilibili.com/video/{info.bvid}/")
    audios = (d.get("dash") or {}).get("audio") or []
    if not audios:
        raise BiliError(f"{info.bvid} 没拿到音轨")
    a = min(audios, key=lambda x: int(x.get("bandwidth") or 0))
    url = a.get("baseUrl") or a.get("base_url")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(client.fetch(url, referer=f"https://www.bilibili.com/video/{info.bvid}/", cookie=False))
    return out


def transcribe(audio: Path, hint: str = "", model_size: str | None = None) -> list[SubtitleLine]:
    """hint 放游戏名词，能明显减少角色名、武器名的识别错误。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise BiliError('语音识别需要 faster-whisper：pip install -e ".[asr]"') from None
    model = WhisperModel(model_size or DEFAULT_MODEL, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(audio), language="zh", vad_filter=True,
                                   initial_prompt=("以下是原神攻略视频，简体中文。" + hint)[:200])
    return [SubtitleLine(round(s.start, 2), round(s.end, 2), s.text.strip()) for s in segments if s.text.strip()]
