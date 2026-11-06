# -*- coding: utf-8 -*-
"""everything_search 插件单元测试：mock subprocess 模拟 es.exe，验证 run() 各分支逻辑。"""
import sys
import os
import json
import importlib.util
import subprocess
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "tool.py")

REAL_EXE = sys.executable
exe_dir, exe_name = os.path.split(REAL_EXE)

_ft = int((datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
           - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000
          ) + 116444736000000000

_MOCK_DB = [
    {"filename": REAL_EXE, "size": 300_000_000, "date_modified": _ft, "extension": "exe", "attributes": 0x20},
    {"filename": r"C:\Users\me\Projects\settings.yaml", "size": 2048, "date_modified": _ft, "extension": "yaml", "attributes": 0x20},
    {"filename": r"D:\Docs\notes.md", "size": 5120, "date_modified": _ft, "extension": "md", "attributes": 0x20},
    {"filename": r"D:\Docs\notes2.md", "size": 8192, "date_modified": _ft, "extension": "md", "attributes": 0x20},
    {"filename": r"D:\Docs\subdir", "size": 0, "date_modified": _ft, "extension": "", "attributes": 0x10},
]


class _MockResult:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


_VALUE_OPTS = {"-n", "-viewport-offset", "-viewport-count", "-get-result-count",
               "-path", "-sort", "-instance", "-timeout", "-highlight-color",
               "-size-format", "-date-format"}


def _extract_query_parts(cmd):
    """从 cmd 中提取查询位置参数（跳过 es 路径和选项及其值）。"""
    parts = []
    i = 1
    while i < len(cmd):
        arg = cmd[i]
        if arg.startswith("-") or arg.startswith("/"):
            if arg in _VALUE_OPTS and i + 1 < len(cmd):
                i += 2
            else:
                i += 1
        else:
            parts.append(arg)
            i += 1
    return parts


def _mock_search(parts):
    """极简模拟 Everything 搜索：根据查询 parts 过滤。"""
    results = list(_MOCK_DB)

    for part in parts:
        if part.startswith("ext:"):
            ext = part[4:].lower()
            results = [r for r in results if r["extension"].lower() == ext]
        elif part == "hidden":
            results = [r for r in results if r["attributes"] & 0x02]
        elif part == "!hidden":
            results = [r for r in results if not (r["attributes"] & 0x02)]
        elif part.startswith(("size:", "dm:", "path:")):
            pass
        else:
            key = part.lower()
            results = [r for r in results if key in os.path.basename(r["filename"]).lower()]

    return results


def _mock_subprocess_run(cmd, **kw):
    """模拟 es.exe 的 subprocess.run 调用。"""
    parts = _extract_query_parts(cmd)

    if "-get-result-count" in cmd:
        matched = _mock_search(parts)
        return _MockResult(stdout=str(len(matched)), returncode=0, stderr="")

    if "-json" in cmd:
        limit, offset = 30, 0
        for i, arg in enumerate(cmd):
            if arg == "-viewport-count" and i + 1 < len(cmd):
                limit = int(cmd[i + 1])
            elif arg == "-viewport-offset" and i + 1 < len(cmd):
                offset = int(cmd[i + 1])

        matched = _mock_search(parts)
        paged = matched[offset:offset + limit]
        return _MockResult(stdout=json.dumps(paged), returncode=0, stderr="")

    return _MockResult(stdout="", returncode=1, stderr="unknown command")


_real_run = subprocess.run
subprocess.run = _mock_subprocess_run

spec = importlib.util.spec_from_file_location("everything_search_tool", PLUGIN)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

tool._ES_PATH_CACHE = r"C:\__fake__\es.exe"
tool._find_es = lambda: r"C:\__fake__\es.exe"
tool._get_everything_proc_dir = lambda: None
tool.os.startfile = lambda p: None

ok = 0
fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS: " + label)
    else:
        fail += 1
        print("FAIL: " + label + ("  << " + str(extra) if extra else ""))


# 1) search 基本路径
r = tool.run(action="search", query=exe_name.split(".")[0], name_only=True, ext="exe", limit=5)
check("search ok", r.get("ok") is True, r)
check("search count==1", r.get("count") == 1, r)
check("search path correct",
      r.get("results") and r["results"][0]["path"].lower() == REAL_EXE.lower(), r)
check("search query_used has ext:exe",
      "ext:exe" in r.get("query_used", ""), r.get("query_used"))

# 2) open 分支（指向真实存在的 exe，isfile 通过）
r2 = tool.run(action="open", query=exe_name)
check("open ok", r2.get("ok") is True, r2)
check("open action", r2.get("action") == "open", r2)
check("open path", (r2.get("opened") or "").lower() == REAL_EXE.lower(), r2)

# 3) 过滤参数 -> 查询语法翻译
r3 = tool.run(action="search", query="", ext="md", path="D:\\*",
              size_min=4096, size_max=65536,
              mdate_min="2024-01-01", mdate_max="2024-12-31", hidden=1)
q = r3.get("query_used", "")
check("query ext", "ext:md" in q, q)
check("query path", 'path:"D:\\*"' in q, q)
check("query size", "size:>=4096" in q and "size:<=65536" in q, q)
check("query date", "dm:>=2024-01-01" in q and "dm:<=2024-12-31" in q, q)
check("query hidden=1 => hidden", "hidden" in q and "!hidden" not in q, q)

# 4) hidden 默认排除隐藏 → !hidden
r4 = tool.run(action="search", query="x", ext="txt")
check("query hidden default excludes", "!hidden" in r4.get("query_used", ""), r4.get("query_used"))

# 5) size 传 0 视为不限
r5 = tool.run(action="search", query="x", ext="txt", size_min=0, size_max=0)
check("size 0 => no size filter", "size:" not in r5.get("query_used", ""), r5.get("query_used"))

# 6) 错误分支
check("empty query error", tool.run(action="search").get("ok") is False)
check("bad action error", tool.run(action="bad").get("ok") is False)
check("open no query error", tool.run(action="open").get("ok") is False)
r_notfound = tool.run(action="open", query="definitely_not_exist_xyz")
check("open not-found error", r_notfound.get("ok") is False, r_notfound)

# 7) 分页
r7 = tool.run(action="search", query="", ext="md", limit=1, offset=1)
check("pagination ok", r7.get("ok") is True, r7)
check("pagination count==1", r7.get("count") == 1, r7)

# 8) serialize 字段完整性
r8 = tool.run(action="search", query="", ext="md", limit=1)
if r8.get("results"):
    item = r8["results"][0]
    check("serialize has name", "name" in item, item)
    check("serialize has path", "path" in item, item)
    check("serialize has size", "size" in item, item)
    check("serialize has modified", "modified" in item, item)
    check("serialize modified format", bool(item["modified"]) and "-" in item["modified"], item)
    check("serialize has is_folder", "is_folder" in item, item)
else:
    check("serialize fields", False, "no results")

# 9) 文件夹识别
r9 = tool.run(action="search", query="subdir", name_only=True, limit=5)
folder_found = any(r.get("is_folder") for r in r9.get("results", []))
check("folder is_folder=True", folder_found, r9.get("results"))

# 10) total 字段
check("total is int", isinstance(r.get("total", None), int), r.get("total"))

subprocess.run = _real_run

print("\nRESULT: {} passed, {} failed".format(ok, fail))
sys.exit(1 if fail else 0)