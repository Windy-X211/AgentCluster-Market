"""game_assistant 插件 —— 单机游戏存档读取与修改助手

定位（重要，写进所有返回值与文档）：
    ⚠️ 仅支持单机游戏。在联机游戏或多人游戏中使用（含云存档、共享存档）
    可能导致存档损坏、数据回档、账号封禁等后果，一切后果由使用者自行承担。

安全设计：
    1. 所有修改类操作（editJson/editText/hexReplace/restore）写盘前强制自动备份；
    2. 备份集中存放于 %LOCALAPPDATA%/AgentCluster/outputs/game_assistant/backups/，
       按存档路径哈希分目录，绝不污染游戏存档目录；
    3. 备份日志 backup_log.jsonl 记录每次备份（时间/来源/备份路径/备注/大小）；
    4. 大于 100MB 的存档拒绝处理；二进制 hex 替换强制等长；多处命中默认拒改。

能力（action 一览，均为 camelCase）：
    scanSaves    扫描常见存档目录（Documents/My Games、Saved Games、AppData 等）
    readSave     读取存档：文本返回内容预览；二进制返回 hexdump + 可读字符串
    analyzeJson  JSON 存档分析：顶层概览 / 按 keyPath 取值 / 全文搜索定位
    editJson     按 keyPath 修改 JSON（set/delete/append），自动备份
    editText     文本存档查找替换（字面或正则），自动备份
    hexReplace   二进制存档等长 hex 替换，自动备份，多处命中需显式确认
    backup       手动备份
    restore      从备份恢复（恢复前会对当前文件再做一次安全备份）
    listBackups  列出某存档的全部备份

调用签名：
    run(action="scanSaves", keyword="xxx", maxResults=40)
    run(action="readSave", filePath="D:/game/saves/a.sav", maxLines=120, maxBytes=65536)
    run(action="analyzeJson", filePath="...", keyPath="", search="", maxResults=50)
    run(action="editJson", filePath="...", keyPath="player.gold", op="set", value=99999)
    run(action="editText", filePath="...", find="gold = 100", replace="gold = 999",
        regex=False, maxCount=0)
    run(action="hexReplace", filePath="...", findHex="0000803F", replaceHex="0000C03F",
        replaceAll=False, offset=-1)
    run(action="backup", filePath="...", note="改金币前备份")
    run(action="restore", filePath="...", timestamp="20261108_153000")
    run(action="listBackups", filePath="...")

返回 {ok, ...}；失败返回 {ok: False, error}；修改类结果附带 modified=True 与
disclaimer（单机免责声明）。
"""
from __future__ import annotations

import hashlib
import json as _json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------------------
# 常量与工具（源码纪律：全程不出现反斜杠字符，换行等一律用 chr() 构造）
# ---------------------------------------------------------------------------
NL = chr(10)
DISCLAIMER = (
    "本插件仅支持单机游戏。在联机游戏或多人游戏中使用（含云存档、共享存档、"
    "房间存档等）可能导致存档损坏、数据回档、成绩失效、账号封禁等后果，"
    "一切后果由使用者自行承担。"
)

MAX_FILE_BYTES = 100 * 1024 * 1024      # 100MB 上限
SCAN_DEPTH = 3                          # 存档目录递归深度
SCAN_MAX_FILES = 60                     # 单次扫描最多列出的文件数
PREVIEW_LIMIT = 120                     # 文本预览默认行数
HEXDUMP_BYTES = 256                     # 二进制预览字节数
STRINGS_LIMIT = 30                      # 二进制可读字符串提取条数
SEARCH_NODE_CAP = 20000                 # JSON 搜索遍历节点上限
_EXCLUDE_DIR_WORDS = ("cache", "temp", "logs", "log", "crash", "shader",
                      "backup", "dumps", "__pycache__", ".git")
