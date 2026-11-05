"""mcp_server 拓展后端 —— 把 AgentCluster 暴露为 MCP 服务端。

挂载点（由 api/extensions.mount_extension_backends 调用 on_mount）：
  Streamable HTTP：/api/ext/mcp_server/mcp    （Bearer Token 鉴权）
  stdio：由外部客户端启动同目录 mcp_stdio.py  （无需 Token）

设计说明：
- MCP SDK 2.x 使用 ``mcp.server.mcpserver.MCPServer``（FastMCP 已更名）。
- 工具实现直接复用本程序核心（core.store / core.run_registry），不依赖
  external_control 拓展，两个拓展可独立安装。
- Streamable HTTP 子应用需要运行自己的 lifespan（session manager），因此由
  ``on_mount`` 手动进入其 lifespan，``on_unmount`` 关闭。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import secrets
import sys
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

_EXT_ID = "mcp_server"
_DIR = Path(__file__).resolve().parent
_SETTINGS_FILE = _DIR / "settings.json"
_TOKEN_FILE = _DIR / "token.txt"
_PAGE_FILE = _DIR / "config.html"

_PAGE_CACHE: tuple[float, str] | None = None

# —— MCP SDK 载入（仓库内置 backend/mcp_vendor，系统已装 mcp 时优先系统包） ——
_MCP_AVAILABLE = False
_MCP_ERROR = ""
_mcp = None


def _find_vendor() -> Path | None:
    for p in [_DIR, *list(_DIR.parents)]:
        cand = p / "backend" / "mcp_vendor"
        if cand.exists() and (cand / "mcp").exists():
            return cand
    return None


def _find_backend_root() -> Path | None:
    for p in [_DIR, *list(_DIR.parents)]:
        cand = p / "backend"
        if (cand / "main.py").exists():
            return cand
    return None


try:
    _vendor = _find_vendor()
    if _vendor and str(_vendor) not in sys.path:
        # 追加到末尾：系统已安装且满足版本要求的包优先，vendored 只补齐缺失依赖
        sys.path.append(str(_vendor))
    from mcp.server.mcpserver import MCPServer  # type: ignore

    _MCP_AVAILABLE = True
except Exception as e:  # 缺少 mcp 依赖时不让整个后端启动失败
    _MCP_ERROR = f"{type(e).__name__}: {e}"
    MCPServer = None  # type: ignore


# ==============================================================
# 配置与 Token
# ==============================================================
def _read_settings() -> dict:
    try:
        if _SETTINGS_FILE.exists():
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[mcp_server] 读取 settings.json 失败：{e}")
    return {}


def _write_settings(data: dict) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _SETTINGS_FILE)


def _allow_write() -> bool:
    return bool(_read_settings().get("allow_write", False))


def _get_token() -> str:
    if _TOKEN_FILE.exists():
        tok = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def _reset_token() -> str:
    tok = secrets.token_urlsafe(32)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def _check_enabled() -> None:
    try:
        from api.extensions import is_extension_enabled
        if not is_extension_enabled(_EXT_ID):
            raise HTTPException(503, "拓展已停用")
    except HTTPException:
        raise
    except Exception:
        pass


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


# ==============================================================
# 工具实现（返回 JSON 字符串；统一 ok/error 包装）
# ==============================================================
def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str)


def _err(msg: Any) -> str:
    return json.dumps({"ok": False, "error": str(msg)}, ensure_ascii=False)


def _require_write() -> None:
    if not _allow_write():
        raise PermissionError(
            "写操作未开启。请在「功能拓展 → MCP 服务端 → 配置」中开启 allow_write 后重试。"
        )


def _tool_system_info() -> str:
    try:
        from core import run_registry
        from core.store import load
        return _ok({
            "service": "AgentCluster MCP Server",
            "version": "1.0.0",
            "time": datetime.utcnow().isoformat(),
            "counts": {
                "agents": len(load("agents") or []),
                "canvases": len(load("canvases") or []),
                "groups": len(load("agent_groups") or []),
                "conversations": len(load("conversations") or []),
            },
            "running": run_registry.list_running(),
        })
    except Exception as e:
        return _err(e)


def _tool_list_agents() -> str:
    try:
        from core.store import load
        agents = load("agents") or []
        # 精简字段：去掉 system_prompt 大正文，避免外部客户端上下文爆炸
        brief = []
        for a in agents:
            brief.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "description": a.get("description", ""),
                "model": a.get("model", ""),
                "commands": a.get("commands", []),
                "skills": a.get("skills", []),
                "plugins": a.get("plugins", []),
                "enabled": a.get("enabled", True),
                "group_id": a.get("group_id"),
            })
        return _ok(brief)
    except Exception as e:
        return _err(e)


def _tool_get_agent(agent_id: int) -> str:
    try:
        from core.store import get_by
        a = get_by("agents", int(agent_id))
        if not a:
            return _err(f"智能体不存在：{agent_id}")
        return _ok(a)
    except Exception as e:
        return _err(e)


def _tool_list_groups() -> str:
    try:
        from core.store import load
        return _ok(load("agent_groups") or [])
    except Exception as e:
        return _err(e)


def _tool_list_canvases() -> str:
    try:
        from core.store import load
        return _ok(load("canvases") or [])
    except Exception as e:
        return _err(e)


def _tool_get_canvas(canvas_id: int) -> str:
    try:
        from core.store import get_by
        c = get_by("canvases", int(canvas_id))
        if not c:
            return _err(f"画布不存在：{canvas_id}")
        return _ok(c)
    except Exception as e:
        return _err(e)


def _tool_list_conversations() -> str:
    try:
        from core.store import load_list_payloads

        def _key(c: dict):
            last_ts = None
            for m in c.get("messages") or []:
                ts = m.get("ts")
                if ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
            return last_ts or c.get("created_at") or f"{c.get('id', 0):020d}"

        entries = load_list_payloads("conversations", _key)
        entries.sort(key=lambda e: e[0], reverse=True)
        out = []
        for _, raw in entries:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            msgs = obj.pop("messages", None) or []
            obj["message_count"] = len(msgs)
            if msgs:
                obj["last_role"] = msgs[-1].get("role")
                obj["last_agent_id"] = msgs[-1].get("agent_id")
                obj["last_ts"] = msgs[-1].get("ts")
                content = msgs[-1].get("content") or ""
                obj["last_preview"] = content[:80].replace("\n", " ") if isinstance(content, str) else ""
            out.append(obj)
        return _ok(out)
    except Exception as e:
        return _err(e)


def _tool_get_conversation(conversation_id: int) -> str:
    try:
        from core.store import get_by
        c = get_by("conversations", int(conversation_id))
        if not c:
            return _err(f"会话不存在：{conversation_id}")
        return _ok(c)
    except Exception as e:
        return _err(e)


def _tool_create_conversation(name: str = "", agent_id: int = 0, agent_ids: list[int] | None = None,
                              canvas_id: int = 0, workdir: str = "", execution_mode: str = "") -> str:
    try:
        from core.store import upsert, next_id
        ids = list(agent_ids or [])
        if agent_id:
            ids.append(int(agent_id))
        # 去重保序
        ids = list(dict.fromkeys(ids))
        item = {
            "name": name or "MCP 新对话",
            "mode": "independent",
            "agent_ids": ids,
            "canvas_id": int(canvas_id) or None,
            "workdir": workdir or None,
            "execution_mode": execution_mode or None,
            "messages": [],
        }
        item["id"] = next_id("conversations")
        item["created_at"] = datetime.utcnow().isoformat()
        return _ok(upsert("conversations", item))
    except Exception as e:
        return _err(e)


async def _tool_send_message(conversation_id: int, agent_id: int, content: str) -> str:
    try:
        from core import run_registry
        from core.store import get_by, upsert
        cid, aid = int(conversation_id), int(agent_id)
        if not content:
            return _err("content 不能为空")
        conv = run_registry.peek_live_conv(cid) or get_by("conversations", cid)
        if not conv:
            return _err(f"会话不存在：{cid}")
        agent = get_by("agents", aid)
        if not agent:
            return _err(f"智能体不存在：{aid}")
        if run_registry.is_running(cid, aid):
            return _err("该智能体正在运行中，请先 stop_agent 或稍后再试")
        canvas = get_by("canvases", conv.get("canvas_id")) if conv.get("canvas_id") else None
        if canvas is not None and conv.get("execution_mode"):
            canvas = {**canvas, "execution_mode": conv["execution_mode"]}
        mode = "linked" if conv.get("canvas_id") else "independent"
        conv.setdefault("messages", []).append({
            "role": "user",
            "content": content,
            "ts": datetime.utcnow().isoformat(),
            "to_agent_id": aid,
        })
        upsert("conversations", conv)
        session = run_registry.start(cid, agent, content, mode, canvas,
                                     base_conv=conv, workdir=conv.get("workdir"))
        events: list[dict] = []
        text_parts: list[str] = []
        async for line in session.subscribe(0):
            stripped = line.rstrip("\n")
            events.append({"seq": len(events) + 1, "data": stripped})
            if stripped == "DONE":
                break
            if stripped.startswith("DATA:"):
                text_parts.append(stripped[5:])
            elif stripped and not stripped.endswith((":END", ":START")) and ":" not in stripped[:10]:
                text_parts.append(stripped)
        return _ok({
            "conv_id": cid,
            "agent_id": aid,
            "text": "".join(text_parts),
            "events": events,
            "event_count": len(events),
        })
    except Exception as e:
        return _err(e)


def _tool_stop_agent(conversation_id: int, agent_id: int = 0) -> str:
    try:
        from core import run_registry
        stopped = run_registry.stop(int(conversation_id), int(agent_id) if agent_id else None)
        return _ok({"stopped": stopped})
    except Exception as e:
        return _err(e)


def _tool_list_running(conversation_id: int = 0) -> str:
    try:
        from core import run_registry
        if conversation_id:
            runs = run_registry.runs_of(int(conversation_id))
            running = [s.describe() for s in runs if s.running]
            return _ok({"conv_id": int(conversation_id), "running": bool(running), "runs": running})
        return _ok(run_registry.list_running())
    except Exception as e:
        return _err(e)


def _tool_list_capabilities() -> str:
    try:
        from core.commands import COMMAND_META
        from plugins import list_plugins
        commands = [{"name": k, "label": v.get("label", k), "params": v.get("params", [])}
                    for k, v in COMMAND_META.items()]
        plugins = [{"name": k, **{kk: vv for kk, vv in v.items() if kk in ("label", "description", "tags")}}
                   for k, v in list_plugins().items()]
        skills: list[dict] = []
        try:
            from skills import list_skills
            for k, v in (list_skills() or {}).items():
                skills.append({"name": k, "description": v.get("description", "")})
        except Exception:
            pass
        return _ok({"commands": commands, "plugins": plugins, "skills": skills})
    except Exception as e:
        return _err(e)


# —— 写操作（默认关闭） ——
def _tool_create_agent(name: str, system_prompt: str = "", description: str = "",
                       model: str = "", commands: list[str] | None = None,
                       skills: list[str] | None = None, plugins: list[str] | None = None) -> str:
    try:
        _require_write()
        from core.store import upsert, next_id
        if not name:
            return _err("name 不能为空")
        item = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "model": model,
            "interface_id": None,
            "reasoning_level": "off",
            "commands": commands or [],
            "skills": skills or [],
            "plugins": plugins or [],
            "trigger": {"type": "manual", "cron": "", "message": ""},
            "tags": [],
            "max_tool_rounds": None,
            "enabled": True,
            "perception_scope": "link",
            "workflow_mode": "none",
            "group_id": None,
        }
        item["id"] = next_id("agents")
        item["created_at"] = datetime.utcnow().isoformat()
        return _ok(upsert("agents", item))
    except Exception as e:
        return _err(e)


def _tool_update_agent(agent_id: int, name: str = "", system_prompt: str = "",
                       description: str = "", model: str = "",
                       commands: list[str] | None = None, skills: list[str] | None = None,
                       plugins: list[str] | None = None) -> str:
    try:
        _require_write()
        from core.store import upsert, get_by
        cur = get_by("agents", int(agent_id))
        if not cur:
            return _err(f"智能体不存在：{agent_id}")
        if name:
            cur["name"] = name
        if description:
            cur["description"] = description
        if system_prompt:
            cur["system_prompt"] = system_prompt
        if model:
            cur["model"] = model
        if commands is not None:
            cur["commands"] = commands
        if skills is not None:
            cur["skills"] = skills
        if plugins is not None:
            cur["plugins"] = plugins
        cur["id"] = int(agent_id)
        return _ok(upsert("agents", cur))
    except Exception as e:
        return _err(e)


def _tool_delete_agent(agent_id: int) -> str:
    try:
        _require_write()
        from core.store import delete_by, get_by, load, upsert
        aid = int(agent_id)
        if not get_by("agents", aid):
            return _err(f"智能体不存在：{agent_id}")
        for c in (load("canvases") or []):
            nodes = [n for n in c.get("nodes", []) if n.get("agent_id") != aid]
            if len(nodes) != len(c.get("nodes", [])):
                node_ids = {n.get("id") for n in nodes}
                c["nodes"] = nodes
                c["edges"] = [e for e in c.get("edges", [])
                              if e.get("from_node") in node_ids and e.get("to_node") in node_ids]
                upsert("canvases", c)
        return _ok({"deleted": delete_by("agents", aid)})
    except Exception as e:
        return _err(e)


_TOOL_IMPLS = [
    ("system_info", "系统信息：智能体/画布/分组/会话数量与运行中会话", _tool_system_info),
    ("list_capabilities", "列出本程序可用的命令、技能、插件（创建智能体时用于挑选能力）", _tool_list_capabilities),
    ("list_agents", "列出所有智能体（不含 system_prompt 大正文）", _tool_list_agents),
    ("get_agent", "获取单个智能体的完整信息，参数 agent_id", _tool_get_agent),
    ("list_groups", "列出智能体分组", _tool_list_groups),
    ("list_canvases", "列出所有画布（智能体团队）", _tool_list_canvases),
    ("get_canvas", "获取画布详情，参数 canvas_id", _tool_get_canvas),
    ("list_conversations", "列出所有会话（仅元数据与最后一条摘要，不含完整消息）", _tool_list_conversations),
    ("get_conversation", "获取会话完整消息，参数 conversation_id", _tool_get_conversation),
    ("create_conversation", "新建会话；可指定 agent_id/agent_ids、canvas_id、workdir、execution_mode", _tool_create_conversation),
    ("send_message", "向会话中的智能体发消息并阻塞至运行结束，返回最终文本与事件；参数 conversation_id、agent_id、content", _tool_send_message),
    ("stop_agent", "停止会话中智能体的运行；参数 conversation_id、agent_id（可选）", _tool_stop_agent),
    ("list_running", "查询运行状态；参数 conversation_id（可选，0 表示全部）", _tool_list_running),
    ("create_agent", "【写操作，需开启 allow_write】创建智能体", _tool_create_agent),
    ("update_agent", "【写操作，需开启 allow_write】更新智能体，参数 agent_id 与要修改的字段", _tool_update_agent),
    ("delete_agent", "【写操作，需开启 allow_write】删除智能体并从画布移除，参数 agent_id", _tool_delete_agent),
]


# ==============================================================
# MCPServer 实例与 HTTP 子应用
# ==============================================================
_MCP_INSTRUCTIONS = """你是 AgentCluster 的 MCP 服务端。
本程序是一个多智能体集群运行环境：智能体（agent）可以配置命令/技能/插件，画布（canvas）把多个智能体连成协作团队，会话（conversation）承载运行记录。
使用建议：
1. 先调用 system_info / list_agents / list_canvases 了解现状；
2. 需要新建智能体时先 list_capabilities 挑选 commands/skills/plugins；
3. 发起对话：create_conversation → send_message（阻塞返回最终文本）；
4. 写操作（create_agent/update_agent/delete_agent）默认关闭，需要用户在拓展配置页开启 allow_write。"""

if _MCP_AVAILABLE:
    _mcp = MCPServer(
        name="agentcluster",
        title="AgentCluster MCP Server",
        description="多智能体集群运行环境的 MCP 服务端",
        version="1.0.0",
        instructions=_MCP_INSTRUCTIONS,
    )
    for _name, _desc, _fn in _TOOL_IMPLS:
        try:
            _mcp.add_tool(_fn, name=_name, description=_desc, structured_output=False)
        except Exception as _e:
            print(f"[mcp_server] 注册工具 {_name} 失败：{_e}")

_SUB_APP: Any = None
_STACK: AsyncExitStack | None = None


class _TokenGuard:
    """Streamable HTTP 层的 Bearer Token 校验（MCP SDK 的 OAuth 鉴权过重，这里做轻量校验）。"""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        provided = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if not provided:
            provided = headers.get("x-mcp-token", "").strip()
        if not provided:
            try:
                qs = scope.get("query_string", b"").decode("latin-1")
                for part in qs.split("&"):
                    if part.startswith("token="):
                        provided = part[6:]
                        break
            except Exception:
                pass
        if not provided or not secrets.compare_digest(provided, _get_token()):
            body = json.dumps({"error": "unauthorized", "detail": "需要 Authorization: Bearer <token>"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"www-authenticate", b'Bearer realm="agentcluster-mcp"'),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


async def on_mount(app: Any) -> None:
    """托管 Streamable HTTP 子应用（子应用 lifespan 需手动进入）。"""
    global _SUB_APP, _STACK
    if not _MCP_AVAILABLE:
        print(f"[mcp_server] MCP SDK 不可用，跳过 HTTP 挂载：{_MCP_ERROR}")
        return
    _SUB_APP = _mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)
    _STACK = AsyncExitStack()
    await _STACK.enter_async_context(_SUB_APP.router.lifespan_context(_SUB_APP))
    app.mount(f"/api/ext/{_EXT_ID}", _TokenGuard(_SUB_APP), name=f"{_EXT_ID}_mcp")
    print(f"[mcp_server] Streamable HTTP 已挂载：/api/ext/{_EXT_ID}/mcp（Bearer Token 鉴权）")


async def on_unmount() -> None:
    global _STACK
    if _STACK is not None:
        try:
            await _STACK.aclose()
        finally:
            _STACK = None


# ==============================================================
# 配置/信息路由
# ==============================================================
@router.get("/config", response_class=HTMLResponse)
def config_page():
    _check_enabled()
    return HTMLResponse(_page_html())


@router.get("/info")
def info(request: Request):
    _check_enabled()
    token = _get_token()
    base = str(request.base_url).rstrip("/")
    return {
        "ok": True,
        "id": _EXT_ID,
        "mcp_available": _MCP_AVAILABLE,
        "mcp_error": _MCP_ERROR,
        "http_url": f"{base}/api/ext/{_EXT_ID}/mcp",
        "token": token,
        "allow_write": _allow_write(),
        "tools": [{"name": n, "description": d} for n, d, _ in _TOOL_IMPLS],
        "stdio": _stdio_config(),
    }


def _stdio_config() -> dict:
    backend_root = _find_backend_root()
    py = sys.executable or "python"
    return {
        "command": py,
        "args": [str(_DIR / "mcp_stdio.py")],
        "cwd": str(backend_root) if backend_root else "",
    }


@router.post("/settings")
def update_settings(payload: dict):
    _check_enabled()
    data = _read_settings()
    if "allow_write" in payload:
        data["allow_write"] = bool(payload["allow_write"])
    _write_settings(data)
    return {"ok": True, "allow_write": _allow_write()}


@router.post("/token/reset")
def reset_token():
    _check_enabled()
    return {"ok": True, "token": _reset_token()}