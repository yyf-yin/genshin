"""抽帧拼图：配装面板只出现在画面上时，给人工审核（或以后接 OCR / 看图模型）用。

移植自 dsh-bili-whale（MIT）的 bili-watch.mjs 和 lib/ffmpeg.mjs。原插件按等间隔加画面变化点取帧；
这里改成按字幕里出现“圣遗物 / 面板 / 词条”等词的时间点取帧，因为我们只关心配装画面。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .api import SubtitleLine, VideoInfo
from .client import BiliClient, BiliError, RateLimited

BUILD_WORDS = ("圣遗物", "面板", "词条", "主词条", "副词条", "武器", "充能", "暴击", "毕业", "配装", "天赋")


def ffmpeg_path() -> str:
    """查找顺序：$FFMPEG → PATH。"""
    p = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not p:
        raise BiliError("找不到 ffmpeg：安装一个（winget install ffmpeg / brew install ffmpeg），"
                        "或用环境变量 FFMPEG 指定路径")
    return p


def build_moments(lines: list[SubtitleLine], n: int = 9, min_gap: float = 8.0) -> list[float]:
    """字幕里提到配装词的时间点，间隔至少 min_gap 秒，最多 n 个。画面通常比台词晚一点，往后挪 1.5 秒。"""
    out: list[float] = []
    for line in lines:
        if any(w in line.text for w in BUILD_WORDS) and (not out or line.start - out[-1] >= min_gap):
            out.append(line.start + 1.5)
    if len(out) > n:
        step = (len(out) - 1) / (n - 1)
        out = [out[round(i * step)] for i in range(n)]
    return out


CHUNK = 4 * 1024 * 1024  # 慢线路实测 0.12 MB/s，4 MB 一段也能在请求总超时内读完


def download_video(client: BiliClient, info: VideoInfo, out: Path, qn: int = 80, chunk: int = CHUNK,
                   tries: int = 6) -> Path:
    """取 mp4 直链分段下载。原插件实测：platform=pc + qn=80 拿到 720p 普通 mp4，不用碰 dash；qn=32 是 480p。

    整个文件一次读完遇到断线就前功尽弃（实测 260 MB 的视频下到 42 MB 断开），所以按 chunk 分段请求，
    每段失败就换备用线路重试；先写到 .part，中断后重跑会接着下。
    """
    d = client.get_json("/x/player/wbi/playurl", {"bvid": info.bvid, "cid": info.cid, "qn": qn, "fnval": 1,
                                                   "platform": "pc", "high_quality": 1}, wbi=True)
    durl = (d.get("durl") or [{}])[0]
    urls = [u for u in [durl.get("url"), *(durl.get("backup_url") or [])] if u]
    if not urls:
        raise BiliError(f"{info.bvid} 没拿到直链，可能要登录或只有 dash 流")
    size = int(durl.get("size") or 0)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    with part.open("ab") as f:
        while not size or done < size:
            end = done + chunk - 1 if not size else min(done + chunk, size) - 1
            for attempt in range(tries):
                try:
                    body = client.fetch(urls[attempt % len(urls)], referer="https://www.bilibili.com/",
                                        cookie=False, extra_headers={"range": f"bytes={done}-{end}"})
                    break
                except RateLimited:
                    raise
                except BiliError as e:
                    err = e
            else:
                raise BiliError(f"{info.bvid} 下载在 {done / 2**20:.0f} MB 处中断（{err}），重跑会接着下")
            want = end - done + 1
            if len(body) > want:  # 线路不认 range，回了整个文件
                if done:
                    raise BiliError(f"{info.bvid} 下载线路不支持分段续传")
                f.write(body)
                done = len(body)
                break
            f.write(body)
            done += len(body)
            if not size and len(body) < want:  # 不知道总大小时，拿到的比要的少就是到头了
                break
    part.replace(out)
    return out


def frame_sheet(video: Path, times: list[float], out: Path, cols: int = 3, tile_width: int = 640) -> Path:
    """在每个时间点各抽一帧，拼成网格图。"""
    if not times:
        raise BiliError("没有要抽帧的时间点")
    ff = ffmpeg_path()
    tmp = out.parent / f".frames-{out.stem}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for i, t in enumerate(times):
            subprocess.run([ff, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                            "-frames:v", "1", "-q:v", "2", str(tmp / f"f{i:02d}.jpg")], check=True)
        rows = -(-len(times) // cols)
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(tmp / "f%02d.jpg"), "-vf",
                        f"scale={tile_width}:-1,tile={cols}x{rows}", "-frames:v", "1", str(out)], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out