_SAVE_EXT = {".sav", ".save", ".sav2", ".dat", ".bin", ".json", ".xml",
             ".cfg", ".ini", ".rpgsave", ".vdf", ".savemg", ".bak", ".zip",
             ".gz", ".rkg", ".wld", ".plr", ".es3", ".dds2"}

_PLUGIN_DIR = Path(__file__).resolve().parent


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def _backup_root() -> Path:
    return _local_appdata() / "AgentCluster" / "outputs" / "game_assistant" / "backups"


def _user_dirs() -> dict[str, Path]:
    """常见存档根目录（测试时通过 monkeypatch 本函数替换为临时目录）。"""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return {
        "myGames": home / "Documents" / "My Games",
        "savedGames": home / "Saved Games",
        "documents": home / "Documents",
        "paradoxDocs": home / "Documents" / "Paradox Interactive",
        "localAppData": _local_appdata(),
        "roamingAppData": Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming")),
    }


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _to_path(filePath: Any) -> Path:
    if not filePath or not isinstance(filePath, str):
        raise ValueError("filePath 参数必填（存档文件的完整路径）")
    p = Path(filePath)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    if not p.is_file():
        raise ValueError(f"路径不是文件: {p}")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"文件超过 100MB 上限（{p.stat().st_size} 字节），拒绝处理")
    return p


def _read_text_with_encoding(p: Path) -> tuple[str, str]:
    """尝试 utf-8 / gbk / latin-1 解码，返回 (文本, 编码名)。
    注意：utf-8 在前且不用 utf-8-sig —— 无 BOM 文件写回时保持无 BOM；
    有 BOM 的文件解码后首字符为 U+FEFF，encode('utf-8') 时会原样还原为 BOM，
    天然保证 round-trip 一致。"""
    raw = p.read_bytes()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError("无法识别文件编码（utf-8/gbk 均失败）")


def _looks_binary(p: Path) -> bool:
    raw = p.read_bytes()[:8192]
    if not raw:
        return False
    if raw[:2] in (b"PK", b"MZ", b"BM", b"Rar"):
        return True
    if b"\x00" in raw:
        return True
    try:
        raw.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("gbk")
        return False
    except UnicodeDecodeError:
        return True


def _hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexs = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08X}  {hexs:<{width * 3 - 1}}  {asc}")
    return NL.join(lines)


def _extract_strings_ascii(data: bytes, min_len: int = 4, limit: int = 0) -> list[str]:
    limit = limit or STRINGS_LIMIT
    out: list[str] = []
    cur: list[int] = []
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= min_len:
                out.append("".join(chr(c) for c in cur))
                if len(out) >= limit:
                    return out
            cur = []
    if len(cur) >= min_len and len(out) < limit:
        out.append("".join(chr(c) for c in cur))
    return out


def _extract_strings_utf16(data: bytes, limit: int = 15) -> list[str]:
    try:
        txt = data.decode("utf-16-le", errors="ignore")
    except Exception:
        return []
    out: list[str] = []
    buf = ""
    for ch in txt:
        if ch.isalnum() or ch == "_":
            buf += ch
        else:
            if len(buf) >= 3:
                out.append(buf)
                if len(out) >= limit:
                    return out
            buf = ""
    if len(buf) >= 3 and len(out) < limit:
        out.append(buf)
    return out


def _preview(v: Any, width: int = 100) -> str:
    if isinstance(v, dict):
        return f"dict({len(v)} keys)"
    if isinstance(v, list):
        return f"list({len(v)} items)"
    s = v if isinstance(v, str) else _json.dumps(v, ensure_ascii=False)
    return s if len(s) <= width else s[:width] + "…"


# ---------------------------------------------------------------------------
# 备份与恢复
# ---------------------------------------------------------------------------
def _path_key(p: Path) -> str:
    key_src = str(p.resolve()).lower()
    h = hashlib.md5(key_src.encode("utf-8", "ignore")).hexdigest()[:10]
    parent = re.sub(r"[^0-9A-Za-z_一-龥]", "_", p.parent.name)[:24] or "root"
    return f"{h}_{parent}"


