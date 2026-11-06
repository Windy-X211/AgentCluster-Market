"""mcp_client 拓展后端 —— 连接外部 MCP 服务器并把工具注入智能体。

架构：
- ``MCPClientManager`` 在专用线程的事件循环里为每个服务器维护一条长连接
  （worker task，断线自动重连）。工具调用通过 ``run_coroutine_threadsafe``
  同步桥接，供同步的 ``_execute_tool`` 使用。
- 通过 ``core.external_tools.register_provider`` 把缓存的工具注入
  ``commands.filter_tools``；工具名统一为 ``mcp__<server>__<tool>``。
- 配置保存在本目录 ``servers.json``；保存后热重连，无需重启后端。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter()

_EXT_ID = "mcp_client"
_DIR = Path(__file__).resolve().parent
_CONFIG_FILE = _DIR / "servers.json"
_PAGE_FILE = _DIR / "config.html"
_PAGE_CACHE: tuple[float, str] | None = None

DEFAULT_OPTIONS = {
    "max_tools": 48,       # 注入智能体的工具上限（防止上下文爆炸）
    "call_timeout": 120,   # 单次工具调用默认超时（秒）
    "startup_wait": 3,     # 后端启动时等待首次连接的最长秒数
}


# ==============================================================
# MCP SDK 载入（仓库内置 backend/mcp_vendor）
# ==============================================================
_MCP_AVAILABLE = False
_MCP_ERROR = ""
httpx2 = None
Client = None
StdioServerParameters = None
streamable_http_client = None
sse_client = None


def _find_vendor() -> Path | None:
    for p in [_DIR, *list(_DIR.parents)]:
        cand = p / "backend" / "mcp_vendor"
        if cand.exists() and (cand / "mcp").exists():
            return cand
    return None


try:
    _vendor = _find_vendor()
    if _vendor and str(_vendor) not in sys.path:
        sys.path.append(str(_vendor))
    import httpx2 as _httpx2  # type: ignore
    from mcp import Client as _Client, StdioServerParameters as _StdioParams  # type: ignore
    from mcp.client.sse import sse_client as _sse_client  # type: ignore
    from mcp.client.streamable_http import streamable_http_client as _streamable_http_client  # type: ignore

    httpx2 = _httpx2
    Client = _Client
    StdioServerParameters = _StdioParams
    sse_client = _sse_client
    streamable_http_client = _streamable_http_client
    _MCP_AVAILABLE = True
except Exception as e:
    _MCP_ERROR = f"{type(e).__name__}: {e}"


# ==============================================================
# 配置
# ==============================================================
def _load_config() -> dict:
    cfg: dict = {}
    try:
        if _CONFIG_FILE.exists():
            cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[mcp_client] 读取 servers.json 失败：{e}")
    if not isinstance(cfg, dict):
        cfg = {}
    options = dict(DEFAULT_OPTIONS)
    options.update(cfg.get("options") or {})
    servers = cfg.get("servers")
    if not isinstance(servers, list):
        servers = []
    return {"options": options, "servers": servers}


def _save_config(cfg: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _CONFIG_FILE)


def _gen_id(name: str = "") -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "server").lower()).strip("_") or "server"
    return f"{base}_{int(time.time()) % 100000}"


def _safe_seg(s: str) -> str:
    v = re.sub(r"[^A-Za-z0-9_-]", "_", str(s or "")).strip("_")
    return v or "tool"


def _make_tool_name(server_id: str, tool_name: str) -> str:
    """生成 <=64 字符、符合 OpenAI 工具名规范的唯一名称。"""
    name = f"mcp__{_safe_seg(server_id)}__{_safe_seg(tool_name)}"
    if len(name) > 64:
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        name = name[:55] + "_" + digest
    return name


def _check_enabled() -> None:
    try:
        from api.extensions import is_extension_enabled
        if not is_extension_enabled(_EXT_ID):
            raise HTTPException(503, "拓展已停用")
    except HTTPException:
        raise
    except Exception:
        pass


# ==============================================================
# 连接管理器
# ==============================================================
class MCPClientManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._config: dict = {"options": dict(DEFAULT_OPTIONS), "servers": []}
        self._workers: dict[str, dict] = {}      # sid -> {"stop": Event, "task": Task}
        self._clients: dict[str, Any] = {}       # sid -> Client
        self._status: dict[str, dict] = {}       # sid -> 状态
        self._tools: list[dict] = []             # [{"schema", "server", "tool", "agents"}]
        self._tool_map: dict[str, tuple[str, str]] = {}  # full_name -> (sid, tool)
        self._rebuild_lock = threading.RLock()

    # ---------- 生命周期 ----------
    def start(self, wait: float = 3.0) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._config = _load_config()
        if not _MCP_AVAILABLE:
            print(f"[mcp_client] MCP SDK 不可用：{_MCP_ERROR}")
            return
        self._thread = threading.Thread(target=self._run, name="mcp-client-loop", daemon=True)
        self._thread.start()
        # 等待首次连接尝试（不阻塞主事件循环，本函数在 to_thread 中调用）
        deadline = time.time() + max(0.0, float(wait or 0))
        while time.time() < deadline:
            if self._any_connected():
                break
            time.sleep(0.1)

    def _any_connected(self) -> bool:
        with self._lock:
            return any(st.get("connected") for st in self._status.values())

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._rebuild())
            self._loop.run_forever()
        except Exception as e:
            print(f"[mcp_client] 事件循环异常：{e}")
        finally:
            try:
                self._loop.run_until_complete(self._stop_all())
            except Exception:
                pass
            try:
                self._loop.close()
            except Exception:
                pass

    def shutdown(self) -> None:
        with self._lock:
            self._started = False
        loop = self._loop
        if loop and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._stop_all(), loop).result(timeout=10)
            except Exception as e:
                print(f"[mcp_client] 关闭连接失败：{e}")
            loop.call_soon_threadsafe(loop.stop)

    # ---------- 连接 worker ----------
    async def _stop_all(self) -> None:
        for sid in list(self._workers.keys()):
            await self._stop_worker(sid)
        self._clients.clear()
        with self._lock:
            self._tools = []
            self._tool_map = {}

    async def _stop_worker(self, sid: str) -> None:
        w = self._workers.pop(sid, None)
        if not w:
            return
        w["stop"].set()
        task = w.get("task")
        if task:
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:
                pass
        self._clients.pop(sid, None)

    async def _rebuild(self) -> None:
        """按当前配置重建全部连接（先停旧 worker 再启新的）。"""
        for sid in list(self._workers.keys()):
            await self._stop_worker(sid)
        self._config = _load_config()
        with self._lock:
            self._tools = []
            self._tool_map = {}
            self._status = {}
        for cfg in self._config.get("servers") or []:
            sid = str(cfg.get("id") or _gen_id(cfg.get("name", "")))
            cfg["id"] = sid
            if not cfg.get("enabled", True):
                self._set_status(sid, connected=False, error="已停用", tool_count=0)
                continue
            stop_evt = threading.Event()
            task = asyncio.create_task(self._worker(cfg, stop_evt))
            self._workers[sid] = {"stop": stop_evt, "task": task}

    async def _worker(self, cfg: dict, stop_evt: threading.Event) -> None:
        sid = str(cfg.get("id"))
        backoff = 1.0
        while not stop_evt.is_set():
            try:
                await self._connect_and_serve(cfg, stop_evt)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._clients.pop(sid, None)
                self._set_status(sid, connected=False, error=f"{type(e).__name__}: {e}", tool_count=0)
                self._drop_tools(sid)
            if stop_evt.is_set():
                break
            # 退避重连，可被 stop 打断
            waited = 0.0
            while waited < backoff and not stop_evt.is_set():
                await asyncio.sleep(0.2)
                waited += 0.2
            backoff = min(backoff * 2, 15.0)

    @asynccontextmanager
    async def _open_client(self, cfg: dict):
        transport = (cfg.get("transport") or "stdio").lower()
        timeout = float(cfg.get("timeout") or DEFAULT_OPTIONS["call_timeout"])
        headers = cfg.get("headers") or None
        if transport == "stdio":
            params = StdioServerParameters(
                command=str(cfg.get("command") or ""),
                args=[str(a) for a in (cfg.get("args") or [])],
                env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()} or None,
                cwd=str(cfg.get("cwd")) if cfg.get("cwd") else None,
            )
            async with Client(params, read_timeout_seconds=timeout) as client:
                yield client
        elif transport == "sse":
            url = str(cfg.get("url") or "")
            async with Client(
                sse_client(
                    url,
                    headers=headers,
                    timeout=min(timeout, 30.0),
                    sse_read_timeout=max(timeout, 60.0),
                ),
                read_timeout_seconds=timeout,
            ) as client:
                yield client
        else:  # http（Streamable HTTP）
            url = str(cfg.get("url") or "")
            http_client = httpx2.AsyncClient(
                headers=headers,
                timeout=httpx2.Timeout(min(timeout, 30.0), read=timeout),
            )
            async with http_client:
                transport_cm = streamable_http_client(url, http_client=http_client)
                async with Client(transport_cm, read_timeout_seconds=timeout) as client:
                    yield client

    async def _connect_and_serve(self, cfg: dict, stop_evt: threading.Event) -> None:
        sid = str(cfg.get("id"))
        async with self._open_client(cfg) as client:
            self._clients[sid] = client
            tools = await asyncio.wait_for(client.list_tools(), timeout=min(
                float(cfg.get("timeout") or 60), 60.0))
            tool_list = list(getattr(tools, "tools", None) or [])
            self._register_tools(cfg, tool_list)
            sinfo = getattr(client, "server_info", None)
            info = {}
            if sinfo is not None:
                info = {
                    "name": getattr(sinfo, "name", ""),
                    "version": getattr(sinfo, "version", ""),
                }
            self._set_status(sid, connected=True, error="", tool_count=len(tool_list),
                             server_info=info)
            while not stop_evt.is_set():
                await asyncio.sleep(0.5)
        self._clients.pop(sid, None)

    def _register_tools(self, cfg: dict, tools: list) -> None:
        sid = str(cfg.get("id"))
        agents = cfg.get("agents", "all")
        new_entries: list[dict] = []
        for tool in tools:
            tname = str(getattr(tool, "name", "") or "")
            if not tname:
                continue
            schema = getattr(tool, "inputSchema", None)
            if schema is None:
                schema = getattr(tool, "input_schema", None)
            if hasattr(schema, "model_dump"):
                try:
                    schema = schema.model_dump()
                except Exception:
                    schema = None
            if not isinstance(schema, dict) or not schema:
                schema = {"type": "object", "properties": {}}
            full = _make_tool_name(sid, tname)
            desc = str(getattr(tool, "description", "") or "").strip()
            server_name = str(cfg.get("name") or sid)
            new_entries.append({
                "schema": {
                    "type": "function",
                    "function": {
                        "name": full,
                        "description": f"[MCP · {server_name}] {desc or tname}".strip(),
                        "parameters": schema,
                    },
                },
                "server": sid,
                "tool": tname,
                "agents": agents,
            })
        with self._lock:
            self._tools = [t for t in self._tools if t["server"] != sid] + new_entries
            for full, (s, _t) in list(self._tool_map.items()):
                if s == sid:
                    self._tool_map.pop(full, None)
            for e in new_entries:
                self._tool_map[e["schema"]["function"]["name"]] = (sid, e["tool"])

    def _drop_tools(self, sid: str) -> None:
        with self._lock:
            self._tools = [t for t in self._tools if t["server"] != sid]
            for full, (s, _t) in list(self._tool_map.items()):
                if s == sid:
                    self._tool_map.pop(full, None)

    def _set_status(self, sid: str, **kwargs: Any) -> None:
        with self._lock:
            st = dict(self._status.get(sid) or {})
            st.update(kwargs)
            st["updated_at"] = datetime.utcnow().isoformat()
            self._status[sid] = st

    # ---------- 对外查询/调用 ----------
    def tools_for(self, agent: dict | None = None) -> list[dict]:
        with self._lock:
            snapshot = list(self._tools)
            max_tools = int((self._config.get("options") or {}).get("max_tools")
                            or DEFAULT_OPTIONS["max_tools"])
        out: list[dict] = []
        for item in snapshot:
            agents = item.get("agents", "all")
            if agent is not None and agents != "all":
                aid = agent.get("id")
                try:
                    ids = [int(x) for x in agents]
                except Exception:
                    ids = []
                if aid not in ids:
                    continue
            out.append(item["schema"])
            if len(out) >= max_tools:
                break
        return out

    def tool_list(self) -> list[dict]:
        with self._lock:
            return [{"name": t["schema"]["function"]["name"],
                     "server": t["server"],
                     "tool": t["tool"],
                     "description": t["schema"]["function"].get("description", "")}
                    for t in self._tools]

    def status(self) -> dict:
        cfg = _load_config()
        with self._lock:
            return {
                "servers": [dict(self._status.get(str(s.get("id")), {}) | {
                    "id": s.get("id"),
                    "name": s.get("name", ""),
                    "enabled": s.get("enabled", True),
                }) for s in (cfg.get("servers") or [])],
                "connected": sum(1 for st in self._status.values() if st.get("connected")),
                "tool_count": len(self._tools),
            }

    def call(self, full_name: str, arguments: dict | None) -> dict:
        if not _MCP_AVAILABLE:
            return {"error": f"MCP SDK 不可用：{_MCP_ERROR}"}
        with self._lock:
            info = self._tool_map.get(full_name)
            opts = dict(self._config.get("options") or {})
        if not info:
            return {"error": f"未知 MCP 工具：{full_name}（可能服务器已断开，请刷新配置）"}
        sid, tool_name = info
        client = self._clients.get(sid)
        if client is None:
            return {"error": f"MCP 服务器未连接：{sid}"}
        timeout = float(opts.get("call_timeout") or DEFAULT_OPTIONS["call_timeout"])
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"error": "MCP 客户端事件循环未运行"}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._acall(client, sid, tool_name, arguments or {}, timeout), loop)
            return fut.result(timeout=timeout + 10)
        except Exception as e:
            return {"error": f"调用 MCP 工具失败：{type(e).__name__}: {e}"}

    async def _acall(self, client: Any, sid: str, tool_name: str, arguments: dict,
                     timeout: float) -> dict:
        res = await asyncio.wait_for(client.call_tool(tool_name, arguments), timeout=timeout)
        parts: list[str] = []
        for item in (getattr(res, "content", None) or []):
            ctype = getattr(item, "type", "")
            if ctype == "text":
                parts.append(str(getattr(item, "text", "")))
            elif ctype in ("image", "audio"):
                parts.append(f"[{ctype} content omitted]")
            else:
                parts.append(str(item))
        return {
            "ok": not bool(getattr(res, "is_error", False)),
            "text": "\n".join(p for p in parts if p),
            "structured": getattr(res, "structured_content", None),
            "server": sid,
            "tool": tool_name,
        }

    # ---------- 热重连 / 测试 ----------
    def refresh(self, timeout: float = 20.0) -> bool:
        loop = self._loop
        if loop is None or not loop.is_running():
            return False
        try:
            asyncio.run_coroutine_threadsafe(self._rebuild(), loop).result(timeout=timeout)
            return True
        except Exception as e:
            print(f"[mcp_client] 重连失败：{e}")
            return False

    def test_server(self, cfg: dict, timeout: float = 30.0) -> dict:
        if not _MCP_AVAILABLE:
            return {"ok": False, "error": f"MCP SDK 不可用：{_MCP_ERROR}"}
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"ok": False, "error": "MCP 客户端事件循环未运行"}
        try:
            return asyncio.run_coroutine_threadsafe(
                self._test_server(cfg), loop).result(timeout=timeout)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def _test_server(self, cfg: dict) -> dict:
        try:
            async with self._open_client(cfg) as client:
                tools = await asyncio.wait_for(
                    client.list_tools(), timeout=float(cfg.get("timeout") or 30))
                names = [str(getattr(t, "name", "")) for t in (getattr(tools, "tools", None) or [])]
                sinfo = getattr(client, "server_info", None)
                return {
                    "ok": True,
                    "tool_count": len(names),
                    "tools": names,
                    "server_info": ({
                        "name": getattr(sinfo, "name", ""),
                        "version": getattr(sinfo, "version", ""),
                    } if sinfo is not None else {}),
                }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


manager = MCPClientManager()


# ==============================================================
# 外部工具提供者注册（注入智能体 function calling）
# ==============================================================
def _provider_tools(agent: dict | None = None) -> list[dict]:
    try:
        from api.extensions import is_extension_enabled
        if not is_extension_enabled(_EXT_ID):
            return []
    except Exception:
        pass
    if not _MCP_AVAILABLE:
        return []
    return manager.tools_for(agent)


def _provider_call(tool_name: str, arguments: dict | None, agent: dict | None = None) -> dict:
    try:
        from api.extensions import is_extension_enabled
        if not is_extension_enabled(_EXT_ID):
            return {"error": "MCP 客户端拓展已停用"}
    except Exception:
        pass
    return manager.call(tool_name, arguments or {})


try:
    from core.external_tools import register_provider
    register_provider(
        "mcp_client",
        tools_fn=_provider_tools,
        call_fn=_provider_call,
        prefix="mcp__",
        label="MCP 客户端",
    )
except Exception as e:
    print(f"[mcp_client] 注册外部工具提供者失败：{e}")


# ==============================================================
# 生命周期
# ==============================================================
async def on_mount(app: Any) -> None:
    cfg = _load_config()
    wait = float((cfg.get("options") or {}).get("startup_wait") or DEFAULT_OPTIONS["startup_wait"])
    await asyncio.to_thread(manager.start, wait)
    if _MCP_AVAILABLE:
        print(f"[mcp_client] 已启动，配置 {len(cfg.get('servers') or [])} 个 MCP 服务器")
    else:
        print(f"[mcp_client] MCP SDK 不可用：{_MCP_ERROR}")


async def on_unmount() -> None:
    await asyncio.to_thread(manager.shutdown)


# ==============================================================
# 配置页面 / API
# ==============================================================
def _page_html() -> str:
    global _PAGE_CACHE
    if not _PAGE_FILE.exists():
        return "<html><body style='background:#0f1626;color:#ddd;font-family:sans-serif;padding:24px'>config.html 缺失</body></html>"
    try:
        mtime = _PAGE_FILE.stat().st_mtime
        if _PAGE_CACHE and _PAGE_CACHE[0] == mtime:
            return _PAGE_CACHE[1]
        html = _PAGE_FILE.read_text(encoding="utf-8")
        _PAGE_CACHE = (mtime, html)
        return html
    except Exception as e:
        return f"<html><body style='background:#0f1626;color:#ddd'>读取配置页失败：{e}</body></html>"


def _normalize_server(payload: dict) -> dict:
    sid = str(payload.get("id") or "").strip() or _gen_id(payload.get("name", ""))
    transport = str(payload.get("transport") or "stdio").lower()
    if transport not in ("stdio", "http", "sse"):
        raise HTTPException(400, f"不支持的传输方式：{transport}")
    args = payload.get("args")
    if isinstance(args, str):
        args = [a for a in re.split(r"\s+", args.strip()) if a]
    env = payload.get("env")
    if isinstance(env, str):
        try:
            env = json.loads(env) if env.strip() else {}
        except Exception:
            raise HTTPException(400, "env 必须是 JSON 对象")
    headers = payload.get("headers")
    if isinstance(headers, str):
        try:
            headers = json.loads(headers) if headers.strip() else {}
        except Exception:
            raise HTTPException(400, "headers 必须是 JSON 对象")
    agents = payload.get("agents", "all")
    if agents not in (None, "", "all"):
        if isinstance(agents, str):
            agents = [int(x) for x in re.split(r"[,\s]+", agents.strip()) if x.strip().isdigit()]
        elif isinstance(agents, list):
            agents = [int(x) for x in agents]
    else:
        agents = "all"
    return {
        "id": sid,
        "name": str(payload.get("name") or sid),
        "enabled": bool(payload.get("enabled", True)),
        "transport": transport,
        "command": str(payload.get("command") or ""),
        "args": [str(a) for a in (args or [])],
        "cwd": str(payload.get("cwd") or ""),
        "env": env or {},
        "url": str(payload.get("url") or ""),
        "headers": headers or {},
        "timeout": int(payload.get("timeout") or DEFAULT_OPTIONS["call_timeout"]),
        "agents": agents,
    }


@router.get("/config", response_class=HTMLResponse)
def config_page():
    _check_enabled()
    return HTMLResponse(_page_html())


@router.get("/info")
def info():
    _check_enabled()
    cfg = _load_config()
    return {
        "ok": True,
        "id": _EXT_ID,
        "mcp_available": _MCP_AVAILABLE,
        "mcp_error": _MCP_ERROR,
        "options": cfg.get("options"),
        "servers": cfg.get("servers"),
        "status": manager.status(),
        "tools": manager.tool_list(),
        "dirty": _MCP_AVAILABLE and not manager.status().get("connected") and bool(cfg.get("servers")),
    }


@router.post("/servers")
def save_server(payload: dict):
    _check_enabled()
    server = _normalize_server(payload or {})
    cfg = _load_config()
    servers = [s for s in cfg.get("servers") or [] if str(s.get("id")) != server["id"]]
    servers.append(server)
    cfg["servers"] = servers
    if payload.get("options") is not None:
        opts = dict(DEFAULT_OPTIONS)
        opts.update(payload.get("options") or {})
        cfg["options"] = opts
    _save_config(cfg)
    refreshed = manager.refresh()
    return {"ok": True, "server": server, "refreshed": refreshed, "status": manager.status()}


@router.delete("/servers/{server_id}")
def delete_server(server_id: str):
    _check_enabled()
    cfg = _load_config()
    servers = [s for s in cfg.get("servers") or [] if str(s.get("id")) != server_id]
    cfg["servers"] = servers
    _save_config(cfg)
    refreshed = manager.refresh()
    return {"ok": True, "refreshed": refreshed, "status": manager.status()}


@router.post("/servers/{server_id}/test")
def test_server(server_id: str):
    _check_enabled()
    cfg = _load_config()
    target = next((s for s in cfg.get("servers") or [] if str(s.get("id")) == server_id), None)
    if not target:
        raise HTTPException(404, f"服务器不存在：{server_id}")
    return manager.test_server(target)


@router.post("/servers/test")
def test_server_inline(payload: dict):
    """测试尚未保存的服务器配置。"""
    _check_enabled()
    return manager.test_server(_normalize_server(payload or {}))


@router.post("/refresh")
def refresh():
    _check_enabled()
    cfg = _load_config()
    ok = manager.refresh()
    return {"ok": ok, "status": manager.status(), "servers": cfg.get("servers")}


@router.post("/options")
def save_options(payload: dict):
    _check_enabled()
    cfg = _load_config()
    opts = dict(DEFAULT_OPTIONS)
    opts.update(cfg.get("options") or {})
    for k in ("max_tools", "call_timeout", "startup_wait"):
        if k in (payload or {}):
            opts[k] = int(payload[k])
    cfg["options"] = opts
    _save_config(cfg)
    manager.refresh()
    return {"ok": True, "options": opts}


@router.post("/call")
def call_tool(payload: dict):
    """调试接口：直接调用一个已连接的 MCP 工具。"""
    _check_enabled()
    name = (payload or {}).get("tool") or (payload or {}).get("name")
    if not name:
        raise HTTPException(400, "缺少 tool")
    return manager.call(str(name), (payload or {}).get("arguments") or {})