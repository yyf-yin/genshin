"""硬字幕（剪进画面里的字）：下载视频，每秒抽一帧只截字幕区域，OCR 后合并成带时间戳的字幕行。

可选功能，默认关闭（sources.yaml 的 ocr）。需要 pip install -e ".[ocr]"；用 OpenCV 解码视频，不用另装 ffmpeg。
比语音识别准：硬字幕是 UP 主自己校对过的文字。
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from .api import SubtitleLine
from .client import BiliError
from .frames import ffmpeg_path

DEFAULT_CROP = (0.84, 0.93, 0.15, 0.85)  # 字幕条的位置：上、下、左、右（占画面的比例，0 是顶 / 左）
# 75%-98% 实测会框进章节进度条、等级血量和两侧按钮；字幕一般在 86%-93%，左右各留 15% 挡掉按钮和文字面板

Crop = tuple[float, ...]
_WORDS = re.compile(r"[\u4e00-\u9fffA-Za-z]")


def _box(crop: Crop) -> tuple[float, float, float, float]:
    """兼容只写上下两个数的旧配置。"""
    top, bottom, *lr = crop
    left, right = lr if len(lr) == 2 else (0.0, 1.0)
    return top, bottom, left, right


# 一个只做 Identity 的最小 ONNX 模型，用来试探显卡能不能真的跑起来
_PROBE = base64.b64decode("CAc6NwoQCgF4EgF5IghJZGVudGl0eRIBcFoPCgF4EgoKCAgBEgQKAggBYg8KAXkSCgoICAESBAoCCAFCBAoAEA0=")
gpu_note = ""  # 显卡用不上时的原因，给日志看


@lru_cache(maxsize=1)
def gpu_provider() -> str | None:
    """装了 onnxruntime-gpu（CUDA）或 onnxruntime-directml（Windows 任意显卡）就用显卡跑，否则 None（CPU）。

    只看可用列表不够：驱动太旧时 CUDA 照样列出来，建会话时才失败并悄悄退回 CPU，所以实际建一次会话确认。
    """
    global gpu_note
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError:
        return None
    if hasattr(ort, "preload_dlls"):  # pip 装的 CUDA / cuDNN 库要先加载，不然会悄悄退回 CPU
        try:
            ort.preload_dlls()
        except Exception:  # noqa: BLE001
            pass
    have = ort.get_available_providers()
    for p in ("CUDAExecutionProvider", "DmlExecutionProvider"):
        if p not in have:
            continue
        try:
            sess = ort.InferenceSession(_PROBE, providers=[p, "CPUExecutionProvider"])
            sess.run(None, {"x": np.ones(1, np.float32)})
            if sess.get_providers()[0] == p:
                return p
            gpu_note = f"{p} 初始化失败，退回了 CPU"
        except Exception as e:  # noqa: BLE001
            gpu_note = f"{p} 初始化失败：{e}"
    if gpu_note.startswith("CUDA"):
        gpu_note += "（多半是显卡驱动比 onnxruntime-gpu 要的 CUDA 旧：升级驱动，或改装 onnxruntime-directml）"
    return None


def _engine():
    """新版包名 rapidocr，旧版 rapidocr_onnxruntime，两种都认。"""
    import logging
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)  # 没字幕的帧每张都报一条“检测结果为空”，会刷屏
    gpu = gpu_provider()
    try:
        from rapidocr import RapidOCR
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            raise BiliError('读硬字幕需要 OCR：pip install -e ".[ocr]"') from None
        if gpu == "CUDAExecutionProvider":
            return RapidOCR(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
        return RapidOCR()
    if gpu == "CUDAExecutionProvider":
        return RapidOCR(params={"EngineConfig.onnxruntime.use_cuda": True})
    if gpu == "DmlExecutionProvider":
        return RapidOCR(params={"EngineConfig.onnxruntime.use_dml": True})
    return RapidOCR()


def _texts(result, min_score: float) -> list[str]:
    """兼容两代返回格式：新版是带 txts / scores 的对象，旧版是 ([box, text, score], ...) 元组。"""
    if hasattr(result, "txts"):
        return [t for t, s in zip(result.txts or (), result.scores or ()) if s >= min_score]
    rows = result[0] if isinstance(result, tuple) else result
    return [r[1] for r in rows or [] if r[2] >= min_score]


def extract_frames(video: Path, out_dir: Path, every: float = 1.0, crop: Crop = DEFAULT_CROP) -> list[Path]:
    """每 every 秒截一张字幕条。优先用 OpenCV（rapidocr 已经带上它，自带解码器，不用另装 ffmpeg）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    top, bottom, left, right = _box(crop)
    try:
        import cv2
    except ImportError:
        subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(video), "-vf",
                        f"fps=1/{every},crop=iw*{right - left:.3f}:ih*{bottom - top:.3f}:iw*{left:.3f}:ih*{top:.3f}",
                        "-q:v", "3", str(out_dir / "%05d.jpg")], check=True)
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
                h, w = frame.shape[:2]
                path = out_dir / f"{len(out) + 1:05d}.jpg"
                cv2.imwrite(str(path), frame[int(h * top):int(h * bottom), int(w * left):int(w * right)])
                out.append(path)
        i += 1
    cap.release()
    return out


