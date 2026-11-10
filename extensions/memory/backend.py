"""长期记忆拓展 · 后端服务。

提供：
1. 记忆管理 API（挂载于 ``/api/ext/memory/``）
2. 给智能体注入 memory.read / memory.write / memory.delete 工具（external_tools provider）
3. 动态刷新 memory_prompt.md（system_prompt_file 机制，供所有智能体 prompt 注入）
4. LLM 自动摘要（后台轮询 conversations，发现新对话即调用模型整理滚动摘要）

记忆条目所有智能体共用一份（memories/shared.json），按智能体 memory_mode 控制各智能体
是否拥有记忆能力（off/manual/summary），全局开关在 settings.memory_enabled。
"""
import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.mainloop import call_llm, call_llm_async, create_task

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_DIR = Path(__file__).resolve().parent
_EXT_ID = "memory"
_PROMPT_FILE = _DIR / "memory_prompt.md"

_logger = logging.getLogger("memory_ext")

# —— 写并发保护 ——
_MEM_LOCK = threading.RLock()

# 合法模式
MODES = ("off", "manual", "summary")

# memory.* 工具名前缀（external_tools provider 用 memory__ 前缀做归属判定）
_TOOL_PREFIX = "memory__"
_TOOL_NAMES = ("read", "write", "delete")


# ==============================================================
# 存储路径 & 核心操作
# ==============================================================
def _get_root() -> Path:
    """返回数据根目录（延迟 import，启动时 backend/ 已在 sys.path）。"""
    from core.store import ROOT
    return Path(ROOT)


def _mem_path() -> Path:
    return _get_root() / "memories" / "shared.json"


def _empty() -> dict:
    return {"summary_text": "", "entries": [], "updated_at": None}


def load_mem() -> dict:
    p = _mem_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("summary_text", "")
                data.setdefault("entries", [])
                if not isinstance(data.get("entries"), list):
                    data["entries"] = []
                return data
    except Exception as e:
        _logger.warning("共享记忆文件读取失败（按空记忆处理）：%s", e)
    return _empty()


def save_mem(mem: dict) -> None:
    mem["updated_at"] = datetime.utcnow().isoformat()
    p = _mem_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _clean_entries(mem: dict) -> list:
    return [e for e in (mem.get("entries") or [])
            if isinstance(e, dict) and (e.get("content") or "").strip()]


def memory_enabled_global() -> bool:
    from core.store import load
    st = load("settings") or {}
    return st.get("memory_enabled", True) is not False


def memory_mode_of(agent: dict | None) -> str:
    if not memory_enabled_global():
        return "off"
    m = (agent or {}).get("memory_mode") or "off"
    return m if m in MODES else "off"


# ==============================================================
# memory_prompt.md 刷新（供 system_prompt_file 机制读取）
# ==============================================================
def refresh_prompt_file() -> None:
    """把当前共享记忆刷新到 memory_prompt.md（原子写入）。

    写入内容 = 记忆内容段 + 两种模式的使用规则。
    文件写好后，agent_runtime 的 mtime 缓存机制会在下一次构建 prompt 时
    自动读取新内容。
    """
    try:
        mem = load_mem()
        entries = _clean_entries(mem)
        pinned = [e for e in entries if e.get("pinned")]
        normal = [e for e in entries if not e.get("pinned")]
        summary_text = (mem.get("summary_text") or "").strip()

        lines = [
            "## 长期记忆（记忆技能已启用）",
            "以下是你与集群中其他拥有记忆技能的智能体共享的长期记忆——在对话中主动运用这些信息，让用户感到你记得他。",
        ]
        if pinned:
            lines.append("### 固定记忆（用户明确要求记住，永不遗忘）")
            lines.extend(f"- {e['content']}" for e in pinned)
        if summary_text:
            lines.append("### 摘要记忆（系统自动整理的会话摘要）")
            lines.append(summary_text)
        if normal:
            lines.append("### 其他记忆")
            lines.extend(f"- {e['content']}" for e in normal)
        if not pinned and not normal and not summary_text:
            lines.append("（当前还没有任何记忆内容）")

        lines.extend([
            "",
            "### 可用工具",
            "- `memory__read()` — 读取完整记忆（固定条目 + 摘要 + 其他条目）",
            "- `memory__write(content, pinned=false)` — 写入一条记忆；pinned=true 表示用户明确要求记住、永不遗忘",
            "- `memory__delete(entry_id)` — 删除一条记忆（entry_id 来自 memory__read 返回）",
            "",
            "### 使用规则",
            "使用记忆时调用 memory__read 查看完整内容；用户主动要求「记住/记入记忆/帮我记着」时，",
            "用 memory__write 且 pinned=true 写入固定记忆（永不遗忘）。",
            "摘要记忆模式下每轮对话后系统自动整理摘要，无需手动写入常规信息。",
            "用户问到过往信息而记忆段落没有时，调用 memory__read 查看完整内容。",
        ])

        content = "\n".join(lines)
        tmp = _PROMPT_FILE.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(_PROMPT_FILE)
    except Exception as e:
        _logger.warning("memory_prompt.md 刷新失败：%s", e)


