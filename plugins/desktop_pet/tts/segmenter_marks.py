# coding=utf-8
"""符号解析 + 分段：把「停顿/连读」符号翻译成精确的静音计划。

符号约定（可配置，默认值见 GAP 表）：
  ~     连读：删掉符号、两侧合成一段，不切、不插静音
  |     短停  pause_short_ms（默认 150ms）
  ||    中停  pause_mid_ms（默认 300ms，逗号级）
  |||   长停  pause_long_ms（默认 500ms，句末级）

除了符号，普通标点也会补边界静音（punct_gap）：
实测 Piper 段内标点几乎不产生停顿、而每段尾会被剪掉静音，
不补的话段与段是「0 静音硬接」，听起来就是一句话被切成几句。
"""

from __future__ import annotations

import re
from typing import Dict, List

HARD_PUNCT = "。！？!?…\n"
SOFT_PUNCT = "，,；;：:、"

UNPUNCT_MAX = 48

_WS = re.compile(r"\s+")

# 无标点硬切处的连接缝隙：比逗号短，避免听成句尾
NOCUT_GAP_MS = 90


def normalize(text: str) -> str:
    s = str(text or "").replace("\u200b", "").replace("\ufeff", "")
    s = _WS.sub(" ", s).strip()
    return s


def _split_clauses(text: str) -> List[str]:
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
        if len(sent) <= UNPUNCT_MAX:
            pieces.append(sent)
        else:
            pieces.extend(_hard_split(sent, max_chars))

    pieces = _merge_short(pieces, min_chars, max_chars)
    return [s.strip() for s in pieces if s.strip()]


