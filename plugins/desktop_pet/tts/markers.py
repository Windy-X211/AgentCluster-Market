# coding=utf-8
"""台词里的内置动画标记。

用法：智能体在 speak 的 text 里写 `[[nod]]`（也认中文 `[[点头]]`），
脚本会把标记**从文本里剥掉**（不能被念出来），并记住它后面紧跟的几个字；
等播放时钟走到那几个字时，再触发对应的内置动画。

为什么不按"全局字符下标"定位：
  * 本地分段在 daemon 里做，且会剥掉停顿符号（`~`/`|`）、重排段落；
  * 在线分段又走另一条路径。
  全局下标在两条链路上都不可靠。用"后面那几个字"当锚点，
  在每段到达时做子串查找，符号被剥、句子被切都不影响。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# 内置动画预设（与 renderer.html 的 BUILTIN_ANIM / pet_window.BUILTIN_ANIM_LABEL 一致）
ANIM_ALIAS: Dict[str, str] = {
    "nod": "nod", "点头": "nod",
    "shake": "shake", "摇头": "shake",
    "tilt": "tilt", "歪头": "tilt",
    "sway": "sway", "摇摆": "sway",
    "look_around": "look_around", "lookaround": "look_around", "张望": "look_around",
    "cheer": "cheer", "欢呼": "cheer",
}

# [[nod]] / [[点头]]，大小写不敏感；中间允许空格
_MARK = re.compile(r"\[\[\s*([A-Za-z_]{2,20}|[^\s\]]{1,8})\s*\]\]")
# 锚点取标记后面最近的内容字符，跳过空白、停顿符号和标点
# （标点常在分段时被剥掉/留在前一段，当锚点会永远匹配不上）
_SKIP = set(" \t\r\n~|｜。，、；：！？!?…,.;:·—「」《》\"'“”‘’()（）")
# 定位用的"内容字符"口径：只排除空白和连读/停顿符号 —— 分段器会剥掉 `~`/`|`，
# 标点则基本保留，所以这个口径在"标记原文"和"分段后的段文本"两边能对齐，
# 用它算出的字符位置可以跨段沿用（锚点子串被切断也不会错位）。
_POS_SKIP = set(" \t\r\n~|｜\ufeff")


def content_index(text: str, limit: int = -1) -> int:
    """text 里有多少个"内容字符"（跳过空白与停顿符号）。

    limit>=0 时只扫前 limit 个字符，用于把内容位数换算回段内的字符下标。
    """
    n = 0
    for i, ch in enumerate(str(text or "")):
        if limit >= 0 and i >= limit:
            break
        if ch not in _POS_SKIP:
            n += 1
    return n


def char_at_content(text: str, k: int) -> int:
    """第 k 个内容字符在 text 里的下标（k 越界就夹到首/尾）。"""
    txt = str(text or "")
    if not txt:
        return 0
    seen = 0
    for i, ch in enumerate(txt):
        if ch in _POS_SKIP:
            continue
        if seen == k:
            return i
        seen += 1
    return max(0, len(txt) - 1)


def parse_anim_markers(text: str) -> Tuple[str, List[Dict[str, object]]]:
    """剥掉动画标记，返回 (干净文本, 标记列表)。

    标记列表每项：``{"name": 内置动画名, "anchor": 锚点文本(1~3字, 可为空), "pos": 内容位}``。
    ``pos`` 是标记在干净文本里的"内容字符位"（跳过空白/停顿符号），
    由 TTS 服务按段落累加内容位数来定位，不依赖锚点子串是否被切断。
    """
    src = str(text or "")
    out: List[str] = []
    marks: List[Dict[str, object]] = []
    pos = 0
    for m in _MARK.finditer(src):
        out.append(src[pos:m.start()])
        raw = (m.group(1) or "").strip()
        name = ANIM_ALIAS.get(raw.lower(), ANIM_ALIAS.get(raw))
        if name:
            # 锚点：从标记位置往后找 1~3 个"内容字符"（跳过空白/符号/标点，
            # 因为标点往往被分段吃掉，带着它当锚点会永远匹配不上）
            k = m.end()
            n = 0
            chars = []
            while k < len(src) and n < 3:
                ch = src[k]
                if ch in _SKIP:
                    k += 1
                    continue
                chars.append(ch)
                n += 1
                k += 1
            anchor = "".join(chars).strip()
            # 标记在"干净文本"里的内容位：已拼出的干净文本的内容字符数
            # （标记本身已被剥掉，不参与计数）
            at_pos = content_index("".join(out))
            marks.append({"name": name, "anchor": anchor, "pos": at_pos})
        else:
            # 不认识的标记：也别念出来，记一笔错误交给 status
            marks.append({"name": "", "anchor": "", "bad": raw})
        pos = m.end()
    out.append(src[pos:])
    clean = "".join(out)
    # 剥标注后可能留下多余空格，但不要动标点（分段依赖标点），
    # 更不能吃换行：歌词的换行就是逐句边界。
    clean = re.sub(r"[ \t\x0b\x0c]{2,}", " ", clean).strip()
    return clean, marks


def bad_marks(marks: List[Dict[str, object]]) -> List[str]:
    return [str(m.get("bad")) for m in marks if m.get("bad")]