# ==============================================================
# 记忆条目 CRUD
# ==============================================================
def add_entry(content: str, pinned: bool = False, source: str = "manual") -> dict | None:
    content = (content or "").strip()
    if not content:
        return None
    with _MEM_LOCK:
        mem = load_mem()
        entry = {
            "id": f"m-{uuid.uuid4().hex[:10]}",
            "content": content,
            "pinned": bool(pinned),
            "source": source,
            "ts": datetime.utcnow().isoformat(),
        }
        mem.setdefault("entries", []).append(entry)
        save_mem(mem)
        refresh_prompt_file()
        return entry


def delete_entry(entry_id: str) -> bool:
    with _MEM_LOCK:
        mem = load_mem()
        entries = mem.get("entries") or []
        kept = [e for e in entries if e.get("id") != entry_id]
        if len(kept) == len(entries):
            return False
        mem["entries"] = kept
        save_mem(mem)
        refresh_prompt_file()
        return True


def update_entry(entry_id: str, content: str | None = None, pinned: bool | None = None) -> dict | None:
    with _MEM_LOCK:
        mem = load_mem()
        for e in mem.get("entries") or []:
            if e.get("id") == entry_id:
                if content is not None and str(content).strip():
                    e["content"] = str(content).strip()
                if pinned is not None:
                    e["pinned"] = bool(pinned)
                save_mem(mem)
                refresh_prompt_file()
                return e
        return None


def clear_memory(keep_pinned: bool = True) -> dict:
    with _MEM_LOCK:
        mem = load_mem()
        mem["entries"] = [e for e in (mem.get("entries") or []) if keep_pinned and e.get("pinned")]
        mem["summary_text"] = ""
        save_mem(mem)
        refresh_prompt_file()
        return mem


def set_summary_text(content: str) -> dict:
    with _MEM_LOCK:
        mem = load_mem()
        mem["summary_text"] = (content or "").strip()
        save_mem(mem)
        refresh_prompt_file()
        return mem


def read_memory() -> dict:
    mem = load_mem()
    entries = _clean_entries(mem)
    return {
        "summary_text": (mem.get("summary_text") or "").strip(),
        "pinned_entries": [e for e in entries if e.get("pinned")],
        "other_entries": [e for e in entries if not e.get("pinned")],
        "updated_at": mem.get("updated_at"),
    }


# ==============================================================
# 摘要记忆：LLM 整理滚动摘要
# ==============================================================
_CONSOLIDATE_SYS = (
    "你是一个智能体集群的长期记忆整理器。请把「已有记忆摘要」和「最新对话」合并整理成一份新的记忆摘要，"
    "供集群中拥有记忆技能的智能体跨会话共用。要求：\n"
    "1. 保留：用户的偏好与习惯、关键事实与决定、项目背景、进行中的任务与待办、用户纠正过的错误认知；\n"
    "2. 合并去重：已有摘要与最新对话中的重复信息合并为一条；信息冲突时以最新对话为准；\n"
    "3. 精简：条目式中文输出，总长不超过 600 字，只保留对后续对话有价值的信息，忽略寒暄与过程细节；\n"
    "4. 用户明确要求记住的内容已单独保存在固定记忆中，不要把这些内容抄进摘要；\n"
    "5. 只输出摘要正文，不要标题、解释或前言；最新对话与记忆无关时原样保留已有摘要。"
)


