# coding=utf-8
"""段内逐字时间轴 + SRT 生成。

时间轴的来源分两种：
  * 段边界：由真实 PCM 样本数算出，误差为 0；
  * 段内逐字：没有声学对齐时按「音节权重」分配（中文 1 字 1 权重，标点折算停顿）。
在线引擎若自带 word boundary（Azure / 火山），调用方会传 native=True 的原始时间，
本模块只做裁剪/兜底。
"""

from __future__ import annotations

from typing import Dict, List, Sequence

# 标点带来的停顿（折算成「字」的权重）
PAUSE_WEIGHT = {
    "，": 0.35, ",": 0.35,
    "、": 0.25,
    "；": 0.40, ";": 0.40,
    "：": 0.30, ":": 0.30,
    "。": 0.70, ".": 0.55,
    "！": 0.55, "!": 0.55,
    "？": 0.55, "?": 0.55,
    "…": 0.60,
    "—": 0.30,
}

_OPEN_QUOTES = "「『（(《【“'\""
_CLOSE_QUOTES = "」』）)》】”'\""

PUNCT_ALL = set(PAUSE_WEIGHT) | set(_OPEN_QUOTES) | set(_CLOSE_QUOTES)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0x3040 <= o <= 0x30FF  # 日文假名
        or 0xF900 <= o <= 0xFAFF
    )


def char_weights(text: str) -> List[float]:
    """每个字符一个权重；标点自身为 0，把停顿加给前一个字。"""
    weights: List[float] = []
    for ch in text:
        if ch in PAUSE_WEIGHT:
            w = 0.0
            if weights:
                # 把停顿记到前一个「非标点」字上，让高亮在标点上停住
                idx = len(weights) - 1
                while idx > 0 and weights[idx] <= 0.0:
                    idx -= 1
                if weights[idx] > 0.0:
                    weights[idx] += PAUSE_WEIGHT[ch]
                else:
                    weights[idx] = PAUSE_WEIGHT[ch]
            else:
                w = 0.0
            weights.append(w)
        elif ch in PUNCT_ALL:
            weights.append(0.0)
        elif ch.isspace():
            weights.append(0.15)
        elif _is_cjk(ch):
            weights.append(1.0)
        elif ch.isascii() and ch.isalnum():
            weights.append(0.58)
        else:
            weights.append(0.45)

    # 纯标点/空串兜底：平均分配
    if sum(weights) <= 0 and weights:
        weights = [1.0] * len(weights)
    return weights


def char_marks(text: str, dur_ms: int) -> List[Dict[str, object]]:
    """把一段文本铺到 dur_ms 上，返回 [{ch,t0,t1}]，单位 ms（段内相对）。"""
    text = str(text or "")
    dur = max(1, int(dur_ms))
    weights = char_weights(text)
    total = float(sum(weights)) or 1.0

    marks: List[Dict[str, object]] = []
    acc = 0.0
    for ch, w in zip(text, weights):
        t0 = int(round(dur * acc / total))
        acc += w
        t1 = int(round(dur * acc / total))
        if t1 <= t0:
            t1 = t0 + 1
        marks.append({"ch": ch, "t0": t0, "t1": min(t1, dur)})

    # 收尾对齐：最后一个非标点字的 t1 必须落到段尾，避免字幕提前消失
    if marks:
        last = max(0, len(marks) - 1)
        while last > 0 and marks[last]["t1"] <= marks[last]["t0"]:
            last -= 1
        marks[last]["t1"] = dur
    return marks


def build_segments(texts: Sequence[str], durations_ms: Sequence[int],
                   start_ms: Sequence[int] = None) -> List[Dict[str, object]]:
    """把多段文本 + 真实时长组装成字幕时间轴。

    durations_ms / start_ms 都来自真实 PCM 样本数，因此段边界是精确的。
    """
    out: List[Dict[str, object]] = []
    cursor = 0
    for i, (text, dur) in enumerate(zip(texts, durations_ms)):
        start = int(start_ms[i]) if start_ms is not None else cursor
        head = {
            "i": i,
            "text": text,
            "start_ms": start,
            "end_ms": start + int(dur),
            "dur_ms": int(dur),
            "aligned": "estimated",
            "chars": char_marks(text, int(dur)),
        }
        out.append(head)
        cursor = start + int(dur)
    return out


def srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(segments: Sequence[Dict[str, object]], max_chars: int = 42) -> str:
    """生成 SRT。段过长时按标点二次切分，时间按字数比例分。"""
    blocks: List[str] = []
    idx = 1
    for seg in segments:
        text = str(seg.get("text", ""))
        start = int(seg.get("start_ms", 0))
        dur = int(seg.get("dur_ms", 0))
        for line, t0, t1 in _split_for_srt(text, start, dur, max_chars):
            blocks.append(
                f"{idx}\n{srt_time(t0)} --> {srt_time(t1)}\n{line}\n"
            )
            idx += 1
    return "\n".join(blocks)


def _split_for_srt(text: str, start: int, dur: int, max_chars: int):
    """按标点把过长的一句拆成若干条字幕，时间按字数比例。"""
    if len(text) <= max_chars:
        yield text, start, start + dur
        return
    clauses = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "，,；;：:。！？!?…":
            clauses.append(buf)
            buf = ""
    if buf:
        clauses.append(buf)
    if len(clauses) <= 1:
        # 没有标点：按字数硬切
        n = (len(text) + max_chars - 1) // max_chars
        size = (len(text) + n - 1) // n
        clauses = [text[i:i + size] for i in range(0, len(text), size)]
    total_chars = max(1, sum(len(c) for c in clauses))
    t = start
    for c in clauses:
        d = max(1, int(round(dur * len(c) / total_chars)))
        yield c, t, min(start + dur, t + d)
        t += d