def _same_frame():
    """返回 same(a, b)：两张字幕条几乎没变（字幕一般停留 2-3 秒）就不用再识别一遍。没装 OpenCV 就每张都识别。"""
    try:
        import cv2
    except ImportError:
        return lambda a, b: False
    cache: dict[Path, object] = {}

    def gray(p: Path):
        if p not in cache:
            if len(cache) > 4:
                cache.clear()
            cache[p] = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        return cache[p]

    def same(a: Path, b: Path) -> bool:
        x, y = gray(a), gray(b)
        return x is not None and y is not None and x.shape == y.shape and float(cv2.absdiff(x, y).mean()) < 2.0
    return same


def drop_static(frames_texts: list[list[str]], share: float = 0.3, min_frames: int = 10) -> list[str]:
    """去掉大多数帧里都有的固定文字（章节进度条“技能解析 装备搭配”、水印等），每帧剩下的拼成一句。"""
    counts: dict[str, int] = {}
    for texts in frames_texts:
        for t in set(texts):
            counts[t] = counts.get(t, 0) + 1
    limit = max(min_frames, share * len(frames_texts))
    static = {t for t, n in counts.items() if n >= limit}
    return ["".join(t for t in texts if t not in static).strip() for texts in frames_texts]


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


def hardsub_lines(video: Path, work_dir: Path, every: float = 1.0, crop: Crop = DEFAULT_CROP,
                  min_score: float = 0.6, keep_frames: bool = False, log=None) -> list[SubtitleLine]:
    engine = _engine()
    if log:
        log(f"    OCR 用 {gpu_provider() or 'CPU'}{'；' + gpu_note if gpu_note else ''}")
    frames_dir = work_dir / f".ocr-{video.stem}"
    same = _same_frame()
    try:
        frames = extract_frames(video, frames_dir, every, crop)
        per_frame: list[list[str]] = []
        last_read = None
        for i, f in enumerate(frames):
            if last_read is not None and same(last_read, f):
                per_frame.append(per_frame[-1])  # 画面没变，沿用上一张的识别结果
                continue
            per_frame.append([t.strip() for t in _texts(engine(str(f)), min_score) if t.strip()])
            last_read = f
            if log and i and i % 200 == 0:
                log(f"    OCR {i}/{len(frames)}")
    finally:
        if not keep_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)
    texts = [t if _WORDS.search(t) else "" for t in drop_static(per_frame)]  # 只剩数字、百分比的是界面读数
    return merge_lines([(i * every, t) for i, t in enumerate(texts)], every)