async def summarize_and_store(user_text: str, assistant_text: str,
                              agent_id: int | None = None) -> dict | None:
    """用 LLM 把本轮对话整理进共享滚动摘要并落盘。

    使用 ext_common.call_llm_async 调默认接口/模型，拓展无需知道任何 LLM 内部细节。
    失败仅记日志，不影响对话。
    """
    if not (user_text or "").strip() and not (assistant_text or "").strip():
        return None
    try:
        with _MEM_LOCK:
            mem = load_mem()
        old_summary = (mem.get("summary_text") or "").strip()
        pinned = [e for e in _clean_entries(mem) if e.get("pinned")]
        segs = []
        if old_summary:
            segs.append("【已有记忆摘要】\n" + old_summary[:3000])
        if pinned:
            segs.append("【固定记忆（已单独保存，不要抄进摘要）】\n"
                        + "\n".join(f"- {e['content']}" for e in pinned[:30]))
        segs.append("【最新对话】\n[用户] " + (user_text or "")[:3000]
                    + "\n[智能体] " + (assistant_text or "")[:3000])

        new_summary = await call_llm_async(
            user_prompt="\n\n".join(segs),
            sys_prompt=_CONSOLIDATE_SYS,
        )
        if not new_summary:
            _logger.warning("摘要记忆整理失败（LLM 无输出）")
            return None

        with _MEM_LOCK:
            mem = load_mem()
            mem["summary_text"] = new_summary[:2500]
            save_mem(mem)
            refresh_prompt_file()
        _logger.info("共享摘要记忆已整理（agent=%s，%d 字）", agent_id, len(new_summary))
        return {"chars": len(new_summary)}
    except Exception as e:
        _logger.warning("摘要记忆整理失败：%s", e)
        return None


def summarize_sync(user_text: str, assistant_text: str,
                   agent_id: int | None = None) -> dict | None:
    """同步版摘要整理（供 worker 线程里调用，如 external_tools call_fn 或手动触发 endpoint）。"""
    if not (user_text or "").strip() and not (assistant_text or "").strip():
        return None
    try:
        with _MEM_LOCK:
            mem = load_mem()
        old_summary = (mem.get("summary_text") or "").strip()
        pinned = [e for e in _clean_entries(mem) if e.get("pinned")]
        segs = []
        if old_summary:
            segs.append("【已有记忆摘要】\n" + old_summary[:3000])
        if pinned:
            segs.append("【固定记忆（已单独保存，不要抄进摘要）】\n"
                        + "\n".join(f"- {e['content']}" for e in pinned[:30]))
        segs.append("【最新对话】\n[用户] " + (user_text or "")[:3000]
                    + "\n[智能体] " + (assistant_text or "")[:3000])

        new_summary = call_llm(
            user_prompt="\n\n".join(segs),
            sys_prompt=_CONSOLIDATE_SYS,
        )
        if not new_summary:
            _logger.warning("摘要记忆整理失败（LLM 无输出）")
            return None

        with _MEM_LOCK:
            mem = load_mem()
            mem["summary_text"] = new_summary[:2500]
            save_mem(mem)
            refresh_prompt_file()
        _logger.info("共享摘要记忆已整理（agent=%s，%d 字）", agent_id, len(new_summary))
        return {"chars": len(new_summary)}
    except Exception as e:
        _logger.warning("摘要记忆整理失败：%s", e)
        return None


# ==============================================================
# API 路由（挂载于 /api/ext/memory/）
# ==============================================================
class EntryIn(BaseModel):
    content: str
    pinned: bool = False


class EntryUpdate(BaseModel):
    content: str | None = None
    pinned: bool | None = None


class SummaryIn(BaseModel):
    content: str = ""


class ModeIn(BaseModel):
    mode: str


class BatchIn(BaseModel):
    agent_ids: list[int]
    mode: str


class GlobalIn(BaseModel):
    enabled: bool


class ClearIn(BaseModel):
    keep_pinned: bool = True


def _check_enabled() -> None:
    from api.extensions import is_extension_enabled
    if not is_extension_enabled(_EXT_ID):
        raise HTTPException(503, "记忆拓展未启用")


@router.get("/overview")
def overview():
    """所有智能体的记忆模式概况。"""
    _check_enabled()
    from core.store import load, get_by
    g_enabled = memory_enabled_global()
    agents = load("agents")
    out = []
    for a in agents:
        aid = a.get("id")
        out.append({
            "agent_id": aid,
            "name": a.get("name") or f"智能体#{aid}",
            "avatar_color": a.get("avatar_color"),
            "configured_mode": a.get("memory_mode") or "off",
            "mode": memory_mode_of(a),
            "global_enabled": g_enabled,
        })
    out.sort(key=lambda x: x["agent_id"] or 0)
    return out


