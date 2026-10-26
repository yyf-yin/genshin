"""硬字幕（剪进画面里的字）：下载视频，每秒抽一帧只截字幕区域，OCR 后合并成带时间戳的字幕行。

可选功能，默认关闭（sources.yaml 的 ocr）。需要 pip install -e ".[ocr]"；用 OpenCV 解码视频，不用另装 ffmpeg。
比语音识别准：硬字幕是 UP 主自己校对过的文字。
"""

from __future__ import annotations

import shutil
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from .api import SubtitleLine
from .client import BiliError
from .frames import ffmpeg_path

DEFAULT_CROP = (0.75, 0.98)  # 字幕条在画面高度的哪一段（从上往下的比例）


def _engine():
    """新版包名 rapidocr，旧版 rapidocr_onnxruntime，两种都认。"""
    try:
        from rapidocr import RapidOCR
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            raise BiliError('读硬字幕需要 OCR：pip install -e ".[ocr]"') from None
    return RapidOCR()


def _texts(result, min_score: float) -> list[str]:
    """兼容两代返回格式：新版是带 txts / scores 的对象，旧版是 ([box, text, score], ...) 元组。"""
    if hasattr(result, "txts"):
        return [t for t, s in zip(result.txts or (), result.scores or ()) if s >= min_score]
    rows = result[0] if isinstance(result, tuple) else result
    return [r[1] for r in rows or [] if r[2] >= min_score]


def extract_frames(video: Path, out_dir: Path, every: float = 1.0, crop: tuple[float, float] = DEFAULT_CROP) -> list[Path]:
    """每 every 秒截一张字幕条。优先用 OpenCV（rapidocr 已经带上它，自带解码器，不用另装 ffmpeg）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    top, bottom = crop
    try:
        import cv2
    except ImportError:
        subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(video), "-vf",
                        f"fps=1/{every},crop=iw:ih*{bottom - top:.3f}:0:ih*{top:.3f}", "-q:v", "3",
                        str(out_dir / "%05d.jpg")], check=True)
        return sorted(out_dir.glob("*.jpg"))
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise BiliError(f"OpenCV 打不开视频：{video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(fps * every))
    out, i = [], 0
    while True:
        ok = cap.grab()  # 只解码要用的帧，跳过的帧不转成图片
        if not ok:
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                h = frame.shape[0]
                path = out_dir / f"{len(out) + 1:05d}.jpg"
                cv2.imwrite(str(path), frame[int(h * top):int(h * bottom)])
                out.append(path)
        i += 1
    cap.release()
    return out


def merge_lines(samples: list[tuple[float, str]], every: float = 1.0, similar: float = 0.8) -> list[SubtitleLine]:
    """相邻帧文字相同或很像（OCR 抖动）的合成一行，取出现次数最多的写法。"""
    lines: list[SubtitleLine] = []
    variants: list[str] = []
    for t, text in samples:
        if not text:
            continue
        last = lines[-1] if lines else None
        if last and t - last.end <= every * 1.5 and SequenceMatcher(None, last.text, text).ratio() >= similar:
            last.end = t + every
            variants.append(text)
            last.text = max(set(variants), key=variants.count)
            continue
        variants = [text]
        lines.append(SubtitleLine(t, t + every, text))
    return lines


def hardsub_lines(video: Path, work_dir: Path, every: float = 1.0, crop: tuple[float, float] = DEFAULT_CROP,
                  min_score: float = 0.6, keep_frames: bool = False) -> list[SubtitleLine]:
    engine = _engine()
    frames_dir = work_dir / f".ocr-{video.stem}"
    try:
        frames = extract_frames(video, frames_dir, every, crop)
        samples = [(i * every, "".join(_texts(engine(str(f)), min_score)).strip()) for i, f in enumerate(frames)]
    finally:
        if not keep_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)
    return merge_lines(samples, every)
