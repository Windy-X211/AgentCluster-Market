# coding=utf-8
"""中英混排文本分段：给 TTS 的句级流水线提供「可合成的短句」。

P0 规则（2026-11-14 重写，改掉"按字数硬切中文"的老毛病）：

1. **先按句末标点切**（。！？…换行），得到句子；
2. 句子不超过 max_chars → 整句一段（合成一次，语流最完整）；
3. 超过 max_chars 且里面有逗号/顿号/分号 → **只在标点处切**，再把条款贪心拼回 max_chars 以内。
   逗号本来就是停顿，切在这里不会改变词义，也能把首声延迟压回 0.3~0.5s；
4. 超过 max_chars 但**一个标点都没有**：UNPUNCT_MAX 以内宁可整句一次合成
   （多等 0.2~0.4s，也不把词砍断）；确实超长才退化到硬切，优先切在逗号/空格后，最后才按字数。
5. 任何情况下都不切开一个英文单词或数字。
"""

from __future__ import annotations

import re
from typing import List

HARD_PUNCT = "。！？!?…\n"
SOFT_PUNCT = "，,；;：:、"

# 没有任何标点的长串：不超过这个长度就整句合成，换取语义完整
UNPUNCT_MAX = 48

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """压缩空白，去掉零宽字符。保留标点原样。"""
    s = str(text or "").replace("\u200b", "").replace("\ufeff", "")
    s = _WS.sub(" ", s).strip()
    return s


def _split_clauses(text: str) -> List[str]:
    """只按句末标点切（标点跟随前一个语块）。逗号/顿号/分号故意不切。"""
    out: List[str] = []
    buf: List[str] = []
    for ch in text:
        buf.append(ch)
        if ch in HARD_PUNCT:
            piece = "".join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _split_soft(text: str) -> List[str]:
    """按逗号/顿号/分号/冒号切（标点跟随前一个语块）。用于超长句子内部降级。"""
    out: List[str] = []
    buf: List[str] = []
    for ch in text:
        buf.append(ch)
        if ch in SOFT_PUNCT:
            piece = "".join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _pack(clauses: List[str], max_chars: int) -> List[str]:
    """把条款贪心拼回 max_chars 以内；拼不下就另起一段。绝不切开单个条款。"""
    out: List[str] = []
    cur = ""
    for c in clauses:
        if not cur:
            cur = c
        elif len(cur) + len(c) <= max_chars:
            cur += c
        else:
            out.append(cur)
            cur = c
    if cur:
        out.append(cur)
    return out


def _safe_cut(text: str, limit: int) -> int:
    """在 limit 附近找一个尽量不破坏语义的切点。

    优先级：逗号/顿号之后 > 空格 > 英文单词边界 > 硬切。
    """
    if len(text) <= limit:
        return len(text)
    window = text[:limit]
    floor = max(1, limit // 2)
    for i in range(len(window) - 1, floor - 1, -1):
        if window[i] in SOFT_PUNCT:
            return i + 1
    sp = window.rfind(" ")
    if sp >= max(0, limit - 12):
        return sp + 1
    i = len(window)
    while i > 0 and window[i - 1].isascii() and (window[i - 1].isalnum()):
        i -= 1
    if i >= max(0, limit - 12) and i > 0:
        return i
    return len(window)


def _hard_split(text: str, max_chars: int) -> List[str]:
    """超长且无标点时的兜底切分（尽量落在标点/空格）。"""
    pieces: List[str] = []
    rest = text
    while len(rest) > max_chars:
        cut = _safe_cut(rest, max_chars)
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest.strip())
    return [p for p in pieces if p]


def _merge_short(pieces: List[str], min_chars: int, max_chars: int) -> List[str]:
    """把过短的段尽量并到相邻段；只并同一句内的（不以句末标点结尾的）。"""
    out: List[str] = []
    for p in pieces:
        if (out and len(p) < min_chars and not out[-1].rstrip()[-1:] in HARD_PUNCT
                and len(out[-1]) + len(p) <= max_chars):
            out[-1] = out[-1] + p
        elif out and len(out[-1]) < min_chars and len(out[-1]) + len(p) <= max_chars:
            out[-1] = out[-1] + p
        else:
            out.append(p)
    return out


def split_segments(text: str, min_chars: int = 6, max_chars: int = 24) -> List[str]:
    """把一段话切成适合逐段合成的短句（只在标点处切，不拆词）。"""
    text = normalize(text)
    if not text:
        return []
    if min_chars < 2:
        min_chars = 2
    if max_chars < min_chars:
        max_chars = min_chars

    pieces: List[str] = []
    for sent in _split_clauses(text):
        if len(sent) <= max_chars:
            pieces.append(sent)
            continue
        clauses = _split_soft(sent)
        if len(clauses) > 1:
            pieces.extend(_pack(clauses, max_chars))
            continue
        # 整句没有任何逗号：短到 UNPUNCT_MAX 就整句合成（宁可慢一点，语义优先）
        if len(sent) <= UNPUNCT_MAX:
            pieces.append(sent)
        else:
            pieces.extend(_hard_split(sent, max_chars))

    pieces = _merge_short(pieces, min_chars, max_chars)
    return [s.strip() for s in pieces if s.strip()]