@router.get("/shared")
def shared_detail():
    """共享记忆详情（查看区展示）。"""
    _check_enabled()
    return read_memory()


@router.post("/entries")
def add_entry_api(e: EntryIn):
    _check_enabled()
    entry = add_entry(e.content, pinned=e.pinned, source="manual")
    if not entry:
        raise HTTPException(400, "记忆内容为空")
    return entry


@router.put("/entries/{entry_id}")
def update_entry_api(entry_id: str, e: EntryUpdate):
    _check_enabled()
    r = update_entry(entry_id, content=e.content, pinned=e.pinned)
    if not r:
        raise HTTPException(404, "entry not found")
    return r


@router.delete("/entries/{entry_id}")
def remove_entry(entry_id: str):
    _check_enabled()
    if not delete_entry(entry_id):
        raise HTTPException(404, "entry not found")
    return {"ok": True}


@router.put("/summary")
def set_summary(s: SummaryIn):
    _check_enabled()
    set_summary_text(s.content)
    return {"ok": True}


@router.post("/clear")
def clear(c: ClearIn):
    _check_enabled()
    clear_memory(keep_pinned=c.keep_pinned)
    return {"ok": True}


@router.post("/batch")
def batch(b: BatchIn):
    """批量设置智能体的记忆模式。"""
    _check_enabled()
    from core.store import get_by, upsert
    if b.mode not in MODES:
        raise HTTPException(400, "mode 须为 off / manual / summary")
    n = 0
    for aid in b.agent_ids:
        a = get_by("agents", aid)
        if not a:
            continue
        a["memory_mode"] = b.mode
        upsert("agents", a)
        n += 1
    return {"ok": True, "updated": n, "mode": b.mode}


@router.put("/global")
def set_global(g: GlobalIn):
    """记忆功能全局总开关。"""
    _check_enabled()
    from core.store import load, save as store_save
    st = load("settings") or {}
    st["memory_enabled"] = bool(g.enabled)
    store_save("settings", st)
    refresh_prompt_file()
    return {"ok": True, "enabled": bool(g.enabled)}


@router.put("/{agent_id}/mode")
def set_mode(agent_id: int, m: ModeIn):
    """设置单个智能体的记忆模式。"""
    _check_enabled()
    from core.store import get_by, upsert
    a = get_by("agents", agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    if m.mode not in MODES:
        raise HTTPException(400, "mode 须为 off / manual / summary")
    a["memory_mode"] = m.mode
    upsert("agents", a)
    return {"ok": True, "mode": m.mode}


class SummarizeIn(BaseModel):
    user_text: str = ""
    assistant_text: str = ""


@router.post("/summarize")
async def summarize_now(s: SummarizeIn):
    """手动触发一次摘要整理（管理页面按钮、或其他拓展调用）。"""
    _check_enabled()
    # 如果调用方没传对话内容，尝试自动取 conversations 里最新一轮 user+assistant
    user_t, ast_t = s.user_text, s.assistant_text
    if not user_t.strip() and not ast_t.strip():
        user_t, ast_t = _pick_latest_dialogue()
        if not user_t.strip() and not ast_t.strip():
            raise HTTPException(400, "没有可整理的对话（请传 user_text + assistant_text，或先进行一轮对话）")
    result = await summarize_and_store(user_t, ast_t)
    if result is None:
        raise HTTPException(502, "摘要整理失败（模型不可用或无输出）")
    return {"ok": True, "chars": result.get("chars")}


@router.get("/summarize/status")
def summarize_status():
    """自动摘要后台任务状态（供前端展示）。"""
    return {
        "running": _auto_summary_task is not None and not _auto_summary_task.done(),
        "last_ts": _auto_summary_state.get("last_ts"),
        "last_chars": _auto_summary_state.get("last_chars"),
        "agent_id": _auto_summary_state.get("agent_id"),
        "last_error": _auto_summary_state.get("last_error"),
    }


# ==============================================================
# external_tools provider —— memory.read / write / delete 工具
# ==============================================================
_MEMORY_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": f"{_TOOL_PREFIX}read",
            "description": "读取长期记忆（共享库：固定记忆条目 + 摘要 + 其他条目）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": f"{_TOOL_PREFIX}write",
            "description": "写入一条长期记忆。content 为要记住的内容（简洁明确）；pinned=true 表示用户明确要求记住、永不遗忘的固定记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容，简洁明确"},
                    "pinned": {"type": "boolean", "description": "是否固定记忆（用户明确要求记住时 true，默认 false）"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": f"{_TOOL_PREFIX}delete",
            "description": "删除一条长期记忆（entry_id 来自 memory__read 的返回）。固定记忆（pinned）除非用户明确要求，否则不要删除。",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "要删除的记忆条目 id"},
                },
                "required": ["entry_id"],
            },
        },
    },
]