def _backup_one(src: Path, note: str = "") -> Path:
    """把 src 拷入集中备份目录，返回备份路径。"""
    broot = _backup_root() / _path_key(src)
    broot.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = broot / f"{src.name}.{ts}.bak"
    n = 1
    while dst.exists():
        dst = broot / f"{src.name}.{ts}_{n}.bak"
        n += 1
    shutil.copy2(src, dst)
    log = _backup_root() / "backup_log.jsonl"
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(src),
        "backup": str(dst),
        "size": src.stat().st_size,
        "note": note,
    }
    with log.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry, ensure_ascii=False) + NL)
    return dst


def _auto_backup(src: Path, note: str = "auto before modify") -> Path:
    return _backup_one(src, note)


# ---------------------------------------------------------------------------
# JSON 存档：路径下钻 / 搜索
# ---------------------------------------------------------------------------
def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise KeyError(f"路径段 [{part}] 在数组中无效（当前路径已到列表）") from None
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"路径段 [{part}] 不存在")
            cur = cur[part]
        else:
            raise KeyError(f"路径段 [{part}] 处无法继续下钻（当前是 {type(cur).__name__}）")
    return cur


def _dig_set(obj: Any, path: str, value: Any, create: bool = True) -> None:
    parts = path.split(".")
    cur = obj
    for i, part in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise KeyError(f"路径段 [{part}] 在数组中无效") from None
        elif isinstance(cur, dict):
            if part not in cur:
                if not create:
                    raise KeyError(f"路径段 [{part}] 不存在（createMissing=False）")
                cur[part] = [] if nxt.isdigit() else {}
            cur = cur[part]
        else:
            raise KeyError(f"路径段 [{part}] 处无法继续下钻（当前是 {type(cur).__name__}）")
    last = parts[-1]
    if isinstance(cur, list):
        try:
            idx = int(last)
        except ValueError:
            raise KeyError(f"路径段 [{last}] 在数组中应为数字下标") from None
        if idx == len(cur):
            cur.append(value)
        elif 0 <= idx < len(cur):
            cur[idx] = value
        else:
            raise IndexError(f"数组下标越界: {idx}（长度 {len(cur)}，仅支持 0..len 或 len 处追加）")
    elif isinstance(cur, dict):
        if last not in cur and not create:
            raise KeyError(f"路径段 [{last}] 不存在（createMissing=False）")
        cur[last] = value
    else:
        raise KeyError(f"路径终点 [{last}] 的父节点是 {type(cur).__name__}，无法设置")


