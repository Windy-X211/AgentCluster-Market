# coding=utf-8
"""在线 TTS：Edge-TTS（免 key，原生 WordBoundary 时间戳）。

注意：Edge 的 WSS 接口会校验本机时间与微软服务器时间的偏差，
偏差过大（或网络受限）会直接 403。这里把错误原样抛出，由上层提示用户。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

import numpy as np


class EdgeTTSError(RuntimeError):
    pass


async def _collect(text: str, voice: str, rate: str, proxy: Optional[str]):
    import edge_tts

    comm = edge_tts.Communicate(
        text,
        voice,
        rate=rate or "+0%",
        boundary="WordBoundary",
        proxy=proxy or None,
    )
    first_audio_at = None
    t0 = time.monotonic()
    audio = bytearray()
    words: List[Dict[str, object]] = []
    try:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                if first_audio_at is None:
                    first_audio_at = (time.monotonic() - t0) * 1000.0
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({
                    "text": chunk.get("text", ""),
                    "t0": int(chunk.get("offset", 0) / 10000),      # 100ns → ms
                    "dur": int(chunk.get("duration", 0) / 10000),
                })
    except Exception as e:
        raise EdgeTTSError(_friendly(e)) from e
    return bytes(audio), words, first_audio_at


def _friendly(e: Exception) -> str:
    s = str(e)
    if "403" in s or "Forbidden" in s:
        return (
            "Edge-TTS 403：微软拒绝了握手。最常见原因是本机系统时间与微软服务器偏差过大，"
            "其次是网络/代理限制。请核对系统时间，或改用在线的火山/Azure。原始错误: " + s
        )
    return s


def _decode_mp3(mp3_path: str, sr: int = 48000) -> np.ndarray:
    wav_path = mp3_path + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3_path,
         "-ac", "1", "-ar", str(sr), "-f", "wav", wav_path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    import soundfile as sf
    data, _ = sf.read(wav_path, dtype="float32", always_2d=True)
    for p in (mp3_path, wav_path):
        try:
            os.remove(p)
        except Exception:
            pass
    return np.clip(data.mean(axis=1), -1.0, 1.0)


def synth_segment(text: str, voice: str, rate: str = "", sr: int = 48000,
                  timeout: float = 30.0) -> Tuple[np.ndarray, int, Optional[List[Dict[str, object]]], float]:
    """合成一段，返回 (int16 PCM@sr, sr, word_marks, ttfa_ms)。"""
    try:
        audio, words, ttfa = asyncio.run(asyncio.wait_for(_collect(text, voice, rate, None), timeout))
    except asyncio.TimeoutError as e:
        raise EdgeTTSError("Edge-TTS 超时（%.1fs）" % timeout) from e
    except EdgeTTSError:
        raise
    except Exception as e:
        raise EdgeTTSError(_friendly(e)) from e

    if not audio:
        raise EdgeTTSError("Edge-TTS 没有返回音频")

    fd, mp3 = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    with open(mp3, "wb") as f:
        f.write(audio)
    pcm = (_decode_mp3(mp3, sr) * 32767.0).astype(np.int16)
    return pcm, sr, words, float(ttfa or 0.0)


def map_words_to_chars(text: str, words: List[Dict[str, object]], dur_ms: int) -> List[Dict[str, object]]:
    """把 WordBoundary 映射到逐字时间轴；对不上的字按剩余时长均分。"""
    marks: List[Dict[str, object]] = [{"ch": ch, "t0": 0, "t1": 0} for ch in text]
    cursor = 0
    for w in words or []:
        wt = str(w.get("text", ""))
        if not wt:
            continue
        idx = text.find(wt, cursor)
        if idx < 0:
            continue
        t0 = int(w.get("t0", 0))
        t1 = t0 + int(w.get("dur", 0))
        for k, ch in enumerate(wt):
            share0 = t0 + (t1 - t0) * k // max(1, len(wt))
            share1 = t0 + (t1 - t0) * (k + 1) // max(1, len(wt))
            marks[idx + k] = {"ch": ch, "t0": share0, "t1": share1}
        cursor = idx + len(wt)

    # 没有时间戳的字符（标点等）用前后锚点补齐
    known = [i for i, m in enumerate(marks) if m["t1"] > 0]
    if not known:
        step = dur_ms / max(1, len(marks))
        for i, m in enumerate(marks):
            m["t0"] = int(round(i * step))
            m["t1"] = int(round((i + 1) * step))
        if marks:
            marks[-1]["t1"] = dur_ms
        return marks

    prev_end = 0
    for i in range(len(marks)):
        if marks[i]["t1"] > 0:
            prev_end = marks[i]["t1"]
            continue
        nxt = next((j for j in range(i + 1, len(marks)) if marks[j]["t1"] > 0), None)
        if nxt is None:
            marks[i]["t0"] = prev_end
            marks[i]["t1"] = dur_ms
        else:
            gap0, gap1 = prev_end, marks[nxt]["t0"]
            marks[i]["t0"] = gap0
            marks[i]["t1"] = max(gap0 + 1, gap1)
    if marks:
        marks[-1]["t1"] = dur_ms
    for m in marks:
        if m["t1"] <= m["t0"]:
            m["t1"] = m["t0"] + 1
    return marks