def _tools_fn(agent: dict | None) -> list[dict]:
    """给 external_tools 返回该智能体可见的 memory 工具。

    只有拓展已启用 + 全局开关开 + agent.memory_mode ∈ {manual, summary} 时才返回工具；
    否则返回空列表（该智能体拿不到 memory.* 工具）。
    """
    try:
        from api.extensions import is_extension_enabled
        if not is_extension_enabled(_EXT_ID):
            return []
    except Exception:
        return []
    if memory_mode_of(agent) == "off":
        return []
    return list(_MEMORY_TOOL_SCHEMAS)


def _call_fn(tool_name: str, arguments: dict, agent: dict | None) -> dict:
    """执行 memory.* 工具调用。"""
    try:
        from api.extensions import is_extension_enabled
        if not is_extension_enabled(_EXT_ID):
            return {"error": "记忆拓展未启用"}
    except Exception:
        return {"error": "记忆拓展未启用"}
    if memory_mode_of(agent) == "off":
        return {"error": "当前智能体未启用记忆技能（memory_mode=off）"}

    stripped = tool_name[len(_TOOL_PREFIX):]
    try:
        if stripped == "read":
            return read_memory()
        elif stripped == "write":
            content = (arguments.get("content") or "").strip()
            pinned = bool(arguments.get("pinned"))
            if not content:
                return {"error": "记忆内容为空"}
            entry = add_entry(content, pinned=pinned,
                              source="user_request" if pinned else "agent_write")
            if not entry:
                return {"error": "写入失败"}
            return {"ok": True, "entry_id": entry["id"], "pinned": entry["pinned"],
                    "message": "已写入固定记忆（永不遗忘）" if entry["pinned"] else "已写入记忆"}
        elif stripped == "delete":
            entry_id = arguments.get("entry_id", "")
            if not entry_id:
                return {"error": "entry_id 缺失"}
            ok = delete_entry(entry_id)
            return {"ok": bool(ok), "message": "已删除" if ok else f"未找到记忆条目: {entry_id}"}
        else:
            return {"error": f"unknown memory tool: {tool_name}"}
    except Exception as e:
        return {"error": f"memory {stripped} 执行异常: {type(e).__name__}: {e}"}


# ==============================================================
# 后台自动摘要（零侵入，轮询 conversations store 感知新对话）
# ==============================================================
_auto_summary_task: asyncio.Task | None = None
_auto_summary_state: dict = {"last_conv_id": None, "last_msg_ts": None,
                             "last_ts": None, "last_chars": None,
                             "agent_id": None, "last_error": None}


def _pick_latest_dialogue() -> tuple:
    """从 conversations store 里拿最新一轮 user+assistant 对话文本。

    Returns: (user_text, assistant_text)
    """
    from core.store import load
    convs = load("conversations") or []
    best_conv = None
    best_ts = ""
    for c in convs:
        msgs = c.get("messages") or []
        # 只取有 assistant reply 的对话
        if not msgs or all((m.get("role") != "assistant") for m in msgs):
            continue
        last_msg = msgs[-1]
        ts = last_msg.get("ts") or ""
        if ts > best_ts:
            best_ts = ts
            best_conv = c
    if not best_conv:
        return "", ""
    msgs = best_conv.get("messages") or []
    # 倒序找最后一个 assistant
    ast_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant":
            ast_idx = i
            break
    if ast_idx < 0:
        return "", ""
    user_idx = -1
    for i in range(ast_idx - 1, -1, -1):
        if msgs[i].get("role") == "user":
            user_idx = i
            break
    user_text = msgs[user_idx].get("content", "") if user_idx >= 0 else ""
    ast_text = msgs[ast_idx].get("content", "")
    return str(user_text or "").strip(), str(ast_text or "").strip()


