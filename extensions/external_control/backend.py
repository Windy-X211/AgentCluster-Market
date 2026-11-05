"""外部 AI 控制 · 后端服务。

让 DeepSeek Harness、Codex、Claude Code 等外部 AI 程序通过 HTTP/WebSocket
连接本程序，发出结构化命令控制智能体团队与对话。

由 ``api.extensions.mount_extension_backends`` 在启动时动态加载，
路由挂载于 ``/api/ext/external_control/`` 前缀。

端点：
- ``GET  /info``                      → 服务信息 + 可用命令清单 + 鉴权方式
- ``GET  /token``                     → 查看当前 token（需已认证）
- ``POST /token/reset``               → 重置 token（需已认证）
- ``POST /command``                   → 统一命令入口 ``{cmd, args, token?}``
- ``WS   /ws/events/{cid}``           → WebSocket 实时订阅会话事件流
- ``GET  /events/{cid}``              → SSE 风格事件流（非 WS 客户端备用）

鉴权：Bearer Token（首次自动生成，存本拓展目录 ``token.txt``）。
请求头 ``Authorization: Bearer <token>``、body ``token`` 字段、query ``?token=`` 任一即可。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()

_EXT_ID = "external_control"
_DIR = Path(__file__).resolve().parent
_TOKEN_FILE = _DIR / "token.txt"

# —— 拓展启用自检 ——
def _check_enabled() -> None:
    try:
        from api.extensions import is_extension_enabled
    except ImportError:
        return
    if not is_extension_enabled(_EXT_ID):
        raise HTTPException(503, "拓展已停用")


# —— Token 鉴权 ——
def _get_token() -> str:
    """读取或首次生成 token。"""
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    tok = secrets.token_urlsafe(32)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def _verify_token(req: Request | None = None, body_token: str | None = None,
                  query_token: str | None = None) -> None:
    """校验 token：Authorization 头 / body / query 任一匹配即可。"""
    _check_enabled()
    expected = _get_token()
    provided: str | None = None
    if req is not None:
        auth = req.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if not provided and body_token:
        provided = body_token
    if not provided and query_token:
        provided = query_token
    if not provided:
        raise HTTPException(401, "未提供 token。请用 Authorization: Bearer <token> 头、body token 字段或 ?token= 传入。")
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(403, "token 无效")


# —— 命令清单 ——
_COMMANDS: dict[str, str] = {
    "system.info": "系统信息（版本、智能体数、画布数、运行中会话）",
    "agents.list": "列出所有智能体",
    "agents.create": "创建智能体 {name, system_prompt?, model?, ...}",
    "agents.update": "更新智能体 {id, ...fields}",
    "agents.delete": "删除智能体 {id}",
    "groups.list": "列出智能体分组",
    "groups.create": "创建分组 {name}",
    "groups.delete": "删除分组 {id}",
    "canvases.list": "列出所有画布（团队）",
    "canvases.create": "创建画布 {name, nodes?, edges?, workdir?, execution_mode?}",
    "canvases.get": "获取画布详情 {id}",
    "canvases.delete": "删除画布 {id}",
    "conversations.list": "列出所有会话（剥离 messages）",
    "conversations.create": "创建会话 {name?, agent_ids?, canvas_id?, workdir?}",
    "conversations.get": "获取会话完整消息 {id}",
    "conversations.delete": "删除会话 {id}",
    "conversations.send": "发消息给智能体 {id, agent_id, content} → 阻塞聚合返回最终文本+事件",
    "conversations.stop": "停止运行 {id, agent_id?}",
    "conversations.running": "查询运行状态 {id}",
}


# —— 命令实现 ——
def _cmd_system_info(args: dict) -> dict:
    from core.store import load
    from core import run_registry
    agents = load("agents") or []
    canvases = load("canvases") or []
    groups = load("agent_groups") or []
    return {
        "service": "AgentCluster External Control",
        "version": "1.0.0",
        "time": datetime.utcnow().isoformat(),
        "counts": {
            "agents": len(agents),
            "canvases": len(canvases),
            "groups": len(groups),
        },
        "running": run_registry.list_running(),
    }


def _cmd_agents_list(args: dict) -> dict:
    from core.store import load
    return load("agents") or []


def _cmd_agents_create(args: dict) -> dict:
    from core.store import upsert, next_id
    required = {"name"}
    missing = required - set(args)
    if missing:
        raise HTTPException(400, f"缺少必填字段：{missing}")
    item = {
        "name": args["name"],
        "description": args.get("description", ""),
        "system_prompt": args.get("system_prompt", ""),
        "model": args.get("model", ""),
        "interface_id": args.get("interface_id"),
        "reasoning_level": args.get("reasoning_level", "off"),
        "commands": args.get("commands", []),
        "skills": args.get("skills", []),
        "plugins": args.get("plugins", []),
        "trigger": args.get("trigger", {"type": "manual", "cron": "", "message": ""}),
        "tags": args.get("tags", []),
        "max_tool_rounds": args.get("max_tool_rounds"),
        "enabled": True,
        "perception_scope": args.get("perception_scope", "link"),
        "workflow_mode": args.get("workflow_mode", "none"),
        "group_id": args.get("group_id"),
    }
    item["id"] = next_id("agents")
    item["created_at"] = datetime.utcnow().isoformat()
    return upsert("agents", item)


def _cmd_agents_update(args: dict) -> dict:
    from core.store import upsert, get_by
    aid = args.get("id")
    if aid is None:
        raise HTTPException(400, "缺少 id")
    cur = get_by("agents", int(aid))
    if not cur:
        raise HTTPException(404, "智能体不存在")
    for k in ("name", "description", "system_prompt", "model", "interface_id",
              "reasoning_level", "commands", "skills", "plugins", "trigger",
              "tags", "max_tool_rounds", "enabled", "perception_scope",
              "workflow_mode", "group_id", "sort_order"):
        if k in args:
            cur[k] = args[k]
    cur["id"] = int(aid)
    return upsert("agents", cur)


def _cmd_agents_delete(args: dict) -> dict:
    from core.store import delete_by, get_by, load, upsert
    aid = args.get("id")
    if aid is None:
        raise HTTPException(400, "缺少 id")
    aid = int(aid)
    # 从画布清理该智能体节点与相关连线
    for c in (load("canvases") or []):
        changed = False
        nodes = [n for n in c.get("nodes", []) if n.get("agent_id") != aid]
        if len(nodes) != len(c.get("nodes", [])):
            changed = True
        node_ids = {n.get("id") for n in nodes}
        edges = [e for e in c.get("edges", [])
                 if e.get("from_node") in node_ids and e.get("to_node") in node_ids]
        if len(edges) != len(c.get("edges", [])):
            changed = True
        if changed:
            c["nodes"] = nodes
            c["edges"] = edges
            upsert("canvases", c)
    return {"ok": delete_by("agents", aid)}


def _cmd_groups_list(args: dict) -> dict:
    from core.store import load
    return load("agent_groups") or []


def _cmd_groups_create(args: dict) -> dict:
    from core.store import upsert, next_id
    if not args.get("name"):
        raise HTTPException(400, "缺少 name")
    item = {"name": args["name"]}
    item["id"] = next_id("agent_groups")
    item["created_at"] = datetime.utcnow().isoformat()
    return upsert("agent_groups", item)


def _cmd_groups_delete(args: dict) -> dict:
    from core.store import delete_by, load, upsert
    gid = args.get("id")
    if gid is None:
        raise HTTPException(400, "缺少 id")
    gid = int(gid)
    for a in (load("agents") or []):
        if a.get("group_id") == gid:
            a["group_id"] = None
            upsert("agents", a)
    return {"ok": delete_by("agent_groups", gid)}


def _cmd_canvases_list(args: dict) -> dict:
    from core.store import load
    return load("canvases") or []


def _cmd_canvases_create(args: dict) -> dict:
    from core.store import upsert, next_id
    if not args.get("name"):
        raise HTTPException(400, "缺少 name")
    item = {
        "name": args["name"],
        "description": args.get("description", ""),
        "nodes": args.get("nodes", []),
        "edges": args.get("edges", []),
        "workdir": args.get("workdir"),
        "execution_mode": args.get("execution_mode", "serial"),
        "ws_pin": args.get("ws_pin"),
    }
    item["id"] = next_id("canvases")
    item["created_at"] = datetime.utcnow().isoformat()
    return upsert("canvases", item)


def _cmd_canvases_get(args: dict) -> dict:
    from core.store import get_by
    cid = args.get("id")
    if cid is None:
        raise HTTPException(400, "缺少 id")
    c = get_by("canvases", int(cid))
    if not c:
        raise HTTPException(404, "画布不存在")
    return c


def _cmd_canvases_delete(args: dict) -> dict:
    from core.store import delete_by
    cid = args.get("id")
    if cid is None:
        raise HTTPException(400, "缺少 id")
    return {"ok": delete_by("canvases", int(cid))}


def _cmd_conversations_list(args: dict) -> dict:
    """列出会话（剥离 messages，仅返回元数据 + 摘要）。"""
    from core.store import load_list_payloads
    entries = load_list_payloads("conversations", _conv_activity_key)
    entries.sort(key=lambda e: e[0], reverse=True)
    out = []
    for _, payload_bytes in entries:
        try:
            obj = json.loads(payload_bytes)
            msgs = obj.pop("messages", None) or []
            obj["message_count"] = len(msgs)
            if msgs:
                obj["last_role"] = msgs[-1].get("role")
                obj["last_agent_id"] = msgs[-1].get("agent_id")
                obj["last_ts"] = msgs[-1].get("ts")
                content = msgs[-1].get("content") or ""
                obj["last_preview"] = (content[:80].replace("\n", " ")
                                       if isinstance(content, str) else "")
            out.append(obj)
        except Exception:
            continue
    return out


def _conv_activity_key(c: dict):
    last_ts = None
    for m in c.get("messages") or []:
        ts = m.get("ts")
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
    return last_ts or c.get("created_at") or f"{c.get('id', 0):020d}"


def _cmd_conversations_create(args: dict) -> dict:
    from core.store import upsert, next_id
    item = {
        "name": args.get("name", "新对话"),
        "mode": args.get("mode", "independent"),
        "agent_ids": args.get("agent_ids", []),
        "canvas_id": args.get("canvas_id"),
        "workdir": args.get("workdir"),
        "execution_mode": args.get("execution_mode"),
    }
    item["id"] = next_id("conversations")
    item["messages"] = []
    item["created_at"] = datetime.utcnow().isoformat()
    return upsert("conversations", item)


def _cmd_conversations_get(args: dict) -> dict:
    from core.store import get_by
    cid = args.get("id")
    if cid is None:
        raise HTTPException(400, "缺少 id")
    c = get_by("conversations", int(cid))
    if not c:
        raise HTTPException(404, "会话不存在")
    return c


def _cmd_conversations_delete(args: dict) -> dict:
    from core.store import delete_by
    cid = args.get("id")
    if cid is None:
        raise HTTPException(400, "缺少 id")
    return {"ok": delete_by("conversations", int(cid))}


async def _cmd_conversations_send(args: dict) -> dict:
    """发消息给智能体，阻塞聚合所有事件直到 DONE，返回最终文本 + 事件列表。

    外部 AI 调用本命令获得完整回复；若需实时流式输出请用 WebSocket /ws/events/{cid}。
    """
    from core.store import get_by, upsert
    from core import run_registry
    cid = args.get("id")
    agent_id = args.get("agent_id")
    content = args.get("content")
    if cid is None or agent_id is None or content is None:
        raise HTTPException(400, "缺少 id / agent_id / content")
    cid, agent_id = int(cid), int(agent_id)
    conv = run_registry.peek_live_conv(cid) or get_by("conversations", cid)
    if not conv:
        raise HTTPException(404, "会话不存在")
    agent = get_by("agents", agent_id)
    if not agent:
        raise HTTPException(404, "智能体不存在")
    if run_registry.is_running(cid, agent_id):
        raise HTTPException(409, "该智能体正在运行中")
    canvas = get_by("canvases", conv.get("canvas_id")) if conv.get("canvas_id") else None
    if canvas is not None and conv.get("execution_mode"):
        canvas = {**canvas, "execution_mode": conv["execution_mode"]}
    mode = "linked" if conv.get("canvas_id") else "independent"
    # 落用户消息
    conv.setdefault("messages", []).append({
        "role": "user", "content": content, "ts": datetime.utcnow().isoformat(),
        "to_agent_id": agent_id,
    })
    upsert("conversations", conv)
    # 启动后台运行
    session = run_registry.start(cid, agent, content, mode, canvas,
                                 base_conv=conv, workdir=conv.get("workdir"))
    # 订阅事件聚合
    events: list[dict] = []
    text_parts: list[str] = []
    final_text = ""
    async for line in session.subscribe(0):
        stripped = line.rstrip("\n")
        events.append({"seq": len(events) + 1, "data": stripped})
        # 解析事件行：常见格式 "DATA:..." / "DONE" / 其他标记
        if stripped == "DONE":
            break
        if stripped.startswith("DATA:"):
            text_parts.append(stripped[5:])
        elif stripped and not stripped.endswith((":END", ":START")) and ":" not in stripped[:10]:
            text_parts.append(stripped)
    final_text = "".join(text_parts)
    return {
        "ok": True,
        "conv_id": cid,
        "agent_id": agent_id,
        "text": final_text,
        "events": events,
        "event_count": len(events),
    }


def _cmd_conversations_stop(args: dict) -> dict:
    from core import run_registry
    cid = args.get("id")
    if cid is None:
        raise HTTPException(400, "缺少 id")
    agent_id = args.get("agent_id")
    return {"ok": run_registry.stop(int(cid), int(agent_id) if agent_id is not None else None)}


def _cmd_conversations_running(args: dict) -> dict:
    from core import run_registry
    cid = args.get("id")
    if cid is None:
        raise HTTPException(400, "缺少 id")
    cid = int(cid)
    runs = run_registry.runs_of(cid)
    running = [s.describe() for s in runs if s.running]
    return {"running": bool(running), "runs": running, "conv_id": cid}


# 同步命令分发表
_SYNC_DISPATCH: dict[str, Any] = {
    "system.info": _cmd_system_info,
    "agents.list": _cmd_agents_list,
    "agents.create": _cmd_agents_create,
    "agents.update": _cmd_agents_update,
    "agents.delete": _cmd_agents_delete,
    "groups.list": _cmd_groups_list,
    "groups.create": _cmd_groups_create,
    "groups.delete": _cmd_groups_delete,
    "canvases.list": _cmd_canvases_list,
    "canvases.create": _cmd_canvases_create,
    "canvases.get": _cmd_canvases_get,
    "canvases.delete": _cmd_canvases_delete,
    "conversations.list": _cmd_conversations_list,
    "conversations.create": _cmd_conversations_create,
    "conversations.get": _cmd_conversations_get,
    "conversations.delete": _cmd_conversations_delete,
    "conversations.stop": _cmd_conversations_stop,
    "conversations.running": _cmd_conversations_running,
}
# 异步命令（含 await）
_ASYNC_DISPATCH: dict[str, Any] = {
    "conversations.send": _cmd_conversations_send,
}


# —— 路由 ——
@router.get("/info")
def info():
    """服务信息 + 可用命令清单。无需鉴权（仅暴露命令名与说明，不含 token）。"""
    _check_enabled()
    return {
        "service": "AgentCluster External Control",
        "version": "1.0.0",
        "description": "让外部 AI 程序通过 HTTP/WebSocket 控制本程序",
        "auth": "Bearer Token（首次自动生成，见本拓展目录 token.txt）",
        "endpoints": {
            "command": "POST /api/ext/external_control/command  body: {cmd, args, token?}",
            "ws_events": "WS  /api/ext/external_control/ws/events/{cid}?token=...",
            "sse_events": "GET /api/ext/external_control/events/{cid}?token=...",
            "token": "GET /api/ext/external_control/token  （需已认证）",
        },
        "commands": _COMMANDS,
    }


@router.get("/token")
def get_token(request: Request):
    """查看当前 token。

    首次（token 文件不存在）直接生成并返回，方便用户首次获取；
    文件已存在时需已认证才能查看（防止泄露）。
    """
    if _TOKEN_FILE.exists():
        _verify_token(req=request)
    return {"token": _get_token()}


@router.post("/token/reset")
def reset_token(request: Request, payload: dict | None = None):
    """重置 token。"""
    _verify_token(req=request, body_token=(payload or {}).get("token"))
    tok = secrets.token_urlsafe(32)
    _TOKEN_FILE.write_text(tok, encoding="utf-8")
    return {"ok": True, "token": tok}


@router.post("/command")
async def command(request: Request, payload: dict):
    """统一命令入口。

    body: ``{"cmd": "agents.create", "args": {...}, "token": "..."}``
    返回: ``{"ok": true, "cmd": "...", "data": ...}`` 或 ``{"ok": false, "error": "..."}``
    """
    cmd = payload.get("cmd")
    args = payload.get("args") or {}
    body_token = payload.get("token")
    _verify_token(req=request, body_token=body_token)
    if not cmd or not isinstance(cmd, str):
        raise HTTPException(400, "缺少 cmd 字段")
    if cmd not in _SYNC_DISPATCH and cmd not in _ASYNC_DISPATCH:
        raise HTTPException(400, f"未知命令：{cmd}。可用命令见 GET /info")
    try:
        if cmd in _ASYNC_DISPATCH:
            data = await _ASYNC_DISPATCH[cmd](args)
        else:
            data = _SYNC_DISPATCH[cmd](args)
        return {"ok": True, "cmd": cmd, "data": data}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "cmd": cmd, "error": f"{type(e).__name__}: {e}"}


@router.websocket("/ws/events/{cid}")
async def ws_events(ws: WebSocket, cid: int, token: str = Query(default="")):
    """WebSocket 实时订阅会话事件流。

    连接：``ws://host/api/ext/external_control/ws/events/{cid}?token=<token>``
    可选 query ``agent_id`` 只订阅该智能体的运行。
    每条消息为 JSON：``{"seq": int, "data": "事件行"}``，最后发 ``{"done": true}``。
    """
    try:
        _verify_token(query_token=token)
    except HTTPException as e:
        await ws.accept()
        await ws.send_json({"error": str(e.detail), "code": e.status_code})
        await ws.close()
        return
    from core import run_registry
    agent_id = ws.query_params.get("agent_id")
    agent_id_int = int(agent_id) if agent_id else None
    session = run_registry.get(cid, agent_id_int)
    if not session:
        await ws.accept()
        await ws.send_json({"error": "没有可恢复的运行（已结束且缓冲已过期）"})
        await ws.close()
        return
    await ws.accept()
    try:
        async for line in session.subscribe(0):
            await ws.send_json({"seq": session.seq, "data": line.rstrip("\n")})
            if line.strip() == "DONE":
                break
        await ws.send_json({"done": True})
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@router.get("/events/{cid}")
async def sse_events(cid: int, request: Request, token: str = Query(default=""),
                     agent_id: int | None = Query(default=None)):
    """SSE 风格事件流（非 WebSocket 客户端备用）。

    每行 ``data: <事件行>\\n\\n``，结束发 ``data: DONE\\n\\n``。
    """
    _verify_token(req=request, query_token=token)
    from core import run_registry
    session = run_registry.get(cid, agent_id)
    if not session:
        raise HTTPException(404, "没有可恢复的运行")

    async def gen():
        try:
            async for line in session.subscribe(0):
                yield f"data: {line.rstrip(chr(10))}\n\n"
                if line.strip() == "DONE":
                    return
            yield "data: DONE\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8")