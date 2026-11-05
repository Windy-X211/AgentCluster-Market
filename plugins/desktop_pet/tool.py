"""desktop_pet 插件 — Live2D 桌宠控制

调用签名 (由 tool.yaml 的 parameters 决定)：
    run(action="show", model_path="...", x=0.5, y=0.8, scale=1.0, ...)

四大功能共 15 个 action：
  1. 窗口控制：show / hide / toggle / close
  2. 布局控制：switch / move / resize / update
  3. 捏脸参数：get_params / set_params / express
  4. 流式动画：start_stream / append_frames / stop_stream
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

# 允许独立 import pet_window（插件目录加入 sys.path）
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 未指定 model_path 时的默认模型（插件自带）
_DEFAULT_MODEL_DIR = Path(_PLUGIN_DIR) / "assets" / "符玄"

# 模块级状态（进程级，多次 run() 调用间保持）
_window = None
_lock = threading.Lock()
_current_model: str | None = None
_current_pos = {"x": 0.5, "y": 0.8}
_current_scale = 1.0
_stream_active = False


def _on_ui_scale_changed(new_scale):
    """滚轮缩放回调 — 从窗口交互同步到模块级状态。"""
    global _current_scale
    _current_scale = new_scale


def _on_ui_model_switched(model_path):
    """右键切换模型回调 — 从窗口交互同步到模块级状态。"""
    global _current_model
    _current_model = model_path


def _on_ui_pos_changed(x, y):
    """鼠标避让/拖拽后的位置回调 — 同步到模块级状态。"""
    _current_pos.update(x=x, y=y)


def run(**kwargs):
    action = kwargs.get("action", "show")
    with _lock:
        handlers = {
            "show": _cmd_show, "hide": _cmd_hide, "toggle": _cmd_toggle,
            "close": _cmd_close, "switch": _cmd_switch,
            "move": _cmd_move, "resize": _cmd_resize, "update": _cmd_update,
            "get_params": _cmd_get_params, "set_params": _cmd_set_params,
            "express": _cmd_express,
            "start_stream": _cmd_start_stream, "append_frames": _cmd_append_frames,
            "stop_stream": _cmd_stop_stream,
        }
        handler = handlers.get(action)
        if not handler:
            return {"error": f"未知 action: {action}，可用：{list(handlers.keys())}"}
        try:
            return handler(kwargs)
        except Exception as e:
            import traceback
            return {"error": str(e), "traceback": traceback.format_exc()}


# ===== 窗口 & 模型 =====
def _resolve_model_path(model_path: str) -> str:
    """支持目录路径（自动找 model3.json）或直接指定 model3.json。"""
    p = Path(model_path).resolve()
    if p.is_file() and p.suffix == ".json":
        return str(p)
    if p.is_dir():
        json_files = list(p.glob("*.model3.json"))
        if json_files:
            return str(json_files[0])
    raise ValueError(f"找不到 model3.json：{model_path}")


def _cmd_show(kwargs):
    global _current_model, _window, _current_pos, _current_scale
    model_path = kwargs.get("model_path") or _current_model
    if not model_path:
        # 未指定模型路径：默认使用插件自带的符玄模型
        if _DEFAULT_MODEL_DIR.exists():
            model_path = str(_DEFAULT_MODEL_DIR)
        else:
            return {"error": f"未指定 model_path，且默认模型不存在：{_DEFAULT_MODEL_DIR}"}

    x = kwargs.get("x", _current_pos["x"])
    y = kwargs.get("y", _current_pos["y"])
    scale = kwargs.get("scale", _current_scale)

    model_file = _resolve_model_path(model_path)
    model_dir = str(Path(model_file).parent)

    if _window and _window.is_running():
        # 已有窗口：发命令显示 + 移动到位置 + 设置缩放
        _window.send_command({"type": "show", "x": x, "y": y, "scale": scale})
        _current_pos.update(x=x, y=y)
        _current_scale = scale
        _current_model = model_path
        return {"ok": True, "action": "show", "reused": True}

    # 启动 HTTP server + 创建窗口
    from pet_window import DesktopPetWindow, serve_model
    http_url = serve_model(model_dir)
    model_http = http_url + Path(model_file).name

    _window = DesktopPetWindow()
    _window.on_scale_change = _on_ui_scale_changed
    _window.on_model_switch = _on_ui_model_switched
    _window.on_pos_change = _on_ui_pos_changed
    _window.start(model_http, x, y, scale)
    _current_model = model_path
    _current_pos.update(x=x, y=y)
    _current_scale = scale

    # 等待窗口就绪（WebEngine 加载完成）
    time.sleep(1.5)

    return {"ok": True, "action": "show", "model": model_path}


def _cmd_hide(kwargs):
    global _window
    if _window:
        _window.send_command({"type": "hide"})
    return {"ok": True, "action": "hide"}


def _cmd_toggle(kwargs):
    global _window
    if _window:
        _window.send_command({"type": "toggle"})
    return {"ok": True, "action": "toggle"}


def _cmd_close(kwargs):
    global _window, _current_model, _stream_active
    if _window:
        _window.send_command({"type": "__close__"})
    _window = None
    _current_model = None
    _stream_active = False
    return {"ok": True, "action": "close"}


def _cmd_switch(kwargs):
    global _current_model
    model_path = kwargs.get("model_path")
    if not model_path:
        return {"error": "switch 需要 model_path"}
    _current_model = model_path
    # 先 close 再 show（简单可靠）
    _cmd_close(kwargs)
    time.sleep(0.5)
    return _cmd_show(kwargs)


# ===== 布局 =====
def _cmd_move(kwargs):
    global _current_pos, _window
    if not _window:
        return _no_window()
    x = kwargs.get("x")
    y = kwargs.get("y")
    dx = kwargs.get("dx", 0)
    dy = kwargs.get("dy", 0)

    new_x = (x if x is not None else _current_pos["x"]) + dx
    new_y = (y if y is not None else _current_pos["y"]) + dy
    new_x = max(0.0, min(1.0, new_x))
    new_y = max(0.0, min(1.0, new_y))
    _current_pos.update(x=new_x, y=new_y)

    _window.send_command({"type": "setPosition", "x": new_x, "y": new_y})
    return {"ok": True, "action": "move", "x": new_x, "y": new_y}


def _cmd_resize(kwargs):
    global _current_scale, _window
    if not _window:
        return _no_window()
    scale = kwargs.get("scale")
    dscale = kwargs.get("dscale", 0)
    new_scale = (scale if scale is not None else _current_scale) + dscale
    new_scale = max(0.3, min(3.0, new_scale))
    _current_scale = new_scale
    _window.send_command({"type": "setScale", "scale": new_scale})
    return {"ok": True, "action": "resize", "scale": new_scale}


def _cmd_update(kwargs):
    """同时更新位置 + 大小（连续输出用）。"""
    r1 = _cmd_move(kwargs)
    r2 = _cmd_resize(kwargs)
    return {"ok": True, "action": "update",
            "pos": {"x": _current_pos["x"], "y": _current_pos["y"]},
            "scale": _current_scale}


# ===== 捏脸参数 =====
def _cmd_get_params(kwargs):
    global _window
    if not _window:
        return _no_window()
    # 使用同步命令获取 JS 返回值
    return _window.send_command_sync({"type": "getParams"})


def _cmd_set_params(kwargs):
    global _window, _stream_active
    if not _window:
        return _no_window()
    params_str = kwargs.get("params", "{}")
    smooth_ms = kwargs.get("smooth_ms", 200)
    try:
        params = json.loads(params_str)
    except json.JSONDecodeError as e:
        return {"error": f"params JSON 解析失败: {e}"}

    # 流式动画运行中，set_params 会被覆盖 — 返回警告
    if _stream_active:
        _window.send_command({"type": "setParams", "params": params, "smoothMs": smooth_ms})
        return {"ok": True, "action": "set_params", "params": params, "smooth_ms": smooth_ms,
                "warning": "流式动画运行中，此设置可能被覆盖"}

    _window.send_command({"type": "setParams", "params": params, "smoothMs": smooth_ms})
    return {"ok": True, "action": "set_params", "params": params, "smooth_ms": smooth_ms}


def _cmd_express(kwargs):
    global _window
    if not _window:
        return _no_window()
    idx = kwargs.get("expression_index", 0)
    _window.send_command({"type": "setExpression", "index": idx})
    return {"ok": True, "action": "express", "expression_index": idx}


# ===== 流式参数动画 =====
def _cmd_start_stream(kwargs):
    global _window, _stream_active
    if not _window:
        return _no_window()

    stream_str = kwargs.get("stream_data", "")
    if not stream_str:
        return {"error": "start_stream 需要 stream_data 参数"}

    try:
        data = json.loads(stream_str)
    except json.JSONDecodeError as e:
        return {"error": f"stream_data JSON 解析失败: {e}"}

    # 验证
    params = data.get("params", [])
    values = data.get("values", [])
    interval = data.get("interval", "500ms")
    repeat = data.get("repeat", 0)

    if not params or not values:
        return {"error": "stream_data 需要 params 和 values 字段"}
    if not all(len(row) == len(params) for row in values):
        return {"error": f"values 每行长度必须与 params ({len(params)}) 一致"}

    _stream_active = True
    _window.send_command({
        "type": "startStream",
        "streamData": stream_str
    })

    interval_ms = _parse_interval(interval)
    per_pass_ms = len(values) * interval_ms

    if repeat == -1:
        duration_desc = "无限循环（直到 stop_stream 或新 start_stream）"
    else:
        actual_repeat = max(1, repeat)
        duration_desc = f"{len(values)} 帧 × {actual_repeat} 遍 × {interval_ms}ms = {per_pass_ms * actual_repeat}ms"

    return {
        "ok": True, "action": "start_stream",
        "params_count": len(params),
        "frames_count": len(values),
        "interval": interval,
        "repeat": repeat,
        "duration_per_pass_ms": per_pass_ms,
        "duration_desc": duration_desc,
        "hint": ("继续用 append_frames 追加帧" if repeat != -1 else "repeat=-1 无限循环；调 stop_stream 停止，或发新 start_stream 覆盖")
    }


def _cmd_append_frames(kwargs):
    global _window, _stream_active
    if not _window:
        return _no_window()
    if not _stream_active:
        return {"warning": "没有活动的流式动画，请先 start_stream 启动一个"}

    eof = kwargs.get("eof", False)

    # 优先取 values_only（更轻量）
    values_only = kwargs.get("values_only")
    cmd = {"type": "appendFrames", "eof": eof}

    if values_only:
        try:
            rows = json.loads(values_only)
            if not all(isinstance(row, list) for row in rows):
                return {"error": "values_only 必须是二维数组"}
            cmd["valuesOnly"] = values_only
        except json.JSONDecodeError as e:
            return {"error": f"values_only JSON 解析失败: {e}"}
    elif kwargs.get("stream_data"):
        cmd["streamData"] = kwargs["stream_data"]
    else:
        return {"error": "append_frames 需要 values_only 或 stream_data"}

    _window.send_command(cmd)

    if eof:
        _stream_active = False

    return {
        "ok": True, "action": "append_frames",
        "eof": eof,
        "stream_active": _stream_active
    }


def _cmd_stop_stream(kwargs):
    global _window, _stream_active
    if _window:
        _window.send_command({"type": "stopStream"})
    _stream_active = False
    return {"ok": True, "action": "stop_stream"}


# ===== 工具函数 =====
def _no_window():
    return {"error": "桌宠窗口未启动，请先调用 action=show + model_path"}


def _parse_interval(s: str) -> int:
    """'500ms' → 500, '1s' → 1000"""
    s = s.strip().lower()
    if s.endswith("ms"):
        return int(s[:-2])
    if s.endswith("s"):
        return int(float(s[:-1]) * 1000)
    return int(s) if s.isdigit() else 500