async def _auto_summary_loop() -> None:
    """后台轮询循环：每 60s 检查是否有新对话 → 有则触发摘要整理。

    这个循环完全独立于 agent_runtime，零侵入——通过检测 conversations store
    里最后一条消息的 (conv_id, ts) 对是否变化来判断是否有新对话。
    """
    from api.extensions import is_extension_enabled
    interval = 60  # 秒
    try:
        while True:
            await asyncio.sleep(interval)
            if not is_extension_enabled(_EXT_ID):
                continue
            if not memory_enabled_global():
                continue
            try:
                from core.store import load
                convs = load("conversations") or []
                # 找最新的有 assistant reply 的对话
                best_conv = None
                best_ts = ""
                for c in convs:
                    msgs = c.get("messages") or []
                    if not msgs or all(m.get("role") != "assistant" for m in msgs):
                        continue
                    last_ts = (msgs[-1].get("ts") or "")
                    if last_ts > best_ts:
                        best_ts = last_ts
                        best_conv = c

                if not best_conv or not best_ts:
                    continue

                conv_id = best_conv.get("id")
                # 对比上一次摘要的位置
                if (_auto_summary_state.get("last_conv_id") == conv_id
                        and _auto_summary_state.get("last_msg_ts") == best_ts):
                    continue  # 没有新对话，跳过

                # 找最后一轮 user + assistant
                msgs = best_conv.get("messages") or []
                ast_idx = -1
                for i in range(len(msgs) - 1, -1, -1):
                    if msgs[i].get("role") == "assistant":
                        ast_idx = i
                        break
                if ast_idx < 0:
                    continue
                user_idx = -1
                for i in range(ast_idx - 1, -1, -1):
                    if msgs[i].get("role") == "user":
                        user_idx = i
                        break
                user_text = str(msgs[user_idx].get("content", "") if user_idx >= 0 else "")
                ast_text = str(msgs[ast_idx].get("content", "") or "")

                if not user_text.strip() and not ast_text.strip():
                    continue

                agent_id = best_conv.get("agent_id")
                result = await summarize_and_store(user_text, ast_text, agent_id)
                if result is not None:
                    _auto_summary_state["last_ts"] = datetime.utcnow().isoformat()
                    _auto_summary_state["last_chars"] = result.get("chars")
                    _auto_summary_state["agent_id"] = agent_id
                    _auto_summary_state["last_error"] = None
                    _auto_summary_state["last_conv_id"] = conv_id
                    _auto_summary_state["last_msg_ts"] = best_ts
            except Exception as e:
                _auto_summary_state["last_error"] = f"{type(e).__name__}: {e}"
                _logger.warning("后台自动摘要异常（将继续）：%s", e)
    except asyncio.CancelledError:
        _logger.info("后台自动摘要循环已取消")
    except Exception as e:
        _logger.warning("后台自动摘要循环退出：%s", e)


# ==============================================================
# on_mount / on_unmount 钩子
# ==============================================================
def on_mount(app: Any) -> None:
    """拓展后端挂载时调用：注册 external_tools provider + 启动后台摘要循环 + 初始化 prompt。"""
    from core.external_tools import register_provider
    register_provider(
        provider_id=_EXT_ID,
        tools_fn=_tools_fn,
        call_fn=_call_fn,
        prefix=_TOOL_PREFIX,
        label="长期记忆",
    )
    # 初始刷新 prompt 文件
    try:
        refresh_prompt_file()
    except Exception as e:
        _logger.warning("on_mount 初始 prompt 刷新失败：%s", e)

    # 启动后台自动摘要循环（不阻塞挂载，loop 不可用时静默跳过）
    global _auto_summary_task
    try:
        _auto_summary_task = create_task(_auto_summary_loop(), name="memory_auto_summary")
        _logger.info("后台自动摘要循环已启动（每 60s 检查）")
    except Exception as e:
        _logger.warning("后台自动摘要循环启动失败（忽略）：%s", e)

    print(f"[extensions] memory 拓展已挂载：backend API + memory__* tools + prompt 注入 + 后台摘要循环")


def on_unmount() -> None:
    """进程关闭时调用：注销 provider + 取消后台任务。"""
    try:
        from core.external_tools import unregister_provider
        unregister_provider(_EXT_ID)
    except Exception:
        pass
    global _auto_summary_task
    if _auto_summary_task is not None:
        try:
            _auto_summary_task.cancel()
        except Exception:
            pass
        _auto_summary_task = None
