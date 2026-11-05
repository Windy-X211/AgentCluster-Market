#!/usr/bin/env python3
"""AgentCluster 外部控制 · 轻量客户端。

供外部 AI（DeepSeek Harness、Codex、Claude Code 等）通过命令行快速操作本程序。

用法：
    python ac_client.py info
    python ac_client.py cmd agents.create name=研究员 system_prompt=你负责研究
    python ac_client.py cmd conversations.send id=5 agent_id=10 content=分析量子计算
    python ac_client.py stream 5
    python ac_client.py raw '{"cmd":"agents.list","args":{}}'

环境变量：
    AC_BASE   后端地址，默认 http://127.0.0.1:8870
    AC_TOKEN  token；缺省时自动读取 token.txt，再回退调用 GET /token
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("缺少依赖 requests。请运行：pip install requests", file=sys.stderr)
    sys.exit(1)


BASE = os.environ.get("AC_BASE", "http://127.0.0.1:8870").rstrip("/")
API = f"{BASE}/api/ext/external_control"

# token.txt 候选路径（脚本位于 <ext>/skills/<skill_id>/scripts/，token 在 <ext>/token.txt）
# 也兼容脚本被拷贝到其他位置的情况，最终回退 GET /token 自动生成。
_SKILL_DIR = Path(__file__).resolve().parent.parent          # <skill_id>/
_EXT_DIR = _SKILL_DIR.parent.parent                          # <ext>/（若在 skills/<id>/scripts 下）
_TOKEN_CANDIDATES = [
    _EXT_DIR / "token.txt",
    _SKILL_DIR.parent / "token.txt",
    _SKILL_DIR / "token.txt",
    Path("AppData/extensions/external_control/token.txt"),
]


def _get_token() -> str:
    """获取 token：环境变量 > token.txt > GET /token 自动生成。"""
    env = os.environ.get("AC_TOKEN")
    if env:
        return env.strip()
    for p in _TOKEN_CANDIDATES:
        try:
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
    try:
        r = requests.get(f"{API}/token", timeout=10)
        if r.status_code == 200:
            return r.json().get("token", "")
    except Exception:
        pass
    return ""


def _coerce(value: str):
    """把命令行字符串值转成 int / bool / 字符串。"""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    return value


def _parse_kv(pairs: list[str]) -> dict:
    """解析 key=value 参数；值支持 JSON 字面量（数组/对象/数字/bool）。"""
    out: dict = {}
    for kv in pairs:
        if "=" not in kv:
            print(f"参数格式错误（应为 key=value）：{kv}", file=sys.stderr)
            sys.exit(1)
        k, v = kv.split("=", 1)
        # 优先尝试 JSON 解析（支持 [1,2] / {"a":1} / true / 数字）
        try:
            out[k] = json.loads(v)
        except (json.JSONDecodeError, ValueError):
            out[k] = _coerce(v)
    return out


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}", "Content-Type": "application/json"}


def cmd_info() -> None:
    r = requests.get(f"{API}/info", timeout=10)
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))


def cmd_command(name: str, args: dict) -> None:
    body = {"cmd": name, "args": args, "token": _get_token()}
    r = requests.post(f"{API}/command", headers=_headers(), json=body, timeout=600)
    try:
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    except Exception:
        print(f"HTTP {r.status_code}: {r.text}", file=sys.stderr)
        sys.exit(1)
    if r.status_code >= 400:
        sys.exit(1)


def cmd_stream(cid: int, agent_id: int | None = None) -> None:
    """WebSocket 实时打印智能体输出。"""
    try:
        import websockets  # type: ignore
    except ImportError:
        print("缺少依赖 websockets。请运行：pip install websockets", file=sys.stderr)
        sys.exit(1)

    token = _get_token()
    url = f"ws://{BASE.split('://', 1)[-1]}/api/ext/external_control/ws/events/{cid}?token={token}"
    if agent_id is not None:
        url += f"&agent_id={agent_id}"

    import asyncio

    async def run() -> None:
        async with websockets.connect(url, max_size=None) as ws:
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("done"):
                    break
                if "error" in msg:
                    print(f"[错误] {msg['error']}", file=sys.stderr)
                    break
                print(msg.get("data", ""), end="", flush=True)

    asyncio.run(run())


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    sub = argv[0]
    if sub == "info":
        cmd_info()
    elif sub == "cmd":
        if len(argv) < 2:
            print("用法：ac_client.py cmd <命令名> [key=value ...]", file=sys.stderr)
            sys.exit(1)
        cmd_command(argv[1], _parse_kv(argv[2:]))
    elif sub == "raw":
        if len(argv) < 2:
            print("用法：ac_client.py raw '<JSON>'", file=sys.stderr)
            sys.exit(1)
        payload = json.loads(argv[1])
        cmd_command(payload.get("cmd", ""), payload.get("args", {}))
    elif sub == "stream":
        if len(argv) < 2:
            print("用法：ac_client.py stream <conv_id> [agent_id]", file=sys.stderr)
            sys.exit(1)
        cmd_stream(int(argv[1]), int(argv[2]) if len(argv) > 2 else None)
    else:
        print(f"未知子命令：{sub}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()