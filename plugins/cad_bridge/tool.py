"""cad_bridge 插件 —— 通过 AutoLISP 常驻桥实时操控已打开的 AutoCAD

架构（参考 new-acad 的「进程内常驻桥」思路，纯 LISP 实现零编译）：
    智能体插件(Python) ──写指令文件──▶ 桥目录 queue/*.cmd
    AutoCAD 内 cad_bridge.lsp（timer reactor 轮询）──entmake 实时绘制──▶ 图纸
    LSP ──写回执/心跳──▶ done/*.res + bridge.heartbeat
    插件 ──读回执──▶ 返回 {ok, handle, ...}

特点：
    - 绘制实时出现在打开的图纸上，用户全程可见
    - LSP 全部用 entmake，不占命令行；reactor 只在空闲安全点触发，用户可随时操作 CAD
    - connect 时若检测到 CAD 运行中则经 COM SendCommand 自动加载 LSP；
      否则提示用户 APPLOAD 手动加载（桥目录经锚点文件自动下发）

调用签名：
    run(action="status" / "connect")
    run(action="line", x1=0, y1=0, x2=100, y2=0, layer="", color=0)
    run(action="polyline", points=[[0,0],[100,0]], closed=True, layer="", color=0)
    run(action="circle" / "arc" / "ellipse" / "rectangle" / "polygon" / "text" / "dimension" ...)
    run(action="erase", handle="2A3")
    run(action="readDrawing") / run(action="zoom") / run(action="save")
    run(action="saveAs", filePath="D:/part.dwg")
    run(action="pause" / "resume" / "stop")
    run(action="template", what="flange", params={...}, baseX=0, baseY=0)

返回 {ok, ...}；失败返回 {ok: False, error}。
"""
from __future__ import annotations

import json as _json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _detect_root() -> Path:
    d = Path(__file__).resolve().parent
    if d.parent.name == "plugins":
        base = d.parent.parent
        if base.name == "market":
            return base.parent
        return base
    return d


_PROJECT_ROOT: Path = _detect_root()
_PLUGIN_DIR: Path = Path(__file__).resolve().parent
LSP_FILE: Path = _PLUGIN_DIR / "cad_bridge.lsp"
BRIDGE_SUBDIR = "outputs/cad_bridge"
ANCHOR_FILE = Path(os.environ.get("LOCALAPPDATA", str(_PROJECT_ROOT))) / "AgentCluster" / "cad_bridge.dir"

_SEQ = 0
_FLUSH_TIMEOUT = 45.0


# ==============================================================
# 桥目录与编码
# ==============================================================
def _bridge_dir() -> Path:
    d = _PROJECT_ROOT / BRIDGE_SUBDIR
    (d / "queue").mkdir(parents=True, exist_ok=True)
    (d / "done").mkdir(parents=True, exist_ok=True)
    _ensure_anchor(d)
    return d


def _ensure_anchor(bdir: Path) -> None:
    """把桥目录路径写入固定锚点文件（LSP 手动 APPLOAD 加载时据此找桥目录）。
    AutoLISP 读 ANSI 文本，必须用 mbcs 编码。"""
    try:
        ANCHOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        ANCHOR_FILE.write_text(str(bdir).replace("\\", "/"), encoding="mbcs")
    except Exception:
        pass


def _hb_path(bdir: Path) -> Path:
    return bdir / "bridge.heartbeat"