# ---------------------------------------------------------------- 符号解析
def parse_marks(text: str, short_ms: int = 150, mid_ms: int = 300,
                long_ms: int = 500) -> List[Dict[str, object]]:
    """把文本按停顿符号切成块：返回 [{"text": 块文本, "gap_ms": 块后静音}]。"""
    s = normalize(text).replace("｜", "|")
    blocks: List[Dict[str, object]] = []
    buf: List[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "~":
            i += 1              # 连读：删掉符号，两侧自然拼在一起
            continue
        if ch == "|":
            j = i
            while j < len(s) and s[j] == "|":
                j += 1
            n = j - i
            gap = short_ms if n == 1 else (mid_ms if n == 2 else long_ms)
            piece = "".join(buf).strip()
            blocks.append({"text": piece, "gap_ms": int(gap)})
            buf = []
            i = j
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        blocks.append({"text": tail, "gap_ms": 0})

    # 空块（连续标记/首标记）把 gap 并到前一块
    out: List[Dict[str, object]] = []
    for b in blocks:
        t = str(b["text"])
        g = int(b["gap_ms"])
        if not t:
            if out:
                out[-1]["gap_ms"] = max(int(out[-1]["gap_ms"]), g)
            continue
        out.append({"text": t, "gap_ms": g})
    if not out:
        return []
    out[-1]["gap_ms"] = 0       # 句末不需要额外尾巴，由 tail_ms 管
    return out


def _need_space(left: str, right: str) -> bool:
    """拼接两块时要补空格吗？只在英文/数字相邻时补，中文直接连起来。"""
    a = (left[-1:] or "")
    b = (right[:1] or "")
    return bool(a.isascii() and a.isalnum() and b.isascii() and b.isalnum())


def split_head_chars(text: str, limit: int, min_tail: int = 5) -> List[str]:
    """把开头 limit 个字切下来（找不到标点就硬切），用于"首段压短、尽快出声"。

    split_segments 对 48 字以内的无标点长句是不硬切的（避免把词劈开），
    但唱歌要的是首声延迟，这里允许在 limit 处切一刀。
    min_tail：尾巴不足这么多个字时，往前退一点再切（宁可首段短两三个字，
    也不要在"帮你|解渴"这种地方断句）。
    """
    s = normalize(text)
    if limit <= 0 or len(s) <= limit:
        return [s] if s else []
    cut = _safe_cut(s, limit)
    if len(s) - cut < min_tail:
        cut = len(s) - min_tail
    if cut < 6:
        return [s]
    head = s[:cut].strip()
    tail = s[cut:].lstrip()
    return [p for p in (head, tail) if p]


def strip_pause_marks(text: str) -> str:
    """把停顿/连读符号（~ | || |||）从文本里删掉，返回"能直接给人看/发给在线 TTS"的干净文本。

    在线后端（Edge / MiMo）自己会按标点停顿，符号只能被当成怪字符：
    轻则字幕里多出一堆 `|`，重则被念出来或让唱歌的咬字乱掉。
    """
    blocks = parse_marks(text)
    if not blocks:
        return normalize(text)
    out = ""
    for b in blocks:
        t = str(b.get("text") or "").strip()
        if not t:
            continue
        if out and _need_space(out, t):
            out += " "
        out += t
    return out


_SENT_END = "。！？!?；;…\n"
_CJK = re.compile(r"[\u3400-\u9fff]")


def _split_sent_punct(s: str) -> List[str]:
    """按句末标点切（标点留在句子里）。"""
    parts: List[str] = []
    buf = ""
    for ch in s:
        buf += ch
        if ch in _SENT_END:
            piece = buf.strip()
            if piece:
                parts.append(piece)
            buf = ""
    tail = buf.strip()
    if tail:
        parts.append(tail)
    return parts


def split_sentences(text: str) -> List[str]:
    """按"行 → 空白 → 句末标点"切成一句一句（标点留在句子里）。

    唱歌是整曲一次合成（一个 seg），如果不切，字幕会把整首歌词一次性摊出来、
    逐字高亮也只能按整段均分。这里只切字幕时间轴，音频不切，旋律照样连贯。

    注意换行和空格：歌词经常只靠换行/空格分行（没有句号），
    `normalize()` 会把换行抹成空格、导致整首变成"一句"——所以必须先按原文拆行。
    有多行时，**一行就是一句**（行内空格保留，不再拆，避免把一句歌词劈成两半）；
    只有整段被压成单行时，才用中文里的空白当分句（纯英文行保留空格，那是词间距）。
    """
    raw = str(text or "").replace("\r", "\n").replace("\u200b", "").replace("\ufeff", "")
    lines = [_WS.sub(" ", ln).strip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln]
    parts: List[str] = []
    single = len(lines) == 1
    for line in lines:
        if single and _CJK.search(line) and " " in line:
            for token in line.split(" "):
                token = token.strip()
                if token:
                    parts.extend(_split_sent_punct(token))
        else:
            parts.extend(_split_sent_punct(line))
    # 极短句（比如单个标点或 1~2 字）并到前一句，别让字幕闪一下
    merged: List[str] = []
    for p in parts:
        if merged and len(merged[-1]) < 4:
            merged[-1] += p
        else:
            merged.append(p)
    return merged



def punct_gap(text: str, short_ms: int = 150, mid_ms: int = 300) -> int:
    """按段尾标点决定段间静音：句末 > 逗号 > 无标点硬接。"""
    t = str(text or "").rstrip()
    if not t:
        return short_ms
    if t[-1] in HARD_PUNCT:
        return int(mid_ms)
    if t[-1] in SOFT_PUNCT:
        return int(short_ms)
    return NOCUT_GAP_MS


def split_segments_marked(text: str, min_chars: int = 6, max_chars: int = 24,
                          short_ms: int = 150, mid_ms: int = 300, long_ms: int = 500,
                          use_marks: bool = True) -> List[Dict[str, object]]:
    """先解析符号，再分段；每段带上「其后该留多少静音」。

    返回 [{"text": ..., "gap_after_ms": ...}]，service 直接按此在段尾补静音。
    """
    s = normalize(text)
    if not s:
        return []
    blocks = parse_marks(s, short_ms, mid_ms, long_ms) if use_marks \
        else [{"text": s, "gap_ms": 0}]
    if not blocks:
        blocks = [{"text": s, "gap_ms": 0}]
    out: List[Dict[str, object]] = []
    n_blocks = len(blocks)
    for bi, b in enumerate(blocks):
        segs = split_segments(str(b["text"]), min_chars, max_chars)
        for si, seg in enumerate(segs):
            if si < len(segs) - 1:
                gap = punct_gap(seg, short_ms, mid_ms)
            else:
                gap = int(b["gap_ms"]) if bi < n_blocks - 1 else 0
                if gap <= 0 and si == len(segs) - 1 and bi < n_blocks - 1:
                    gap = punct_gap(seg, short_ms, mid_ms)
            out.append({"text": seg, "gap_after_ms": int(gap)})
    if out:
        out[-1]["gap_after_ms"] = 0
    return out
