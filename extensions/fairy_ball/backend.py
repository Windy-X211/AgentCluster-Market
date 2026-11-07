"""精灵悬浮球拓展 · 后端服务。

由 ``api.extensions.mount_extension_backends`` 在启动时动态加载，
路由挂载于 ``/api/ext/fairy_ball/`` 前缀。

端点：
- ``GET  /page``               → 悬浮球页面 HTML（Electron 透明置顶小窗加载）
- ``GET  /menu``               → 右键独立菜单窗页面 HTML（Electron 透明置顶菜单小窗加载）
- ``GET  /snip``               → 区域截图选区页 HTML（全屏遮罩框选）
- ``GET  /sprite/{name}``      → 宝可梦精灵资源 (PNG / WebM / GIF)，
                                  assets/ 本地内置，白名单校验防目录穿越；
                                  支持 .png / .webm / .gif，自动推断 media_type
- ``GET  /sprite/{slug}/best`` → 按名称返回最佳可用资源 JSON
- ``GET  /state``              → 精灵状态聚合：idle / running + 运行中智能体名单
- ``GET  /enabled``            → 拓展启用状态（Electron 轮询决定球窗开关；停用后仍可访问）
- ``POST /quickchat/cleanup``  → 小窗关闭时清理空临时会话（发过消息的保留）

页面文件（fairy.html / menu.html / snip.html）由本模块读取下发，按 mtime 缓存 ——
改页面无需重启后端，也无需重建前端 dist。精灵资源为静态文件直接 FileResponse
（自带 ETag / Last-Modified 协商缓存）。

临时对话小窗（quickchat）不走本拓展页面：直接加载主窗口前端应用的
``/#/quickchat`` 路由（QuickChatView = RightPanel 精简壳），复用主窗全部
渲染能力（markdown / viz 思维导图 / 思考折叠 / 附件），渲染行为与主窗一致。
"""
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter()

_EXT_ID = "fairy_ball"
_DIR = Path(__file__).resolve().parent

# 页面缓存 { 文件名: (mtime, html) }
_page_cache: dict[str, tuple[float, str]] = {}

# 精灵资源白名单：小写字母数字下划线连字符 + 扩展名
# 支持 .png (HOME 静态高清) / .webm (HOME 3D 官方动画) / .gif (旧版 Showdown 像素动画)
_SPRITE_RE = re.compile(r"^[a-z0-9_-]+\.(png|webm|gif)$")
_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")

# 扩展名 → media_type 映射
_MIME_BY_EXT = {
    ".png": "image/png",
    ".webm": "video/webm",
    ".gif": "image/gif",
}


def _page(name: str) -> str:
    fp = _DIR / name
    if not fp.exists():
        raise HTTPException(500, f"拓展页面缺失：{name}")
    mtime = fp.stat().st_mtime
    cached = _page_cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    html = fp.read_text(encoding="utf-8")
    _page_cache[name] = (mtime, html)
    return html


def _check_enabled() -> None:
    """拓展停用后路由返回 503（Electron 轮询 /enabled 发现后自动关球窗）。"""
    try:
        from api.extensions import is_extension_enabled
    except ImportError:
        return
    if not is_extension_enabled(_EXT_ID):
        raise HTTPException(503, "拓展已停用")


@router.get("/page", response_class=HTMLResponse)
def fairy_page():
    _check_enabled()
    return HTMLResponse(_page("fairy.html"))


@router.get("/snip", response_class=HTMLResponse)
def snip_page():
    _check_enabled()
    return HTMLResponse(_page("snip.html"))


@router.get("/menu", response_class=HTMLResponse)
def fairy_menu_page():
    """右键独立菜单窗页面（三视图：主菜单 / 精灵样式 / 大小档位）。

    皮肤与大小选择写 localStorage（与球窗同源共享），球窗经 storage 事件即时换装。
    """
    _check_enabled()
    return HTMLResponse(_page("menu.html"))


@router.get("/sprite/{name}")
def fairy_sprite(name: str):
    """宝可梦精灵资源 (assets/ 内置)。

    支持三种格式：
    - .png   HOME 官方 3D 静态高清插画 (475x475)
    - .webm  HOME 官方 3D 动画 (VP9, 1.3s 循环)
    - .gif   Showdown 像素动画 (旧版兜底)
    """
    _check_enabled()
    if not _SPRITE_RE.match(name):
        raise HTTPException(400, "invalid sprite name")
    fp = _DIR / "assets" / name
    if not fp.exists():
        raise HTTPException(404, "sprite not found")
    ext = fp.suffix.lower()
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    return FileResponse(fp, media_type=mime, headers={"Cache-Control": "no-cache"})


@router.get("/sprite/{slug}/best")
def fairy_sprite_best(slug: str):
    """按名称（无扩展名）返回该精灵最佳可用资源。

    优先级: .webm (HOME 3D 动画) > .png (HOME 静态) > .gif (旧版)
    返回 { url, mime_type, ext } JSON，前端据此选择 <video> 或 <img> 渲染。
    """
    _check_enabled()
    slug = slug.lower().strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(400, "invalid sprite slug")
    assets = _DIR / "assets"
    for ext in (".webm", ".png", ".gif"):
        fp = assets / f"{slug}{ext}"
        if fp.exists():
            mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
            return {
                "slug": slug,
                "ext": ext.lstrip("."),
                "mime_type": mime,
                "url": f"/api/ext/fairy_ball/sprite/{slug}{ext}",
                "size": fp.stat().st_size,
            }
    raise HTTPException(404, f"no sprite found for {slug}")


@router.get("/state")
def fairy_state():
    """精灵状态：聚合 run_registry 全局运行 + 智能体名字。

    球页面每 3s 轮询本端点切换动画：
    - running：任一 (会话, 智能体) 运行中 → 欢快跳动发光
    - idle：无运行 → HOME 3D 动画原生播放 + 缓慢漂浮
    - 页面 fetch 失败（后端挂了）自行切 error 灰色歪头
    """
    _check_enabled()
    from core import run_registry
    from core.store import load

    try:
        runs = run_registry.list_running()
    except Exception:
        runs = []
    if not runs:
        return {"status": "idle", "running": []}
    try:
        names = {a.get("id"): a.get("name") for a in load("agents")}
    except Exception:
        names = {}
    return {
        "status": "running",
        "running": [
            {
                "conv_id": r.get("conv_id"),
                "agent_id": r.get("agent_id"),
                "agent_name": names.get(r.get("agent_id"), f'#{r.get("agent_id")}'),
            }
            for r in runs
        ],
    }


@router.get("/enabled")
def fairy_enabled():
    """拓展启用状态。此端点不做 _check_enabled（停用后 Electron 靠它感知并关球窗）。"""
    try:
        from api.extensions import is_extension_enabled
        return {"enabled": is_extension_enabled(_EXT_ID)}
    except ImportError:
        return {"enabled": True}


@router.post("/quickchat/cleanup")
def quickchat_cleanup(payload: dict):
    """小窗关闭时清理：会话 0 条消息（用户没发送任何东西）→ 删除；
    有消息 → 保留（可在主窗口会话列表回看）。"""
    cid = payload.get("conv_id")
    if not isinstance(cid, int):
        raise HTTPException(400, "conv_id required")
    from core.store import get_by, delete_by

    conv = get_by("conversations", cid)
    if not conv:
        return {"ok": True, "deleted": False, "reason": "not_found"}
    if not conv.get("messages"):
        return {"ok": True, "deleted": False, "reason": "not_found"}
    return {"ok": True, "deleted": delete_by("conversations", cid)}
