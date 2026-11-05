'''everything_search 插件执行代码 — 对应 manifest.yaml + tool.yaml

基于 Everything 官方命令行工具 es.exe，通过 subprocess 调用 es.exe，
经 IPC 与 Everything 搜索引擎通信（非 HTTP），实现毫秒级文件搜索与应用启动。

前置条件：
  1. 安装并运行 Everything（https://www.voidtools.com/）
  2. es.exe 随 Everything 安装包提供，位于 Everything 同目录下
  3. 若自动探测不到 es.exe，可用环境变量 EVERYTHING_ES_PATH 指定其完整路径
'''

import os
import json
import shutil
import subprocess
from datetime import datetime, timezone, timedelta

_ES_PATH_CACHE = None
_ES_TIMEOUT = 15

_STARTUPINFO = None
if os.name == "nt":
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUPINFO.wShowWindow = 0


def _decode_stdout(raw):
    """解码 es.exe 输出：先 UTF-8 后 GBK（中文 Windows 兼容）。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")

_FILETIME_UNIX_OFFSET = 116444736000000000
_FILE_ATTRIBUTE_DIRECTORY = 0x10

_ES_CANDIDATES = (
    r"F:\Everything\main\es.exe",
    r"C:\Program Files\Everything\es.exe",
    r"C:\Program Files (x86)\Everything\es.exe",
    r"C:\Program Files\Everything 1.5a\es.exe",
    r"C:\Program Files\Everything 1.4\es.exe",
)


def _err(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def _get_everything_proc_dir():
    """通过 wmic 获取 Everything.exe 所在目录，用于推导 es.exe 路径。"""
    try:
        r = subprocess.run(
            ["wmic", "process", "where", "name='Everything.exe'", "get", "ExecutablePath"],
            capture_output=True, timeout=5, startupinfo=_STARTUPINFO,
        )
        stdout = _decode_stdout(r.stdout)
        for line in stdout.splitlines():
            line = line.strip()
            if line.lower().endswith("everything.exe") and os.path.isfile(line):
                return os.path.dirname(line)
    except Exception:
        pass
    return None


def _find_es():
    """探测 es.exe 路径：环境变量 → Everything 进程同目录 → 常见路径 → PATH。"""
    global _ES_PATH_CACHE
    if _ES_PATH_CACHE:
        return _ES_PATH_CACHE

    env_path = os.environ.get("EVERYTHING_ES_PATH")
    if env_path and os.path.isfile(env_path):
        _ES_PATH_CACHE = env_path
        return env_path

    proc_dir = _get_everything_proc_dir()
    if proc_dir:
        candidate = os.path.join(proc_dir, "es.exe")
        if os.path.isfile(candidate):
            _ES_PATH_CACHE = candidate
            return candidate

    for p in _ES_CANDIDATES:
        if os.path.isfile(p):
            _ES_PATH_CACHE = p
            return p

    found = shutil.which("es") or shutil.which("es.exe")
    if found:
        _ES_PATH_CACHE = found
        return found

    return None


def _filetime_to_str(ft):
    """Windows FILETIME（100ns since 1601-01-01）→ 'YYYY-MM-DD HH:MM:SS'。"""
    try:
        ft = int(ft)
    except (TypeError, ValueError):
        return ""
    if ft <= 0:
        return ""
    try:
        us = (ft - _FILETIME_UNIX_OFFSET) // 10
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=us)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _num(value):
    """把 0 / '' / None 统一视为「不限」，返回 None；否则返回 int。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _build_query(query, name_only=False, ext="", path="", size_min=None,
                 size_max=None, mdate_min="", mdate_max="", hidden=0):
    """把结构化参数翻译成 Everything 查询 parts 列表。

    返回 list[str]，每个元素作为 es.exe 的独立位置参数传递（es.exe 把
    多个位置参数按 AND 组合；单参数内的空格不会被当作分隔符）。
    name_only 在 es.exe 下不使用 name: 前缀（es.exe 不支持该语法）。
    """
    parts = []
    q = (query or "").strip()
    if q:
        parts.append(q)

    if ext:
        e = ext.strip().lstrip(".")
        if e:
            parts.append("ext:" + e)

    if path:
        p = path.strip().rstrip("\\/")
        if p:
            parts.append('path:"' + p + '"')

    smin, smax = _num(size_min), _num(size_max)
    if smin is not None:
        parts.append("size:>={}".format(smin))
    if smax is not None:
        parts.append("size:<={}".format(smax))

    if mdate_min:
        parts.append("dm:>=" + mdate_min.strip())
    if mdate_max:
        parts.append("dm:<=" + mdate_max.strip())

    if hidden == 1:
        parts.append("hidden")
    elif hidden != 2:
        parts.append("!hidden")

    return parts