def _read_ansi(p: Path) -> str:
    """读桥目录写出的文本：优先 mbcs(ANSI)，失败退回 utf-8 / gbk / latin-1。
    （真机上 LSP 写出的中文报错串用 mbcs 解码会抛 UnicodeError，故做多编码回退）"""
    data = p.read_bytes()
    for enc in ("mbcs", "utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeError, LookupError):
            continue
    return data.decode("latin-1", "replace")


def _state_path(bdir: Path) -> Path:
    return bdir / "bridge.state"


def _hb_age(bdir: Path) -> float | None:
    try:
        return max(0.0, time.time() - _hb_path(bdir).stat().st_mtime)
    except OSError:
        return None


def _bridge_state(bdir: Path) -> str:
    try:
        return _read_ansi(_state_path(bdir)).strip()
    except OSError:
        return ""


def _is_alive(bdir: Path) -> bool:
    """心跳新鲜即活；阈值放宽到 15s（真机 timer reactor 触发周期未经真机标定，
    过紧会把正常的慢轮询误判为离线）。STOPPED 状态由调用方结合 state 判定。"""
    age = _hb_age(bdir)
    return age is not None and age < 15.0


def _is_connected(bdir: Path) -> bool:
    return _is_alive(bdir) and _bridge_state(bdir) != "STOPPED"


# ==============================================================
# 指令队列
# ==============================================================
def _enqueue(ops: list[str]) -> str:
    """把一批指令行写入队列，返回文件名。文件名零填充定宽保证 LSP 端字符串排序正确。
    写入后立即经 COM 唤醒 ACBWAKE 消费，消除等 timer tick 的秒级延迟。"""
    global _SEQ
    _SEQ += 1
    name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{_SEQ:06d}_{len(ops)}.cmd"
    (Path(_bridge_dir()) / "queue" / name).write_text("\n".join(ops) + "\n", encoding="mbcs")
    _wake()
    return name


def _read_res(name: str) -> list[str] | None:
    p = Path(_bridge_dir()) / "done" / f"{name}.res"
    try:
        return [l for l in _read_ansi(p).splitlines() if l]
    except OSError:
        return None


def _flush(names: list[str], timeout: float = _FLUSH_TIMEOUT) -> dict[str, Any]:
    """轮询等待回执，汇总结果"""
    deadline = time.time() + timeout
    results: dict[str, list[str]] = {}
    while len(results) < len(names) and time.time() < deadline:
        for n in names:
            if n not in results:
                r = _read_res(n)
                if r is not None:
                    results[n] = r
            if len(results) < len(names):
                time.sleep(0.06)
    drawn = 0
    handles: list[str] = []
    errors: list[str] = []
    debug: list[str] = []
    for n in names:
        for line in results.get(n, []):
            parts = line.split("|", 2)
            if parts[0] == "OK" and parts[1] not in ("READ", "EMPTY", "SAVE", "SAVEAS", "ZOOM"):
                drawn += 1
                if len(parts) > 2 and parts[2] not in ("", "?"):
                    handles.append(parts[2])
            elif parts[0] == "ERR":
                errors.append(parts[1] + ":" + (parts[2] if len(parts) > 2 else ""))
            elif parts[0] == "DBG":
                debug.append(line)
    return {
        "ok": len(results) == len(names) and not errors,
        "drawn": drawn,
        "handles": handles,
        "errors": errors,
        "pending": len(names) - len(results),
        "debug": debug,
    }


def _parse_read(res_lines: list[str]) -> dict[str, Any]:
    """解析 READ 回执。回执是竖线展平的段流：TOTAL|n|C|类型|次数|...|T|TEXT|T|文字
    （文字内容若含竖线会被拆段，属已知限制）。"""
    data: dict[str, Any] = {"entityTotal": 0, "entityCounts": {}, "texts": []}
    for line in res_lines:
        if not line.startswith("OK|READ|"):
            continue
        segs = line[8:].split("|")
        i = 0
        while i < len(segs):
            s = segs[i]
            if s == "TOTAL" and i + 1 < len(segs):
                data["entityTotal"] = int(segs[i + 1] or 0)
                i += 2
            elif s == "C" and i + 2 < len(segs):
                data["entityCounts"][segs[i + 1]] = int(segs[i + 2] or 0)
                i += 3
            elif s == "T" and i + 1 < len(segs):
                data["texts"].append(segs[i + 1])
                i += 2
            else:
                i += 1
    return data


# ==============================================================
# COM 探测与自动加载（pywin32 可选依赖，缺了不影响手动 APPLOAD 路线）
# ==============================================================
def _probe_cad() -> tuple[bool, str]:
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False, "pywin32 未安装（不影响手动 APPLOAD 加载）"
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass
    for prog_id in ("AutoCAD.Application", "ZWCAD.Application", "Gcad.Application"):
        try:
            win32com.client.GetActiveObject(prog_id)
            return True, prog_id
        except Exception:
            continue
    return False, ""


def _com_load_lsp(bdir: Path) -> tuple[bool, str]:
    """经 COM SendCommand 在 CAD 内加载 LSP（预设 *ACB-DIR* 后 load）"""
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        return False, f"pywin32 未安装：{e}"
    try:
        pythoncom.CoInitialize()
        app = win32com.client.GetActiveObject("AutoCAD.Application")
        doc = app.ActiveDocument
        if doc is None:
            # CAD 在运行但没有活动文档：自动新建一张（零打扰，桥需要活动图纸承载实体）
            try:
                doc = app.Documents.Add()
            except Exception:
                return False, "CAD 已运行但没有打开的图纸，且自动新建失败"
        d = str(bdir).replace("\\", "/")
        lsp = str(LSP_FILE).replace("\\", "/")
        cmd = f'(progn (setq *ACB-DIR* "{d}") (load "{lsp}"))\n'
        doc.SendCommand(cmd)
        return True, "AutoCAD.Application"
    except Exception as e:
        return False, f"COM 加载失败：{e}"


def _launch_cad() -> tuple[bool, str]:
    """自动启动 AutoCAD 并确保有一张活动图纸（冷启动约 10-40 秒，一次性成本）"""
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        return False, f"pywin32 未安装：{e}"
    try:
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("AutoCAD.Application")
        try:
            app.Visible = True
        except Exception:
            pass
        deadline = time.time() + 90.0
        doc = None
        while time.time() < deadline:
            try:
                doc = app.ActiveDocument
                if doc is not None:
                    return True, "AutoCAD.Application"
            except Exception:
                pass
            try:
                doc = app.Documents.Add()
                if doc is not None:
                    return True, "AutoCAD.Application"
            except Exception:
                pass
            time.sleep(0.6)
        return False, "AutoCAD 启动超时（90 秒内未出现活动图纸）"
    except Exception as e:
        return False, f"启动 AutoCAD 失败：{e}"


def _wake() -> None:
    """入队后经 COM 发 ACBWAKE 命令唤醒桥立即消费，消除等待 timer tick 的秒级延迟。
    失败静默（timer tick 兜底仍会消费）。"""
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        app = win32com.client.GetActiveObject("AutoCAD.Application")
        doc = app.ActiveDocument
        if doc is not None:
            doc.SendCommand("_ACBWAKE\n")
    except Exception:
        pass


def _wait_alive(bdir: Path, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_alive(bdir):
            return True
        time.sleep(0.3)
    return False


# ==============================================================
# 模板（生成指令行序列）
# ==============================================================
def _num(params: dict, key: str, default: float) -> float:
    try:
        v = params.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _get_params(kwargs: dict) -> dict[str, Any]:
    p = kwargs.get("params")
    if isinstance(p, str):
        try:
            p = _json.loads(p)
        except Exception:
            return {}
    return p if isinstance(p, dict) else {}


def _pf(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _u(s: str) -> str:
    """把字符串转为「纯 ASCII 的 MTEXT 安全内容」（两步，顺序关键）：
    1) 先按 MTEXT 格式规则转义字面特殊字符：\\ → \\\\，{ → \\{，} → \\}
       （必须在原始文本上做，避免字面 \\ 与下一步生成的 \\U+ 前缀拼成假转义序列）
    2) 再把非 ASCII 字符转 \\U+XXXX（MTEXT 官方 Unicode 转义，渲染层还原中文；
       TEXT 单行不保证解析，LSP 端已改用 MTEXT 实体承载文字）
    最终桥指令文件 100% 纯 ASCII，从源头消除编码歧义。"""
    escaped = s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    out = []
    for ch in escaped:
        o = ord(ch)
        out.append(ch if o < 128 else f"\\U+{o:04X}")
    return "".join(out)


def _tmpl_flange(params: dict, bx: float, by: float) -> list[str]:
    outer_d = _num(params, "outerD", 100.0)
    inner_d = _num(params, "innerD", 50.0)
    bolt_n = max(int(_num(params, "boltCount", 6)), 1)
    bolt_cd = _num(params, "boltCircleD", 75.0)
    bolt_d = _num(params, "boltD", 9.0)
    r_o, r_i, r_bc, r_b = outer_d / 2, inner_d / 2, bolt_cd / 2, bolt_d / 2
    cx, cy = bx + r_o, by + r_o
    ext = 8.0
    ops = [
        f"LINE|{_pf(cx - r_o - ext)}|{_pf(cy)}|{_pf(cx + r_o + ext)}|{_pf(cy)}|CENTER|1",
        f"LINE|{_pf(cx)}|{_pf(cy - r_o - ext)}|{_pf(cx)}|{_pf(cy + r_o + ext)}|CENTER|1",
        f"CIRCLE|{_pf(cx)}|{_pf(cy)}|{_pf(r_bc)}|CENTER|1",
        f"CIRCLE|{_pf(cx)}|{_pf(cy)}|{_pf(r_o)}|OUTLINE|7",
        f"CIRCLE|{_pf(cx)}|{_pf(cy)}|{_pf(r_i)}|OUTLINE|7",
    ]
    for i in range(bolt_n):
        a = math.radians(360.0 * i / bolt_n + 90.0)
        hx, hy = cx + r_bc * math.cos(a), cy + r_bc * math.sin(a)
        ops.append(f"CIRCLE|{_pf(hx)}|{_pf(hy)}|{_pf(r_b)}|OUTLINE|7")
        ops.append(f"LINE|{_pf(hx - r_b * 1.6)}|{_pf(hy)}|{_pf(hx + r_b * 1.6)}|{_pf(hy)}|CENTER|1")
        ops.append(f"LINE|{_pf(hx)}|{_pf(hy - r_b * 1.6)}|{_pf(hx)}|{_pf(hy + r_b * 1.6)}|CENTER|1")
    ops.append(f"DIM|{_pf(cx - r_o)}|{_pf(cy)}|{_pf(cx + r_o)}|{_pf(cy)}|10|DIM|3")
    ops.append(f"TEXT|TEXT|4|{_pf(cx)}|{_pf(cy - r_o - 22)}|5|0|{_u(f'法兰 DN{inner_d:.0f}')}")
    return ops


def _tmpl_gasket(params: dict, bx: float, by: float) -> list[str]:
    w = _num(params, "width", 120.0)
    h = _num(params, "height", 80.0)
    cr = min(_num(params, "cornerR", 10.0), w / 2 - 1, h / 2 - 1)
    hole_n = max(int(_num(params, "holeCount", 4)), 1)
    hole_d = _num(params, "holeD", 9.0)
    margin = _num(params, "edgeMargin", 10.0)
    x2, y2 = bx + w, by + h
    r_h = hole_d / 2
    ops: list[str] = []
    if cr > 0.5:
        ops += [
            f"ARC|{_pf(bx + cr)}|{_pf(by + cr)}|{_pf(cr)}|180|270|OUTLINE|7",
            f"ARC|{_pf(x2 - cr)}|{_pf(by + cr)}|{_pf(cr)}|270|360|OUTLINE|7",
            f"ARC|{_pf(x2 - cr)}|{_pf(y2 - cr)}|{_pf(cr)}|0|90|OUTLINE|7",
            f"ARC|{_pf(bx + cr)}|{_pf(y2 - cr)}|{_pf(cr)}|90|180|OUTLINE|7",
            f"LINE|{_pf(bx + cr)}|{_pf(by)}|{_pf(x2 - cr)}|{_pf(by)}|OUTLINE|7",
            f"LINE|{_pf(x2)}|{_pf(by + cr)}|{_pf(x2)}|{_pf(y2 - cr)}|OUTLINE|7",
            f"LINE|{_pf(bx + cr)}|{_pf(y2)}|{_pf(x2 - cr)}|{_pf(y2)}|OUTLINE|7",
            f"LINE|{_pf(bx)}|{_pf(by + cr)}|{_pf(bx)}|{_pf(y2 - cr)}|OUTLINE|7",
        ]
    else:
        pts = f"{_pf(bx)},{_pf(by)} {_pf(x2)},{_pf(by)} {_pf(x2)},{_pf(y2)} {_pf(bx)},{_pf(y2)}"
        ops.append(f"PLINE|{pts}|1|OUTLINE|7")
    hole_y = by + margin + r_h
    if hole_n >= 2:
        span = w - 2 * (margin + r_h)
        for i in range(hole_n):
            hx = bx + margin + r_h + span * i / (hole_n - 1)
            ops.append(f"CIRCLE|{_pf(hx)}|{_pf(hole_y)}|{_pf(r_h)}|OUTLINE|7")
            ops.append(f"LINE|{_pf(hx - r_h * 1.6)}|{_pf(hole_y)}|{_pf(hx + r_h * 1.6)}|{_pf(hole_y)}|CENTER|1")
            ops.append(f"LINE|{_pf(hx)}|{_pf(hole_y - r_h * 1.6)}|{_pf(hx)}|{_pf(hole_y + r_h * 1.6)}|CENTER|1")
    else:
        ops.append(f"CIRCLE|{_pf(bx + w / 2)}|{_pf(hole_y)}|{_pf(r_h)}|OUTLINE|7")
    ops.append(f"DIM|{_pf(bx)}|{_pf(by - 12)}|{_pf(x2)}|{_pf(by - 12)}|0|DIM|3")
    ops.append(f"DIM|{_pf(x2 + 12)}|{_pf(by)}|{_pf(x2 + 12)}|{_pf(y2)}|0|DIM|3")
    ops.append(f"TEXT|TEXT|4|{_pf(bx + w / 2)}|{_pf(by - 26)}|5|0|{_u(f'垫片 {w:.0f}x{h:.0f}')}")
    return ops


def _tmpl_shaft(params: dict, bx: float, by: float) -> list[str]:
    segs: list[tuple[float, float]] = []
    raw = params.get("segments")
    if isinstance(raw, list):
        for s in raw:
            if isinstance(s, dict):
                segs.append((_num(s, "d", 30.0), _num(s, "len", 40.0)))
            elif isinstance(s, (list, tuple)) and len(s) >= 2:
                segs.append((float(s[0]), float(s[1])))
    if not segs:
        segs = [(30.0, 50.0), (45.0, 30.0), (30.0, 50.0)]
    r_max = max(d for d, _ in segs) / 2
    total_len = sum(ln for _, ln in segs)
    ops = [f"LINE|{_pf(bx - 8)}|{_pf(by)}|{_pf(bx + total_len + 8)}|{_pf(by)}|CENTER|1"]
    top: list[tuple[float, float]] = [(bx, by + segs[0][0] / 2)]
    x = bx
    for d, ln in segs:
        x += ln
        top.append((x, by + d / 2))
    bot: list[tuple[float, float]] = []
    x = bx + total_len
    for d, ln in reversed(segs):
        bot.append((x, by - d / 2))
        x -= ln
    tps = " ".join(f"{_pf(px)},{_pf(py)}" for px, py in top)
    bps = " ".join(f"{_pf(px)},{_pf(py)}" for px, py in bot)
    ops.append(f"PLINE|{tps}|0|OUTLINE|7")
    ops.append(f"PLINE|{bps}|0|OUTLINE|7")
    ops.append(f"LINE|{_pf(bx)}|{_pf(by + segs[0][0] / 2)}|{_pf(bx)}|{_pf(by - segs[0][0] / 2)}|OUTLINE|7")
    ops.append(f"LINE|{_pf(bx + total_len)}|{_pf(by + segs[-1][0] / 2)}|{_pf(bx + total_len)}|{_pf(by - segs[-1][0] / 2)}|OUTLINE|7")
    x = bx
    prev_r = segs[0][0] / 2
    for d, ln in segs[1:]:
        x += ln
        r = d / 2
        if abs(r - prev_r) > 0.01:
            ops.append(f"LINE|{_pf(x - ln)}|{_pf(by + prev_r)}|{_pf(x - ln)}|{_pf(by + r)}|OUTLINE|7")
            ops.append(f"LINE|{_pf(x - ln)}|{_pf(by - prev_r)}|{_pf(x - ln)}|{_pf(by - r)}|OUTLINE|7")
        prev_r = r
    x = bx
    for d, ln in segs:
        ops.append(f"DIM|{_pf(x)}|{_pf(by - r_max - 14)}|{_pf(x + ln)}|{_pf(by - r_max - 14)}|0|DIM|3")
        ops.append(f"TEXT|TEXT|4|{_pf(x + ln / 2)}|{_pf(by + r_max + 10)}|4|0|D{d:.0f}")
        x += ln
    return ops


def _tmpl_hex_bolt(params: dict, bx: float, by: float) -> list[str]:
    across = _num(params, "headAcross", 24.0)
    bolt_d = _num(params, "boltD", 16.0)
    bolt_len = _num(params, "boltLen", 50.0)
    r_head = (across / 2) / math.cos(math.radians(30))
    cx, cy = bx + r_head, by + r_head
    pts = []
    for i in range(6):
        a = math.radians(60 * i + 90)
        pts.append((cx + r_head * math.cos(a), cy + r_head * math.sin(a)))
    hex_pts = " ".join(f"{_pf(px)},{_pf(py)}" for px, py in pts)
    e = r_head + 6
    ops = [
        f"PLINE|{hex_pts}|1|OUTLINE|7",
        f"CIRCLE|{_pf(cx)}|{_pf(cy)}|{_pf(across / 2)}|OUTLINE|7",
        f"CIRCLE|{_pf(cx)}|{_pf(cy)}|{_pf(bolt_d / 2)}|OUTLINE|7",
        f"ARC|{_pf(cx)}|{_pf(cy)}|{_pf(bolt_d / 2 * 0.82)}|120|30|OUTLINE|7",
        f"LINE|{_pf(cx - e)}|{_pf(cy)}|{_pf(cx + e)}|{_pf(cy)}|CENTER|1",
        f"LINE|{_pf(cx)}|{_pf(cy - e)}|{_pf(cx)}|{_pf(cy + e)}|CENTER|1",
        f"TEXT|TEXT|4|{_pf(cx)}|{_pf(cy - r_head - 14)}|4.5|0|M{bolt_d:.0f}x{bolt_len:.0f}",
    ]
    return ops


def _tmpl_room(params: dict, bx: float, by: float) -> list[str]:
    w = _num(params, "width", 6000.0)
    h = _num(params, "height", 4000.0)
    t = _num(params, "wallT", 240.0)
    door_w = _num(params, "doorWidth", 900.0)
    win_w = _num(params, "windowWidth", 1500.0)
    ox, oy = bx + t / 2, by + t / 2
    ix, iy = bx - t / 2, by - t / 2
    door_x0 = ox + (w - door_w) / 2
    door_x1 = door_x0 + door_w
    ops = [
        f"LINE|{_pf(ox)}|{_pf(oy)}|{_pf(door_x0)}|{_pf(oy)}|WALL|7",
        f"LINE|{_pf(door_x1)}|{_pf(oy)}|{_pf(ox + w)}|{_pf(oy)}|WALL|7",
        f"PLINE|{_pf(ox + w)},{_pf(oy)} {_pf(ox + w)},{_pf(oy + h)} {_pf(ox)},{_pf(oy + h)} {_pf(ox)},{_pf(oy)}|1|WALL|7",
        f"PLINE|{_pf(ix)},{_pf(iy)} {_pf(ix + w)},{_pf(iy)} {_pf(ix + w)},{_pf(iy + h)} {_pf(ix)},{_pf(iy + h)}|1|WALL|7",
        f"LINE|{_pf(door_x0)}|{_pf(oy)}|{_pf(door_x0)}|{_pf(iy)}|WALL|7",
        f"LINE|{_pf(door_x1)}|{_pf(oy)}|{_pf(door_x1)}|{_pf(iy)}|WALL|7",
        f"LINE|{_pf(door_x0)}|{_pf(oy)}|{_pf(door_x0)}|{_pf(oy + door_w)}|WALL|7",
        f"ARC|{_pf(door_x0)}|{_pf(oy)}|{_pf(door_w)}|0|90|WALL|7",
    ]
    wx0 = ox + (w - win_w) / 2
    wx1 = wx0 + win_w
    y_in = oy + h - t
    ops += [
        f"LINE|{_pf(wx0)}|{_pf(oy + h)}|{_pf(wx1)}|{_pf(oy + h)}|WALL|7",
        f"LINE|{_pf(wx0)}|{_pf(y_in)}|{_pf(wx1)}|{_pf(y_in)}|WALL|7",
        f"LINE|{_pf(wx0)}|{_pf(oy + h - t / 3)}|{_pf(wx1)}|{_pf(oy + h - t / 3)}|WALL|7",
        f"LINE|{_pf(wx0)}|{_pf(oy + h - t * 2 / 3)}|{_pf(wx1)}|{_pf(oy + h - t * 2 / 3)}|WALL|7",
        f"DIM|{_pf(ox)}|{_pf(oy - 600)}|{_pf(ox + w)}|{_pf(oy - 600)}|0|DIM|3",
        f"DIM|{_pf(ox - 600)}|{_pf(oy)}|{_pf(ox - 600)}|{_pf(oy + h)}|0|DIM|3",
        f"TEXT|TEXT|4|{_pf(ox + w / 2)}|{_pf(oy + h + 600)}|200|0|{w:.0f}x{h:.0f}",
    ]
    return ops


def _tmpl_grid(params: dict, bx: float, by: float) -> list[str]:
    col_sp = params.get("colSpacing")
    row_sp = params.get("rowSpacing")
    if isinstance(col_sp, list) and col_sp:
        col_spans = [float(v) for v in col_sp]
    else:
        col_spans = [_num(params, "colSpacing", 6000.0)] * max(int(_num(params, "cols", 3)), 1)
    if isinstance(row_sp, list) and row_sp:
        row_spans = [float(v) for v in row_sp]
    else:
        row_spans = [_num(params, "rowSpacing", 6000.0)] * max(int(_num(params, "rows", 2)), 1)
    ext, r_lab = 1000.0, 400.0
    W, H = sum(col_spans), sum(row_spans)
    xs = [bx]
    for s in col_spans:
        xs.append(xs[-1] + s)
    ys = [by]
    for s in row_spans:
        ys.append(ys[-1] + s)
    letters = "ABCDEFGHJKLM"
    ops: list[str] = []
    for i, x in enumerate(xs):
        ops.append(f"LINE|{_pf(x)}|{_pf(by - ext)}|{_pf(x)}|{_pf(by + H + ext)}|CENTER|1")
        ops.append(f"CIRCLE|{_pf(x)}|{_pf(by + H + ext + r_lab)}|{_pf(r_lab)}|TEXT|4")
        ops.append(f"TEXT|TEXT|4|{_pf(x)}|{_pf(by + H + ext + r_lab)}|{_pf(r_lab * 1.1)}|0|{i + 1}")
    for j, y in enumerate(ys):
        ops.append(f"LINE|{_pf(bx - ext)}|{_pf(y)}|{_pf(bx + W + ext)}|{_pf(y)}|CENTER|1")
        ops.append(f"CIRCLE|{_pf(bx - ext - r_lab)}|{_pf(y)}|{_pf(r_lab)}|TEXT|4")
        ops.append(f"TEXT|TEXT|4|{_pf(bx - ext - r_lab)}|{_pf(y)}|{_pf(r_lab * 1.1)}|0|{letters[j % len(letters)]}")
    ops.append(f"DIM|{_pf(xs[0])}|{_pf(by - ext - 500)}|{_pf(xs[-1])}|{_pf(by - ext - 500)}|0|DIM|3")
    ops.append(f"DIM|{_pf(bx - ext - 500)}|{_pf(ys[0])}|{_pf(bx - ext - 500)}|{_pf(ys[-1])}|0|DIM|3")
    return ops


_TEMPLATES = {
    "flange": _tmpl_flange,
    "gasket": _tmpl_gasket,
    "shaft": _tmpl_shaft,
    "hexBolt": _tmpl_hex_bolt,
    "room": _tmpl_room,
    "grid": _tmpl_grid,
}


# ==============================================================
# action 处理器
# ==============================================================
def _ensure_alive(auto_open: bool = True) -> Path:
    """统一连接逻辑（幂等，已就绪时零开销返回）：
    桥活着 → 直接返回；心跳陈旧/未加载 → 重投 LSP（acb-init 幂等停旧建新）；
    CAD 未运行且 auto_open → 自动启动 AutoCAD 并新建图纸后继续。
    绘图类 action 前自动调用，实现「对话即绘图」全自动链路。"""
    bdir = _bridge_dir()
    _ensure_anchor(bdir)
    if _is_connected(bdir):
        return bdir
    running, _prog = _probe_cad()
    if not running and auto_open:
        running, launch_err = _launch_cad()
        if not running:
            raise RuntimeError(f"CAD 未运行且自动启动失败：{launch_err}")
    ok, detail = _com_load_lsp(bdir)
    if not ok:
        raise RuntimeError(f"桥自动加载失败：{detail}（也可手动 APPLOAD 加载 {LSP_FILE}）")
    if not _wait_alive(bdir, timeout=6.0):
        raise RuntimeError("LSP 已投递但未见心跳；若 AutoCAD 弹出加载确认框请允许后重试")
    return bdir


def _layer_color(kwargs: dict) -> tuple[str, int]:
    return str(kwargs.get("layer") or "").strip(), int(kwargs.get("color") or 0)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _act_status(kwargs: dict) -> dict[str, Any]:
    bdir = _bridge_dir()
    state = _bridge_state(bdir) or ("RUNNING" if _is_alive(bdir) else "OFFLINE")
    age = _hb_age(bdir)
    cad_running, prog = _probe_cad()
    return {
        "ok": True,
        "connected": _is_connected(bdir),
        "state": state,
        "heartbeatAge": round(age, 1) if age is not None else None,
        "queuePending": len(list((bdir / "queue").glob("*.cmd"))),
        "cadRunning": cad_running,
        "cadProgId": prog,
        "bridgeDir": str(bdir),
        "lspPath": str(LSP_FILE),
        "templates": sorted(_TEMPLATES.keys()),
    }


def _act_connect(kwargs: dict) -> dict[str, Any]:
    auto_open = kwargs.get("autoOpen", True)
    try:
        bdir = _ensure_alive(auto_open=bool(auto_open))
    except RuntimeError as e:
        return {"ok": False, "connected": False, "error": str(e), "lspPath": str(LSP_FILE)}
    return {"ok": True, "connected": True, "state": _bridge_state(bdir) or "RUNNING",
            "bridgeDir": str(bdir), "lspPath": str(LSP_FILE),
            "hint": "桥已就绪，后续绘制将实时出现在当前图纸上"}


def _act_line(kwargs: dict) -> dict[str, Any]:
    lay, col = _layer_color(kwargs)
    name = _enqueue([f"LINE|{_f(kwargs.get('x1'))}|{_f(kwargs.get('y1'))}|"
                     f"{_f(kwargs.get('x2'))}|{_f(kwargs.get('y2'))}|{lay}|{col}"])
    r = _flush([name])
    r["entity"] = "LINE"
    r["handle"] = r["handles"][0] if r["handles"] else ""
    return r


def _get_pts(kwargs: dict, key: str = "points") -> list[tuple[float, float]]:
    v = kwargs.get(key)
    if v is None:
        raise ValueError(f"缺少参数 {key}")
    if isinstance(v, str):
        pts = []
        for seg in v.replace("，", ",").split(";"):
            seg = seg.strip()
            if not seg:
                continue
            xy = seg.split(",")
            if len(xy) < 2:
                raise ValueError(f"{key} 格式错误：{seg!r}")
            pts.append((float(xy[0]), float(xy[1])))
        return pts
    out = []
    for p in v:
        if isinstance(p, dict):
            out.append((_f(p.get("x")), _f(p.get("y"))))
        else:
            seq = list(p)
            if len(seq) < 2:
                raise ValueError(f"{key} 每项至少 x,y 两个数")
            out.append((_f(seq[0]), _f(seq[1])))
    if len(out) < 2:
        raise ValueError(f"{key} 至少需要 2 个点")
    return out


def _act_polyline(kwargs: dict) -> dict[str, Any]:
    pts = _get_pts(kwargs)
    lay, col = _layer_color(kwargs)
    pstr = " ".join(f"{_pf(x)},{_pf(y)}" for x, y in pts)
    name = _enqueue([f"PLINE|{pstr}|{1 if kwargs.get('closed') else 0}|{lay}|{col}"])
    r = _flush([name])
    r["entity"] = "LWPOLYLINE"
    r["handle"] = r["handles"][0] if r["handles"] else ""
    return r


def _act_circle(kwargs: dict) -> dict[str, Any]:
    r = _f(kwargs.get("r") or kwargs.get("radius"))
    if r <= 0:
        return {"ok": False, "error": "半径 r 必须大于 0"}
    lay, col = _layer_color(kwargs)
    name = _enqueue([f"CIRCLE|{_f(kwargs.get('cx') or kwargs.get('x'))}|{_f(kwargs.get('cy') or kwargs.get('y'))}|{_pf(r)}|{lay}|{col}"])
    rr = _flush([name])
    rr["entity"] = "CIRCLE"
    rr["handle"] = rr["handles"][0] if rr["handles"] else ""
    return rr


def _act_arc(kwargs: dict) -> dict[str, Any]:
    r = _f(kwargs.get("r") or kwargs.get("radius"))
    if r <= 0:
        return {"ok": False, "error": "半径 r 必须大于 0"}
    lay, col = _layer_color(kwargs)
    name = _enqueue([f"ARC|{_f(kwargs.get('cx') or kwargs.get('x'))}|{_f(kwargs.get('cy') or kwargs.get('y'))}|{_pf(r)}"
                     f"|{_f(kwargs.get('startAngle'))}|{_f(kwargs.get('endAngle', 90.0))}|{lay}|{col}"])
    rr = _flush([name])
    rr["entity"] = "ARC"
    rr["handle"] = rr["handles"][0] if rr["handles"] else ""
    return rr


def _act_ellipse(kwargs: dict) -> dict[str, Any]:
    ratio = _f(kwargs.get("ratio"), 0.5)
    if not (0 < ratio <= 1):
        return {"ok": False, "error": "ratio（短轴/长轴）必须在 (0, 1] 内"}
    lay, col = _layer_color(kwargs)
    name = _enqueue([f"ELLIPSE|{_f(kwargs.get('cx') or kwargs.get('x'))}|{_f(kwargs.get('cy') or kwargs.get('y'))}"
                     f"|{_f(kwargs.get('majorX', 50.0))}|{_f(kwargs.get('majorY'))}|{_pf(ratio)}|{lay}|{col}"])
    rr = _flush([name])
    rr["entity"] = "ELLIPSE"
    rr["handle"] = rr["handles"][0] if rr["handles"] else ""
    return rr


def _act_rectangle(kwargs: dict) -> dict[str, Any]:
    x, y = _f(kwargs.get("x")), _f(kwargs.get("y"))
    w, h = _f(kwargs.get("width")), _f(kwargs.get("height"))
    if w <= 0 or h <= 0:
        return {"ok": False, "error": "width / height 必须大于 0"}
    lay, col = _layer_color(kwargs)
    pstr = f"{_pf(x)},{_pf(y)} {_pf(x + w)},{_pf(y)} {_pf(x + w)},{_pf(y + h)} {_pf(x)},{_pf(y + h)}"
    name = _enqueue([f"PLINE|{pstr}|1|{lay}|{col}"])
    r = _flush([name])
    r["entity"] = "LWPOLYLINE"
    r["handle"] = r["handles"][0] if r["handles"] else ""
    return r


def _act_polygon(kwargs: dict) -> dict[str, Any]:
    n = int(_f(kwargs.get("sides"), 6))
    r = _f(kwargs.get("r") or kwargs.get("radius"))
    if n < 3 or r <= 0:
        return {"ok": False, "error": "sides ≥ 3 且 r > 0"}
    cx, cy = _f(kwargs.get("cx") or kwargs.get("x")), _f(kwargs.get("cy") or kwargs.get("y"))
    pts = [(cx + r * math.cos(math.radians(360 * i / n + 90)),
            cy + r * math.sin(math.radians(360 * i / n + 90))) for i in range(n)]
    lay, col = _layer_color(kwargs)
    pstr = " ".join(f"{_pf(px)},{_pf(py)}" for px, py in pts)
    name = _enqueue([f"PLINE|{pstr}|1|{lay}|{col}"])
    rr = _flush([name])
    rr["entity"] = "LWPOLYLINE"
    rr["sides"] = n
    rr["handle"] = rr["handles"][0] if rr["handles"] else ""
    return rr


def _act_text(kwargs: dict) -> dict[str, Any]:
    s = str(kwargs.get("text") or "").strip()
    if not s:
        return {"ok": False, "error": "text 不能为空"}
    lay, col = _layer_color(kwargs)
    name = _enqueue([f"TEXT|{lay}|{col}|{_f(kwargs.get('x'))}|{_f(kwargs.get('y'))}"
                     f"|{_f(kwargs.get('height'), 3.5)}|{_f(kwargs.get('rotation'))}|{_u(s)}"])
    r = _flush([name])
    r["entity"] = "MTEXT"
    r["handle"] = r["handles"][0] if r["handles"] else ""
    return r


def _act_dimension(kwargs: dict) -> dict[str, Any]:
    lay, col = _layer_color(kwargs)
    name = _enqueue([f"DIM|{_f(kwargs.get('x1'))}|{_f(kwargs.get('y1'))}|{_f(kwargs.get('x2'))}|{_f(kwargs.get('y2'))}"
                     f"|{_f(kwargs.get('offset'), 8.0)}|{lay}|{col}"])
    r = _flush([name])
    r["entity"] = "DIMENSION"
    r["handle"] = r["handles"][0] if r["handles"] else ""
    return r


def _act_erase(kwargs: dict) -> dict[str, Any]:
    h = str(kwargs.get("handle") or "").strip()
    if not h:
        return {"ok": False, "error": "需要 handle 参数"}
    name = _enqueue([f"ERASE|{h}"])
    r = _flush([name])
    r["erased"] = h
    return r


def _act_read_drawing(kwargs: dict) -> dict[str, Any]:
    name = _enqueue(["READ|"])
    r = _flush([name], timeout=60.0)
    if not r["ok"]:
        return r
    lines = _read_res(name) or []
    data = _parse_read(lines)
    return {"ok": True, **data}


def _act_save(kwargs: dict) -> dict[str, Any]:
    name = _enqueue(["SAVE|"])
    r = _flush([name])
    for n in [name]:
        for line in (_read_res(n) or []):
            if line.startswith("OK|SAVE|"):
                r["file"] = line[8:]
    return r


def _act_save_as(kwargs: dict) -> dict[str, Any]:
    fp = str(kwargs.get("filePath") or "").strip()
    if not fp:
        return {"ok": False, "error": "需要 filePath 参数"}
    p = Path(fp)
    p.parent.mkdir(parents=True, exist_ok=True)
    name = _enqueue([f"SAVEAS|{str(p).replace(chr(92), '/')}"])
    r = _flush([name])
    for n in [name]:
        for line in (_read_res(n) or []):
            if line.startswith("OK|SAVEAS|"):
                r["file"] = line[11:]
    return r


def _act_zoom(kwargs: dict) -> dict[str, Any]:
    name = _enqueue(["ZOOM|E"])
    return _flush([name])


def _act_control(kwargs: dict, cmd: str) -> dict[str, Any]:
    bdir = _bridge_dir()
    (bdir / "control.txt").write_text(cmd + "\n", encoding="mbcs")
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if _bridge_state(bdir) == ({"PAUSE": "PAUSED", "RESUME": "RUNNING", "STOP": "STOPPED"}[cmd]):
            break
        time.sleep(0.2)
    return {"ok": True, "command": cmd, "state": _bridge_state(bdir) or "未知", "connected": _is_alive(bdir)}


def _act_install_autoload(kwargs: dict) -> dict[str, Any]:
    """把桥写入 AutoCAD「启动组」（注册表 Applications 键，LOADCTRLS=2）。
    之后每次打开 AutoCAD 桥自动就绪，connect 近乎零耗时（已开 CAD 时秒连）。"""
    try:
        import winreg
    except ImportError:
        return {"ok": False, "error": "winreg 不可用（仅限 Windows）"}
    installed: list[str] = []
    base = r"Software\Autodesk\AutoCAD"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                r_key = winreg.EnumKey(root, i)
                try:
                    with winreg.OpenKey(root, r_key) as rk:
                        for j in range(winreg.QueryInfoKey(rk)[0]):
                            acad = winreg.EnumKey(rk, j)
                            if not acad.startswith("ACAD-"):
                                continue
                            app_key = f"{base}\\{r_key}\\{acad}\\Applications\\CadBridge"
                            try:
                                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app_key) as k:
                                    winreg.SetValueEx(k, "LOADER", 0, winreg.REG_SZ, str(LSP_FILE))
                                    winreg.SetValueEx(k, "LOADCTRLS", 0, winreg.REG_DWORD, 2)
                                    winreg.SetValueEx(k, "MANAGED", 0, winreg.REG_DWORD, 0)
                                    winreg.SetValueEx(k, "DESCRIPTION", 0, winreg.REG_SZ, "AgentCluster CAD bridge")
                                installed.append(f"{r_key}\\{acad}")
                            except OSError:
                                continue
                except OSError:
                    continue
    except OSError:
        return {"ok": False, "error": "未找到 Autodesk 注册表项（本机可能未安装 AutoCAD）"}
    if not installed:
        return {"ok": False, "error": "未找到任何 AutoCAD 配置项，无法写入启动组"}
    return {"ok": True, "installed": installed, "lspPath": str(LSP_FILE),
            "hint": "下次启动 AutoCAD 时桥自动加载（当前会话需 connect 一次或重载）"}


def _act_uninstall_autoload(kwargs: dict) -> dict[str, Any]:
    try:
        import winreg
    except ImportError:
        return {"ok": False, "error": "winreg 不可用（仅限 Windows）"}
    removed: list[str] = []
    base = r"Software\Autodesk\AutoCAD"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                r_key = winreg.EnumKey(root, i)
                try:
                    with winreg.OpenKey(root, r_key) as rk:
                        for j in range(winreg.QueryInfoKey(rk)[0]):
                            acad = winreg.EnumKey(rk, j)
                            if not acad.startswith("ACAD-"):
                                continue
                            app_key = f"{base}\\{r_key}\\{acad}\\Applications\\CadBridge"
                            try:
                                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, app_key)
                                removed.append(f"{r_key}\\{acad}")
                            except OSError:
                                continue
                except OSError:
                    continue
    except OSError:
        pass
    return {"ok": True, "removed": removed}


def _act_template(kwargs: dict) -> dict[str, Any]:
    what = str(kwargs.get("what") or "").strip()
    fn = _TEMPLATES.get(what)
    if fn is None:
        return {"ok": False, "error": f"未知模板：{what}，可选：{sorted(_TEMPLATES.keys())}"}
    ops = fn(_get_params(kwargs), _f(kwargs.get("baseX")), _f(kwargs.get("baseY")))
    name = _enqueue(ops)
    r = _flush([name])
    r["template"] = what
    r["ops"] = len(ops)
    return r


def _act_self_test(kwargs: dict) -> dict[str, Any]:
    """文字链路一键自诊断：入队一条含中文的 TEXT，检查回执里 LSP 读到的原始字节。
    - debug 字节全部 <128 且含 \\U+ 序列 → Python 转义已生效，LSP 侧读到纯 ASCII（渲染交由 MTEXT 官方转义解析）
    - debug 字节含 >127 → 当前后端跑的是未转义的旧代码（需托盘退出重启后端）"""
    bdir = _ensure_alive(auto_open=bool(kwargs.get("autoOpen", True)))
    sample = "中文测试AB123"
    name = _enqueue([f"TEXT|TEXT|4|0|0|5|0|{_u(sample)}"])
    r = _flush([name])
    dbg = r.get("debug", [])
    raw = ""
    for d in dbg:
        if d.startswith("DBG|TEXT|"):
            raw = d[len("DBG|TEXT|"):].strip()
            break
    byte_list = [int(x) for x in raw.split() if x.isdigit()]
    all_ascii = bool(byte_list) and all(b < 128 for b in byte_list)
    has_escape = raw.startswith("92 85 43")  # "\U+" 的 ASCII 码
    verdict = ("PASS: 转义已生效，LSP 读到纯 ASCII（文字应由 MTEXT 正常渲染中文）"
               if all_ascii and has_escape
               else "FAIL: LSP 读到非 ASCII 字节——当前后端仍在跑未转义的旧代码，请托盘退出重启后端后重试")
    return {
        "ok": True,
        "verdict": verdict,
        "pythonEscaped": all_ascii and has_escape,
        "lspRawBytes": raw[:200],
        "debug": dbg,
        "entity": r.get("handle"),
    }


_ACTIONS = {
    "status": _act_status,
    "connect": _act_connect,
    "line": _act_line,
    "polyline": _act_polyline,
    "circle": _act_circle,
    "arc": _act_arc,
    "ellipse": _act_ellipse,
    "rectangle": _act_rectangle,
    "polygon": _act_polygon,
    "text": _act_text,
    "dimension": _act_dimension,
    "erase": _act_erase,
    "readDrawing": _act_read_drawing,
    "save": _act_save,
    "saveAs": _act_save_as,
    "zoom": _act_zoom,
    "pause": lambda kw: _act_control(kw, "PAUSE"),
    "resume": lambda kw: _act_control(kw, "RESUME"),
    "stop": lambda kw: _act_control(kw, "STOP"),
    "template": _act_template,
    "selfTest": _act_self_test,
    "installAutoload": _act_install_autoload,
    "uninstallAutoload": _act_uninstall_autoload,
}

# 需要桥就绪才能执行的 action（执行前自动连接，CAD 未运行时自动启动）
_NEED_BRIDGE = {"line", "polyline", "circle", "arc", "ellipse", "rectangle", "polygon",
                "text", "dimension", "erase", "readDrawing", "save", "saveAs", "zoom", "template"}


def run(action: str = "", **kwargs: Any) -> dict[str, Any]:
    if not action:
        return {"ok": False, "error": "action 不能为空"}
    handler = _ACTIONS.get(action)
    if handler is None:
        return {"ok": False, "error": f"未知 action：{action}，可选：{sorted(_ACTIONS.keys())}"}
    try:
        if action in _NEED_BRIDGE:
            _ensure_alive(auto_open=True)
        result = handler(kwargs)
        result.setdefault("mode", "lisp-bridge")
        if _is_connected(Path(_bridge_dir())):
            result.setdefault("connected", True)
        return result
    except Exception as e:
        return {"ok": False, "error": f"执行 {action} 失败：{e}"}