def _iter_paths(obj: Any, prefix: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            yield p, k, v
            yield from _iter_paths(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}.{i}" if prefix else str(i)
            yield p, str(i), v
            yield from _iter_paths(v, p)


def _json_summary(root: Any) -> dict:
    if isinstance(root, dict):
        return {"type": "object", "topLevelKeys": {
            k: _preview(v, 60) for k, v in list(root.items())[:50]}}
    if isinstance(root, list):
        return {"type": "array", "length": len(root),
                "firstItems": [_preview(v, 60) for v in root[:5]]}
    return {"type": type(root).__name__, "preview": _preview(root)}


# ---------------------------------------------------------------------------
# Action 实现
# ---------------------------------------------------------------------------
def _scan_saves(keyword: str = "", maxResults: int = 40) -> dict:
    dirs = _user_dirs()
    maxResults = max(1, min(int(maxResults or 40), 200))
    keyword = (keyword or "").strip()
    files: list[dict] = []
    scanned_roots: list[str] = []

    def _excluded(name: str) -> bool:
        low = name.lower()
        return any(w in low for w in _EXCLUDE_DIR_WORDS)

    def _push_file(p: Path) -> bool:
        if len(files) >= SCAN_MAX_FILES:
            return False
        try:
            st = p.stat()
        except OSError:
            return True
        files.append({
            "path": str(p),
            "size": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
        return True

    def _walk_dir(d: Path, depth: int) -> None:
        if depth < 0 or len(files) >= SCAN_MAX_FILES:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_dir(), x.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.is_dir():
                if not _excluded(e.name):
                    _walk_dir(e, depth - 1)
            else:
                if e.suffix.lower() in _SAVE_EXT or "save" in e.parent.name.lower():
                    if not _push_file(e):
                        return

    if keyword:
        kw = keyword.lower()
        for label in ("myGames", "savedGames", "paradoxDocs", "documents",
                      "localAppData", "roamingAppData"):
            base = dirs[label]
            if not base.exists():
                continue
            scanned_roots.append(str(base))
            try:
                children = sorted(base.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir() and kw in child.name.lower():
                    _walk_dir(child, SCAN_DEPTH)
                elif child.is_file() and kw in child.name.lower():
                    _push_file(child)
    else:
        for label in ("myGames", "savedGames", "paradoxDocs"):
            base = dirs[label]
            if not base.exists():
                continue
            scanned_roots.append(str(base))
            try:
                for child in sorted(base.iterdir()):
                    if child.is_dir() and not _excluded(child.name):
                        _walk_dir(child, SCAN_DEPTH - 1)
            except OSError:
                continue

    files.sort(key=lambda x: x["mtime"], reverse=True)
    files = files[:maxResults]
    return {
        "ok": True,
        "keyword": keyword,
        "scannedRoots": scanned_roots,
        "count": len(files),
        "files": files,
        "hint": ("未传 keyword 时仅浏览 My Games / Saved Games / Paradox 目录概览；"
                 "传游戏名关键词（如 keyword='星露谷' 或 'skyrim'）可扩大到 Documents/"
                 "AppData 全盘常见位置搜索。二进制容器（zip/gzip）建议配合 hexReplace 使用。"),
    }


def _read_save(filePath: str = "", maxLines: int = PREVIEW_LIMIT,
               maxBytes: int = 65536) -> dict:
    p = _to_path(filePath)
    maxLines = max(1, min(int(maxLines or PREVIEW_LIMIT), 2000))
    maxBytes = max(200, min(int(maxBytes or 65536), 512 * 1024))
    head = p.read_bytes()[:2]
    info: dict = {"ok": True, "path": str(p), "size": p.stat().st_size,
                  "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(p.stat().st_mtime))}
    if head == b"PK":
        info.update({"kind": "binary", "container": "zip",
                     "note": "zip 容器存档（如群星等）：内含 meta/gamestate 等条目，"
                             "文本方式不可读；如需修改请谨慎，建议先 backup。"})
        info["hexdump"] = _hexdump(p.read_bytes()[:HEXDUMP_BYTES])
        return info
    if _looks_binary(p):
        raw = p.read_bytes()
        info.update({
            "kind": "binary",
            "hexdump": _hexdump(raw[:HEXDUMP_BYTES]),
            "strings": _extract_strings_ascii(raw),
            "stringsUtf16": _extract_strings_utf16(raw),
            "note": "二进制存档：无法直接文本编辑。可用 hexReplace 做等长字节替换"
                    "（先 backup，用 analyzeJson/外部工具确认数值字节序后再操作）。",
        })
        return info
    text, enc = _read_text_with_encoding(p)
    lines = text.splitlines()
    info.update({
        "kind": "text",
        "encoding": enc,
        "totalLines": len(lines),
        "preview": NL.join(lines[:maxLines]),
        "truncated": len(lines) > maxLines,
        "note": ("看起来是 JSON 存档时，建议用 analyzeJson 定位字段、editJson 精确修改，"
                 "比 editText 文本替换更安全。"),
    })
    if len(lines) > maxLines:
        info["tail"] = NL.join(lines[-3:])
    return info


def _load_json(p: Path) -> tuple[Any, str]:
    text, enc = _read_text_with_encoding(p)
    try:
        return _json.loads(text.lstrip(chr(65279))), enc
    except _json.JSONDecodeError as e:
        raise ValueError(f"不是合法 JSON（{e}）。若为文本存档请用 editText；"
                         f"二进制请用 hexReplace。") from None


def _analyze_json(filePath: str = "", keyPath: str = "", search: str = "",
                  maxResults: int = 50) -> dict:
    p = _to_path(filePath)
    root, enc = _load_json(p)
    out: dict = {"ok": True, "path": str(p), "encoding": enc}
    if keyPath:
        try:
            val = _dig(root, keyPath.strip())
        except KeyError as e:
            return {"ok": False, "error": str(e), "path": str(p)}
        out["keyPath"] = keyPath
        out["type"] = type(val).__name__
        out["value"] = val if isinstance(val, (int, float, bool)) else _preview(val, 2000)
        if isinstance(val, dict):
            out["keys"] = list(val.keys())[:100]
        elif isinstance(val, list):
            out["length"] = len(val)
            out["firstItems"] = [_preview(v, 60) for v in val[:5]]
        return out
    if search:
        term = str(search)
        hits: list[dict] = []
        numeric_eq = False
        try:
            num = float(term)
            numeric_eq = True
        except ValueError:
            num = None
        count = 0
        for path, key, val in _iter_paths(root):
            count += 1
            if count > SEARCH_NODE_CAP:
                out["truncated"] = True
                break
            matched = False
            if isinstance(val, str) and term.lower() in val.lower():
                matched = True
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                if numeric_eq and float(val) == num:
                    matched = True
                elif term == str(val):
                    matched = True
            if not matched and term.lower() in str(key).lower() and val is not None:
                matched = True
            if matched:
                hits.append({"path": path, "type": type(val).__name__,
                             "value": _preview(val, 80)})
                if len(hits) >= max(1, min(int(maxResults or 50), 200)):
                    out["truncated"] = True
                    break
        out["search"] = term
        out["hits"] = hits
        out["hitCount"] = len(hits)
        out["hint"] = ("命中过多时换更具体的词；找到目标后用 editJson 按 path 精确修改。"
                       "数值匹配支持十进制相等；浮点数建议多试几种写法（1.0/1）。")
        return out
    out["summary"] = _json_summary(root)
    return out


def _coerce_value(value: Any) -> Any:
    """value 约定：能被 JSON 解析则用解析值，否则按字面字符串。
    数字 99999 传 99999 或 '99999' 均得数字 99999；要字符串 '10' 请传 '\"10\"'。"""
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("{", "[", '"')) or s in ("true", "false", "null") or \
                re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", s or ""):
            try:
                return _json.loads(s)
            except _json.JSONDecodeError:
                return value
        return value
    return value


def _edit_json(filePath: str = "", keyPath: str = "", op: str = "set",
               value: Any = None, createMissing: bool = True) -> dict:
    p = _to_path(filePath)
    if not keyPath and op != "append":
        raise ValueError("editJson 需要 keyPath（如 player.gold；数组下标用点分数字 a.items.0）")
    root, enc = _load_json(p)
    op = (op or "set").lower()
    val = _coerce_value(value)
    if op == "set":

        try:
            old = _dig(root, keyPath)
        except KeyError:
            old = None
        _dig_set(root, keyPath, val, createMissing)
        new = _dig(root, keyPath)
        result_old, result_new = old, new
    elif op == "append":
        target = _dig(root, keyPath) if keyPath else root
        if not isinstance(target, list):
            raise ValueError(f"append 目标必须是数组（当前 {type(target).__name__}）")
        old = None
        target.append(val)
        result_old, result_new = old, val
    elif op == "delete":
        parts = keyPath.split(".")
        parent = _dig(root, ".".join(parts[:-1])) if len(parts) > 1 else root
        last = parts[-1]
        if isinstance(parent, dict):
            if last not in parent:
                raise KeyError(f"键 [{last}] 不存在")
            result_old = parent.pop(last)
        elif isinstance(parent, list):
            idx = int(last)
            result_old = parent.pop(idx)
        else:
            raise ValueError("delete 的父节点必须是 dict 或 list")
        result_new = None
    else:
        raise ValueError(f"未知 op: {op}（支持 set/append/delete）")

    bak = _auto_backup(p)
    indent = 2 if NL in p.read_bytes()[:4096].decode("latin-1", "ignore") else None
    p.write_bytes(_json.dumps(root, ensure_ascii=False, indent=indent)
                  .encode(enc, "replace"))
    return {
        "ok": True, "modified": True, "path": str(p), "encoding": enc,
        "op": op, "keyPath": keyPath,
        "old": result_old if isinstance(result_old, (int, float, bool, type(None)))
               else _preview(result_old, 500),
        "new": result_new if isinstance(result_new, (int, float, bool, type(None)))
               else _preview(result_new, 500),
        "backup": str(bak),
        "disclaimer": DISCLAIMER,
        "note": "建议重新进入游戏验证修改是否生效；JSON 已按缩进重新格式化，"
                "若游戏对格式敏感异常，用 backup 路径可直接还原。",
    }


def _edit_text(filePath: str = "", find: str = "", replace: str = "",
               regex: bool = False, maxCount: int = 0) -> dict:
    p = _to_path(filePath)
    if not find:
        raise ValueError("find 参数必填（要查找的内容）")
    text, enc = _read_text_with_encoding(p)
    if regex:
        try:
            new_text, n = re.subn(find, replace or "", text,
                                  count=int(maxCount or 0))
        except re.error as e:
            raise ValueError(f"正则表达式无效: {e}") from None
    else:
        n = text.count(find)
        if n and maxCount:
            n = min(n, int(maxCount))
        new_text = text.replace(find, replace or "", int(maxCount) if maxCount else -1)
    if n == 0:
        return {"ok": False, "error": "没有找到任何匹配，文件未被修改",
                "path": str(p), "find": find, "regex": regex}
    bak = _auto_backup(p)
    p.write_bytes(new_text.encode(enc, "replace"))
    return {
        "ok": True, "modified": True, "path": str(p), "encoding": enc,
        "replacements": n, "regex": regex,
        "backup": str(bak), "disclaimer": DISCLAIMER,
        "note": "替换完成。建议重新进入游戏验证；异常时用 backup 路径还原。",
    }


def _clean_hex(s: str) -> bytes:
    hx = re.sub(r"[^0-9A-Fa-f]", "", s or "")
    if not hx or len(hx) % 2:
        raise ValueError(f"hex 无效（必须偶数位十六进制）: {s!r}")
    try:
        return bytes.fromhex(hx)
    except ValueError as e:
        raise ValueError(f"hex 无效: {e}") from None


def _hex_replace(filePath: str = "", findHex: str = "", replaceHex: str = "",
                 replaceAll: bool = False, offset: int = -1) -> dict:
    p = _to_path(filePath)
    find = _clean_hex(findHex)
    rep = _clean_hex(replaceHex)
    if len(find) != len(rep):
        raise ValueError(f"等长替换要求 find 与 replace 字节数一致"
                         f"（{len(find)} != {len(rep)}）；改不同长度会破坏文件结构")
    data = p.read_bytes()
    if not find:
        raise ValueError("findHex 必填（如 0000803F 表示 float 1.0 小端）")
    offsets: list[int] = []
    i = data.find(find)
    while i != -1 and len(offsets) <= 500:
        offsets.append(i)
        i = data.find(find, i + 1)
    if not offsets:
        return {"ok": False, "error": "未找到目标字节序列，文件未修改",
                "findHex": find.hex(), "path": str(p),
                "hint": "用 readSave(kind=binary 的 hexdump) 核对字节，注意小端序"}
    if offset >= 0:
        if offset not in offsets:
            raise ValueError(f"offset {offset} 处没有目标序列（实际命中: {offsets[:20]}）")
        chosen = [offset]
    elif len(offsets) == 1 or replaceAll:
        chosen = offsets
    else:
        return {"ok": False, "error": f"命中 {len(offsets)} 处，默认拒绝修改",
                "offsets": offsets[:50],
                "hint": "确认后传 replaceAll=True 全部替换，或传 offset=<位置> 只改一处",
                "findHex": find.hex(), "path": str(p)}
    new_data = bytearray(data)
    for off in chosen:
        new_data[off:off + len(find)] = rep
    bak = _auto_backup(p)
    p.write_bytes(bytes(new_data))
    return {
        "ok": True, "modified": True, "path": str(p),
        "replacements": len(chosen), "offsets": chosen[:50],
        "backup": str(bak), "disclaimer": DISCLAIMER,
        "note": "字节替换完成。二进制存档常有校验和/压缩，若游戏读档异常用 backup 还原。",
    }


def _backup(filePath: str = "", note: str = "") -> dict:
    p = _to_path(filePath)
    bak = _backup_one(p, note or "manual")
    return {"ok": True, "path": str(p), "backup": str(bak),
            "disclaimer": DISCLAIMER}


def _list_backups(filePath: str = "") -> dict:
    p = _to_path(filePath)
    bdir = _backup_root() / _path_key(p)
    items: list[dict] = []
    if bdir.exists():
        for f in sorted(bdir.glob("*.bak"), reverse=True):
            st = f.stat()
            items.append({"backup": str(f), "size": st.st_size,
                          "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                                time.localtime(st.st_mtime))})
    return {"ok": True, "path": str(p), "count": len(items), "backups": items,
            "note": "restore 时传 timestamp（备份文件名中那段 YYYYMMDD_HHMMSS）。"}


def _restore(filePath: str = "", timestamp: str = "") -> dict:
    p = _to_path(filePath)
    if not timestamp:
        raise ValueError("timestamp 必填（备份文件名中的时间戳，见 listBackups）")
    ts = re.sub(r"[^0-9_]", "", timestamp)
    bdir = _backup_root() / _path_key(p)
    cands = [b for b in (sorted(bdir.glob(f"{p.name}.{ts}.bak"), reverse=True)
                         if bdir.exists() else [])]
    if not cands:
        raise FileNotFoundError(f"没有找到匹配 {ts} 的备份（目录 {bdir}）")
    bak = cands[0]
    safety = _auto_backup(p, note="safety before restore")
    shutil.copy2(bak, p)
    return {"ok": True, "modified": True, "path": str(p),
            "restoredFrom": str(bak), "safetyBackup": str(safety),
            "disclaimer": DISCLAIMER}


_ACTIONS = {
    "scanSaves": _scan_saves,
    "readSave": _read_save,
    "analyzeJson": _analyze_json,
    "editJson": _edit_json,
    "editText": _edit_text,
    "hexReplace": _hex_replace,
    "backup": _backup,
    "listBackups": _list_backups,
    "restore": _restore,
}

_MODIFY_ACTIONS = {"editJson", "editText", "hexReplace", "restore"}


def run(action: str = "scanSaves", **kwargs: Any) -> dict:
    try:
        fn = _ACTIONS.get(action)
        if fn is None:
            return {"ok": False, "error": f"未知 action: {action}",
                    "actions": sorted(_ACTIONS)}
        result = fn(**kwargs)
        if isinstance(result, dict):
            result.setdefault("action", action)
            if action in _MODIFY_ACTIONS and result.get("ok"):
                result["modified"] = True
                result["disclaimer"] = DISCLAIMER
        return result
    except Exception as e:  # noqa: BLE001 —— 插件边界统一兜底
        return {"ok": False, "action": action,
                "error": f"{type(e).__name__}: {e}", "disclaimer": DISCLAIMER}


if __name__ == "__main__":
    print(run(action="scanSaves", keyword=""))