def _run_es(query_parts, limit, offset):
    """调用 es.exe 执行搜索，返回 (items_list, error_or_None)。"""
    es = _find_es()
    if not es:
        return None, _err(
            "未找到 es.exe（Everything 命令行工具）。请确认已安装 Everything，"
            "或设置环境变量 EVERYTHING_ES_PATH 指向 es.exe 的完整路径。"
        )

    cmd = [
        es, "-json", "-size", "-dm", "-extension", "-attributes",
        "-viewport-offset", str(offset),
        "-viewport-count", str(limit),
    ]
    cmd.extend(query_parts)

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_ES_TIMEOUT,
            startupinfo=_STARTUPINFO,
        )
    except subprocess.TimeoutExpired:
        return None, _err("es.exe 查询超时（{}s）".format(_ES_TIMEOUT))
    except Exception as e:
        return None, _err("调用 es.exe 失败：{}".format(e))

    if r.returncode != 0:
        err_msg = _decode_stdout(r.stderr).strip() or "未知错误（返回码 {}）".format(r.returncode)
        return None, _err("es.exe 错误：{}".format(err_msg))

    stdout = _decode_stdout(r.stdout).strip()
    if not stdout or stdout == "[]":
        return [], None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None, _err("es.exe 输出解析失败", raw=stdout[:200])

    return data if isinstance(data, list) else [], None


def _get_total(query_parts):
    """用 -get-result-count 获取匹配总数（轻量调用）。"""
    es = _find_es()
    if not es:
        return 0
    try:
        cmd = [es, "-get-result-count"]
        cmd.extend(query_parts)
        r = subprocess.run(
            cmd,
            capture_output=True, timeout=_ES_TIMEOUT, startupinfo=_STARTUPINFO,
        )
        return int(_decode_stdout(r.stdout).strip() or 0)
    except Exception:
        return 0


def _serialize(item):
    """把 es.exe JSON 输出项序列化为 LLM 友好的 dict。"""
    filename = item.get("filename", "") or ""
    attr = int(item.get("attributes", 0) or 0)
    clean_path = filename.rstrip("\\/")
    return {
        "name": os.path.basename(clean_path),
        "path": filename,
        "parent": os.path.dirname(filename),
        "ext": item.get("extension", "") or "",
        "size": int(item.get("size", 0) or 0),
        "is_folder": bool(attr & _FILE_ATTRIBUTE_DIRECTORY),
        "modified": _filetime_to_str(item.get("date_modified")),
    }


def _do_search(query, name_only, ext, path, size_min, size_max,
               mdate_min, mdate_max, hidden, limit, offset):
    parts = _build_query(
        query, name_only=name_only, ext=ext, path=path,
        size_min=size_min, size_max=size_max,
        mdate_min=mdate_min, mdate_max=mdate_max, hidden=hidden,
    )
    search_text = " ".join(parts)
    if not [p for p in parts if p not in ("hidden", "!hidden")]:
        return _err("query / ext / path 至少提供一个", query_used=search_text)

    limit = max(1, min(int(limit or 30), 1000))
    offset = max(0, int(offset or 0))

    items, error = _run_es(parts, limit, offset)
    if error:
        error.setdefault("query_used", search_text)
        return error

    results = [_serialize(item) for item in items]
    total = _get_total(parts)

    return {
        "ok": True,
        "action": "search",
        "query_used": search_text,
        "total": total,
        "count": len(results),
        "results": results,
    }


def _do_open(query):
    q = (query or "").strip()
    if not q:
        return _err("open 模式必须提供 query（应用名）")
    if q.lower().endswith(".exe"):
        q = q[:-4]

    parts = _build_query(q, name_only=True, ext="exe", hidden=2)
    search_text = " ".join(parts)
    items, error = _run_es(parts, 5, 0)
    if error:
        error.setdefault("query_used", search_text)
        return error

    if not items:
        return _err(
            '未找到名为 "{}" 的可执行程序'.format(query),
            query_used=search_text,
            hint="可先用 action=search, ext=exe 确认程序名",
        )

    target_path = items[0].get("filename", "")
    if not target_path or not os.path.isfile(target_path):
        return _err("目标文件不存在或已被移动：{}".format(target_path))

    try:
        if hasattr(os, "startfile"):
            os.startfile(target_path)  # noqa: S606
        else:
            subprocess.Popen(
                [target_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        return _err("启动失败：{}".format(e), path=target_path)

    return {
        "ok": True,
        "action": "open",
        "opened": target_path,
        "name": os.path.basename(target_path),
        "message": "已启动：{}".format(target_path),
    }


def run(action: str = "search", query: str = "", name_only: bool = False,
        ext: str = "", path: str = "", size_min: int = 0, size_max: int = 0,
        mdate_min: str = "", mdate_max: str = "", hidden: int = 0,
        limit: int = 30, offset: int = 0) -> dict:
    """Everything 搜索入口。返回 dict；失败时返回 {'ok': False, 'error': '...'}。"""
    action = (action or "search").strip().lower()
    if action not in ("search", "open"):
        return _err('action 必须是 "search" 或 "open"')

    if action == "open":
        return _do_open(query)

    return _do_search(query, name_only, ext, path, size_min, size_max,
                      mdate_min, mdate_max, hidden, limit, offset)