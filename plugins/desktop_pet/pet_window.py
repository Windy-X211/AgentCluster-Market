"""pet_window.py — PyQt5 透明窗口 + 离屏 WebEngine 渲染 + QPainter 逐帧合成

渲染链路（主）：离屏 QWebEngineView(WebGL) → gl.readPixels 原始 RGBA
→ fetch POST 127.0.0.1 帧服务器 → QImage（预乘 alpha）→ QPainter 镜像绘制。
渲染链路（回退）：toDataURL(PNG) → QWebChannel → Python 解码 → QPainter。

原始 RGBA 通道省掉了 PNG 编码（65ms/帧）+ 解码（32ms/帧），实测帧率从
≈9fps 提升到 25~40fps；端点不可用时自动回退旧的 dataURL 通道。

注意：readPixels 行序自下而上（OpenGL 原点在左下），因此 QImage 里是倒置的，
绘制时用 QPainter scale(1,-1) 镜像；负 stride 的 QImage 会 access violation（已实测）。

不能直接用透明 QWebEngineView 显示（Windows GPU 合成不输出 alpha，透明变黑）。
"""
import json
import math
import os
import re
import socket
import base64
import ctypes
import subprocess
import urllib.request
import hashlib
import shutil
import functools
import threading
import queue
import random
import sys
import time
import traceback
import uuid
import http.server
import socketserver
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# === 路径常量（纯计算，import 时不做任何磁盘 / 环境操作）===
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PROFILE_DIR = os.path.join(_PLUGIN_DIR, "_webengine_profile")
# worker 自述文件目录：父进程重启后靠它把还在屏幕上的 worker 找回来（否则注册表一空
# 就「漏掉已经打开的模型」）。按 pid 命名，同一模型多开也不会互相覆盖。
_WORKERS_DIR = os.path.join(_PLUGIN_DIR, "_workers")
# 诊断：JS console 落盘（原先被 SilentPage 静默吞掉，炸模时无任何取证信息）
_JS_LOG_PATH = os.path.join(_PLUGIN_DIR, "_js_console.log")
# 诊断：帧落盘（仅当插件目录下存在 _dump_frames.on 时生效）
_DUMP_FLAG_PATH = os.path.join(_PLUGIN_DIR, "_dump_frames.on")
_DUMP_FRAMES = False          # 真正启动窗口时由 _ensure_runtime_env() 决定
_FRAME_DUMP_DIR = os.path.join(_PLUGIN_DIR, "_frames")
_FRAME_DUMP_STATE = {"n": 0}
_ENV_READY = False


class _ElectronPage:
    """把渲染页换成一个本地 Electron 窗口的 HTTP 桥。

    只实现 worker 实际用到的 runJavaScript：命令走一个串行后台队列，
    结果同步命令通过 callback 回传，避免阻塞 Qt 主线程。
    """

    def __init__(self, port: int):
        self.port = int(port)
        self._q = queue.Queue()
        self._bounds_q = queue.Queue(maxsize=1)   # 只保留最新几何，缩放时合并旧帧
        self._bounds_ack = threading.Event()      # Electron 已应用上一帧 bounds
        self._bounds_ack.set()
        self._target_q = queue.Queue(maxsize=16)   # 动画步进按顺序发送，不能被合并掉
        # 全屏模式：窗口几何固定，缩放/拖动只更新 Stage/ClipBox。
        # layout 队列独立于旧 target/bounds 队列，动画每帧仍按顺序送达。
        self._layout_q = queue.Queue(maxsize=16)
        self._closed = False
        self._union_mode = False
        # __petTarget 的全局单调序号：动画步进（_target_worker 异步线程）与
        # 动画结束时的最终几何（Qt 主线程同步 /eval）并发下发时可能乱序到达，
        # 旧步进覆盖新目标会让模型先偏再弹回。所有写目标的地方统一取序号，
        # 渲染层只接受序号递增的写入。
        self._tseq = 0
        self._t = threading.Thread(target=self._worker, name="electron-page", daemon=True)
        self._t.start()
        self._bt = threading.Thread(target=self._bounds_worker,
                                    name="electron-bounds", daemon=True)
        self._bt.start()
        self._tt = threading.Thread(target=self._target_worker,
                                    name="electron-target", daemon=True)
        self._tt.start()
        self._lt = threading.Thread(target=self._layout_worker,
                                    name="electron-layout", daemon=True)
        self._lt.start()
        # 画面延迟探针：仅在 PERF 打开时每秒向渲染层读一次 __lagStat，
        # 拿到"下发→应用"的真实延迟（不打印到 console，避免日志洪泛）。
        if _PERF["on"]:
            self._lagt = threading.Thread(target=self._lag_worker,
                                          name="electron-lagprobe", daemon=True)
            self._lagt.start()

    def _lag_worker(self):
        while not self._closed:
            time.sleep(1.0)
            try:
                res = self._post(
                    "/eval", {"js":
                              "(function(){var s=window.__lagStat||{n:0,sum:0,max:0,n_old:0};"
                              "var r=JSON.stringify({n:s.n,sum:s.sum,max:s.max,n_old:s.n_old});"
                              "s.n=0;s.sum=0;s.max=0;s.n_old=0;return r;})()"},
                    timeout=1.5)
                raw = res.get("result") if isinstance(res, dict) else None
                if isinstance(raw, str) and raw:
                    st = json.loads(raw)
                    n = int(st.get("n") or 0)
                    _PERF["vlag_n"] = n
                    _PERF["vlag_sum"] = float(st.get("sum") or 0.0) if n else 0.0
                    _PERF["vlag_max"] = float(st.get("max") or 0.0) if n else 0.0
                    _PERF["vlag_old_n"] = int(st.get("n_old") or 0)
            except Exception:
                pass

    def _post(self, path: str, obj: dict, timeout: float = 6.0) -> dict:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path), data=data,
            headers={"Content-Type": "application/json; charset=utf-8"})
        _t = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        finally:
            if _PERF["on"]:
                _perf_http(path, (time.monotonic() - _t) * 1000.0)

    def _worker(self):
        while not self._closed:
            try:
                script, cb = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                res = None
                for attempt in range(8):
                    res = self._post("/eval", {"js": script}, timeout=8.0)
                    err = str(res.get("error") or "")
                    if res.get("ok") or "not defined" not in err:
                        break
                    time.sleep(0.25)
                out = res.get("result") if isinstance(res, dict) else None
            except Exception:
                out = None
            if cb is not None:
                try:
                    cb(out)
                except Exception:
                    pass

    def runJavaScript(self, script: str, callback=None):
        if _PERF["on"]:
            _PERF["js_n"] += 1
        if not self._closed:
            self._q.put((script, callback))

    def _next_seq(self) -> int:
        """取下一个全局单调序号（只在 Qt 主线程调用）。"""
        self._tseq += 1
        return self._tseq

    def set_bounds(self, x: int, y: int, width: int, height: int):
        """非阻塞地同步 Electron 窗口几何。

        滚轮缩放每 16ms 改一次窗口，旧实现直接在 Qt 主线程里 urllib
        同步 POST；网络往返会把动画节拍拖成几十毫秒一节，Qt 覆盖层和
        Electron 模型窗就不同步，肉眼看到的就是模型左右/上下晃。
        这里只保留最新一帧，由后台线程发送，主线程永不等待。
        """
        if self._closed:
            return
        payload = {"x": int(x), "y": int(y),
                   "width": int(width), "height": int(height),
                   "seq": self._next_seq()}
        self._bounds_ack.clear()
        try:
            self._bounds_q.get_nowait()      # 丢掉还没发出去的旧尺寸
        except queue.Empty:
            pass
        try:
            self._bounds_q.put_nowait(payload)
        except queue.Full:
            pass

    def bounds_pending(self) -> bool:
        return not self._bounds_ack.is_set()

    def set_target(self, x: int, y: int, width: int, height: int):
        """只告诉渲染层“目标几何”，不动 Electron 窗口。

        滚轮缩放时窗口保持一个能容纳起止两帧的并集，模型由 renderer
        按目标几何做缩放/位移；这样窗口位置和内容尺寸谁先谁后都不会
        让模型先偏移再弹回。
        """
        if self._closed:
            return
        payload = {"x": int(x), "y": int(y),
                   "width": int(width), "height": int(height),
                   "seq": self._next_seq()}
        try:
            self._target_q.put_nowait(payload)
        except queue.Full:
            pass

    def clear_targets(self):
        try:
            while True:
                self._target_q.get_nowait()
        except queue.Empty:
            pass

    def set_layout(self, stage: tuple, clip: tuple, mode: str):
        """全屏渲染层的布局：Stage=模型局部盒，Clip=可见裁剪盒。

        窗口保持全屏不动；缩放/拖动只更新这两个屏幕矩形。走后台队列，
        Qt 主线程不等 HTTP，动画步进不会被网络往返拖慢。
        """
        if self._closed:
            return
        payload = {
            "stage": [int(round(v)) for v in stage],
            "clip": [int(round(v)) for v in clip],
            "mode": "full" if str(mode) == "full" else "local",
            "seq": self._next_seq(),
            # 下发时刻（墙钟毫秒）：渲染层应用后回算"光标→画面"的真实延迟。
            "t": int(time.time() * 1000.0),
        }
        try:
            self._layout_q.put_nowait(payload)
        except queue.Full:
            try:
                self._layout_q.get_nowait()   # 丢最旧的，保留最新布局
            except queue.Empty:
                pass
            try:
                self._layout_q.put_nowait(payload)
            except queue.Full:
                pass

    def show_window(self):
        """显示 Electron 层；Qt show 时它可能仍处于 hide。"""
        try:
            self._post("/visible", {"on": True}, timeout=1.5)
            # show 后重新压置顶；窗口几何由启动时的 force bounds 负责。
            self._post("/eval", {"js": "true"}, timeout=1.0)
        except Exception:
            pass

    def hide_window(self):
        try:
            self._post("/visible", {"on": False}, timeout=1.2)
        except Exception:
            pass

    def _layout_worker(self):
        while not self._closed:
            try:
                b = self._layout_q.get(timeout=0.5)
            except queue.Empty:
                continue
            # 拖动时鼠标每 8ms 就发一帧，而每次 __applyPetLayout 的往返要 ~17ms，
            # 队列一积压，画面就稳定落后光标 100ms 以上（实测 avg 128 / max 178ms）。
            # 布局只需要"最新一帧"（旧帧渲染层还会因 seq 过期丢掉），所以这里把
            # 队列里剩下的全部丢弃、只应用最后一条：积压恒定 ≤1 帧。
            while True:
                try:
                    b = self._layout_q.get_nowait()
                except queue.Empty:
                    break
            try:
                script = "window.__applyPetLayout(" + json.dumps(b, ensure_ascii=False) + ");true"
                for _ in range(6):
                    _lt0 = time.monotonic() if _PERF["on"] else 0.0
                    res = self._post("/eval", {"js": script}, timeout=1.2)
                    if _PERF["on"]:
                        _lms = (time.monotonic() - _lt0) * 1000.0
                        _PERF["layout_eval_n"] += 1
                        _PERF["layout_eval_ms"] += _lms
                        if _lms > _PERF["layout_eval_max"]:
                            _PERF["layout_eval_max"] = _lms
                    err = str(res.get("error") or "")
                    if res.get("ok") or "not defined" not in err:
                        break
                    time.sleep(0.15)
            except Exception:
                pass

    def set_union_mode(self, on: bool):
        """固定并集动画期间，窗口 bounds 不能反过来覆盖动画目标。"""
        self._union_mode = bool(on)

    def _target_worker(self):
        while not self._closed:
            try:
                b = self._target_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._post(
                    "/eval",
                    {"js": "window.__petTarget=" + json.dumps(b) + ";true"},
                    timeout=1.0)
            except Exception:
                pass

    def _bounds_worker(self):
        while not self._closed:
            try:
                b = self._bounds_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                # 目标几何要“前后各发一次”：
                # - setBounds 之前发：窗口位置一变，renderer 下一帧就知道该
                #   把模型画到哪个目标位置；
                # - setBounds 之后发：setter 立刻重算一次，不等下一帧。
                # 固定并集动画期间目标由动画步进负责；这里只改窗口，
                # 否则 bounds 会把“最终的并集几何”写成 __petTarget，
                # 模型先跳到最终大小，再被后续动画步拉回，就是缩放突变。
                if not self._union_mode:
                    try:
                        self._post(
                            "/eval",
                            {"js": "window.__petTarget=" + json.dumps(b) + ";true"},
                            timeout=1.0)
                    except Exception:
                        pass
                self._post("/bounds", b, timeout=2.0)
                if not self._union_mode:
                    try:
                        self._post(
                            "/eval",
                            {"js": "window.__petTarget=" + json.dumps(b) + ";true"},
                            timeout=1.0)
                    except Exception:
                        pass
                # setBounds 返回时 Electron 的窗口框已经变了，但 Chromium
                # 的视口/模型居中可能还晚 1~2 帧。这里等 renderer 的
                # targetW/targetH 追平 innerWidth/Height 再放行下一帧，
                # 避免 Qt 先走一步、模型在屏幕上偏移后再被拉回。
                t0 = time.monotonic()
                while time.monotonic() - t0 < 0.25:
                    try:
                        r = self._post(
                            "/eval",
                            {"js": "(window.innerWidth===targetW&&window.innerHeight===targetH)?'ok':'no'"},
                            timeout=1.0)
                    except Exception:
                        break
                    if str((r or {}).get("result")) == "ok":
                        break
                    time.sleep(0.01)
            except Exception:
                # 失败就丢弃：不把旧尺寸补发回去。缩放动画期间重试旧值
                # 会让模型先退回上一帧、再弹到新尺寸，看起来就是“晃一下”。
                # 启动阶段的窗口未就绪由 0/300/900/1800ms 的补同步兜底。
                pass
            finally:
                self._bounds_ack.set()

    def close(self):
        self._closed = True


def _start_electron_display(model_url: str, x: int, y: int, w: int, h: int,
                           scale: float, log_path: str, layout: dict = None):
    """启动 Electron 透明显示窗，返回 (Popen, http_port)。"""
    exe = os.path.join(_PLUGIN_DIR, "electron_runtime", "Hime Display.exe")
    if not os.path.exists(exe):
        exe = os.path.join(_PLUGIN_DIR, "electron_runtime", "pet_display.exe")
    if not os.path.exists(exe):
        raise RuntimeError("electron runtime missing: %s" % exe)
    renderer = os.path.join(_PLUGIN_DIR, "renderer.html")
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    stage = clip = ""
    clip_mode = "local"
    if layout:
        stage = ",".join(str(int(round(v))) for v in layout.get("stage") or [])
        clip = ",".join(str(int(round(v))) for v in layout.get("clip") or [])
        clip_mode = "full" if layout.get("mode") == "full" else "local"
    args = [
        exe,
        "--port=%d" % port,
        "--renderer=%s" % renderer,
        "--model=%s" % model_url,
        "--scale=%s" % scale,
        "--x=%d" % int(x), "--y=%d" % int(y),
        "--w=%d" % int(w), "--h=%d" % int(h),
        "--stage=%s" % stage,
        "--clip=%s" % clip,
        "--clipMode=%s" % clip_mode,
        "--log=%s" % log_path,
        # 让显示壳知道自己的父进程是谁：worker 被强杀时壳要靠这个自己退出，
        # 否则会留下一个透明置顶窗继续显示模型（"关闭了模型还在"）。
        "--parent-pid=%d" % os.getpid(),
        # 窗口哪怕被挡在别的窗口后面、或整体挪到显示器之外（无头测试/别的虚拟桌面），
        # Chromium 都会把它判成 occluded 并停掉 rAF 与 WebGL 绘制 —— 表现是"模型不动、
        # 抓帧全空"。这三个开关关掉遮挡降频，渲染与 visible 状态解耦。
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
    ]
    try:
        flags = subprocess.CREATE_NO_WINDOW
    except AttributeError:
        flags = 0
    logf = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(args, cwd=os.path.dirname(exe),
                            stdout=logf, stderr=subprocess.STDOUT,
                            creationflags=flags)
    # 等 HTTP 桥真正起来（不等 executeJavaScript，页面/大模型加载可能远超 15s）
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/ping" % port, timeout=1.5) as r:
                if json.loads(r.read().decode("utf-8") or "{}").get("ok"):
                    return proc, port
        except Exception:
            time.sleep(0.25)
    try:
        proc.terminate()
    except Exception:
        pass
    raise RuntimeError("electron display not ready")


def _stop_electron_display(owner=None, wait: float = 0.8) -> bool:
    """让本实例的 Electron 显示壳彻底退出（worker 退出前必须调用）。

    只结束 worker 自己是不够的：Electron 是 worker 用 subprocess.Popen 起的
    独立进程，不随 parent 退出 —— 于是右键「关闭」后清单里已经没有这个模型，
    屏幕上却还留着一块透明置顶窗显示模型，而负责点击的 Qt 覆盖层已经没了，
    表现就是"模型还在、但点不动"。这里先请求 /quit 优雅退出，超时 terminate，
    最后 taskkill /F /T 兜底（连带 GPU/渲染子进程）。
    """
    if owner is None:
        owner = globals().get("_PET_SINGLETON")
    page = getattr(owner, "_electron_page", None)
    proc = getattr(owner, "_electron_proc", None)
    try:
        if page is not None:
            page._post("/quit", {}, timeout=0.8)
    except Exception:
        pass
    if proc is None:
        return True
    try:
        deadline = time.monotonic() + max(0.4, wait)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.1)
        proc.terminate()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.05)
    except Exception:
        pass
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
    return True


def _log_tag() -> str:
    """实例标签前缀：多 worker 共写同一份 _js_console.log 时用于区分来源。"""
    t = (os.environ.get("PET_LOG_TAG") or "").strip()
    return "[%s] " % t if t else ""


# ==== 性能探针（只在 PET_PERF_LOG=1 时开着，关掉时只剩两次 monotonic 调用）====
# 「快速操作后未响应 + 窗口变白」这类问题不是猜出来的：要能分别看到
# Qt 主线程有没有卡（tick 延迟）、卡在 setMask 还是建 region、
# 以及到 Electron 的每条 HTTP 往返有多慢。
_PERF = {
    "on": (os.environ.get("PET_PERF_LOG") or "").strip() in ("1", "true", "yes"),
    "t0": time.monotonic(),
    "last_tick": time.monotonic(),
    "tick_n": 0,
    "lag_max": 0.0,
    "mask_n": 0,
    "mask_build_ms": 0.0,
    "mask_set_ms": 0.0,
    "mask_rects_max": 0,
    "mask_key": None,
    "layout_n": 0,
    "js_n": 0,
    # 拖动路径单独计数：mouseMove → set_stage_rect 的次数、其中真正重建掩膜的次数
    "drag_n": 0,
    "mask_new_n": 0,
    "mask_coarse_n": 0,
    # 拖动主线程占用拆分：mouseMove 处理总时长 / set_stage_rect 内部时长
    "move_n": 0,
    "move_ms": 0.0,
    "drag_ms": 0.0,
    # 布局下发到渲染层真正生效的往返：每次 __applyPetLayout 的 HTTP+JS 耗时
    "layout_eval_n": 0,
    "layout_eval_ms": 0.0,
    "layout_eval_max": 0.0,
    # 画面延迟：渲染层应用布局时回算的"下发→真正应用"毫秒数（每秒重置）
    "vlag_n": 0,
    "vlag_sum": 0.0,
    "vlag_max": 0.0,
    "vlag_old_n": 0,
    # 松手那条路径 + 每个 10ms 轮询体的耗时（定位"松手后一下卡"到底卡在哪）
    "rel_n": 0,
    "rel_ms": 0.0,
    "poll_ms": 0.0,
    "poll_max": 0.0,
    # 平移快路径命中次数 / getModelRect 请求次数（判断是否真的没再重建热区）
    "geom_skip_n": 0,
    "rect_n": 0,
    # __mask__ 自检本身的耗时（pet.mask() = GetWindowRgn，可能慢到秒级）
    "probe_ms": 0.0,
    # Python 分代 GC 的停顿（判断 1.3s 那种"全零停顿"是 GC 还是进程被外部冻住）
    "gc_n": 0,
    "gc_ms": 0.0,
    # 轮询体内部拆分：perf_tick / 掩膜 / 命令循环（定位 lag_max 到底是什么占的）
    "seg_pt": 0.0,
    "seg_mask": 0.0,
    "seg_cmd": 0.0,
}
_HTTP_STAT = {}


def _perf_install_gc() -> None:
    """把每次 GC 的停顿累计进 _PERF，用来区分两种"秒级全零停顿"：
    Python GC 会显示 gc_ms≈停顿；进程被系统/驱动冻住时 gc_ms=0。"""
    if not _PERF["on"]:
        return
    import gc as _gcmod
    _t = {}

    def _cb(phase, info):
        try:
            if phase == "start":
                _t["p"] = time.monotonic()
            elif phase == "stop":
                _p0 = _t.pop("p", None)
                if _p0 is not None:
                    _PERF["gc_n"] += 1
                    _PERF["gc_ms"] += (time.monotonic() - _p0) * 1000.0
        except Exception:
            pass

    _gcmod.callbacks.append(_cb)


_perf_install_gc()

# 心跳：_perf_tick 每 10ms 更新一次，卡顿录栈看门狗靠它判断 Qt 主线程还活着没
_HB = {"t": time.monotonic(), "beat_ms": 0.0}
_SW = {"on": False}


def _perf_hb() -> None:
    if not _PERF["on"]:
        return
    now = time.monotonic()
    _HB["beat_ms"] = (now - _HB["t"]) * 1000.0
    _HB["t"] = now


def _perf_install_stallwatch() -> None:
    """卡顿录栈看门狗：Qt 主线程超过 600ms 没心跳就把它当时的调用栈打进日志。

    为什么需要它："松手后卡 1.3s" 这类停顿的特征是**所有内部计数器都是 0**
    （没有掩膜、没有布局、没有 HTTP），说明线程卡在某个 C 调用里（
    SetWindowRgn / readPixels / DWM 之类）。C 调用阻塞时 GIL 是释放的，
    所以守护线程能在停顿**进行中**抓到主线程栈，事后复现不出来的问题就靠它定位。
    """
    if not _PERF["on"]:
        return
    import sys
    import threading

    st = {"n": 0, "cool": 0.0, "grace": time.monotonic() + 3.0}

    def _watch():
        while True:
            try:
                time.sleep(0.08)
                now = time.monotonic()
                if now < st["grace"]:
                    continue
                age = (now - _HB["t"]) * 1000.0
                if age < 600.0 or now < st["cool"]:
                    continue
                if st["n"] >= 10:
                    return
                st["n"] += 1
                st["cool"] = now + 2.0
                frames = sys._current_frames()
                names = {t.ident: t.name for t in threading.enumerate()}
                out = ["STALL 心跳断了 %.0fms（#%d）" % (age, st["n"])]
                for tid, fr in frames.items():
                    nm = names.get(tid, "?")
                    stack = "".join(traceback.format_stack(fr, limit=8))
                    out.append("--- 线程 %s ---\n%s" % (nm, stack))
                _log_line("PERF", "\n".join(out))
            except Exception:
                pass

    threading.Thread(target=_watch, name="pet-stallwatch", daemon=True).start()


def _perf_http(path: str, ms: float) -> None:
    s = _HTTP_STAT.get(path)
    if s is None:
        s = _HTTP_STAT[path] = [0, 0.0]
    s[0] += 1
    if ms > s[1]:
        s[1] = ms


def _perf_tick() -> None:
    """每次 _poll_cmds（10ms）调用一次；每满 1 秒打一行汇总并清零。"""
    if not _PERF["on"]:
        return
    _perf_hb()
    if not _SW["on"]:
        _SW["on"] = True
        _perf_install_stallwatch()
    now = time.monotonic()
    lag = (now - _PERF["last_tick"]) - 0.010
    if lag > _PERF["lag_max"]:
        _PERF["lag_max"] = lag
    _PERF["last_tick"] = now
    _PERF["tick_n"] += 1
    if now - _PERF["t0"] < 1.0:
        return
    _http = ", ".join("%s n=%d max=%.0fms" % (k, v[0], v[1])
                      for k, v in sorted(_HTTP_STAT.items()))
    _log_line("PERF", "qt: tick=%d lag_max=%.0fms | mask n=%d build=%.0fms set=%.0fms rects=%d "
              "coarse=%d | layout=%d js=%d drag=%d | move=%d/%.0fms drag_ms=%.0fms | http: %s"
              % (_PERF["tick_n"], _PERF["lag_max"] * 1000.0,
                 _PERF["mask_n"], _PERF["mask_build_ms"], _PERF["mask_set_ms"],
                 _PERF["mask_rects_max"], _PERF.get("mask_coarse_n", 0),
                 _PERF["layout_n"], _PERF["js_n"], _PERF["drag_n"],
                 _PERF.get("move_n", 0), _PERF.get("move_ms", 0.0),
                 _PERF.get("drag_ms", 0.0),
                 _http or "-"))
    _log_line("PERF", "layout: n=%d avg=%.1fms max=%.0fms | visual lag: n=%d avg=%.0fms max=%.0fms stale=%d"
              % (_PERF.get("layout_eval_n", 0),
                 (_PERF.get("layout_eval_ms", 0.0) / _PERF["layout_eval_n"])
                 if _PERF.get("layout_eval_n") else 0.0,
                 _PERF.get("layout_eval_max", 0.0),
                 _PERF.get("vlag_n", 0),
                 (_PERF.get("vlag_sum", 0.0) / _PERF["vlag_n"])
                 if _PERF.get("vlag_n") else 0.0,
                 _PERF.get("vlag_max", 0.0),
                 _PERF.get("vlag_old_n", 0)))
    _log_line("PERF", "path: release n=%d %.0fms | poll body %.0fms | geom skip=%d rect=%d"
              " | seg: tick=%.0fms mask=%.0fms cmd=%.0fms other=%.0fms"
              % (_PERF.get("rel_n", 0), _PERF.get("rel_ms", 0.0),
                 _PERF.get("poll_ms", 0.0), _PERF.get("geom_skip_n", 0),
                 _PERF.get("rect_n", 0),
                 _PERF.get("seg_pt", 0.0), _PERF.get("seg_mask", 0.0),
                 _PERF.get("seg_cmd", 0.0),
                 max(0.0, _PERF.get("poll_ms", 0.0) - _PERF.get("seg_pt", 0.0)
                     - _PERF.get("seg_mask", 0.0) - _PERF.get("seg_cmd", 0.0))))
    if _PERF.get("probe_ms"):
        _log_line("PERF", "probe: __mask__ %.0fms（自检自身开销）"
                  % _PERF.get("probe_ms", 0.0))
    if _PERF.get("gc_n"):
        _log_line("PERF", "gc: n=%d %.0fms（Python 分代回收停顿）"
                  % (_PERF.get("gc_n", 0), _PERF.get("gc_ms", 0.0)))
    _PERF.update({"t0": now, "tick_n": 0, "lag_max": 0.0, "mask_n": 0,
                  "mask_build_ms": 0.0, "mask_set_ms": 0.0,
                  "mask_rects_max": 0, "layout_n": 0, "js_n": 0,
                  "drag_n": 0, "mask_coarse_n": 0,
                  "move_n": 0, "move_ms": 0.0, "drag_ms": 0.0,
                  "layout_eval_n": 0, "layout_eval_ms": 0.0, "layout_eval_max": 0.0,
                  "vlag_n": 0, "vlag_sum": 0.0, "vlag_max": 0.0, "vlag_old_n": 0,
                  "rel_n": 0, "rel_ms": 0.0, "poll_ms": 0.0, "poll_max": 0.0,
                  "geom_skip_n": 0, "rect_n": 0, "probe_ms": 0.0,
                  "gc_n": 0, "gc_ms": 0.0,
                  "seg_pt": 0.0, "seg_mask": 0.0, "seg_cmd": 0.0})
    _HTTP_STAT.clear()


def _log_line(level: str, msg: str) -> None:
    """插件侧诊断写入 _js_console.log（与 JS 控制台同一份，按时间线对照排查）。"""
    try:
        try:
            if os.path.getsize(_JS_LOG_PATH) > 4 * 1024 * 1024:
                os.replace(_JS_LOG_PATH, _JS_LOG_PATH + ".old")
        except OSError:
            pass
        with open(_JS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s[%s] P%s %s\n"
                    % (_log_tag(), time.strftime("%H:%M:%S"), level, str(msg)[:2000]))
    except Exception:
        pass

# 内置动画的中文名（仅用于双击 / 菜单的提示文案；真实列表以 renderer 的
# builtin_animations 为准，菜单里能拿到就不用这份兜底）
BUILTIN_ANIM_LABEL = {
    "nod": "点头", "shake": "摇头", "tilt": "歪头", "sway": "摇摆",
    "look_around": "张望", "cheer": "欢呼",
}


def _purge_webengine_caches(root: str) -> None:
    """删除上次运行残留的 Chromium GPU / HTTP 缓存目录。

    GPU 对象（纹理、顶点缓冲、program）每个进程都是新创建的，跨进程唯一会复用的
    是磁盘缓存（shader program cache / http cache）。上次若异常退出（本插件进程
    本身就以 0xC0000005 收场），这些缓存可能只写了一半，下次启动首载就复用坏数据
    → 部件顶点与贴图错位（"关闭程序再打开就炸"）。清掉只让首帧慢一点。
    """
    names = ("GPUCache", "ShaderCache", "GrShaderCache", "GraphiteDawnCache", "Cache")
    removed = []
    for base, dirs, _files in os.walk(root):
        for d in list(dirs):
            if d in names:
                p = os.path.join(base, d)
                try:
                    shutil.rmtree(p)
                    removed.append(os.path.relpath(p, root))
                except OSError:
                    pass
                dirs.remove(d)
    if removed:
        print("[webengine] 清理残留缓存:", ", ".join(removed))


def _profile_dir() -> str:
    """Chromium profile 目录：默认插件目录下唯一一份。

    多实例 worker 模式下每个子进程必须各自独占一个 profile（Chromium 的
    user-data-dir 带进程级锁，两个进程同时打开同一目录会互踢/写坏缓存），
    由 worker 启动前通过 PET_PROFILE_DIR 环境变量指定（见 _worker_main）。
    """
    return os.environ.get("PET_PROFILE_DIR") or _PROFILE_DIR


def _apply_chromium_flags() -> None:
    """把需要的 Chromium 开关**追加**到已有 flags，不覆盖、不抢宿主配置。"""
    want = [
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        # 允许无用户手势播放音频（桌宠是离屏页面，永远没有点击），
        # 否则模型动作的配音会被自动播放策略拦截。
        "--autoplay-policy=no-user-gesture-required",
        # 不复用上次运行写下的 shader 缓存（写坏时会让画面出错）
        "--disable-gpu-shader-disk-cache",
        # 渲染页与模型资源同为 file:// 时允许互相读取（模型默认走 file:// 直读）
        "--allow-file-access-from-files",
    ]
    cur = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS") or "").strip()
    # 宿主进程若已指定自己的 profile 目录，就沿用宿主的：两个 --user-data-dir
    # 同时存在行为未定义，且会把宿主的缓存写到插件目录里。
    if "--user-data-dir" not in cur:
        want.append('--user-data-dir="%s"' % _profile_dir().replace("\\", "/"))
    missing = [f for f in want if f.split("=")[0] not in cur]
    if missing:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (cur + " " + " ".join(missing)).strip()


def _ensure_runtime_env() -> None:
    """首次真正创建桌宠窗口前执行的一次性副作用（幂等）。

    为什么必须延迟到这里：插件被宿主进程「仅加载」时（读 manifest / 试 import），
    本模块若顺手改了进程级 QTWEBENGINE_CHROMIUM_FLAGS 或删掉磁盘缓存，会污染
    宿主的 QtWebEngine（典型后果：宿主被强行指定成插件的 --user-data-dir，
    随后创建 WebEngine 直接崩）。所以 import 本模块必须零副作用。
    """
    global _ENV_READY, _DUMP_FRAMES
    if _ENV_READY:
        return
    _ENV_READY = True
    _DUMP_FRAMES = os.path.exists(_DUMP_FLAG_PATH)
    try:
        os.makedirs(_profile_dir(), exist_ok=True)
    except OSError as e:
        print("[webengine] 无法创建 profile 目录:", e)
    _purge_webengine_caches(_profile_dir())
    _apply_chromium_flags()

# === 临时 HTTP server 提供模型文件 ===
_MODELS = {}  # dir → (httpd, url, last_used)
_MODELS_LOCK = threading.Lock()
# 端口池。原来是 18765-18779 共 15 个 —— 但端口是**跨进程**的：桌宠每开一个实例
# 就要自己绑，一个进程切过 15 次模型就把整个池吃满，别的实例（甚至重启后自己）
# 直接"无法绑定端口"启动失败。所以池放大 + 单进程只留最近用过的几个。
_MODEL_HTTP_PORTS = (18765, 18845)     # 半开区间：18765..18844，共 80 个
_MODEL_HTTP_KEEP = 4                   # 每进程常驻 4 个（当前 + 最近切过的 3 个）


def _model_token(abs_dir: str) -> str:
    """每个模型目录一个稳定的 URL 前缀（内容 = 目录绝对路径的短哈希）。

    为什么必须带前缀：pixi 的 TextureCache 以「贴图绝对 URL」为唯一 key，
    而模型目录里的贴图相对路径几乎都叫 `textures/texture_00.png` 这种同名文件。
    端口池会复用（切过的模型超过常驻数量就回收端口），于是两个不同模型的贴图
    URL 可能完全相同（http://127.0.0.1:18765/textures/texture_00.png）
    → 后加载的模型直接拿到前一个模型的贴图对象（实测同一 BaseTexture 实例），
    整屏贴图错乱、看不出人形。加上目录专属前缀后，每个模型的贴图 URL 永远唯一。
    """
    return hashlib.sha1(os.path.normcase(os.path.abspath(abs_dir)).encode("utf-8")).hexdigest()[:12]


class _ModelDirHandler(http.server.SimpleHTTPRequestHandler):
    """只服务 `/<token>/…` 这一棵子树，其它前缀一律 404（防止跨目录读文件）。"""

    def __init__(self, *args, token: str = "", **kwargs):
        self._token = token
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        p = urllib.parse.urlparse(path).path
        prefix = "/%s/" % self._token
        if not p.startswith(prefix):
            return ""                      # 空路径 → open() 抛 OSError → 404
        return super().translate_path("/" + p[len(prefix):])

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        """所有响应都带 CORS 头。

        页面是 file:// （origin=null），模型资源在 http://127.0.0.1 —— 严格来说
        每次 XHR 都是跨域请求。Qt 的 LocalContentCanAccessRemoteUrls 通常能放行，
        但实测偶发在同一毫秒里被 Chromium 以 abort（status 0）丢弃，页面就永远空白。
        自己带上 ACAO 之后，放行不再依赖浏览器设置里的隐式宽免。
        """
        try:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            # 私有网络访问（PNA / CORS-RFC1918）：file:// 页面访问 127.0.0.1 属于
            # 「非私有 → 私有」请求，Chromium 会先发预检；不明确允许就会有请求被
            # 拦掉（表现为 XHR abort、status=0）。
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        except Exception:
            pass
        super().end_headers()

    def do_OPTIONS(self):
        """预检请求：CORS / PNA 都走这里，直接回 204 + 上面那套允许头。"""
        self.send_response(204)
        try:
            self.send_header("Content-Length", "0")
        except Exception:
            pass
        self.end_headers()

    def do_GET(self):
        # model3.json 走一层「配音路径修正」：Sound 指错时库永远拉不到音频
        try:
            if self.path.lower().split("?")[0].endswith(".model3.json"):
                fs = self.translate_path(self.path)
                if fs and os.path.isfile(fs):
                    body, _fixes = model_json_for_serve(fs)
                    if body:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(body)
                        return
        except Exception:
            pass
        return super().do_GET()


def serve_model(model_dir: str) -> str:
    """启动临时 HTTP server 提供模型目录，返回访问 URL。

    每个 server 通过 `directory` 参数固定自己的服务目录，
    不用 os.chdir（进程级全局）——切换模型不会破坏其他
    server 的服务目录，因此初始模型可以随时切回。
    端口池固定 18765-18779；每个目录独占一个端口
    （Windows 的 SO_REUSEADDR 允许重复绑定同一端口，必须关闭），
    用满时回收最久未使用的服务。
    """
    abs_dir = os.path.abspath(model_dir)
    key = os.path.normcase(abs_dir)
    token = _model_token(abs_dir)
    handler = functools.partial(_ModelDirHandler, directory=abs_dir, token=token)

    # 必须多线程：配音（HTMLAudio 拉 wav）+ 动作 json + 贴图会并发请求，
    # 单线程 TCPServer 会让音频流把后续请求全部堵住（实测配音延迟 1.5s）。
    class _ThreadingServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        # Windows 下 SO_REUSEADDR 允许重复绑定同一端口，多个模型服务会抢同一端口，
        # 浏览器请求被随机分发到错误目录（404 → 切换 Network error）。必须独占。
        allow_reuse_address = False

    def _bind_free_port():
        used = set()
        for v in _MODELS.values():
            try:
                used.add(v[0].server_address[1])
            except Exception:
                pass
        for port in range(*_MODEL_HTTP_PORTS):
            if port in used:
                continue
            try:
                srv = _ThreadingServer(("127.0.0.1", port), handler)
            except OSError:
                continue
            if srv.server_address[1] != port:
                try:
                    srv.server_close()
                except Exception:
                    pass
                continue
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                             daemon=True).start()
            return srv, "http://127.0.0.1:%d/%s/" % (port, token)
        return None, None

    def _evict_one():
        """回收最久未用的 server（同步释放端口，最坏等 poll_interval=0.1s）。"""
        if not _MODELS:
            return False
        victim = min(_MODELS.items(), key=lambda kv: kv[1][2])[0]
        old_srv = _MODELS.pop(victim)[0]
        try:
            old_srv.shutdown()
        except Exception:
            pass
        try:
            old_srv.server_close()
        except Exception:
            pass
        return True

    with _MODELS_LOCK:
        cached = _MODELS.get(key)
        if cached:
            _MODELS[key] = (cached[0], cached[1], time.monotonic())
            return cached[1]

        httpd, url = _bind_free_port()
        # 绑不到（别的实例占着 / 自己缓存太多）就逐个回收再试
        while httpd is None and _evict_one():
            httpd, url = _bind_free_port()

        if httpd is None:
            raise RuntimeError(
                "无法绑定端口（%d-%d 均被占用：还有别的桌宠实例在跑）"
                % (_MODEL_HTTP_PORTS[0], _MODEL_HTTP_PORTS[1] - 1))

        _MODELS[key] = (httpd, url, time.monotonic())
        # 常驻数量收敛：切过的旧模型不必一直占着端口 —— 切回本来也要重载 ~2s，
        # 而一个进程长期占十几个端口会把多实例/重启直接饿死。
        while len(_MODELS) > _MODEL_HTTP_KEEP:
            _evict_one()
        return url


# === 右键菜单用：模型目录记忆 + 模型列表扫描 ===
_UI_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pet_ui.json")
_MODEL_CACHE = {"root": "", "items": [], "ts": 0.0}


def load_ui_cfg() -> dict:
    """读取右键菜单记忆（上次的模型根目录 / 上次加载的模型）。"""
    try:
        with open(_UI_CFG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ui_cfg(**kw) -> None:
    data = load_ui_cfg()
    data.update({k: v for k, v in kw.items() if v is not None})
    try:
        with open(_UI_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _wrap_cjk(text: str, fm, max_w: float, max_lines: int = 3) -> list:
    """按像素宽度折行：中文逐字断，英文优先在空格断（字幕用，不引入额外换行符）。"""
    s = " ".join(str(text or "").split())
    if not s:
        return []
    if fm.horizontalAdvance(s) <= max_w:
        return [s]
    lines, cur = [], ""
    for ch in s:
        cand = cur + ch
        if fm.horizontalAdvance(cand) <= max_w or not cur:
            cur = cand
            continue
        # 英文单词别被切断：退回到最后一个空格
        if ch != " " and " " in cur:
            head, tail = cur.rsplit(" ", 1)
            if fm.horizontalAdvance(tail) > 0:
                lines.append(head)
                cur = tail + ch
                continue
        lines.append(cur)
        cur = "" if ch == " " else ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and fm.horizontalAdvance(last + "…") > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


_SOUND_CACHE = {}     # model3.json 路径 → (mtime, 声明配音的动作数, 动作总数, 音频文件数)
_AUDIO_EXT = (".wav", ".mp3", ".ogg", ".m4a", ".aac", ".flac", ".wma", ".opus")
# 这些目录里不可能有语音，扫的时候整棵跳过（贴图目录动辄上百个文件）
_SKIP_DIRS = ("textures", "texture", "motions", "motion", "physics",
              "expressions", "exp", "cmoc", "_old")


def count_audio_files(model_dir: str, depth: int = 2) -> int:
    """数一个模型目录里的音频文件（voice/、audio/ 或与 model3.json 同级）。

    很多模型包把语音文件和动作一起拷进来了，却没在 model3.json 里写 Sound ——
    这种"有素材、播不出声"的模型也得让用户看得出区别，所以单独数一遍。
    """
    if not model_dir or not os.path.isdir(model_dir):
        return 0
    base = os.path.abspath(model_dir).count(os.sep)
    n = 0
    try:
        for cur, dirs, files in os.walk(model_dir):
            if os.path.abspath(cur).count(os.sep) - base > depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
            for fn in files:
                if fn.lower().endswith(_AUDIO_EXT):
                    n += 1
    except Exception:
        pass
    return n


def model_sound_info(path: str) -> tuple:
    """扫一个 *.model3.json，返回 (声明配音的动作数, 动作总数, 目录里的音频文件数)。

    Cubism 的配音写在 model3.json 的 FileReferences.Motions[*].Sound 里，
    所以切换模型菜单不必等模型加载完，读一下这个几十 KB 的小文件就知道。
    结果按 mtime 缓存：菜单要列几百个模型，重复开菜单不该重复读盘。
    """
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return (0, 0, 0)
    ent = _SOUND_CACHE.get(path)
    if ent and ent[0] == mt:
        return (ent[1], ent[2], ent[3])
    n_snd = n_all = n_aud = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        refs = ((data or {}).get("FileReferences") or {})
        groups = refs.get("Motions") or {}
        seq = groups.values() if isinstance(groups, dict) else groups
        for arr in (seq or []):
            if isinstance(arr, dict):
                arr = [arr]
            if not isinstance(arr, list):
                continue
            for m in arr:
                n_all += 1
                if isinstance(m, dict) and m.get("Sound"):
                    n_snd += 1
    except Exception:
        n_snd = n_all = 0
    try:
        n_aud = count_audio_files(os.path.dirname(path))
    except Exception:
        n_aud = 0
    _SOUND_CACHE[path] = (mt, n_snd, n_all, n_aud)
    return (n_snd, n_all, n_aud)


_AUDIO_NAME_CACHE = {}      # 模型目录 → (目录 mtime, {小写文件名: 相对路径})
_PATH_INDEX_CACHE = {}      # 模型目录 → (目录 mtime, {小写文件名: 相对路径})（全部文件）
_SERVE_JSON_CACHE = {}      # (model3.json 路径, mtime, size) → (响应字节, 修正记录)

# model3.json 里会被拿来当相对路径用的键：模型包被挪目录时它们会一起指向错误层级
_SERVE_PATH_KEYS = ("File", "Sound", "Moc", "Physics", "Pose", "UserData", "MotionSync")


def _dir_path_index(model_dir: str, depth: int = 3, audio_only: bool = False) -> dict:
    """目录内的文件索引：{小写文件名: 相对 model_dir 的 posix 路径}（同名取路径最短的）。"""
    if not model_dir or not os.path.isdir(model_dir):
        return {}
    try:
        mt = os.path.getmtime(model_dir)
    except OSError:
        mt = 0.0
    cache = _AUDIO_NAME_CACHE if audio_only else _PATH_INDEX_CACHE
    ent = cache.get(model_dir)
    if ent and ent[0] == mt:
        return ent[1]
    base = os.path.abspath(model_dir).count(os.sep)
    idx = {}
    try:
        for cur, dirs, files in os.walk(model_dir):
            if os.path.abspath(cur).count(os.sep) - base > depth:
                dirs[:] = []
                continue
            # audio_only 才跳过 motions/textures 等目录（只为数配音素材）；
            # 修正 File/Sound 路径必须能看到 motions/、textures/ 里的文件
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and not (audio_only and d.lower() in _SKIP_DIRS)]
            for fn in files:
                if audio_only and not fn.lower().endswith(_AUDIO_EXT):
                    continue
                rel = os.path.relpath(os.path.join(cur, fn), model_dir).replace(os.sep, "/")
                k = fn.lower()
                if k not in idx or len(rel) < len(idx[k]):
                    idx[k] = rel
    except Exception:
        pass
    cache[model_dir] = (mt, idx)
    return idx


def audio_name_index(model_dir: str, depth: int = 2) -> dict:
    """模型目录里的音频文件名索引：{小写文件名: 相对 model_dir 的 posix 路径}。

    用来判断某个动作的配音文件到底在不在本地 —— 不在就不显示字幕，
    免得又出现「有字幕没声音」。
    """
    return _dir_path_index(model_dir, depth=depth, audio_only=True)


def model_json_for_serve(path: str) -> tuple:
    """读 model3.json 原文，顺手把指错的相对路径改对；返回 (响应字节, 修正记录)。

    Live2D 库按 model3.json 所在目录解析 File/Sound，路径多一层或少一层就是 404：
    动作加载不到、音频也拉不到 —— 用户听到的就是「有台词、字幕也显示，却没声音」。
    zhala_2 就是典型：`Idle` 与 `Tapbodylower #0..#2` 写成 `zhala_2/motions/...`、
    `zhala_2/vioce/...`，文件其实都在 `motions/`、`vioce/` 下。
    这里按文件名在模型目录里找回来，改写成相对路径再发给渲染器；
    找不到同名文件的保持原样（那条配音确实不存在）。
    """
    try:
        st = os.stat(path)
    except OSError:
        return (b"", [])
    key = (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size)
    ent = _SERVE_JSON_CACHE.get(key)
    if ent:
        return ent
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return (b"", [])
    body = raw
    fixes = []
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        base = os.path.dirname(path)
        idx = [None]

        def _fix(holder, k, val, where):
            if not isinstance(val, str) or not val:
                return
            low = val.lower()
            if low.startswith(("http://", "https://", "data:", "blob:")):
                return
            if os.path.isfile(os.path.join(base, val.replace("/", os.sep))):
                return
            if idx[0] is None:
                idx[0] = _dir_path_index(base, depth=3)
            cand = idx[0].get(os.path.basename(val.replace("\\", "/")).lower())
            if not cand:
                return
            holder[k] = cand
            fixes.append({"at": where, "from": val, "to": cand})

        def _walk(node, where=""):
            if isinstance(node, dict):
                for k, v in list(node.items()):
                    if isinstance(v, str) and k in _SERVE_PATH_KEYS:
                        _fix(node, k, v, "%s/%s" % (where, k))
                    elif isinstance(v, list):
                        for i, s in enumerate(v):
                            if isinstance(s, str) and k == "Textures":
                                _fix(v, i, s, "%s/%s[%d]" % (where, k, i))
                            elif isinstance(s, (dict, list)):
                                _walk(s, "%s/%s[%d]" % (where, k, i))
                    elif isinstance(v, (dict, list)):
                        _walk(v, "%s/%s" % (where, k))
            elif isinstance(node, list):
                for i, s in enumerate(node):
                    _walk(s, "%s[%d]" % (where, i))

        _walk((data or {}).get("FileReferences") or {}, "FileReferences")
        if fixes:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            _log_line("INFO", "model json fix: %s 修正 %d 条相对路径 → %s" % (
                os.path.basename(path), len(fixes),
                ", ".join("%s=%s" % (f["at"].replace("FileReferences/", ""), f["to"]) for f in fixes[:6])))
    except Exception:
        body = raw
        fixes = []
    _SERVE_JSON_CACHE[key] = (body, fixes)
    return (body, fixes)


def model_arg_for_serve(model_file: str) -> str:
    """模型文件 → 交给渲染页的 model URL。

    默认返回 file:// 直读地址（原因见 _worker_main 里的长注释：本机 Chromium 的
    异步 XHR 链路偶发整体失效，http://127.0.0.1 的模型请求会被直接 abort）。
    环境变量 PET_MODEL_SCHEME=http 可强制回到本地 HTTP 方案。
    """
    mf = os.path.abspath(str(model_file or ""))
    scheme = (os.environ.get("PET_MODEL_SCHEME") or "file").strip().lower()
    if scheme != "http" and os.path.isfile(mf):
        try:
            from PyQt5.QtCore import QUrl
            return QUrl.fromLocalFile(mf).toString()
        except Exception:
            pass
    try:
        base = serve_model(os.path.dirname(mf))
    except Exception:
        return ""
    return (base + os.path.basename(mf)) if base else ""


def _file_url_to_local(u: str) -> str:
    """file:// URL → 本地绝对路径（不是 file:// 就返回空串）。"""
    s = str(u or "").strip()
    if not s.lower().startswith("file:"):
        return ""
    s = s[5:].split("?")[0].split("#")[0]
    if s.startswith("//"):
        s = s[2:]
    try:
        s = urllib.parse.unquote(s)
    except Exception:
        pass
    if s.startswith("/") and len(s) > 2 and s[1].isalpha() and s[2] == ":":
        s = s[1:]
    return s.replace("/", os.sep) if os.sep == "\\" else s


def local_dir_of_model_url(model_url: str) -> str:
    """把渲染器里的模型 URL 换回本地模型目录（http 只认本进程 serve 过的目录）。"""
    u = str(model_url or "")
    if not u:
        return ""
    fp = _file_url_to_local(u)
    if fp:
        d = os.path.dirname(fp)
        return d if os.path.isdir(d) else ""
    try:
        with _MODELS_LOCK:
            for key, val in list(_MODELS.items()):
                base = val[1] if len(val) > 1 else ""
                if base and u.startswith(base):
                    return key
    except Exception:
        pass
    return ""


def local_model_json_of_url(model_url: str) -> str:
    """模型 URL（…/<token>/xxx.model3.json 或 file:///…/xxx.model3.json）→ 本地绝对路径。"""
    u = str(model_url or "").split("?")[0]
    if not u:
        return ""
    fp = _file_url_to_local(u)
    if fp:
        return fp if os.path.isfile(fp) else ""
    try:
        with _MODELS_LOCK:
            for key, val in list(_MODELS.items()):
                base = val[1] if len(val) > 1 else ""
                if base and u.startswith(base):
                    tail = urllib.parse.unquote(u[len(base):])
                    return os.path.join(key, tail.replace("/", os.sep))
    except Exception:
        pass
    return ""


_HITAREA_CACHE = {}


def model_hit_areas(path: str) -> list:
    """读 model3.json 的 HitAreas（Cubism 标准字段）→ 作者声明的交互区。

    返回 [{"id": ..., "name": ...}]。HitAreas.Id 在 Cubism 里指向的是画命中区用的
    ArtMesh（drawable）Id，个别模型写 Part Id；渲染器那边两种都认。
    作者画过的交互区比我们按部件中文名猜部位靠谱，有就按它布点，没有才退回推断。
    按 mtime 缓存。
    """
    if not path:
        return []
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return []
    ent = _HITAREA_CACHE.get(path)
    if ent and ent[0] == mt:
        return ent[1]
    items = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        for it in (data.get("HitAreas") or []):
            if isinstance(it, dict):
                hid = str(it.get("Id") or it.get("Name") or "").strip()
                nm = str(it.get("Name") or "").strip()
            else:
                hid, nm = str(it or "").strip(), ""
            if hid and not any(x["id"] == hid for x in items):
                items.append({"id": hid, "name": nm})
    except Exception:
        items = []
    _HITAREA_CACHE[path] = (mt, items)
    return items


def clear_menu(menu) -> None:
    """清空菜单，并释放上一次构建留下的子菜单控件。

    QMenu.clear() 只摘掉条目，`addMenu()` 建出来的子菜单控件仍然是父菜单的
    子控件（藏在那里、越攒越多）。右键菜单现在复用同一个实例，每次重建前
    必须把它们一起删掉，否则又变成另一种"点得越多越慢"。
    """
    try:
        menu.clear()
    except Exception:
        pass
    try:
        # QMenu 是 _qapp_main 里局部 import 的，模块级拿不到这个名字；
        # 这里按"和父菜单同类型"来找子菜单，不依赖具体的 Qt 类名。
        for child in menu.findChildren(type(menu)):
            child.setParent(None)      # 立刻脱离父子关系（不等事件循环）
            child.deleteLater()
    except Exception:
        pass


def lazy_submenu(parent_menu, title, filler, log=None):
    """建一个"用户展开时才填内容"的子菜单，返回 (sub_menu, fill_now)。

    QMenu 是同步构建的：如果弹出来的那一瞬间就把「播放动画 / 切换模型」
    全部填好，等于把扫模型目录（264 个模型冷启动实测 0.67s）压在右键事件里，
    桌宠会明显卡一下，Windows 还可能直接标成「未响应」。这里只挂一个空壳，
    等 aboutToShow 才去碰磁盘。

    fill_now 是幂等的填充入口：`__menu_dump__` 自检要立刻看到展开后的完整
    条目树时直接调用它。
    """
    sub = parent_menu.addMenu(title)
    state = {"filled": False}

    def fill_now():
        if state["filled"]:
            return
        state["filled"] = True
        try:
            filler(sub)
        except Exception as exc:      # 单个子菜单填失败不能连累整份菜单
            if log:
                log("WARN", "填充子菜单失败(%s): %r" % (title, exc))

    sub.aboutToShow.connect(fill_now)
    return sub, fill_now


def menu_tree(menu) -> list:
    """把 QMenu 的条目树转成 dict（自检用：核对菜单文案 / 标记 / 禁用状态）。"""
    out = []
    try:
        acts = menu.actions()
    except Exception:
        return out
    for act in acts:
        try:
            if act.isSeparator():
                out.append({"sep": True})
                continue
            node = {"text": act.text(),
                    "enabled": bool(act.isEnabled()),
                    "checked": bool(act.isChecked()) if act.isCheckable() else None}
            tip = act.toolTip()
            # PyQt 里没设过 tooltip 的 QAction 会把 text 当 tooltip 返回，只记真正设过的
            if tip and tip != act.text():
                node["tip"] = tip
            sub = act.menu()
            if sub is not None:
                node["items"] = menu_tree(sub)
            out.append(node)
        except Exception:
            continue
    return out


def scan_models(root: str, limit: int = 500, ttl: float = 300.0) -> list:
    """扫描 root 下所有 *.model3.json，返回 [(显示名, 绝对路径)]。

    只向下 3 层、跳过 `_` 开头的临时目录，结果缓存 ttl 秒（目录/模型变动后可右键「重新扫描模型」）。
    显示名用模型所在目录名（例如 aijier_3_hx），同名时补上父目录名以示区分。
    """
    if not root or not os.path.isdir(root):
        return []
    now = time.monotonic()
    if _MODEL_CACHE["root"] == root and (now - _MODEL_CACHE["ts"]) < ttl:
        return _MODEL_CACHE["items"]
    root_depth = os.path.abspath(root).rstrip("\\/").count(os.sep)
    found = []
    try:
        for cur, dirs, files in os.walk(root):
            depth = os.path.abspath(cur).count(os.sep) - root_depth
            dirs[:] = [d for d in dirs if not d.startswith("_") and depth < 2]
            for fn in files:
                if fn.lower().endswith(".model3.json"):
                    found.append(os.path.join(cur, fn))
                    if len(found) >= limit:
                        break
            if len(found) >= limit:
                break
    except Exception:
        pass

    names = {}
    for path in found:
        base = os.path.basename(os.path.dirname(path)) or os.path.splitext(
            os.path.basename(path))[0]
        names[path] = base
    dup = {}
    for base in names.values():
        dup[base] = dup.get(base, 0) + 1
    items = []
    for path, base in names.items():
        if dup.get(base, 0) > 1:
            parent = os.path.basename(os.path.dirname(os.path.dirname(path)))
            base = "%s (%s)" % (base, parent) if parent else base
        items.append((base, path))
    items.sort(key=lambda t: t[0].lower())
    _MODEL_CACHE.update({"root": root, "items": items, "ts": now})
    return items


# === 换装：模型自带的服装参数通道 ===
# 这类 Live2D 模型把"衣装"做成参数（cdi3 里 Clothes1..5 = 长短裙 / 大小变 /
# 胸口衣服 / 鞋子 / 尾巴），配套 `Exp3/*.exp3.json` 就是作者写好的切换动作
# （裙子切换、脱鞋、体型2…）。所以：
#   * 一个服装参数 = 一个换装通道；
#   * 通道里每个 exp3 文件 = 该通道的一个可选状态（值 = exp3 里写的 Value）；
#   * 没有服装参数、或参数没有任何 exp3 状态的模型返回 []，右键菜单不显示「换装」。
_OUTFIT_PARAM_RE = re.compile(r"^(?:clothes|costume|outfit|dress|skin)\d*$", re.I)
_OUTFIT_CACHE = {}     # model3.json 路径 → (mtime, channels)

# === 参数型换装通道（碧蓝航线这类模型）===
# 这类模型没有 exp3 换装动作，而是把"裙脱 / 鞋脱 / 内衣切换 / 手套切换"
# 拆成一堆互不相干的独立开关参数（Touch_drag16、Circulation_Head_16……），
# 一个参数只管一个部位的一个状态。识别办法是"服装词 + 切换词"同时出现，
# 再排除形变/物理/姿势这类同名噪音。
_OUTFIT_WEAR_WORDS = ("裙", "裤袜", "丝袜", "袜", "鞋", "靴", "内衣", "内裤",
                      "手套", "毛衣", "外套", "兜帽", "帽", "披风", "围巾",
                      "泳装", "羽织", "胸罩", "肩带", "戒指", "耳环")
_OUTFIT_SWITCH_WORDS = ("穿脱", "脱", "穿", "切换", "消失", "有无", "选1",
                        "开关", "取下", "放下", "戴上")
# 这些词一出现就不是"换装开关"，而是同名的形变/物理/姿势参数：
#   "裙子穿脱物理1"（kz1）= 裙子摆动触发，"左手套动画" = 手臂动画，
#   "背景切换开关" = 背景，"姿势切换" = 姿势。
_OUTFIT_SKIP_WORDS = ("物理", "姿势", "动作", "表情", "动画", "层级", "变化",
                      "尺寸", "SIZE", "位置", "角度", "背景", "颜色",
                      "透明度", "旋转", "摇", "缩放", "Offset", "手部")


def _param_outfit_channels(cdi_data, labels: dict, order: dict, used: set) -> list:
    """从 cdi3 参数名里捡出"服装开关"通道。一个参数 = 一个通道。

    为什么不把"裙脱 / 裙消失 / 裙子穿脱"合成一个通道：它们不是同一个开关的
    多个档位，而是互相独立的布尔量（有的还带物理），合成一个就得猜联动关系，
    一猜错就是穿模。拆开让用户逐个点，是可控的；同部位轮转交给中键的
    "点哪换哪"（组内自动轮到下一个通道）。
    """
    out = []
    if not isinstance(cdi_data, dict):
        return out
    for i, p in enumerate(cdi_data.get("Parameters") or []):
        pid = str((p or {}).get("Id") or "").strip()
        nm = str((p or {}).get("Name") or "").strip()
        if not pid or not nm or pid in used:
            continue
        if not any(w in nm for w in _OUTFIT_WEAR_WORDS):
            continue
        if not any(w in nm for w in _OUTFIT_SWITCH_WORDS):
            continue
        if any(w in nm for w in _OUTFIT_SKIP_WORDS):
            continue
        # "2选1" 这类名字直接把档数写在脸上；其余按二态兜底，
        # 真正的值域在模型就绪后由 apply_outfit_ranges 用 getParams 修正。
        n = 2
        m = re.search(r"(\d+)\s*选\s*(\d+)", nm)
        if m:
            try:
                n = max(2, min(8, int(m.group(1))))
            except (TypeError, ValueError):
                n = 2
        states = []
        for k in range(n):
            v = round(k / float(n - 1), 4) if n > 1 else 0.0
            states.append({"name": "默认" if k == 0 else "%g" % v,
                           "value": float(v), "file": ""})
        out.append({
            "param": pid,
            "label": _clean_outfit_label(labels.get(pid) or "", pid),
            "default": 0.0,
            "states": states,
            "source": "param",
            "_order": order.get(pid, 999),
        })
    return out


def apply_outfit_ranges(channels: list, params) -> bool:
    """用运行时 getParams 的真实值域，把参数型通道的档位改成整数枚举。

    `getParams` 回的是 `{result: {params: [{name, min, max}]}}`，其中 name 是
    **参数 Id**（不是 cdi3 中文名）—— 这点和 cdi3 容易混。
    整数参数（内衣切换 1..5、裤袜 0..6）枚举成 1,2,3,4,5；连续参数取 5 等分。
    返回是否真的改过（没改就不用刷菜单）。
    """
    if not channels or not params:
        return False
    rng = {}
    for p in params:
        try:
            nm = str((p or {}).get("name") or "").strip()
            lo, hi = float((p or {}).get("min")), float((p or {}).get("max"))
        except (TypeError, ValueError):
            continue
        if nm and hi > lo:
            rng[nm] = (lo, hi)
    if not rng:
        return False
    changed = False
    for ch in channels:
        if ch.get("source") != "param":
            continue
        r = rng.get(ch.get("param"))
        if not r:
            continue
        lo, hi = r
        span = hi - lo
        # 整数参数：值域两端都是整数、跨度不超过 12 → 逐档枚举
        int_like = (span <= 12 and abs(lo - round(lo)) < 1e-6
                    and abs(hi - round(hi)) < 1e-6)
        if int_like:
            vals = [float(v) for v in range(int(round(lo)), int(round(hi)) + 1)]
        else:
            vals = [round(lo + span * i / 4.0, 4) for i in range(5)]
        if len(vals) < 2:
            continue
        new_states = []
        for k, v in enumerate(vals):
            new_states.append({"name": "默认" if k == 0 else "%g" % v,
                               "value": float(v), "file": ""})
        if new_states != ch.get("states"):
            ch["states"] = new_states
            changed = True
    return changed


def _load_json_safe(path: str):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _clean_outfit_label(label: str, fallback: str) -> str:
    """「1. 长短裙切换」→「长短裙切换」：去掉 cdi3 里作者加的序号前缀。"""
    txt = re.sub(r"^\s*\d+\s*[.、:：．]\s*", "", str(label or "")).strip()
    return txt or fallback


def scan_outfits(model_file: str) -> list:
    """扫描模型的换装通道。返回 [{param, label, default, states:[{name, value, file}]}]。"""
    if not model_file or not os.path.isfile(model_file):
        return []
    try:
        _mt = os.path.getmtime(model_file)
    except OSError:
        return []
    _hit = _OUTFIT_CACHE.get(model_file)
    if _hit and _hit[0] == _mt:
        return [dict(ch, states=[dict(s) for s in ch["states"]]) for ch in _hit[1]]

    mdef = _load_json_safe(model_file) or {}
    refs = mdef.get("FileReferences") or {}
    model_dir = os.path.dirname(os.path.abspath(model_file))

    # 参数 id → 可读名 / 排序（按 cdi3 里作者的顺序展示）
    labels, order = {}, {}
    cdi_rel = str(refs.get("DisplayInfo") or "")
    cdi_path = os.path.join(model_dir, cdi_rel) if cdi_rel else ""
    cdi_data = _load_json_safe(cdi_path) if (cdi_path and os.path.isfile(cdi_path)) else None
    if isinstance(cdi_data, dict):
        for i, p in enumerate(cdi_data.get("Parameters") or []):
            pid = str((p or {}).get("Id") or "").strip()
            if not pid:
                continue
            labels[pid] = str((p or {}).get("Name") or pid)
            order[pid] = i

    # 扫 exp3（模型目录向下 2 层；跳过贴图/动作等不可能放表情的目录）
    states, seq = {}, 0
    try:
        base_depth = os.path.abspath(model_dir).count(os.sep)
        for cur, dirs, files in os.walk(model_dir):
            depth = os.path.abspath(cur).count(os.sep) - base_depth
            dirs[:] = [d for d in dirs
                       if not d.startswith("_") and depth < 2
                       and d.lower() not in ("textures", "texture", "motions", "motion")]
            for fn in files:
                if not fn.lower().endswith(".exp3.json"):
                    continue
                data = _load_json_safe(os.path.join(cur, fn)) or {}
                name = fn[:-len(".exp3.json")] or os.path.splitext(fn)[0]
                for pr in (data.get("Parameters") or []):
                    pid = str((pr or {}).get("Id") or "").strip()
                    if not _OUTFIT_PARAM_RE.match(pid):
                        continue
                    try:
                        val = float((pr or {}).get("Value"))
                    except (TypeError, ValueError):
                        continue
                    seq += 1
                    states.setdefault(pid, []).append({
                        "name": name, "value": round(val, 4),
                        "file": os.path.join(cur, fn), "_seq": seq,
                    })
    except Exception:
        pass

    channels = []
    for pid, items in states.items():
        # Clothes 这类参数的范围是 0..1，而作者写 exp3 用的是 Add 语义：
        #   * Value = -1  → 表示"从现在切到 0"（例如裙子切换、尾巴开关）
        #   * Value = +1  → 表示"从现在切到 1"（例如胸口衣服、脱鞋）
        #   * Value = 0.4 → 中间体型
        # Cubism 写入时会裁到 0..1，所以 -1 直接写等于没写。这里把 exp3 的值
        # 归到 [0,1] 里当"目标态"，再补上另一端的 1.0，通道才真的能来回切。
        vals, names, neg = {}, {}, False
        vals[0.0] = 0.0
        neg_name = ""
        for it in items:
            raw = float(it["value"])
            if raw < 0:
                neg = True
                neg_name = neg_name or str(it["name"])
            v = round(max(0.0, min(1.0, raw)), 4)
            if v in vals:
                # 与默认值重复（例如「默认体型」= 0）：只是给 0 换个说法，跳过
                continue
            vals[v] = v
            names[v] = str(it["name"])
        if len(vals) < 2 and neg and neg_name:
            vals[1.0] = 1.0
            names[1.0] = "开启（%s）" % neg_name
        if len(vals) < 2:
            continue
        ch_states = []
        for v in sorted(vals):
            nm = names.get(v) or ("默认" if v == 0.0 else ("%.4g" % v))
            ch_states.append({"name": nm, "value": float(v),
                              "file": ""})
        channels.append({
            "param": pid,
            "label": _clean_outfit_label(labels.get(pid) or "", pid),
            "default": 0.0,
            "states": ch_states,
            "_order": order.get(pid, 999),
        })
    # 参数型通道（碧蓝这类没有 exp3 的模型）：与 exp3 通道共存；
    # 同一个参数 Id 已经被 exp3 通道用掉的，不再重复出一个。
    try:
        channels.extend(_param_outfit_channels(
            cdi_data, labels, order, {c["param"] for c in channels}))
    except Exception:
        pass
    channels.sort(key=lambda c: (c["_order"], c["param"]))

    for c in channels:
        c.pop("_order", None)
    _OUTFIT_CACHE[model_file] = (_mt, channels)
    return [dict(ch, states=[dict(s) for s in ch["states"]]) for ch in channels]


# === 全局快捷键：Ctrl + 方向键 ===
# 桌宠窗口是"置顶但不抢焦点"的（showInactive / 透明覆盖层），普通 QShortcut 与
# keyPressEvent 根本收不到按键 —— 只能用系统级键盘钩子。
# 方向键有两套 vkCode：主键盘的 VK_LEFT/UP/RIGHT/DOWN(0x25..0x28) 与小键盘的
# VK_NUMPAD4/6/8/2(0x64/0x66/0x68/0x62)。紧凑键盘/笔记本常常只有小键盘那种，
# NumLock 开着时按方向键给出的是 NUMPAD 码 —— 两套都要认。
_HOTKEY_VKS = {
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x64: "left", 0x68: "up", 0x66: "right", 0x62: "down",
}
_VK_CONTROL, _VK_SHIFT, _VK_MENU = 0x11, 0x10, 0x12

# 最近一次方向键的观测结果（钩子线程写、/state 读）：用户说"按了没反应"时，
# 直接看这里就知道是"没收到键"、"Ctrl 没被判到"还是"被归属判断丢掉了"。
_HK_STAT = {"vk": 0, "name": "", "hit": "", "seq": 0, "ts": ""}


class _GlobalHotkeys:
    """WH_KEYBOARD_LL 钩子：Ctrl+←/→ 切模型、Ctrl+↑/↓ 切动作。

    设计要点：
      * **不吞键**：回调一律 CallNextHookEx —— 编辑器里 Ctrl+← 照样移动光标，
        插件只是"顺便"收到一次，不会把按键吃掉；
      * 只在 `hit_test()` 为真时入队 —— 多模型时每个 worker 都会装钩子，
        这样同一次按键只会落到光标所在的那个模型上；
      * 回调里只做 GetAsyncKeyState + 入队，Qt 对象一律留给主线程。
    """

    def __init__(self, hit_test, on_key, log=None):
        self._hit_test = hit_test
        self._on_key = on_key
        self._log = log
        self._proc = None          # 必须留引用：被 GC 掉钩子会直接失效
        self._hook = None
        self._thread = None
        self._running = False

    def _logline(self, msg, level="WARN"):
        try:
            if self._log:
                self._log(level, msg)
        except Exception:
            pass

    def start(self):
        if os.name != "nt" or self._running:
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="pet-hotkey", daemon=True)
        self._thread.start()
        return True

    def _loop(self):
        import ctypes
        from ctypes import wintypes as wt

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, wt.WPARAM, wt.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                        ("flags", wt.DWORD), ("time", wt.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        WH_KEYBOARD_LL, HC_ACTION = 13, 0
        WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104

        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.CallNextHookEx.restype = ctypes.c_int
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          wt.WPARAM, wt.LPARAM]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                             ctypes.c_void_p, wt.DWORD]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p,
                                       wt.UINT, wt.UINT]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def _down(vk):
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)

        # 钩子自己维护一份修饰键状态：GetAsyncKeyState 在注入键（keybd_event /
        # 自动化脚本）、某些键鼠驱动与 IME 抢占时并不可靠，两条路取或 —— 宁可
        # 多响应一次，也别让用户按了没反应。
        _mods = {"ctrl": False, "shift": False, "alt": False}
        _VK_CTRL_LR = (0x11, 0xA2, 0xA3)
        _VK_SHIFT_LR = (0x10, 0xA0, 0xA1)
        _VK_MENU_LR = (0x12, 0xA4, 0xA5)
        _WKEYUP, _WSYSKEYUP = 0x0101, 0x0105
        _dbg = {"ts": 0.0}

        def _dbgline(text):
            """每次真正相关的按键写一行（同一键 0.5s 内只记一条，长按不刷屏）。"""
            now = time.monotonic()
            if now - _dbg["ts"] < 0.5:
                return
            _dbg["ts"] = now
            self._logline("HOTKEY " + text, "PLOG")

        def _proc(nCode, wParam, lParam):
            try:
                if nCode == HC_ACTION:
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk, wp = int(kb.vkCode), int(wParam)
                    is_down = wp in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    is_up = wp in (_WKEYUP, _WSYSKEYUP)
                    if is_down or is_up:
                        if vk in _VK_CTRL_LR:
                            _mods["ctrl"] = is_down
                        elif vk in _VK_SHIFT_LR:
                            _mods["shift"] = is_down
                        elif vk in _VK_MENU_LR:
                            _mods["alt"] = is_down
                    name = _HOTKEY_VKS.get(vk)
                    # 必须正好是 Ctrl（左右皆可）；带 Alt/Shift 时当作别人的快捷键
                    if is_down and name:
                        ctrl = _down(_VK_CONTROL) or _mods["ctrl"]
                        shift = _down(_VK_SHIFT) or _mods["shift"]
                        alt = _down(_VK_MENU) or _mods["alt"]
                        if ctrl and not alt and not shift:
                            hit, why = self._hit_test()
                            try:
                                _HK_STAT.update({"vk": vk, "name": name, "hit": why,
                                                 "seq": int(_HK_STAT.get("seq") or 0) + 1,
                                                 "ts": time.strftime("%H:%M:%S")})
                            except Exception:
                                pass
                            _dbgline("vk=0x%02X name=%s ctrl=%d alt=%d shift=%d "
                                     "hit=%s" % (vk, name, ctrl, alt, shift, why))
                            if hit:
                                self._on_key(name)
            except Exception:
                pass
            # 永远下发：绝不抢走按键
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc = HOOKPROC(_proc)
        try:
            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        except Exception as exc:
            self._logline("快捷键钩子安装异常: %r" % (exc,))
            self._hook = None
        if not self._hook:
            self._logline("快捷键钩子安装失败（SetWindowsHookEx 返回 0）")
            self._running = False
            return

        msg = wt.MSG()
        while self._running:
            try:
                r = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
            except Exception:
                break
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))



# === 鼠标避让的纯几何计算（不依赖 Qt，方便单独验证）===
AVOID_PLAN_PAD = 2   # 规划时比安全间距再多挪一点，避免"刚好贴着"被判成没躲开


def avoid_dist_sq(px, py, vr, cursor_xy):
    """窗口放在 (px, py) 时，鼠标到模型可见 rect 的最近距离的平方（在内部则 0）。"""
    lx, ly, vw, vh = vr
    left, top = px + lx, py + ly
    right, bottom = left + vw, top + vh
    cx, cy = cursor_xy
    dx = max(left - cx, 0, cx - right)
    dy = max(top - cy, 0, cy - bottom)
    return dx * dx + dy * dy


def avoid_clamp(px, py, vr, screen_w, screen_h):
    """保证「模型可见区」完整留在屏幕内（窗口可以部分出屏）。"""
    lx, ly, vw, vh = vr
    px = max(-lx, min(px, screen_w - lx - vw))
    py = max(-ly, min(py, screen_h - ly - vh))
    return px, py


def avoid_plan(vr, cursor_xy, orig_xy, screen_w, screen_h, margin):
    """挑一个让鼠标脱离模型可见区的窗口位置。

    候选 = 左/右/上/下 + 四个对角线（每个先算出「刚好脱离 margin」所需位移），
    逐个 clamp 进屏幕后按「是否达标 → 脱离多远 → 位移更小」打分取最优。
    这样贴屏幕边、被 clamp 压住的方向会自动让位给真正挪得开的另一条方向，
    不再出现"往某个方向挪了一点、模型仍压着鼠标"。
    返回 (tx, ty, dir_x, dir_y)。
    """
    lx, ly, vw, vh = vr
    ox, oy = orig_xy
    cx, cy = cursor_xy
    need = margin + AVOID_PLAN_PAD
    vl, vt = ox + lx, oy + ly
    v_right, v_bottom = vl + vw, vt + vh

    need_x = {1: (cx + need) - vl, -1: (cx - need) - v_right}
    need_y = {1: (cy + need) - vt, -1: (cy - need) - v_bottom}

    cands = []
    for sx in (1, -1):
        if need_x[sx] * sx > 0:
            cands.append((need_x[sx], 0))
    for sy in (1, -1):
        if need_y[sy] * sy > 0:
            cands.append((0, need_y[sy]))
    for sx in (1, -1):
        for sy in (1, -1):
            if need_x[sx] * sx > 0 and need_y[sy] * sy > 0:
                cands.append((need_x[sx], need_y[sy]))
    if not cands:
        cands = [(0, -(vh + need))]

    best = None
    for ddx, ddy in cands:
        tx, ty = avoid_clamp(ox + ddx, oy + ddy, vr, screen_w, screen_h)
        d2 = avoid_dist_sq(tx, ty, vr, cursor_xy)
        moved = abs(tx - ox) + abs(ty - oy)
        key = (1 if d2 >= margin * margin else 0, min(d2, margin * margin), -moved)
        if best is None or key > best[0]:
            best = (key, int(round(tx)), int(round(ty)), ddx, ddy)
    _, tx, ty, ddx, ddy = best
    dir_x = 1 if ddx > 0 else (-1 if ddx < 0 else 0)
    dir_y = 1 if ddy > 0 else (-1 if ddy < 0 else 0)
    return tx, ty, dir_x, dir_y



# === 原始 RGBA 帧接收服务器（file:// 页面 → 127.0.0.1 POST）===
class FrameServer:
    """接收渲染器推来的原始 RGBA 帧（预乘 alpha，行序自下而上）。

    file:// 页面 → http://127.0.0.1 属跨源请求；渲染器用 `text/plain` Blob
    发送，属 CORS 安全内容类型 → 不触发预检，实测单帧 ≈ 2.5ms + 9.5ms/MB。
    这里只负责收字节并转交给 Qt 线程，解码/绘制全部在 Qt 线程内完成。
    """

    def __init__(self, on_frame):
        self._on_frame = on_frame
        self._httpd = None
        self._thread = None
        self.port = 0
        self.token = ""
        # 端到端背压：HTTP 线程等 Qt 侧 ack 后才回 204，渲染器 _inFlight 才会放下一帧。
        # 同一时刻只允许 1 帧在途，超时或已有在途帧时直接丢弃，避免 Qt 信号队列堆积。
        self._ack_cond = threading.Condition()
        self._ack_timeout = 2.0
        self._seq = 0
        self._ack_seq = 0
        self._pending_seq = None
        self._pending_since = 0.0
        self._timeouts = 0
        self._dropped = 0
        self._posts = 0
        self._accepted = 0
        self._bad_token = 0
        self._bad_len = 0
        self._http_requests = 0
        self._options = 0

    def stats(self):
        """供 /state 读取的轻量背压指标。"""
        with self._ack_cond:
            pending = self._pending_seq is not None
            age = ((time.monotonic() - self._pending_since) * 1000.0
                   if pending else 0.0)
            return {
                "queue_depth": 1 if pending else 0,
                "frame_age_ms": round(max(0.0, age), 1),
                "dropped_frames": self._dropped,
                "ack_timeouts": self._timeouts,
                "frame_posts": self._posts,
                "frame_accepted": self._accepted,
                "frame_bad_token": self._bad_token,
                "frame_bad_len": self._bad_len,
                "http_requests": self._http_requests,
                "http_options": self._options,
            }

    def _submit(self, data, w, h, cw, ch, ox, oy):
        """提交一帧并等待 Qt 消费确认；返回是否被接收。"""
        with self._ack_cond:
            self._seq += 1
            seq = self._seq
            if self._pending_seq is not None:
                # 上一帧还没被 Qt 消费：丢弃新帧，先让主线程把旧帧处理完。
                self._dropped += 1
                return False
            self._pending_seq = seq
            self._pending_since = time.monotonic()
        try:
            accepted = bool(self._on_frame(data, w, h, cw, ch, ox, oy, seq))
        except Exception:
            accepted = False
        if not accepted:
            with self._ack_cond:
                if self._pending_seq == seq:
                    self._pending_seq = None
                    self._pending_since = 0.0
                self._ack_cond.notify_all()
            return False
        with self._ack_cond:
            acked = self._ack_cond.wait_for(
                lambda: self._ack_seq >= seq, timeout=self._ack_timeout)
            if not acked:
                self._timeouts += 1
                # Qt 超时未确认时释放槽位，允许后续帧继续尝试；旧信号若已排在
                # Qt 队列里，最多留下 1 帧，不会再无限增长。
                if self._pending_seq == seq:
                    self._pending_seq = None
                    self._pending_since = 0.0
        return acked

    def ack(self, seq):
        """Qt 线程消费完一帧后调用，放行下一帧。"""
        try:
            seq = int(seq or 0)
        except Exception:
            seq = 0
        if seq <= 0:
            return
        with self._ack_cond:
            if seq > self._ack_seq:
                self._ack_seq = seq
            if self._pending_seq == seq:
                self._pending_seq = None
                self._pending_since = 0.0
            self._ack_cond.notify_all()

    def start(self, ports=range(18780, 18800), token=""):
        submit = self._submit
        fs = self
        self.token = token or ""

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"   # keep-alive：每帧复用同一条连接

            def log_message(self, *args):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Cache-Control", "no-store")

            def do_OPTIONS(self):
                fs._options += 1
                # 兜底：万一将来改成非安全内容类型触发预检
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                fs._http_requests += 1
                try:
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        length = 0
                    body = self.rfile.read(length) if length > 0 else b""
                    parsed = urllib.parse.urlparse(self.path)
                    if parsed.path == "/__pet_frame__" and length > 0:
                        fs._posts += 1
                        qs = urllib.parse.parse_qs(parsed.query)
                        # 帧令牌：只收自己这个实例渲染器推来的帧。
                        # 旧进程/别的实例（端口被抢过）推来的帧一律丢弃，避免画面来回闪。
                        ftoken = qs.get("ftoken", [""])[0]
                        ok_token = bool(token) and ftoken == token
                        w = int(qs.get("w", ["0"])[0])
                        h = int(qs.get("h", ["0"])[0])
                        # 裁剪区域几何：cw/ch=画布尺寸，ox/oy=裁剪左上角（左上原点）
                        # 缺省时按整幅处理（旧渲染器 / PNG 通道仍能工作）
                        cw = int(qs.get("cw", ["0"])[0])
                        ch = int(qs.get("ch", ["0"])[0])
                        ox = int(qs.get("ox", ["0"])[0])
                        oy = int(qs.get("oy", ["0"])[0])
                        if not ok_token:
                            fs._bad_token += 1
                        elif w <= 0 or h <= 0 or len(body) != w * h * 4:
                            fs._bad_len += 1
                        elif submit(body, w, h, cw, ch, ox, oy):
                            fs._accepted += 1
                except Exception:
                    pass
                # 帧数据不关心响应体；必须回 Content-Length 才能 keep-alive
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

        class _Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            # 必须独占：Windows 上 SO_REUSEADDR=True 会让两个插件实例同时绑住同一个端口，
            # 两边的帧互相投递 → 窗口在「自己的模型」和「别人的模型」之间来回闪。
            # 不能用单线程 HTTPServer：浏览器可能保留 OPTIONS/上一帧的 keep-alive
            # 连接，单线程会卡在那条空闲连接上，新的帧 POST 永远进不来（frames=0）。
            allow_reuse_address = False

        for port in ports:
            try:
                httpd = _Server(("127.0.0.1", port), Handler)
            except OSError:
                continue
            self._httpd = httpd
            self.port = port
            self._thread = threading.Thread(
                target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
            self._thread.start()
            return port
        return 0

    def stop(self):
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass


class DesktopPetWindow:
    """桌宠窗口管理器 — 在守护线程运行 PyQt5 事件循环。

    渲染链路：离屏 QWebEngineView(WebGL) → toDataURL → QWebChannel
    → Python Bridge → QPixmap → QPainter 绘制到透明 QWidget。

    支持交互：左键拖拽移动、滚轮缩放（中心固定）、右键菜单
    （鼠标跟随 / 鼠标避让 / 隐藏 / 关闭 / 切换模型）。
    鼠标避让会在原始位置生成 ZoneMarker 虚线框标记区域，
    鼠标进入则桌宠沿锁定方向推离，离开后平滑回位；
    可在标记区域上右键关闭避让。
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._app = None
        self._cmd_queue: queue.Queue = queue.Queue()
        self._running = False
        self._sync_result = None
        self._sync_event = threading.Event()
        self._shutdown_event = threading.Event()  # 线程彻底退出信号
        self._frame_server = None     # 原始 RGBA 帧接收服务器（FrameServer）
        self.frame_count = 0          # 已接收的原始帧数（诊断用）
        self.fps = 0.0                # 最近一次采样到的到达帧率（诊断用）
        self.on_scale_change = None   # 回调(new_scale) — 滚轮缩放后通知 tool.py
        self.on_model_switch = None   # 回调(model_path) — 右键切换模型后通知 tool.py
        self.on_pos_change = None     # 回调(x, y) — 鼠标避让/拖拽后更新
        self._model_ready_event = threading.Event()  # 模型加载/切换完成事件
        self._model_ready = False                    # True=最近一次加载成功
        self._model_error: Optional[str] = None      # 最近一次加载失败原因
        self._visible = False                        # 桌宠窗口当前是否可见（hide/close 后为 False）

    def start(self, model_http_url: str, x: float, y: float, scale: float):
        """在守护线程启动 PyQt5 + 离屏渲染 + 透明宠物窗口。"""
        self._shutdown_event.clear()
        self._thread = threading.Thread(
            target=self._qapp_main,
            args=(model_http_url, x, y, scale),
            daemon=True
        )
        self._thread.start()
        self._running = True

    def _qapp_main(self, model_url, x, y, scale):
        from PyQt5.QtCore import (Qt, QUrl, QObject, pyqtSlot, pyqtSignal, QByteArray,
                                  QTimer, QRect, QRectF, QPoint, QPointF, QEvent)
        from PyQt5.QtWidgets import (
            QApplication, QWidget, QMenu, QAction, QFileDialog, QLabel, QPushButton,
            QMessageBox,
        )
        from PyQt5.QtWebEngineWidgets import (QWebEngineView, QWebEnginePage, QWebEngineSettings,
                                              QWebEngineProfile)
        from PyQt5.QtWebChannel import QWebChannel
        from PyQt5.QtGui import (QColor, QImage, QPixmap, QPainter, QCursor, QPainterPath,
                                 QFont, QFontMetrics, QBrush, QPen, QRegion)

        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        app = QApplication.instance() or QApplication(sys.argv)
        # 桌宠"关闭"只隐藏、不退出事件循环：QApplication / QWebEngine 是进程级单例，
        # 销毁后在本进程内二次创建必然 access violation（QtWebEngine 不支持同进程重建）。
        # 保持常驻，供多次 show/hide 复用同一窗口与渲染上下文。
        app.setQuitOnLastWindowClosed(False)

        # === Chromium profile 落盘位置：必须显式设置，不能靠 --user-data-dir ===
        # 实测 QtWebEngine 5.15 忽略 QTWEBENGINE_CHROMIUM_FLAGS 里的 --user-data-dir，
        # 默认 profile 永远落在 <AppLocalData>/python/cache/QtWebEngine/Default。
        # 单进程时无感，多 worker 时就是灾难：多个 Chromium 抢同一个
        # Cookies/Cache 的 sqlite（日志刷 "database is locked"、"Unable to create cache"）。
        # 这里把 default profile 指到本进程专属目录，并彻底关掉 HTTP 缓存与持久
        # cookie —— 桌宠只渲染本地 file:// 页面，这两样纯属多余。
        # 必须在任何 QWebEngineView / QWebEnginePage 之前执行。
        try:
            _prof = QWebEngineProfile.defaultProfile()
            _pdir = _profile_dir()
            _prof.setPersistentStoragePath(os.path.join(_pdir, "storage"))
            _prof.setCachePath(os.path.join(_pdir, "cache"))
            _prof.setHttpCacheType(QWebEngineProfile.NoCache)
            try:
                _prof.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
            except Exception:
                pass
            _log_line("INFO", "webengine profile -> %s" % _pdir)
        except Exception as _e:
            _log_line("WARN", "设置 webengine profile 路径失败: %r" % (_e,))

        BASE_W, BASE_H = 700, 800   # 基础窗口尺寸（验证原型同款，避免原始尺寸过小）
        # 缩放范围：滚轮 / 命令 / 右键菜单共用一套上下限（tool.py 的 _cmd_resize 与之同步）
        MIN_SCALE, MAX_SCALE = 0.3, 5.0
        # === 画布尺寸策略：只准超采样，绝不低于 1:1 ===
        # 现场日志证实：旧版按「动/静」在 0.50×/1.05× 之间来回切画布，
        # 700×800 的窗口实际画布在 471×538 ↔ 682×780 之间每 1~3 秒跳一次。
        # 倍率 <1 → 帧被双线性放大到窗口 = 发虚；每次切换还要重建渲染缓冲
        # 并重算 fitScale（0.0688↔0.0997，模型瞬间变大变小）= 肉眼看到的卡顿。
        # 所以：清晰度用像素下限锁死（q>=1.0），流畅度改由「只传模型实际占用
        # 的区域」来省（Live2D 画布是 6500×6500 的方形，实测人物只占 41%）。
        MIN_RENDER_PIX = 500_000        # 小窗口超采样下限（≈707×707），缩小后依然锐利
        MAX_RENDER_PIX = 2_600_000      # 大窗口帧体积兜底（≈1612×1612）

        def _render_pixels(w, h):
            """目标画布像素数：不小于窗口像素（1:1 是清晰度硬下限）。"""
            return min(MAX_RENDER_PIX, max(MIN_RENDER_PIX, int(w * h)))

        def _fit_render_size(w, h):
            """窗口尺寸 → 实际渲染画布尺寸（统一出入口，两处调用保持一致）。

            q = sqrt(目标像素/窗口像素)，且钳到 [1.0, 3.0]：下界保证任何情况下
            画布都不小于窗口（不再有"帧被放大"的模糊），上界防止极小窗口
            无脑超采样把帧体积顶爆。
            """
            w, h = max(1, int(w)), max(1, int(h))
            q = (_render_pixels(w, h) / float(w * h)) ** 0.5
            q = max(1.0, min(3.0, q))
            rw, rh = int(w * q), int(h * q)
            pix = rw * rh
            if pix > MAX_RENDER_PIX:
                cap = (MAX_RENDER_PIX / pix) ** 0.5
                rw, rh = int(rw * cap), int(rh * cap)
            return max(100, rw), max(100, rh)
        scale = max(MIN_SCALE, min(MAX_SCALE, float(scale)))
        # 渲染模式：direct = 可见透明 QWebEngineView 直接上屏（默认）；
        # frame = 旧版离屏 readPixels + HTTP 回传 + QPainter 绘制（保留回退）。
        _render_mode = (os.environ.get("PET_RENDER_MODE") or "electron").strip().lower()
        _electron_runtime = os.path.join(_PLUGIN_DIR, "electron_runtime", "Hime Display.exe")
        _require_electron = (os.environ.get("PET_REQUIRE_ELECTRON") or "0").strip() in ("1", "true", "yes")
        if _render_mode in ("auto", "electron") and not os.path.exists(_electron_runtime):
            # 以前这里是**静默**降级：缺 Electron 运行时就直接走 Qt 的 color-key 渲染，
            # 用户以为装好了，实际画面边缘发毛、性能也差一档。现在改成：
            #   1) 一定要在日志里大声说明（便于在"插件日志"里查）；
            #   2) 把状态记进 _WSTATE，`/state` 与工具回包能看到；
            #   3) 除非显式 PET_REQUIRE_ELECTRON=1，否则仍然降级（不阻断使用），
            #      但会在首次 show 的回包里给出可操作提示。
            _WSTATE["electron_missing"] = True
            print("[pet] " + "=" * 62, flush=True)
            print("[pet] ⚠ 缺少 Electron 运行时：%s" % _electron_runtime, flush=True)
            print("[pet]   已降级为 Qt WebEngine 渲染（color-key 抠黑底：边缘可能有毛边、"
                  "性能较低、透明度不如原生）", flush=True)
            print("[pet]   修复：能力管理 → 依赖安装 → 重新扫描/安装依赖（会自动下载约 115MB）；"
                  "或设 PET_REQUIRE_ELECTRON=1 让它直接报错而不是降级", flush=True)
            print("[pet] " + "=" * 62, flush=True)
            if _require_electron:
                raise RuntimeError(
                    "缺少 Electron 运行时（%s）。请在 能力管理 → 依赖安装 里安装该插件的"
                    "文件型依赖，或删除环境变量 PET_REQUIRE_ELECTRON 以允许降级渲染。"
                    % _electron_runtime
                )
            _render_mode = "direct"
        else:
            _WSTATE.pop("electron_missing", None)
        _electron_mode = _render_mode == "electron"
        _direct_mode = _render_mode != "frame"
        # 鼠标事件排障：PET_MOUSE_LOG=1 时把按下/拖动写进 worker 日志
        _MOUSE_DBG = (os.environ.get("PET_MOUSE_LOG") or "").strip() in ("1", "true", "yes")
        _WSTATE["render_mode"] = _render_mode   # /state 观测：electron/direct/frame
        # 子控件模式（实验）：QWebEngineView 作为 PetWidget 子控件，借父窗口的透明通道；
        # 顶层原生 QWebEngineView 在 Windows 上会自带黑底。
        _direct_child = (os.environ.get("PET_DIRECT_CHILD") or "0").strip() == "1"
        # Windows 上 QWebEngine 顶层窗不支持真正的逐像素透明：改用纯色键
        # （LWA_COLORKEY）把页面底色抠掉，模型仍由 GPU 直接合成上屏。
        _colorkey = (os.environ.get("PET_COLORKEY") or "1").strip() != "0"

        PET_W = int(BASE_W * scale)
        PET_H = int(BASE_H * scale)
        # 全屏透明窗口：窗口固定覆盖整块显示器；PET_W/PET_H 只是 Stage
        # （模型局部显示盒）的初始尺寸，不再等于 OS 窗口尺寸。
        try:
            _primary = app.primaryScreen()
            _pg = _primary.geometry()
            _at = QPoint(int(_pg.x() + x * _pg.width()),
                         int(_pg.y() + y * _pg.height()))
            _scr = app.screenAt(_at) or _primary
            screen = _scr.geometry()
        except Exception:
            screen = app.primaryScreen().geometry()
        _stage_x = screen.x() + int(round(x * max(0, screen.width() - PET_W)))
        _stage_y = screen.y() + int(round(y * max(0, screen.height() - PET_H)))
        _cfg_modes = load_ui_cfg().get("display_modes") or {}
        _mode_raw = str(_cfg_modes.get(str(model_url), "local") or "local").lower()
        _display_mode = "full" if _mode_raw in ("full", "all", "screen") else "local"

        # 共享可变状态（滚轮缩放 + 眼睛/头部跟随 + 鼠标避让）
        # 鼠标跟随默认开启；用户右键关掉后写入 _pet_ui.json，下次启动沿用他的选择
        _cfg_track = load_ui_cfg().get("track")
        # 多实例：第二个起的桌宠默认不跟随鼠标 —— 多个窗口共用一个光标位置，
        # 全部跟随会互相盖住/互相推挤；由父进程用 PET_TRACK=0 显式关掉，
        # 用户之后仍可在右键菜单里单独打开（写回 _pet_ui.json）。
        if (os.environ.get("PET_TRACK") or "").strip() == "0":
            _cfg_track = False
        state = {
            "scale": scale,
            # 全屏架构：窗口固定为显示器大小，模型放在 stage 里。
            # stage_rect = 模型局部显示盒（屏幕坐标）；clip_rect = 实际可见范围。
            "stage_rect": (_stage_x, _stage_y, int(PET_W), int(PET_H)),
            "display_mode": _display_mode,   # local / full
            "model_url": str(model_url),
            # 直出模式：模型包围盒宽高比（模型就绪后由 getModelRect 回填）；
            # Stage 宽度按它扩展，宽度不再反过来压模型高度。
            "model_aspect": None,
            # 模型可见矩形（归一化到画布/窗口，x,y,w,h）。用于把 Qt 交互
            # 覆盖层的鼠标命中区裁到模型上：模型外的透明边恢复点击穿透。
            "model_rect_norm": None,
            # 上面那份包围盒是按"哪个 Stage 原点"量出来的（拖动/缩放夹取要用它
            # 把位移补回去，否则贴边极限会随拖动距离漂移）
            "model_rect_stage": None,
            # 模型 alpha 剪影（降采样 0/1 网格）；有它时优先按剪影做命中区，
            # 包围盒只作为模型未就绪/采样失败时的兜底。
            "alpha_mask": None,
            "alpha_mask_ver": 0,
            # 画布尺寸：只随窗口缩放变化（不再随"动/静"切换，杜绝 resize 抖动）
            "cur_render": (PET_W, PET_H),
            "track": True if _cfg_track is None else bool(_cfg_track),
            "was_near": False,
            "avoid": False,
            "avoid_orig_win_pos": None,   # (x, y) — 开启避让瞬间的窗口左上角，永不丢失
            "avoid_zone": None,           # ZoneMarker QWidget — 可见区域的标记
            "avoid_pushing": False,
            "avoid_push_dir": None,
            "avoid_target": None,         # (x, y) — 本次避让计算出的窗口目标位置
            "avoid_hold": False,          # 连续躲避超上限后的强制暂停（菜单用 menu_open）
            "avoid_grace_until": 0.0,     # 菜单刚收起的一小段缓冲期内不避让（monotonic 时间）
            "menu_open": False,           # 右键菜单是否正展开（展开期间不挪窗口、不避让）
            "avoid_burst": 0,             # 连续躲避次数（超过上限就强制暂停，绝不死循环）
            "avoid_visible_rect": None,   # (lx, ly, w, h) — 模型可见 rect（相对窗口左上角）
            "part_mode": False,           # 部件隐藏：点击选中模式
            "part_pending": None,         # 已选中但未确认的部件
            "render_mode": _render_mode,  # electron / direct / frame（A1 观测用）
            # 右键菜单「播放动画」用的资源缓存（getAssets 结果），模型切换时刷新
            "assets": None,
            "assets_at": 0.0,
            # 换装：模型自带的服装参数通道（Clothes1..5 之类）+ 各通道当前状态下标
            "outfits": [],
            "outfit_sel": {},
            "outfit_cursor": 0,
            "anim_loop": bool(load_ui_cfg().get("anim_loop", False)),
            "dblclick": True if load_ui_cfg().get("dblclick") is None else bool(load_ui_cfg().get("dblclick")),
            # 配音字幕（底部半透明条）：默认开，右键菜单可关
            "subtitles": True if load_ui_cfg().get("subtitles") is None else bool(load_ui_cfg().get("subtitles")),
            # 双击热区地图：每个动画一个互不重合的触发点（模型就绪后异步建好）
            "click_map": None,
            # 说话时的程序化微动作偏移量（TTS 服务每 50ms 推一次，叠加在鼠标跟随目标上）
            "gesture": {},
            # 由 TTS 服务调用的"立刻重发跟随目标"入口（闭包注入，见 _send_track_params）
            "send_track": None,
        }
        # /state 快照里也要有位置信息：全屏模式下 __geom__ 得等渲染层空闲，
        # 工具/回归脚本只想知道"模型现在在哪"时不该被 JS 往返卡住。
        _WSTATE["stage_rect"] = state["stage_rect"]
        _WSTATE["display_mode"] = state["display_mode"]
        _WSTATE["model_rect_norm"] = None
        _WSTATE["model_rect_stage"] = None

        # === 加载提示消息泡泡（模型加载/切换时显示）===
        class LoadingBubble(QWidget):
            """桌宠窗口内的消息泡泡 — 圆角气泡 + 小尾巴，带呼吸/省略号/升降动画。

            作为 PetWidget 的子控件，自动随窗口移动缩放；鼠标事件穿透，
            不影响拖拽与避让检测。
            """

            def __init__(self, parent):
                super().__init__(parent)
                self.setAttribute(Qt.WA_TranslucentBackground, True)
                self.setAttribute(Qt.WA_NoSystemBackground, True)
                self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                self.setFixedSize(128, 54)
                self._text = "加载中"
                self._dot_count = 0
                self._dots = True        # True=省略号动画（加载中）；False=纯文字（进度）
                self._ratio = None       # 0.0~1.0 = 画进度条；None = 不画
                self._opacity = 0.0
                self._fade = "in"        # in | steady | out
                self._start = 0.0        # 动画起点（呼吸/省略号计时）
                self._fade_start = 0.0
                self._timer = QTimer(self)
                self._timer.timeout.connect(self._tick)
                self.hide()

            def show_loading(self, text="加载中"):
                self._text = text
                self._dot_count = 0
                self._dots = True
                self._ratio = None
                self.setFixedSize(128, 54)
                self._opacity = 0.0
                self._fade = "in"
                self._fade_start = time.monotonic()
                self._start = self._fade_start
                if not self._timer.isActive():
                    self._timer.start(50)   # 20fps 动画步进
                self.reposition()
                self.show()
                self.raise_()
                self.update()
                self._resync_mask()

            def show_progress(self, text, ratio=None):
                """进度模式：纯文字 + 底部进度条，ratio=None 时只显示文字（不确定进度）。

                和 show_loading 的区别只是不画省略号、多一条进度条、宽一点。
                已经显示时不重新走淡入，避免每 300ms 更新一次就闪一下。
                """
                self._text = " ".join(str(text or "").split()) or "处理中"
                self._ratio = None if ratio is None else max(0.0, min(1.0, float(ratio)))
                self._dots = False
                if self.width() != 150 or self.height() != 58:
                    self.setFixedSize(150, 58)
                if (not self.isVisible()) or self._fade in ("out", "done"):
                    self._opacity = 0.0
                    self._fade = "in"
                    self._fade_start = time.monotonic()
                    self._start = self._fade_start
                    self.reposition()
                    self.show()
                    self.raise_()
                if not self._timer.isActive():
                    self._timer.start(50)
                self.update()
                self._resync_mask()

            def hide_loading(self):
                if not self.isVisible():
                    return
                self._fade = "out"
                self._fade_start = time.monotonic()
                if not self._timer.isActive():
                    self._timer.start(50)

            def _resync_mask(self):
                """气泡出现/消失后立刻重算覆盖层命中区。

                setMask 会把整窗（含子控件）一起裁掉，气泡贴在模型头顶的剪影
                之外；不主动重算就得等下一次剪影刷新（几十毫秒到几秒），
                这段时间里气泡是"被撕掉一半"的状态。
                """
                try:
                    p = self.parent()
                    if p is not None:
                        p._apply_interaction_mask()
                except Exception:
                    pass

            def reposition(self):
                """水平居中于 Stage 顶部（贴近模型头部上方）。"""
                p = self.parent()
                try:
                    sx, sy, sw, sh = p.stage_local_rect()
                except Exception:
                    sw = p.width() if p else 700
                    sx, sy = 0, 0
                x = sx + (sw - self.width()) // 2
                self.move(x, sy + 6 + int(getattr(self, "_bob", 0)))

            def _tick(self):
                now = time.monotonic()
                if self._fade == "in":
                    self._opacity = min(1.0, (now - self._fade_start) / 0.25)
                    if self._opacity >= 1.0:
                        self._fade = "steady"
                elif self._fade == "out":
                    self._opacity = max(0.0, 1.0 - (now - self._fade_start) / 0.22)
                    if self._opacity <= 0.0:
                        self._fade = "done"
                        self._timer.stop()
                        self.hide()
                        self._resync_mask()
                        return
                if self._fade == "steady":
                    t = now - self._start
                    # 呼吸：不透明度 0.72~1.0 正弦脉动
                    self._opacity = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(t * 3.0))
                    # 省略号：每 330ms 进一位（0→3 循环）；进度模式不显示省略号
                    if self._dots:
                        self._dot_count = int(t * 3.0) % 4
                    # 上下浮动 ±2px
                    self._bob = 2.0 * math.sin(t * 2.2)
                    self.reposition()
                self.update()

            def paintEvent(self, e):
                if self._opacity <= 0.01:
                    return
                qp = QPainter(self)
                qp.setRenderHint(QPainter.Antialiasing, True)
                w, h = self.width(), self.height()
                body_h = h - 9                    # 尾巴高度 9px
                body = QRectF(1.5, 1.5, w - 3, body_h - 3)

                path = QPainterPath()
                path.addRoundedRect(body, 12, 12)
                cx = w / 2.0
                tw = 13.0
                path.moveTo(cx - tw / 2, body_h - 3)
                path.lineTo(cx, h - 1.5)
                path.lineTo(cx + tw / 2, body_h - 3)
                path.closeSubpath()

                qp.fillPath(path, QColor(38, 38, 54, int(225 * self._opacity)))
                qp.setPen(QColor(255, 255, 255, int(55 * self._opacity)))
                qp.setBrush(Qt.NoBrush)
                qp.drawPath(path)

                qp.setPen(QColor(255, 255, 255, int(238 * self._opacity)))
                font = QFont()
                font.setPointSize(9)
                font.setBold(True)
                qp.setFont(font)
                if self._ratio is None:
                    # 加载/不确定进度：文字 + 省略号，垂直居中
                    qp.drawText(body, Qt.AlignCenter, self._text + "." * self._dot_count)
                else:
                    # 进度模式：文字略上移，底部一条进度条（轨道 + 已完成的填充）
                    text_rect = QRectF(body.x(), body.y() - 2, body.width(), body.height() - 8)
                    qp.drawText(text_rect, Qt.AlignCenter, self._text)
                    bar = QRectF(body.x() + 10, body.bottom() - 12, body.width() - 20, 5)
                    qp.setPen(Qt.NoPen)
                    qp.setBrush(QColor(255, 255, 255, int(60 * self._opacity)))
                    qp.drawRoundedRect(bar, 2.5, 2.5)
                    done = QRectF(bar.x(), bar.y(), bar.width() * float(self._ratio), bar.height())
                    qp.setBrush(QColor(120, 200, 255, int(235 * self._opacity)))
                    qp.drawRoundedRect(done, 2.5, 2.5)
                qp.end()

        # === 透明宠物窗口（QPainter 逐帧绘制，真正可见）===
        class PetWidget(QWidget):
            _shutting_down = False  # 关闭过程中禁止 paintEvent 绘制，避免 access violation
            def __init__(self):
                super().__init__()
                self.setWindowFlags(
                    Qt.FramelessWindowHint |
                    Qt.WindowStaysOnTopHint |
                    Qt.Tool
                )
                self.setAttribute(Qt.WA_TranslucentBackground, True)
                self.setAttribute(Qt.WA_NoSystemBackground, True)
                self._img = QImage()      # 当前帧（原始通道=倒置缓冲，dataURL 通道=正常方向）
                self._pix = QPixmap()     # 兼容旧 dataURL 通道
                self._imgRaw = False      # True = 原始 readPixels 帧（行序自下而上）
                self._rawBytes = None     # 保活：QImage 不复制底层缓冲
                # 帧几何：JS 只回传模型实际占用的裁剪区域（裁剪省带宽，画布尺寸不变）
                self._canvas = None       # (cw, ch) 画布像素；None = 等于当前帧
                self._crop = None         # (ox, oy) 裁剪区域左上角（画布像素，左上原点）
                self._dragging = False
                self._offset = None
                self._press_gpos = None   # 按下时的全局坐标（判断双击前有没有拖动）
                self._press_moved = False
                # 双击热区提示：[(nx, ny, 序号)] + 到期时间（画在模型位置上方）
                self._hotspot_pts = []
                self._hotspot_until = 0.0
                # 配音字幕：底部半透明条（默认开，右键菜单「显示配音字幕」可关）
                self._sub_text = ""
                self._sub_until = 0.0
                # 字幕位置：相对**模型 Stage 中心**的归一化偏移，None = 贴模型底部居中。
                # 只活在"本次打开"里：用户拖动才记住，重开/换模型一律回默认，不写盘。
                # （旧的 _pet_ui.json:sub_pos 是"相对整屏"的归一化坐标，换模型后会把字幕
                #   钉死在屏幕原处，现已弃用、不再读取。）
                self._sub_rel = None
                self._sub_rect = QRectF()
                # 上一帧真正画在屏幕上的矩形：setMask 要一并罩住它，
                # 否则缩放/拖动时上一帧的字会被新掩膜咬掉边（"字幕被剪影"）。
                self._sub_paint_rect = QRectF()
                # 刚撤掉的那条字幕占过的屏幕矩形（见 `_sub_ghost_sweep`）：
                # 掩膜以后要是重新罩住它，就在那一帧强制同步擦一次。
                self._sub_ghost = None
                self._sub_ghost_until = 0.0
                self._sub_geom = None
                self._dragging_sub = False
                self._sub_drag_offset = None
                self._sub_drag_moved = False
                # 字幕自带到期定时器：即使服务层清屏没跑到，UI 也会在到期时自己消失
                self._sub_timer = QTimer(self)
                self._sub_timer.setSingleShot(True)
                self._sub_timer.timeout.connect(self._sub_expire)
                # TTS 逐字高亮：已念出的字数（-1=不启用高亮）
                self._sub_spoken = -1
                self.on_visibility_change = None   # 回调(visible) — 隐藏/显示时暂停帧推送
                self.on_geometry_change = None     # 回调() — 移动/缩放时同步直出渲染窗
                self._direct_mode = _direct_mode   # 直出模式：模型由 QWebEngineView 自己显示
                self._mask_key = None              # 当前模型命中区（模型外透明穿透）
                # 初始命中区先收到 1px：窗口是整屏的，没拿到模型剪影之前
                # 整窗都能接鼠标，等于把桌面的点击全吃掉。
                self.setMask(QRegion(0, 0, 1, 1))
                self._bubble = LoadingBubble(self)   # 加载提示消息泡泡
                self._menu = None         # 右键菜单单实例（首次右键才建，之后复用）
                if _electron_mode:
                    # 全屏透明窗口：模型由 Electron 显示，Qt 只做交互覆盖层。
                    self.setGeometry(screen)
                else:
                    self.resize(PET_W, PET_H)
                    self.move(int(x * (screen.width() - PET_W)),
                              int(y * (screen.height() - PET_H)))

            def stage_rect(self):
                """模型 Stage（屏幕坐标）(x, y, w, h)。"""
                r = state.get("stage_rect")
                if r and len(r) == 4:
                    return tuple(int(v) for v in r)
                return (screen.x(), screen.y(), int(self.width()), int(self.height()))

            def stage_local_rect(self):
                """Stage 相对 Qt 窗口（全屏窗口左上角）的矩形。"""
                sx, sy, sw, sh = self.stage_rect()
                return (sx - screen.x(), sy - screen.y(), sw, sh)

            def clip_rect(self):
                """当前可见裁剪盒（屏幕坐标）：local=Stage，full=整块显示器。"""
                if str(state.get("display_mode") or "local") == "full":
                    return (screen.x(), screen.y(), screen.width(), screen.height())
                return self.stage_rect()

            def move_stage_to(self, x, y):
                """拖动：只移动 Stage，全屏窗口保持不动。

                允许模型越过屏幕边缘（不再把整个 Stage 锁在屏幕内），
                但至少保留一小块模型可见，避免完全拖丢后无法再抓回来。
                """
                _sx, _sy, sw, sh = self.stage_rect()
                keep = 24
                # 优先用模型在 Stage 内的实际包围盒，让"至少露一点"落在
                # 模型可见部分上，而不是 Stage 的透明留白上。
                norm = state.get("model_rect_norm")
                try:
                    if norm and len(norm) == 4:
                        nx, ny, nw, nh = [float(v) for v in norm]
                    else:
                        nx, ny, nw, nh = 0.0, 0.0, 1.0, 1.0
                    # model_rect_norm 是**整块画布**归一化（直出画布 = 全屏覆盖层
                    # = 屏幕），所以模型屏幕坐标 = norm × 覆盖层尺寸，跟 Stage 的
                    # 455×264 没有关系。旧实现拿 norm × Stage 宽高来算，量纲差了
                    # 4 倍多（1920 vs 455），夹出来的边界离屏幕边还有 350~590px
                    # 就"拖不动了" —— 这正是"模型移不出屏幕 / 到不了边缘"。
                    pw = float(self.width()) or float(screen.width())
                    ph = float(self.height()) or float(screen.height())
                    # norm 是"抓帧那一刻的 Stage 原点"量出来的，拖动期间 Stage
                    # 一直在跑；把这段位移补回去，否则夹边会随拖动距离漂移。
                    _nsx, _nsy = state.get("model_rect_stage") or (_sx, _sy)
                    _ml = nx * pw + (_sx - float(_nsx))
                    _mr = (nx + nw) * pw + (_sx - float(_nsx))
                    _mt = ny * ph + (_sy - float(_nsy))
                    _mb = (ny + nh) * ph + (_sy - float(_nsy))
                    # 以"当前几何"为基准算允许的位移增量：
                    # 模型右边 >= 屏幕左边 + keep；模型左边 <= 屏幕右边 - keep
                    min_x = int(round(_sx + (screen.x() + keep - _mr)))
                    max_x = int(round(_sx + (screen.x() + screen.width() - keep - _ml)))
                    min_y = int(round(_sy + (screen.y() + keep - _mb)))
                    max_y = int(round(_sy + (screen.y() + screen.height() - keep - _mt)))
                except Exception:
                    min_x = screen.x() - sw + keep
                    max_x = screen.x() + screen.width() - keep
                    min_y = screen.y() - sh + keep
                    max_y = screen.y() + screen.height() - keep
                x = max(min_x, min(int(x), max_x))
                y = max(min_y, min(int(y), max_y))
                try:
                    _set_stage_rect(x, y, sw, sh)
                except Exception:
                    state["stage_rect"] = (int(x), int(y), int(sw), int(sh))
                    hook = getattr(self, "_stage_hook", None)
                    if hook:
                        try:
                            hook()
                        except Exception:
                            pass

            def setFrame(self, dataUrl: str):
                """兼容通道：接收 JS 推来的 dataURL（PNG），解码 → QImage + QPixmap。"""
                try:
                    b64 = dataUrl.split(',', 1)[1]
                    img = QImage.fromData(QByteArray.fromBase64(b64.encode()))
                    if not img.isNull():
                        self._img = img          # 保留 QImage，pixelColor() 查 alpha
                        self._pix = QPixmap.fromImage(img)
                        self._imgRaw = False
                        self._rawBytes = None
                        self._canvas = None      # PNG 通道是整幅画布
                        self._crop = None
                        self.update()
                except Exception:
                    pass

            def setFrameRaw(self, data, w: int, h: int, cw=None, ch=None,
                            ox=None, oy=None):
                """主通道：接收 gl.readPixels 原始 RGBA（预乘 alpha、行序自下而上）。

                w/h 是本次回传的**裁剪区域**尺寸，cw/ch 是画布尺寸，
                ox/oy 是裁剪区域在画布内的左上角坐标（左上原点）。
                没有 cw/ch 时按整幅处理（向后兼容）。
                QImage 直接包装 Python bytes（不复制，≈0ms），仅需保持 bytes 存活。
                """
                try:
                    if w <= 0 or h <= 0 or len(data) != w * h * 4:
                        return
                    img = QImage(data, w, h, w * 4,
                                 QImage.Format_RGBA8888_Premultiplied)
                    if img.isNull():
                        return
                    # 不做预缩放：实测「绘制时双线性（SmoothPixmapTransform）」比
                    # 「QImage.scaled 面积平均」更锐（拉普拉斯均值 7.98 vs 7.28），
                    # 缩小显示交给 paintEvent 的 drawImage 即可。
                    self._rawBytes = data      # 先保活引用，再交给 QImage
                    self._img = img
                    self._canvas = (int(cw) if cw else w, int(ch) if ch else h)
                    self._crop = (int(ox or 0), int(oy or 0))
                    self._imgRaw = True
                    self._pix = QPixmap()
                    self.update()
                except Exception:
                    pass

            def _canvas_geom(self):
                """(画布宽, 画布高, 裁剪左上 x, 裁剪左上 y)；几何不自洽时退回整幅。"""
                img = self._img
                iw, ih = (img.width(), img.height()) if img is not None else (0, 0)
                cw, ch = self._canvas or (iw, ih)
                ox, oy = self._crop or (0, 0)
                if (cw <= 0 or ch <= 0 or ox < 0 or oy < 0
                        or ox + iw > cw + 1 or oy + ih > ch + 1):
                    return iw, ih, 0, 0
                return cw, ch, ox, oy

            def _dst_rect(self, pw, ph, cw, ch):
                """画布在窗口内的目标矩形：比例接近则铺满，否则等比留白。"""
                if cw > 0 and ch > 0 and abs(cw / ch - pw / ph) < 0.02:
                    return 0.0, 0.0, float(pw), float(ph)
                scale = min(pw / cw, ph / ch)
                w, h = cw * scale, ch * scale
                return (pw - w) / 2.0, (ph - h) / 2.0, w, h

            def canvas_norm(self, pos):
                """窗口内坐标 → 画布归一化坐标 (0~1)。"""
                if self._direct_mode:
                    # 直出模式的画布就是窗口 1:1，没有 Qt 帧缓冲可算几何；
                    # 这里必须直接归一化，否则双击会因 cw/ch=0 被静默丢弃。
                    pw, ph = self.width(), self.height()
                    if pw <= 0 or ph <= 0:
                        return None
                    return pos.x() / float(pw), pos.y() / float(ph)
                pw, ph = self.width(), self.height()
                cw, ch, _ox, _oy = self._canvas_geom()
                dx, dy, dw, dh = self._dst_rect(pw, ph, cw, ch)
                if dw <= 0 or dh <= 0:
                    return None
                return (pos.x() - dx) / dw, (pos.y() - dy) / dh

            def alpha_at_norm(self, nx, ny):
                """画布归一化坐标处的不透明度（0=透明，裁剪区域外也算透明）。"""
                img = self._img
                if img is None or img.isNull():
                    # 直出模式没有 Qt 帧缓冲，用 Electron 回传的 alpha 剪影
                    # 代替；否则双击判定会把整个模型都当成透明而拒绝响应。
                    mask = state.get("alpha_mask")
                    if mask and mask.get("bits"):
                        try:
                            cols = int(mask.get("cols") or 0)
                            rows = int(mask.get("rows") or 0)
                            bits = mask["bits"]
                            if cols > 0 and rows > 0:
                                c = max(0, min(cols - 1, int(nx * cols)))
                                r = max(0, min(rows - 1, int(ny * rows)))
                                return 255 if bits[r][c] == "1" else 0
                        except Exception:
                            pass
                    return 0
                cw, ch, ox, oy = self._canvas_geom()
                iw, ih = img.width(), img.height()
                px = int(nx * cw) - ox
                py = int(ny * ch) - oy
                if px < 0 or py < 0 or px >= iw or py >= ih:
                    return 0
                if self._imgRaw:
                    py = ih - 1 - py      # readPixels 行序自下而上
                return img.pixelColor(px, py).alpha()

            def bounds_rel_y(self, ny):
                """画布归一化 y → 模型可见区域内的相对高度（0=顶 1=底）。

                以当前回传的裁剪框（≈模型实际占用区域）为基准，比"画布比例"
                更贴人物：不同模型在画布里留白多少差很多。
                """
                cw, ch, _ox, oy = self._canvas_geom()
                img = self._img
                ih = img.height() if img is not None else 0
                if ih <= 0 or ch <= 0:
                    return float(ny)
                return max(0.0, min(1.0, (ny * ch - oy) / float(ih)))

            def show_hotspots(self, pts, secs=3.0):
                """在模型上标出双击热区：pts = [(画布归一化 x, y, 序号)]。

                叠在帧上一起画（不额外开窗口），到点自动消失；
                双击命中的那一下只传一个点，就是"这次点到了哪个位置"的反馈。
                """
                self._hotspot_pts = list(pts or [])
                self._hotspot_until = time.monotonic() + float(secs)
                _zdbg("show_hotspots n=%d secs=%.1f vis=%s geom=%s"
                      % (len(self._hotspot_pts), secs, self.isVisible(),
                         (self.width(), self.height())))
                # 圆点和序号画在本窗口上，窗口必须在自己模型之上才看得见：
                # 一显示就立刻补一次 z 序，不等 2~3 秒后的定时器（热区总共只显示 3 秒）。
                try:
                    _pin_qt_topmost()
                except Exception:
                    pass
                self.update()
                QTimer.singleShot(int(secs * 1000) + 60, self.update)

            def _sub_note_ghost(self, r):
                """记下"刚撤掉的那条字幕"占过的屏幕矩形。

                字幕像素是在掩膜还罩着它的时候画进窗口表面的。撤字幕时若同一轮
                里紧跟着就收窄掩膜（生产里 `_sub_housekeeping` 紧跟
                `_apply_interaction_mask` 就是这个顺序），异步 paint 赶到时那块
                已经不在掩膜里，绘制被裁剪 —— 表面里就可能留着字幕像素，日后
                掩膜再罩回来就会"显影"。
                这里不猜什么时候会显影，只记住矩形；真正擦除交给
                `_sub_ghost_sweep`：等掩膜**重新罩住**它的那一帧做一次同步擦除。
                一次性开销，缩放每帧都不多画。
                """
                try:
                    if r is None or r.isEmpty():
                        return
                    self._sub_ghost = QRectF(r)
                    self._sub_ghost_until = time.monotonic() + 6.0
                except Exception:
                    pass

            def _sub_ghost_sweep(self, region):
                """掩膜重新罩住"刚撤掉的字幕"那块时，立刻同步擦一次。

                这是"字幕消失后放大又冒出来"的兜底不变量：
                **任何重新变成可交互的字幕旧址，都必须是重画过的**。
                只在真的罩住时触发（点包含判定），不改变缩放每帧的开销。
                """
                g = getattr(self, "_sub_ghost", None)
                if g is None:
                    return
                if self._sub_text or time.monotonic() > self._sub_ghost_until:
                    self._sub_ghost = None
                    return
                try:
                    _r = g.toRect().adjusted(-6, -6, 6, 6)
                    _pts = (_r.center(),
                            _r.topLeft() + QPoint(3, 3),
                            _r.bottomRight() - QPoint(3, 3))
                    if all(region.contains(_p) for _p in _pts):
                        self._sub_ghost = None
                        _log_line("SUBGHOST", "cover-erase rect=%r"
                                  % (_r.getRect(),))
                        self.repaint(_r)
                except Exception:
                    self._sub_ghost = None

            def _sub_clear(self, repaint=True):
                """把字幕状态一次收干净（文本、到期时间、逐字进度、几何、定时器）。

                只清 `_sub_text` 而把 `_sub_rect` 留着，会让掩膜里残留一条看不见
                的矩形继续吃点击；只清几何而把文本留着，下一次重绘又是一条字幕。
                所以任何"撤掉字幕"的入口都必须走这一个函数。
                """
                self._sub_text = ""
                self._sub_spoken = -1
                self._sub_until = 0.0
                self._sub_rect = QRectF()
                self._sub_geom = None
                try:
                    self._sub_timer.stop()
                except Exception:
                    pass
                # 字幕区域必须**同步**擦掉，不能只 update() 等异步重绘：
                # 下一轮 _poll_cmds 紧接着就会 _apply_interaction_mask() 把掩膜
                # 收窄到模型剪影，而 Qt 的绘制本身就被掩膜裁剪 —— 异步的 paint
                # 赶到时那块已经被裁掉，背面缓冲里就留着"字幕像素"。之后放大
                # 模型、掩膜重新罩到这块区域，系统直接合成旧像素 → 用户看到
                # "字幕消失后又冒出来，缩小又消失"。
                _pr = self._sub_paint_rect
                self._sub_paint_rect = QRectF()
                self._sub_note_ghost(_pr)
                if repaint:
                    _er = None
                    if not _pr.isEmpty() and not (
                            os.environ.get("PET_SUB_NO_ERASE") or "").strip() in ("1", "true", "yes"):
                        _er = _pr.toRect().adjusted(-18, -18, 18, 18)
                    if _er is not None:
                        try:
                            self.repaint(_er)      # 此刻掩膜还罩着它，擦得干净
                            return
                        except Exception:
                            pass
                    self.update()

            def _sub_housekeeping(self):
                """每 10ms 轮询里调一次的自愈检查。

                `_sub_timer` 是单次定时器，主线程被 setMask / 抓帧拖住时可能晚触发；
                反过来，文本已经清掉而几何还在的窗口期也必须收掉。两件事都在这里兜住，
                避免"字幕不消失"和"掩膜里留着字幕残块"。
                """
                if self._sub_text:
                    if time.monotonic() > self._sub_until:
                        self._sub_clear()
                elif not self._sub_rect.isEmpty():
                    self._sub_rect = QRectF()

            def _sub_arm(self):
                """按 _sub_until 起一个单次定时器，到点由 UI 自己清字幕。"""
                if not self._sub_text:
                    self._sub_timer.stop()
                    return
                ms = max(0, int((self._sub_until - time.monotonic()) * 1000)) + 50
                self._sub_timer.start(ms)

            def _sub_expire(self):
                if not self._sub_text:
                    return
                if time.monotonic() >= self._sub_until:
                    self._sub_clear()
                else:
                    self._sub_arm()

            def show_subtitle(self, text, secs=3.6):
                """配音字幕：底部一条半透明黑底白字，到点自动消失。

                text 为空或用户关掉了字幕 → 立即清掉（换动画时不会残留上一条）。
                """
                txt = " ".join(str(text or "").split())
                if not txt or not state.get("subtitles"):
                    self._sub_clear()
                    return
                sec = max(1.2, float(secs or 3.6))
                self._sub_text = txt
                self._sub_ghost = None      # 新字幕要占这块，别再擦它
                self._sub_until = time.monotonic() + sec
                self.update()
                self._sub_arm()

            def subtitle_extend(self, secs):
                """音频时长比预估长 → 把字幕到期时间往后推（跟配音同步消失）。"""
                if not self._sub_text:
                    return
                self._sub_until = max(self._sub_until,
                                     time.monotonic() + max(1.2, float(secs or 0)))
                self.update()
                self._sub_arm()

            def subtitle_hold(self, ms):
                """配音播完后把字幕到期时间收紧到 hold_ms。

                服务层还会发一次清屏；这里兜底，避免字幕时间轴比音频长时
                出现「声音完了、字幕还挂着」。
                """
                if not self._sub_text:
                    return
                sec = max(0.3, float(ms or 0) / 1000.0 - 0.1)
                self._sub_until = time.monotonic() + sec
                self.update()
                self._sub_arm()

            def sub_karaoke(self, text, spoken=0, secs=None):
                """TTS 逐字字幕：全文白字 + 已念出的字变金色。

                spoken = 已念字数（按真实播放样本算出来的，不是定时器估的）。
                secs 为 None 时沿用当前到期时间（同段刷新不清屏）。
                """
                txt = " ".join(str(text or "").split())
                if not txt or not state.get("subtitles"):
                    self._sub_clear()
                    return
                self._sub_text = txt
                self._sub_spoken = int(max(0, min(len(txt), int(spoken or 0))))
                self._sub_ghost = None      # 同上：新字幕要占这块
                if secs is not None:
                    self._sub_until = time.monotonic() + max(0.5, float(secs))
                elif self._sub_until < time.monotonic():
                    self._sub_until = time.monotonic() + 1.0
                self.update()
                self._sub_arm()

            def tts_progress(self, text, ratio=None):
                """合成进度气泡：唱歌这类"整段推理"的合成要十几秒才出第一声，
                先让用户看到"在做什么、到哪了"，而不是以为卡死。

                text 为空 → 收起（合成完 / 出错 / 被 stop 都会调这个）。
                ratio=0~1 画进度条并显示百分比；None 只显示文字（总量未知）。
                """
                txt = " ".join(str(text or "").split())
                try:
                    if not txt:
                        self._bubble.hide_loading()
                        return
                    if ratio is not None:
                        txt = "%s %d%%" % (txt, int(round(max(0.0, min(1.0, float(ratio))) * 100)))
                    self._bubble.show_progress(txt, ratio)
                except Exception:
                    pass

            # 口型/动作同播的挂钩：由 TTS 服务调用，找不到能力时静默跳过，
            # 绝不让配音因为桌宠这边缺实现而失败。
            def tts_play_motion(self, name):
                """点名播一个动作，配音读到时由 TTS 服务调用。

                优先级：内置动画预设（参数关键帧，任何模型都能用）→ 程序化动作序列。
                model3.json 里 Motions=0 的模型（符玄就是）没有 motion 文件，
                所以这里绝不能只依赖模型自带动作，否则 animation 参数等于没生效。
                """
                n = str(name or "").strip()
                if not n:
                    return False
                if n in BUILTIN_ANIM_LABEL:
                    cmd = {"type": "playMotion", "priority": "force",
                           "loop": False, "builtin": n}
                    # ① 命令队列（与 /cmd 同一路径，取的是"当前"渲染页，最可靠）
                    sender = state.get("send_cmd") or getattr(self, "_send_cmd", None)
                    if sender is not None:
                        try:
                            sender(cmd)
                            return True
                        except Exception as e:
                            try:
                                _log_line("WARN", "tts_play_motion 命令队列失败: %r" % (e,))
                            except Exception:
                                pass
                    # ② 直连渲染页兜底。注意 `_cmd_page` 是建窗时抓的一次性引用，
                    # 页面重建后就是旧对象（往它 runJavaScript 不报错、但什么也不会发生），
                    # 所以只能当兜底，不能当主路径。
                    page = getattr(self, "_cmd_page", None)
                    if page is not None:
                        try:
                            page.runJavaScript(
                                "setPetCommand(" + json.dumps(cmd, ensure_ascii=False) + ")")
                            return True
                        except Exception:
                            pass
                    return False
                if not fn:
                    return False
                try:
                    return bool(fn(n))
                except Exception:
                    return False

            def tts_gesture(self, offsets):
                """接收说话微动作的偏移量（度/参数原生单位），立刻重发一次跟随目标。

                只叠加、不直写：鼠标跟随仍是主体，微动作是叠加在目标上的细节，
                所以不会出现"动作把跟随抹掉"或"跟随把动作抹掉"。
                """
                try:
                    state["gesture"] = dict(offsets or {})
                except Exception:
                    state["gesture"] = {}
                send = state.get("send_track")
                if send is not None:
                    try:
                        lx, ly = state.get("track_look") or (0.0, 0.0)
                        send(float(lx), float(ly))
                    except Exception:
                        pass
                return True

            def tts_lip_start(self, pcm, sr):
                """配音开始：开启口型跟随。

                真正的振幅由 TTS 服务的 tick 每 30ms 推进来（tts_lip_level），
                这里只负责打开开关、把嘴先闭上，避免接着上一句的开口状态。
                """
                self._lip_on = True
                self._lip_last = -1.0
                self._lip_n = 0
                try:
                    page = getattr(self, "_cmd_page", None)
                    if page is not None:
                        cmd = {"type": "mouthLock", "on": True,
                               "open": 0.0, "form": 0.0, "hold": False}
                        page.runJavaScript(
                            "setPetCommand(" + json.dumps(cmd) + ")")
                except Exception:
                    pass
                return True

            def tts_lip_level(self, level):
                """把当前人声振幅（0~1）写进 ParamMouthOpenY，驱动 Live2D 嘴型。

                幅度变化小于 0.04 就不推，省掉一半无意义的 IPC；参数 id 用 Cubism 标准名，
                模型没有这个参数时 setParameterValueById 会被安全忽略（不会报错）。
                """
                if not getattr(self, "_lip_on", False):
                    return False
                try:
                    v = max(0.0, min(1.0, float(level)))
                except Exception:
                    return False
                v = round(v, 2)
                if abs(v - float(getattr(self, "_lip_last", -1.0))) < 0.04:
                    return True
                self._lip_last = v
                page = getattr(self, "_cmd_page", None)
                if page is None:
                    return False
                try:
                    cmd = {"type": "mouthLock", "on": True,
                           "open": v, "form": round(-0.15 + 0.45 * v, 2),
                           "hold": False}
                    page.runJavaScript(
                        "setPetCommand(" + json.dumps(cmd, ensure_ascii=False) + ")")
                    if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                        self._lip_n = int(getattr(self, "_lip_n", 0)) + 1
                        if self._lip_n <= 6 or self._lip_n % 20 == 0:
                            print("[lip] #%d y=%.2f" % (self._lip_n, v), flush=True)
                    return True
                except Exception:
                    return False

            def tts_lip_stop(self):
                """配音结束/打断：嘴巴回到闭合；若动作还在继续，把嘴锁在闭合状态。"""
                self._lip_on = False
                self._lip_last = -1.0
                try:
                    page = getattr(self, "_cmd_page", None)
                    if page is not None:
                        cmd = {"type": "mouthLock", "on": True,
                               "open": 0.0, "form": 0.0, "hold": True}
                        page.runJavaScript(
                            "setPetCommand(" + json.dumps(cmd) + ")")
                except Exception:
                    pass
                return True

            def _sub_layout(self):
                """字幕几何的唯一出处（paint 与 setMask 都读它）。

                以前几何只在 paintEvent 里算，`_apply_interaction_mask` 拿到的是
                **上一帧**的 `_sub_rect`：
                  * 字幕刚出现 / 刚被拖走时，掩膜还是旧的 → 条子落在模型剪影
                    之外的部分被 setMask 裁掉（"字幕被剪影"）；
                  * 字幕到期清掉时，掩膜里那条旧矩形还在 → 看不见却继续吃点击。
                现在两边共用这一个纯几何计算（不碰 QPainter），掩膜永远等于
                这一帧要画的字幕；到期/关掉字幕时返回 None，掩膜里也就没有它了。

                返回 None = 现在没有字幕要画。缓存键是（文本, 相对偏移, Stage, 窗口），
                10ms 轮询里重复调用不会重复排版。
                """
                txt = self._sub_text
                if not txt or not state.get("subtitles") or time.monotonic() > self._sub_until:
                    self._sub_geom = None
                    return None
                try:
                    pw = float(self.width())
                    ph = float(self.height())
                    st = tuple(float(v) for v in self.stage_local_rect())
                    rel = self._sub_rel
                    key = (txt,
                           (round(float(rel[0]), 5), round(float(rel[1]), 5)) if rel else None,
                           round(st[0], 1), round(st[1], 1),
                           round(st[2], 1), round(st[3], 1), pw, ph)
                    cached = getattr(self, "_sub_geom", None)
                    if cached and cached.get("key") == key:
                        return cached
                    f = QFont()
                    # 字号随模型大小轻微缩放（限幅 0.85~1.35 倍）：字幕是"模型的字幕"，
                    # 大模型配大字，但模型缩到 0.3 倍时也不能小到看不清。
                    try:
                        _fb = float(st[3]) / 430.0
                    except Exception:
                        _fb = 1.0
                    f.setPointSizeF(11.0 * max(0.85, min(1.35, _fb)))
                    fm = QFontMetrics(f, self)
                    maxw = max(160.0, min(st[2] * 0.92, pw - 24.0))
                    lines = _wrap_cjk(txt, fm, maxw - 26.0)
                    if not lines:
                        self._sub_geom = None
                        return None
                    lineh = fm.height()
                    tw = max(fm.horizontalAdvance(l) for l in lines)
                    bw = max(96.0, min(maxw, tw + 26.0))
                    bw = min(bw, max(64.0, pw - 8.0))
                    bh = len(lines) * lineh + 15.0
                    if rel:
                        # 相对 Stage 中心的归一化偏移：模型平移/缩放时字幕整体跟着走，
                        # 始终维持用户拖出来的那个相对关系（超出屏幕再夹回来）。
                        _u = float(rel[0])
                        _v = float(rel[1])
                        cx = st[0] + st[2] * (0.5 + _u)
                        cy = st[1] + st[3] * (0.5 + _v)
                        x = max(4.0, min(pw - bw - 4.0, cx - bw * 0.5))
                        y = max(4.0, min(ph - bh - 4.0, cy - bh * 0.5))
                    else:
                        x = max(4.0, min(pw - bw - 4.0,
                                        st[0] + st[2] * 0.5 - bw * 0.5))
                        y = max(4.0, min(ph - bh - 4.0,
                                        st[1] + st[3] - bh - 32.0))
                    self._sub_geom = {"key": key, "rect": QRectF(x, y, bw, bh),
                                      "lines": lines, "font": f, "fm": fm,
                                      "lineh": lineh}
                    return self._sub_geom
                except Exception:
                    self._sub_geom = None
                    return None

            def _paint_subtitle(self, qp, dx, dy, dw, dh):
                """画底部字幕（在帧之后、正常坐标系里画，不受镜像变换影响）。"""
                lay = self._sub_layout()
                if lay is None:
                    self._sub_note_ghost(self._sub_paint_rect)
                    self._sub_rect = QRectF()
                    self._sub_paint_rect = QRectF()
                    return
                try:
                    _r = lay["rect"]
                    x, y, bw, bh = _r.x(), _r.y(), _r.width(), _r.height()
                    lines, fm, lineh = lay["lines"], lay["fm"], lay["lineh"]
                    self._sub_rect = _r
                    self._sub_paint_rect = QRectF(_r)
                    # 诊断：setMask 会把整窗（含字幕）一起裁掉，所以"字幕条四角
                    # 是否都在当前 mask 里"决定了它会不会被剪掉半截。
                    # PET_SUBDBG=1 时每帧记一行，用来确认掩膜有没有跟上字幕位置。
                    if os.environ.get("PET_SUBDBG"):
                        try:
                            _m = self.mask()
                            _pts = [(x + 2, y + bh / 2.0), (x + bw - 3, y + bh / 2.0),
                                    (x + bw / 2.0, y + 2), (x + bw / 2.0, y + bh - 2)]
                            _miss = sum(0 if _m.contains(QPoint(int(_px), int(_py))) else 1
                                        for _px, _py in _pts)
                            _log_line("SUBDBG", "paint sub=[%.0f,%.0f,%.0f,%.0f] "
                                      "mask_rects=%d miss_corners=%d"
                                      % (x, y, bw, bh,
                                         len(_m.rects()) if hasattr(_m, "rects") else -1,
                                         _miss))
                        except Exception:
                            pass
                    qp.setFont(lay["font"])
                    qp.setPen(Qt.NoPen)
                    qp.setBrush(QBrush(QColor(16, 16, 20, 176)))
                    qp.drawRoundedRect(_r, 8.0, 8.0)
                    qp.setPen(QPen(QColor(255, 255, 255, 246)))
                    ty = y + 7.0 + fm.ascent()
                    # 逐字高亮：整句先画白，再把「已念出的前 k 个字」用金色叠一遍，
                    # 用 clipRect 精确裁到第 k 个字右边界，避免半个字被染色。
                    spoken = int(getattr(self, "_sub_spoken", -1))
                    off = 0
                    for l in lines:
                        lw = fm.horizontalAdvance(l)
                        tx = x + (bw - lw) * 0.5
                        qp.drawText(QPointF(tx, ty), l)
                        if spoken > off and l:
                            k = min(len(l), spoken - off)
                            if k > 0:
                                gw = fm.horizontalAdvance(l[:k])
                                qp.save()
                                qp.setClipRect(QRectF(tx - 1.0, y, gw + 2.0, bh))
                                qp.setPen(QPen(QColor(255, 210, 74, 252)))
                                qp.drawText(QPointF(tx, ty), l)
                                qp.restore()
                                qp.setPen(QPen(QColor(255, 255, 255, 246)))
                        off += len(l)
                        ty += lineh
                except Exception:
                    pass


            def resizeEvent(self, e):
                """缩放后立即用当前帧重绘，避免留白/闪动。"""
                super().resizeEvent(e)
                self._bubble.reposition()
                try:
                    _reposition_part_ui()
                except Exception:
                    pass
                if self.on_geometry_change:
                    self.on_geometry_change()
                self._apply_interaction_mask()
                self.update()

            def moveEvent(self, e):
                super().moveEvent(e)
                if self.on_geometry_change:
                    self.on_geometry_change()

            def showEvent(self, e):
                super().showEvent(e)
                if self.on_visibility_change:
                    self.on_visibility_change(True)

            def hideEvent(self, e):
                super().hideEvent(e)
                # 窗口被收起来（右键「隐藏」/ agent 发 hide 命令/关闭）时，
                # 右键菜单必须一起关 —— 否则会留一个没有归属的弹层在屏幕上，
                # 看上去就是这个插件"卡住了"。
                if self._menu is not None:
                    try:
                        if self._menu.isVisible():
                            self._menu.close()
                    except RuntimeError:
                        pass
                if self.on_visibility_change:
                    self.on_visibility_change(False)

            def show_loading(self, text="加载中"):
                self._bubble.show_loading(text)

            def hide_loading(self):
                self._bubble.hide_loading()

            def paintEvent(self, e):
                if self._shutting_down:
                    return
                img = self._img
                if not self._direct_mode and (img is None or img.isNull()):
                    return
                try:
                    qp = QPainter(self)
                    pw, ph = self.width(), self.height()
                    if self._direct_mode:
                        # 模型由下面的透明 QWebEngineView 直接上屏；
                        # 本窗口只画字幕/热区等覆盖层，不做任何图像缩放。
                        # 关键：给交互覆盖层一个 alpha=1 的极淡底色。
                        # Windows 对分层窗口的“全透明区域”会做透明命中测试，
                        # 整窗 alpha=0 时鼠标会直接穿过 Qt 覆盖层落到桌面，
                        # 表现为模型点不动、拖不走。alpha=1 肉眼不可见但能接住鼠标。
                        qp.fillRect(self.rect(), QColor(0, 0, 0, 1))
                        dx, dy, dw, dh = 0.0, 0.0, float(pw), float(ph)
                    else:
                        iw, ih = img.width(), img.height()
                        if iw <= 0 or ih <= 0:
                            qp.end()
                            return
                        qp.setRenderHint(QPainter.SmoothPixmapTransform, True)
                        cw, ch, ox, oy = self._canvas_geom()
                        # 画布整体在窗口里的位置，再按比例把裁剪区域映射进去
                        dx, dy, dw, dh = self._dst_rect(pw, ph, cw, ch)
                        sx = dx + dw * ox / float(cw)
                        sy = dy + dh * oy / float(ch)
                        sw = dw * iw / float(cw)
                        sh = dh * ih / float(ch)
                        qp.save()
                        if self._imgRaw:
                            # readPixels 行序自下而上 → 垂直镜像绘制（负 stride QImage 会崩）
                            qp.translate(0, ph)
                            qp.scale(1, -1)
                            qp.drawImage(QRectF(sx, ph - sy - sh, sw, sh), img,
                                         QRectF(0, 0, iw, ih))
                        elif not self._pix.isNull():
                            qp.drawPixmap(QRectF(sx, sy, sw, sh), self._pix,
                                          QRectF(0, 0, iw, ih))
                        else:
                            qp.drawImage(QRectF(sx, sy, sw, sh), img,
                                         QRectF(0, 0, iw, ih))
                        qp.restore()
                    # 热区提示画在正常坐标系上（上面的镜像变换已 restore）
                    if self._hotspot_pts and time.monotonic() < self._hotspot_until:
                        _zdbg("paint_hotspots n=%d dx=%.0f dy=%.0f dw=%.0f dh=%.0f"
                              % (len(self._hotspot_pts), dx, dy, dw, dh))
                        self._paint_hotspots(qp, dx, dy, dw, dh)
                    # 配音字幕压在最上层（底部），不随帧裁剪区域移动
                    self._paint_subtitle(qp, dx, dy, dw, dh)
                    qp.end()
                except RuntimeError:
                    pass  # 底层 C++ 对象已销毁（如关闭过程中），忽略

            def _apply_interaction_mask(self):
                """把 Qt 交互覆盖层的鼠标命中区裁到模型**可见剪影**上。

                直出模式下覆盖层整窗 alpha=1 才能接住鼠标，但模型轮廓外的
                空白（包括包围盒内部的透明区域）也会挡住桌面点击。
                优先用 Electron 一帧 alpha 降采样成的网格做 setMask；
                模型未就绪/采样失败时退回包围盒，保证至少能点。
                """
                if not self._direct_mode:
                    return False
                # 交互护栏：模型没加载完就不该有"模型命中区"。这时剪影还没抓到，
                # 一旦退回"包围盒"（甚至整窗），整块屏幕都会把桌面输入吃掉
                # —— 表现就是在任何地方拖一下都会挪动还没画出来的模型。
                # 但 setMask 会连子控件一起裁掉，所以只把**加载气泡**那一块留下：
                # 其它区域一律不可交互，气泡照样看得见。
                if not state.get("ready"):
                    region = QRegion()
                    try:
                        _bw = getattr(self, "_bubble", None)
                        if _bw is not None and _bw.isVisible():
                            region = QRegion(_bw.geometry().adjusted(-10, -10, 10, 10)
                                             .intersected(self.rect()))
                    except Exception:
                        region = QRegion()
                    if region.isEmpty():
                        region = QRegion(0, 0, 1, 1)
                    key = ("loading", tuple(region.boundingRect().getRect()))
                    if key != self._mask_key:
                        self.setMask(region)
                        self._mask_key = key
                    return False
                pw, ph = self.width(), self.height()
                # 抓帧几何 → 当前几何的换算基准：**以 Stage 中心为锚、按 Stage 高度
                # 等比缩放**（两轴共用一个比例 k）。
                #
                # 直出模式里 applyFit 只用 Stage 高度决定模型缩放
                # （fitScale = stage.h / baseH * 0.95），模型画在 Stage 中心；
                # Stage 宽度只决定左右留白，而且会随模型宽高比变（换模型/补宽缓动
                # 时"高度不变、宽度变了"很常见）。旧实现按 Stage 左上角为锚、
                # 横向用 cur_w/cap_w、纵向用 cur_h/cap_h 各算一个比例：一旦这两个
                # 比例不相等，整块剪影就被横竖不等地拉一遍，模型上大片看得见的
                # 部位落不进命中区 —— 用户看到的就是"一些可见部分点下去穿透到
                # 下面的窗口"。中心锚 + 单一 k 才和渲染层的实际布局一致。
                _st_now = pet.stage_rect()

                def _stage_xform(ref):
                    """返回 (k, 抓帧中心x, 抓帧中心y, 当前中心x, 当前中心y)；坏几何给 None。"""
                    try:
                        if not ref or len(list(ref)) < 4:
                            return None
                        rx, ry, rw_, rh_ = [float(v) for v in list(ref)[:4]]
                        if rw_ < 2 or rh_ < 2:
                            return None
                        k = float(_st_now[3]) / rh_
                        if not (0.02 < k < 50.0):
                            return None
                        return (k, rx + rw_ * 0.5, ry + rh_ * 0.5,
                                float(_st_now[0]) + float(_st_now[2]) * 0.5,
                                float(_st_now[1]) + float(_st_now[3]) * 0.5)
                    except (TypeError, ValueError, IndexError):
                        return None

                mask = state.get("alpha_mask")
                # 字幕几何必须在这里现算（不依赖上一帧的 _sub_rect）：
                # 掩膜 = 模型剪影 ∪ 字幕条，算早算晚差一帧就会把字幕裁掉/留下残块。
                sub_rect = None
                _lay = self._sub_layout()
                if _lay is not None:
                    self._sub_rect = QRectF(_lay["rect"])
                    # 掩膜必须同时罩住"这一帧要画的位置"和"上一帧已经画在屏幕上的位置"：
                    # 缩放/拖动时 Stage 每帧都在变，只罩新位置的话，上一帧那条还没
                    # 擦掉的字会被新掩膜裁掉半截（肉眼看就是字幕被剪影咬边）。
                    # 外扩 16px 再吸收一般的每帧位移。
                    sub_rect = _lay["rect"].toRect().adjusted(-16, -16, 16, 16)
                    _pr = getattr(self, "_sub_paint_rect", None)
                    if _pr is not None and not _pr.isEmpty():
                        sub_rect = sub_rect.united(_pr.toRect().adjusted(-16, -16, 16, 16))
                elif not self._sub_rect.isEmpty():
                    # 字已经没了：立刻把残块从掩膜里撤掉（窗口形状跟着缩小，
                    # 屏幕上的旧像素当帧就消失），也别让它继续吃点击。
                    # 但只撤掩膜不够：背面缓冲里那块字幕像素还在（绘制一直被
                    # 掩膜裁着，从没被重绘成透明），以后掩膜再罩回来就会"显影"
                    # —— 就是"字幕消失后放大又冒出来"。这里先同步擦一遍。
                    _pr = getattr(self, "_sub_paint_rect", None)
                    self._sub_note_ghost(_pr)
                    self._sub_rect = QRectF()
                    self._sub_paint_rect = QRectF()
                    if _pr is not None and not _pr.isEmpty() and not (
                            os.environ.get("PET_SUB_NO_ERASE") or "").strip() in ("1", "true", "yes"):
                        try:
                            self.repaint(_pr.toRect().adjusted(-18, -18, 18, 18))
                        except Exception:
                            pass
                # 部件隐藏模式的提示条/确认/取消是 Qt 子控件；剪影 mask 会把
                # 模型外的子控件一起裁掉，必须把它们并进命中区才会显示和可点。
                ui_rects = []
                if state.get("part_mode"):
                    for _w in (part_hint, part_ok, part_cancel):
                        try:
                            if _w.isVisible():
                                rr = _w.geometry().intersected(self.rect())
                                if not rr.isEmpty():
                                    ui_rects.append(tuple(rr.getRect()))
                        except Exception:
                            pass
                # 加载/切换模型的气泡、双击部位提示同样是 Qt 子控件，而且都贴在
                # 模型头顶**剪影之外**：不并进命中区就会被 setMask 裁成"只剩压在
                # 头发上的那一小块"，用户看到的就是"消息提醒显示不完全"。
                for _w in (getattr(self, "_bubble", None),
                           getattr(self, "_hint_widget", None)):
                    try:
                        if _w is not None and _w.isVisible():
                            # 气泡会上下浮动 ±2px、提示条会跟着鼠标走；这里外扩 4px
                            # 并直接并进命中区，避免每帧位移都触发一次 setMask 重建。
                            rr = _w.geometry().adjusted(-4, -4, 4, 4).intersected(self.rect())
                            if not rr.isEmpty():
                                ui_rects.append(tuple(rr.getRect()))
                    except Exception:
                        pass
                ui_key = tuple(ui_rects) if ui_rects else None
                # 双击热区点本身也并进命中区：alpha 剪影是粗网格，热区若
                # 恰好落在网格边界，点击会被裁掉；让它至少能接到这一小圈。
                # 范围要给"显示双击热区"的小圆点+序号留足（半径 7.5 的圆
                # 加两位数字，±10 起才不会被 setMask 裁成半个），否则圆点
                # 只在模型剪影里露出半截，像是被模型盖住了。
                seed_rects = []
                # 热区种子同样要按 Stage 的平移/缩放搬到现在的位置：它的 nx/ny 是
                # "抓帧那一刻"的画布归一化坐标，不搬的话拖完/缩完这些 30×26 的
                # 小方块会留在原地，模型身上反而没有双击热区，桌面上却多出几个
                # 点不透的洞。
                _rsx = _rsy = 1.0
                _rcsx = _rcsy = 0.0
                _rstx = _rsty = 0.0
                try:
                    _xf_s = _stage_xform(state.get("_cm_stage"))
                    if _xf_s:
                        _rsx = _rsy = _xf_s[0]
                        _rcsx, _rcsy = _xf_s[1], _xf_s[2]
                        _rstx, _rsty = _xf_s[3], _xf_s[4]
                except Exception:
                    _rsx = _rsy = 1.0
                if state.get("dblclick", True):
                    for _s in ((state.get("click_map") or {}).get("seeds") or []):
                        try:
                            sx = int(round(_rstx + (float(_s["nx"]) * pw - _rcsx) * _rsx))
                            sy = int(round(_rsty + (float(_s["ny"]) * ph - _rcsy) * _rsy))
                            seed_rects.append((sx - 15, sy - 13, 30, 26))
                        except Exception:
                            pass
                seed_key = tuple(seed_rects) if seed_rects else None
                # 拖动中：精确剪影要逐格拼上千个矩形（实测 drag 期间 build
                # 465~545ms/s，占掉 Qt 主线程一半以上，鼠标事件被压后合并 ——
                # 90 步只跟到 123px / 期望 180px）。拖动期间退化成"模型可见
                # 包围盒"一条粗掩膜：照样能接住整只模型的点击，掩膜重建≈0.1ms。
                # 松手后 220ms 的 _refresh_hit_geometry 会把精确剪影补回来。
                if getattr(self, "_dragging", False):
                    _nm = state.get("model_rect_norm")
                    if _nm and len(_nm) == 4:
                        _nx, _ny, _nw, _nh = [float(v) for v in _nm]
                        left = max(0, int(_nx * pw) - 8)
                        top = max(0, int(_ny * ph) - 8)
                        right = min(pw, int((_nx + _nw) * pw) + 8)
                        bottom = min(ph, int((_ny + _nh) * ph) + 8)
                        if right - left < 1:
                            right = left + 1
                        if bottom - top < 1:
                            bottom = top + 1
                        region = QRegion(left, top, right - left, bottom - top)
                        if sub_rect is not None:
                            region = region.united(QRegion(sub_rect))
                        for _r in ui_rects:
                            region = region.united(QRegion(*_r))
                        for _r in seed_rects:
                            region = region.united(QRegion(*_r))
                        key = ("drag", left, top, right - left, bottom - top,
                               tuple(sub_rect.getRect()) if sub_rect else None,
                               ui_key, seed_key)
                        if _PERF["on"]:
                            _PERF["mask_n"] += 1
                            _PERF["mask_coarse_n"] += 1
                            _PERF["mask_build_ms"] += (time.monotonic() - _pt0) * 1000.0
                            _pt0 = time.monotonic()
                        if key != self._mask_key:
                            self.setMask(region)
                            self._mask_key = key
                            self._sub_ghost_sweep(region)
                        if _PERF["on"]:
                            _PERF["mask_set_ms"] += (time.monotonic() - _pt0) * 1000.0
                        return True
                # 命中区一律优先用**双击热区那套前排格子**（角色本体剪影），
                # 两级退路才是 alpha 网格 / 包围盒：
                # 1) 全屏可见模式下 Stage 就是整块屏幕，用包围盒等于把桌面和
                #    其他模型的点击一起吃掉 —— 多模型并存时别的模型就"拖不动"；
                # 2) local 模式的包围盒是一整块矩形（模型透明边也被算成命中区），
                #    两个模型贴在一起时，上面那块矩形会把下面模型的身上的点击
                #    一起吃掉。格子是逐格筛过的角色本体，不会误伤邻居。
                # 格子列表、行列与 band 由 _build_click_map 直接存在 state 私有键里：
                # click_map 里对外只暴露"格子数量"，铺 mask 需要逐格坐标。
                cells = state.get("_cm_cells") or []
                cm = state.get("click_map") or {}
                band = state.get("_cm_band") or None
                cols = int(state.get("_cm_cols") or 0)
                rows = int(state.get("_cm_rows") or 0)
                cell_nw = cell_nh = 0.0
                try:
                    if band and cols > 0 and rows > 0:
                        cell_nw = max(0.0, float(band[2]) - float(band[0])) / cols
                        cell_nh = max(0.0, float(band[3]) - float(band[1])) / rows
                except (TypeError, ValueError, IndexError):
                    cell_nw = cell_nh = 0.0
                full_mode = str(state.get("display_mode") or "local") == "full"
                # 格子是按"发布那一刻的 Stage 几何"算出来的；而 Stage 会被拖动（平移）
                # 和滚轮缩放（缩放）改掉，换模型时宽度还会按新模型的宽高比重算
                # （高度不变），热区重算是防抖后才跑的。
                # 这里必须按 Stage 的**平移 + 缩放**一起把格子映射过去：
                # 只做平移时，滚轮放大之后模型长出来了、命中区还停在旧尺寸，
                # 光标落在放大后新长出的地方就会被判成"在模型外" —— 表现是
                # 缩放后点不动/拖不动，而且模型上的滚轮会穿透到下面的窗口。
                # 换算基准由 _stage_xform 统一给出（中心锚 + 按高度等比）。
                seq = int(state.get("_cm_seq") or 0)
                _xf = _stage_xform(state.get("_cm_stage"))
                if isinstance(cells, list) and len(cells) >= 8 and cell_nw > 0 and cell_nh > 0:
                    mask = {"cells": cells, "cell_nw": cell_nw, "cell_nh": cell_nh,
                            "ver": seq, "xf": _xf}
                    norm = None
                elif full_mode:
                    # 还没建出热区地图（模型刚加载）：退回 Stage 包围盒，
                    # 至少模型能点、能拖。
                    _sx, _sy, _sw, _sh = pet.stage_local_rect()
                    mask = None
                    norm = (_sx / float(pw), _sy / float(ph),
                            _sw / float(pw), _sh / float(ph))
                else:
                    mask = state.get("alpha_mask")
                    norm = state.get("model_rect_norm")
                # 掩膜 key 里的"包围盒"分量：拖动/换位时 model_rect_norm 是
                # 异步刷新的，只有把它算进 key，刷新回来的那一拍才会真正重建掩膜。
                # 全屏可见模式下它同时决定 alpha 的裁框，过期框会把剪影裁空。
                try:
                    _nm_key = (full_mode,) + tuple(
                        round(float(v), 4) for v in (state.get("model_rect_norm") or ()))
                except (TypeError, ValueError):
                    _nm_key = (full_mode,)
                if mask and mask.get("cells"):
                    _xf_k = tuple(round(float(v), 4) for v in (_xf or ()))
                    # 包围盒也要进 key：模型没换、只是拖动/换位时几何量里唯一
                    # 会被异步刷新的就是它；不进 key 就会出现"先用旧框裁、
                    # 新框回来也不重建"，错误掩膜一直挂着（可见部位点不动）。
                    key = ("cells", mask["ver"], _xf_k,
                           round(mask["cell_nw"], 5),
                           round(mask["cell_nh"], 5),
                           state.get("alpha_mask_ver") or 0,
                           _nm_key,
                           pw, ph,
                           tuple(sub_rect.getRect()) if sub_rect else None,
                           ui_key, seed_key)
                elif mask and mask.get("bits"):
                    key = ("alpha", state.get("alpha_mask_ver") or 0,
                           _nm_key,
                           pw, ph, tuple(sub_rect.getRect()) if sub_rect else None,
                           ui_key, seed_key)
                else:
                    if not norm:
                        if self._mask_key is not None:
                            self.clearMask()
                            self._mask_key = None
                        return False
                    nx, ny, nw, nh = [float(v) for v in norm]
                    left = max(0, min(pw, int(round(nx * pw)) - 2))
                    top = max(0, min(ph, int(round(ny * ph)) - 2))
                    right = max(left + 1, min(pw, int(round((nx + nw) * pw)) + 2))
                    bottom = max(top + 1, min(ph, int(round((ny + nh) * ph)) + 2))
                    key = ("bbox", left, top, right - left, bottom - top,
                           tuple(sub_rect.getRect()) if sub_rect else None,
                           ui_key, seed_key)
                if key == self._mask_key:
                    return False
                _pt0 = time.monotonic()
                try:
                    region = QRegion()
                    if mask and mask.get("cells"):
                        # 前排格子 → 一格一块命中区（角色本体剪影）。
                        # 格子中心与格宽都是**画布归一化**值（渲染层 canvas 就是
                        # 整块屏幕）。格子是在"发布那一刻的 Stage"里量出来的，
                        # 这里按 _stage_xform 给出的基准（Stage 中心 + 高度等比 k）
                        # 把它搬到现在的位置：
                        #   屏幕x = 当前Stage中心x + (格子屏幕x - 抓帧Stage中心x) * k
                        # 每格外扩 1px，避免格与格之间出现 1px 的穿透缝
                        # （模型身上出现"点不到的小洞"）。
                        _xf_m = mask.get("xf") or (1.0, 0.0, 0.0, 0.0, 0.0)
                        try:
                            _k, _c0x, _c0y, _c1x, _c1y = [float(v) for v in list(_xf_m)[:5]]
                        except (TypeError, ValueError):
                            _k, _c0x, _c0y, _c1x, _c1y = 1.0, 0.0, 0.0, 0.0, 0.0
                        hw = max(2.0, mask["cell_nw"] * pw * _k / 2.0 + 1.0)
                        hh = max(2.0, mask["cell_nh"] * ph * _k / 2.0 + 1.0)
                        for _c in mask["cells"]:
                            try:
                                cxx = _c1x + ((float(_c["x"]) * pw) - _c0x) * _k
                                cyy = _c1y + ((float(_c["y"]) * ph) - _c0y) * _k
                            except Exception:
                                continue
                            region = region.united(
                                QRegion(int(round(cxx - hw)), int(round(cyy - hh)),
                                        max(1, int(round(hw * 2))),
                                        max(1, int(round(hh * 2)))))
                    elif mask and mask.get("bits"):
                        # 只有 alpha、还没有格子（模型刚就绪的那一小段）：
                        # 同样按抓帧时的 Stage 搬到现在，不能当"整屏绝对坐标"用。
                        cols = int(mask.get("cols") or 0)
                        rows = int(mask.get("rows") or 0)
                        bits = mask["bits"]
                        if cols <= 0 or rows <= 0:
                            return False
                        _xf_a = _stage_xform(mask.get("stage"))
                        _ak, _a0x, _a0y, _a1x, _a1y = (_xf_a if _xf_a
                                                       else (1.0, 0.0, 0.0, 0.0, 0.0))
                        for r, line in enumerate(bits[:rows]):
                            _ry0 = float(r) * ph / float(rows)
                            _ry1 = float(r + 1) * ph / float(rows)
                            y0 = int(round(_a1y + (_ry0 - _a0y) * _ak)) - 1
                            y1 = int(round(_a1y + (_ry1 - _a0y) * _ak)) + 1
                            if y1 <= y0:
                                continue
                            c = 0
                            while c < cols and c < len(line):
                                if line[c] != "1":
                                    c += 1
                                    continue
                                start = c
                                while c < cols and c < len(line) and line[c] == "1":
                                    c += 1
                                x0 = int(round(start * pw / float(cols)))
                                x1 = int(round(c * pw / float(cols)))
                                if x1 > x0:
                                    region = region.united(
                                        QRegion(x0, y0, x1 - x0, y1 - y0))
                    elif norm:
                        # 没有 alpha 网格时直接用当前命中包围盒：
                        # full 模式这里是 Stage，local 模式这里是模型可见包围盒。
                        # 不能等 region 为空后再回退到 model_rect_norm，否则
                        # full 模式的 Stage 命中区会被模型包围盒覆盖。
                        nx, ny, nw, nh = [float(v) for v in norm]
                        left = max(0, min(pw, int(round(nx * pw)) - 2))
                        top = max(0, min(ph, int(round(ny * ph)) - 2))
                        right = max(left + 1, min(pw, int(round((nx + nw) * pw)) + 2))
                        bottom = max(top + 1, min(ph, int(round((ny + nh) * ph)) + 2))
                        region = QRegion(left, top, right - left, bottom - top)
                    # alpha 位图（比格子细一个数量级）一并并进来。
                    # 只用格子时命中区会漏掉模型边缘和下半身：格子是"前排格子"
                    # 按格内平均 alpha 筛出来的，裙摆/腿这种细条经常整格被判成透明，
                    # 结果模型上看得到的地方一点下去就穿透到下面的窗口。
                    #
                    # 但只能并"角色附近那一圈"：全屏可见模式下 alpha 网格里还有
                    # 背景/布景，整幅并进来等于把整块屏幕做成命中区 —— 邻居模型的
                    # 点击会被这个窗口抢走（多模型矩阵回归会直接全挂）。
                    _am = state.get("alpha_mask") or {}
                    if _am.get("bits"):
                        try:
                            # 剪影必须是**这一个模型**的：换模型后旧剪影是另一只
                            # 模型的形状，并进来等于把命中区裁成别人的轮廓。
                            _am_model = str(_am.get("model") or "")
                            _cur_mu = str(state.get("model_url") or "")
                            if _am_model and _cur_mu and _am_model != _cur_mu:
                                _am = {}
                            _ar = self._region_from_alpha(_am, pw, ph)
                            if mask and mask.get("cells") and not region.isEmpty():
                                # 并入的 alpha 只取"模型看得见的那一块"：全屏可见模式
                                # 下网格里可能还有布景/特效，整幅并进来会把桌面也
                                # 变成可点区域。
                                #
                                # **local 模式不裁**：alpha 网格在抓帧时就按 Stage
                                # （= 模型的窗口）圈过 band，外面一律是 0，本来就没有
                                # 布景可混进来。裁框反而带来一个致命副作用（实测复现）：
                                # 拖动/换位后 model_rect_norm 还没刷新，裁框停在**旧位置**，
                                 # 缩放后的剪影跟它没有交集 → 整块 alpha 被裁光，掩膜只剩
                                # 那圈"前排格子"（≈剪影一半），模型上大片看得见的部位
                                # 一点就穿透；而 _mask_key 里又没有包围盒，等包围盒刷新
                                # 回来也不会重建，错误掩膜就那么一直挂着，直到下一次缩放
                                # 才意外恢复。
                                _rb = None
                                _norm = state.get("model_rect_norm")
                                if full_mode and _norm and len(_norm) == 4:
                                    # 全屏可见模式才需要"别把桌面并进来"的裁框；
                                    # 而且必须确认这份包围盒是**当前 Stage** 量的，
                                    # 过期就宁可先不裁，也不能拿旧框把剪影裁没。
                                    _fresh = True
                                    try:
                                        _org = state.get("model_rect_stage")
                                        if _org and (abs(int(_org[0]) - int(_st_now[0])) > 8
                                                     or abs(int(_org[1]) - int(_st_now[1])) > 8):
                                            _fresh = False
                                    except (TypeError, ValueError, IndexError):
                                        _fresh = True
                                    if _fresh:
                                        try:
                                            _nx, _ny, _nw, _nh = [float(v) for v in _norm]
                                            _rb = QRegion(
                                                int(round(_nx * pw)) - 20,
                                                int(round(_ny * ph)) - 20,
                                                max(1, int(round(_nw * pw)) + 40),
                                                max(1, int(round(_nh * ph)) + 40))
                                        except (TypeError, ValueError):
                                            _rb = None
                                if _rb is not None:
                                    _ar = _ar.intersected(_rb)
                            region = region.united(_ar)
                        except Exception:
                            pass
                    if region.isEmpty():
                        # 剪影全透明说明采样异常，退回包围盒，避免整窗点不动。
                        norm = state.get("model_rect_norm")
                        if not norm:
                            return False
                        nx, ny, nw, nh = [float(v) for v in norm]
                        left = max(0, min(pw, int(round(nx * pw)) - 2))
                        top = max(0, min(ph, int(round(ny * ph)) - 2))
                        right = max(left + 1, min(pw, int(round((nx + nw) * pw)) + 2))
                        bottom = max(top + 1, min(ph, int(round((ny + nh) * ph)) + 2))
                        region = QRegion(left, top, right - left, bottom - top)
                    if sub_rect is not None:
                        region = region.united(QRegion(sub_rect))
                    for _r in ui_rects:
                        region = region.united(QRegion(*_r))
                    for _r in seed_rects:
                        region = region.united(QRegion(*_r))
                    if _PERF["on"]:
                        try:
                            _pn = len(region.rects())
                        except Exception:
                            _pn = 0
                        _PERF["mask_n"] += 1
                        _PERF["mask_build_ms"] += (time.monotonic() - _pt0) * 1000.0
                        if _pn > _PERF["mask_rects_max"]:
                            _PERF["mask_rects_max"] = _pn
                        _pt0 = time.monotonic()
                    self.setMask(region)
                    if _PERF["on"]:
                        _PERF["mask_set_ms"] += (time.monotonic() - _pt0) * 1000.0
                    self._mask_key = key
                    # 掩膜要是重新罩住了"刚撤掉的字幕"旧址，就在这一帧同步擦干净
                    # —— 否则那里可能是没被重画过的旧像素，一罩就"显影"。
                    self._sub_ghost_sweep(region)
                    return True
                except Exception:
                    return False

            def _region_from_alpha(self, am, pw, ph):
                """alpha 位图 → 命中区（按行做 run-length，避免上千个矩形）。

                网格列/行是**整块画布归一化**（渲染层 canvas 就是整块屏幕），
                但打点是"抓帧那一刻的 Stage"里量出来的，所以这里和 cells 用
                同一套映射（见 _apply_interaction_mask 里的 _stage_xform）：
                    k    = 当前Stage高 / 抓帧时Stage高
                    屏幕x = 当前Stage中心x + (网格屏幕x - 抓帧Stage中心x) * k
                两轴共用一个 k、以 Stage 中心为锚 —— 渲染层 applyFit 就是"按
                Stage 高度定缩放、把模型画在 Stage 中心"，Stage 宽度只是左右留白。
                旧实现按左上角为锚、横纵各用一个宽高比：换模型/补宽时宽度和高度
                的比例不同，剪影就被横竖不等地拉一遍，命中区不再贴着模型 ——
                那正是"模型有时被鼠标穿透"的来源。
                """
                region = QRegion()
                try:
                    cols = int(am.get("cols") or 0)
                    rows = int(am.get("rows") or 0)
                    bits = am.get("bits") or []
                    if cols <= 0 or rows <= 0 or not bits:
                        return region
                    a_st = am.get("stage") or state.get("stage_rect") or None
                    _st = self.stage_rect()
                    _kr = 1.0
                    _c0x = _c0y = _c1x = _c1y = 0.0
                    if a_st and len(list(a_st)) >= 4:
                        try:
                            _rx, _ry, _rw, _rh = [float(v) for v in list(a_st)[:4]]
                            if _rw >= 2 and _rh >= 2 and float(_st[3]) >= 2:
                                _kr = float(_st[3]) / _rh
                                _c0x, _c0y = _rx + _rw * 0.5, _ry + _rh * 0.5
                                _c1x = float(_st[0]) + float(_st[2]) * 0.5
                                _c1y = float(_st[1]) + float(_st[3]) * 0.5
                        except (TypeError, ValueError, IndexError):
                            _kr = 1.0
                            _c0x = _c0y = _c1x = _c1y = 0.0
                    for r, line in enumerate(bits[:rows]):
                        cy0 = r * ph / float(rows)
                        cy1 = (r + 1) * ph / float(rows)
                        y0 = int(round(_c1y + (cy0 - _c0y) * _kr))
                        y1 = int(round(_c1y + (cy1 - _c0y) * _kr))
                        if y1 <= y0:
                            continue
                        c = 0
                        while c < cols and c < len(line):
                            if line[c] != "1":
                                c += 1
                                continue
                            start = c
                            while c < cols and c < len(line) and line[c] == "1":
                                c += 1
                            cx0 = start * pw / float(cols)
                            cx1 = c * pw / float(cols)
                            x0 = int(round(_c1x + (cx0 - _c0x) * _kr)) - 1
                            x1 = int(round(_c1x + (cx1 - _c0x) * _kr)) + 1
                            if x1 > x0:
                                region = region.united(
                                    QRegion(x0, y0 - 1, x1 - x0, (y1 - y0) + 2))
                except Exception:
                    pass
                return region

            def _paint_hotspots(self, qp, dx, dy, dw, dh):
                """热区小圆点 + 序号（坐标是画布归一化值，序号与右键菜单里的编号一致）。"""
                try:
                    f = QFont()
                    f.setPointSize(9)
                    f.setBold(True)
                    qp.setFont(f)
                    fm = qp.fontMetrics()
                    for pt in self._hotspot_pts:
                        try:
                            nx, ny, n = float(pt[0]), float(pt[1]), pt[2]
                        except Exception:
                            continue
                        cx = dx + dw * nx
                        cy = dy + dh * ny
                        # 圆点要压得住模型上的深色美术：半径给到 9.5，先描一圈
                        # 深色底再填亮黄，任何底色上都看得清。
                        qp.setBrush(QBrush(QColor(255, 208, 64, 235)))
                        qp.setPen(QPen(QColor(38, 28, 8, 250), 1.8))
                        qp.drawEllipse(QPointF(cx, cy), 9.5, 9.5)
                        txt = str(n)
                        tw = fm.horizontalAdvance(txt)
                        qp.setPen(QPen(QColor(28, 20, 4, 255)))
                        qp.drawText(QPointF(cx - tw / 2.0, cy + fm.ascent() / 2.0 - 1.5), txt)
                except Exception:
                    pass

            def mousePressEvent(self, e):
                # 交互护栏：模型没加载完（剪影遮罩还没按模型裁过，整块屏幕都会被
                # 当成命中区）时，任何按下都不许拖 —— 否则在屏幕任意位置拖一下，
                # 就会把还没画出来的模型挪走。
                if not state.get("ready"):
                    e.ignore()
                    return
                # 用户主动点自己的模型 —— 这是唯一允许"抢到最前"的时机：
                # 多模型并存时，谁被点谁就在最上面（而不是靠定时器互相抢）。
                try:
                    _raise_own_windows()
                except Exception:
                    pass
                # 新的按下打断上一次拖拽，采样清零（避免把别的位置序列当成速度）
                self._drag_samples = []
                if _MOUSE_DBG:
                    _log_line("INFO", "[MOUSE] press btn=%s pos=%s global=%s" % (
                        int(e.button()), (e.pos().x(), e.pos().y()),
                        (e.globalPos().x(), e.globalPos().y())))
                if e.button() == Qt.MiddleButton:
                    # 中键 = 换装：点哪个部位就切那个部位相关的服装通道
                    # （手/头/上身/腰臀/腿/脚）。该部位没有通道会提示一句；
                    # 模型完全没有换装通道时静默忽略，不影响其它中键行为。
                    try:
                        _norm = pet.canvas_norm(e.pos())
                        _cycle_outfit(_norm[0] if _norm else None,
                                      _norm[1] if _norm else None)
                    except Exception:
                        pass
                    e.accept()
                    return
                if e.button() == Qt.LeftButton:
                    if state.get("part_mode"):
                        try:
                            _pick_part_at(e.pos())
                        except Exception:
                            pass
                        return
                    # 字幕优先：点在字幕条上只拖字幕，不拖整个桌宠窗口。
                    if self._sub_text and self._sub_rect.contains(QPointF(e.pos())):
                        self._dragging_sub = True
                        self._sub_drag_offset = QPointF(e.pos()) - self._sub_rect.topLeft()
                        self._sub_drag_moved = False
                        self._press_gpos = e.globalPos()
                        self._press_moved = False
                        return
                    self._dragging = True
                    # 缩放动画还在跑的话先定格到目标再拖：动画每 16ms 会用"启动
                    # 那一刻的几何快照"重算 Stage 位置，用户拖出来的位移下一拍就
                    # 被覆盖回去（模型弹回锚点原位，光标落空 → 看起来"拖不动、
                    # 鼠标穿透"）。定格之后几何只由拖动决定。
                    try:
                        _fin = state.get("finish_scale_anim")
                        if _fin:
                            _fin()
                    except Exception:
                        pass
                    _sx, _sy, _sw, _sh = self.stage_rect()
                    self._offset = e.globalPos() - QPoint(_sx, _sy)
                    self._press_gpos = e.globalPos()
                    self._press_moved = False
                    if _MOUSE_DBG:
                        _log_line("INFO", "[MOUSE] drag start offset=(%d,%d)"
                                  % (self._offset.x(), self._offset.y()))

            def mouseMoveEvent(self, e):
                if _MOUSE_DBG and self._dragging:
                    _log_line("INFO", "[MOUSE] move global=%s dragging=%s" % (
                        (e.globalPos().x(), e.globalPos().y()), self._dragging))
                if self._dragging_sub:
                    if (self._press_gpos is not None
                            and (e.globalPos() - self._press_gpos).manhattanLength() > 4):
                        self._press_moved = True
                        self._sub_drag_moved = True
                    if self._sub_drag_offset is None:
                        return
                    pw = max(1.0, float(self.width()))
                    ph = max(1.0, float(self.height()))
                    bw = self._sub_rect.width()
                    bh = self._sub_rect.height()
                    x = max(4.0, min(pw - bw - 4.0, e.pos().x() - self._sub_drag_offset.x()))
                    y = max(4.0, min(ph - bh - 4.0, e.pos().y() - self._sub_drag_offset.y()))
                    self._sub_rect = QRectF(x, y, bw, bh)
                    # 记"相对模型 Stage 中心"的归一化偏移：只活在本次打开，不落盘；
                    # 模型被拖走/缩放时按它重新落位（字幕跟着模型走）。
                    try:
                        _st = self.stage_local_rect()
                        _sw = max(1.0, float(_st[2]))
                        _sh = max(1.0, float(_st[3]))
                        _u = ((x + bw * 0.5) - (_st[0] + _st[2] * 0.5)) / _sw
                        _v = ((y + bh * 0.5) - (_st[1] + _st[3] * 0.5)) / _sh
                        self._sub_rel = (max(-2.5, min(2.5, _u)),
                                         max(-2.5, min(2.5, _v)))
                    except Exception:
                        pass
                    self.update()
                    return
                if self._dragging:
                    if (self._press_gpos is not None
                            and (e.globalPos() - self._press_gpos).manhattanLength() > 4):
                        self._press_moved = True   # 有拖动 → 不认作双击
                    g = e.globalPos() - self._offset
                    if _MOUSE_DBG:
                        _log_line("INFO", "[MOUSE] drag g=(%d,%d) off=(%d,%d) before=%s" % (
                            g.x(), g.y(), self._offset.x(), self._offset.y(),
                            self.stage_rect()))
                    _mt0 = time.monotonic() if _PERF["on"] else 0.0
                    self.move_stage_to(g.x(), g.y())
                    if _PERF["on"]:
                        _PERF["move_n"] += 1
                        _PERF["move_ms"] += (time.monotonic() - _mt0) * 1000.0
                    if _MOUSE_DBG:
                        _log_line("INFO", "[MOUSE] drag after=%s" % (self.stage_rect(),))

            # === 拖拽惯性 ===
            # 注意：**"惯性"不是让整个模型位置继续滑行**（用户明确否决：那样像拖动有延迟）。
            # 真正的惯性做在渲染层：拖拽时按 Stage 位移的速度/加速度驱动身体、头发、
            # 衣物的参数摆动（renderer.html 的 _sway*，二阶弹簧），松手后自动回摆收敛。
            # Python 侧只负责把 Stage 位移下发（move_stage_to → layout），不做任何。

            def mouseReleaseEvent(self, e):
                if _MOUSE_DBG:
                    _log_line("INFO", "[MOUSE] release btn=%s dragging=%s moved=%s"
                              % (int(e.button()), self._dragging, self._press_moved))
                if self._dragging_sub:
                    self._dragging_sub = False
                    self._sub_drag_offset = None
                    # 不再写盘：偏移只在本次打开内有效，重开/换模型一律回默认位置。
                    self._sub_drag_moved = False
                    return
                if self._dragging:
                    self._dragging = False
                    _rt0 = time.monotonic() if _PERF["on"] else 0.0
                    # 拖拽结束才回报位置：拖动中每帧都发会把父进程状态刷爆，
                    # 但松手必须同步一次，否则工具层仍认为模型停在旧坐标。
                    try:
                        _notify_pos()
                    except Exception:
                        pass
                    # 松手立刻按**最终几何**重抓一次命中区：拖动期间的重算是
                    # 220ms 防抖 + 异步抓帧，最后一次抓帧很可能发生在半路，
                    # 停手后命中区就停在错位状态、再也不重算 —— 表现就是
                    # "拖完模型点不到、右键也没了"。这里补一次确定性重算。
                    try:
                        _refresh_hit_geometry()
                    except Exception:
                        pass
                    if _PERF["on"]:
                        _PERF["rel_n"] += 1
                        _PERF["rel_ms"] += (time.monotonic() - _rt0) * 1000.0
                    # 松手后**不动位置**：惯性只体现在身体/衣物摆动（渲染层 _sway 已经
                    # 一直在按 Stage 位移算速度，这里不需要给额外推力）。

            def mouseDoubleClickEvent(self, e):
                """双击不同部位 → 播放不同动画（部位判定见 _zone_for_point）。"""
                if e.button() != Qt.LeftButton:
                    return
                # 字幕条上不触发模型双击互动，避免拖动时误播动画。
                if self._sub_text and self._sub_rect.contains(QPointF(e.pos())):
                    return
                if self._press_moved or state.get("part_mode"):
                    return
                if not state.get("dblclick", True):
                    return
                if _MOUSE_DBG:
                    _log_line("INFO", "[MOUSE] dblclick 取消拖动（_press_moved=%s）"
                              % (self._press_moved,))
                self._dragging = False
                try:
                    _on_double_click(e.pos())
                except Exception as _err:
                    _log_line("ERROR", "dblclick failed: %r" % (_err,))


            def wheelEvent(self, e):
                """滚轮缩放：上滚放大，下滚缩小，以鼠标位置为锚点。

                两条护栏（都是现场 bug）：
                1. 模型没加载完不接受滚轮 —— 那时整屏都在命中区里，随便在哪滚一下
                   就会改掉还没画出来的模型的缩放。
                2. 滚轮**必须吃掉**（e.accept()）：不吃掉的话事件会继续往系统里传，
                   压在下面的聊天/浏览器窗口也跟着滚动，就是"底部界面受影响"。
                """
                if not state.get("ready"):
                    e.ignore()
                    return
                delta = e.angleDelta().y()
                if delta == 0:
                    e.accept()
                    return
                # 命中判定：只有点在模型上才缩放，模型外的空白让给下面的窗口
                inside = False
                try:
                    inside = bool(self.mask().contains(e.pos()))
                except Exception:
                    inside = False
                if not inside:
                    # 兜底：细格子命中区偶尔会有"洞"（头发缝隙、刚缩放完还没重算）。
                    # 只要点落在模型包围盒里就认作在模型上，并把这一小圈并进命中区
                    # —— 否则下一格又被判成"在外面"，滚轮就继续漏给下面的窗口。
                    try:
                        norm = state.get("model_rect_norm")
                        if norm:
                            nx, ny, nw, nh = [float(v) for v in norm]
                            px, py = e.pos().x(), e.pos().y()
                            if (nx * self.width() <= px <= (nx + nw) * self.width()
                                    and ny * self.height() <= py <= (ny + nh) * self.height()):
                                inside = True
                                self.setMask(self.mask().united(
                                    QRegion(px - 6, py - 6, 12, 12)))
                                self._mask_key = None   # 下一次几何回调强制重算
                    except Exception:
                        pass
                if not inside:
                    e.ignore()
                    return
                e.accept()
                if _MOUSE_DBG:
                    _log_line("INFO", "[MOUSE] wheel delta=%s global=%s inside=%s" % (
                        delta, (e.globalPos().x(), e.globalPos().y()), inside))
                _scale_wheel(delta, e.globalPos())

            def _build_context_menu(self, menu=None, expand_all=False):
                """右键菜单：鼠标跟随 / 鼠标避让 / 双击互动 / 播放动画 / 切换模型。

                单独抽成方法：`contextMenuEvent` 弹它，`__menu_dump__` 自检也构建同
                一份 —— 验证菜单文案（♪ 配音标记之类）时看到的和用户看到的一致。

                menu 传入时**原地重建**：右键菜单整个进程只保留一个 QMenu 实例，
                不再每点一次右键就 new 一个挂在桌宠下面的菜单（旧写法会一漏一个，
                点得越多菜单对象越多，越点越慢）。

                「播放动画」「切换模型」两个子菜单走 lazy_submenu —— 弹菜单的瞬间
                只建顶层条目，扫目录 / 读 model3.json 都推迟到用户真正展开子菜单时
                才做（264 个模型冷启动实测 0.67s，以前全压在右键事件里）。

                expand_all=True：立刻把懒建子菜单填满，供 `__menu_dump__` 自检看到
                展开后应有的完整条目树。
                """
                menu = menu if menu is not None else QMenu(self)
                clear_menu(menu)
                fillers = []
                follow_act = QAction("鼠标跟随", menu)
                follow_act.setCheckable(True)
                follow_act.setChecked(state["track"])
                follow_act.triggered.connect(_toggle_track)
                menu.addAction(follow_act)
                avoid_act = QAction("鼠标避让", menu)
                avoid_act.setCheckable(True)
                avoid_act.setChecked(state["avoid"])
                avoid_act.triggered.connect(_toggle_avoid)
                menu.addAction(avoid_act)
                dbl_act = QAction("双击互动", menu)
                dbl_act.setCheckable(True)
                dbl_act.setChecked(bool(state.get("dblclick", True)))
                dbl_act.triggered.connect(_toggle_dblclick)
                menu.addAction(dbl_act)
                # 每个模型独立记忆显示范围：local=只显示模型包围盒（默认，
                # 动画里的全屏黑贴图会被裁掉）；full=整块显示器可见。
                range_menu = menu.addMenu("显示范围")
                local_act = QAction("局部包围盒", range_menu)
                local_act.setCheckable(True)
                local_act.setChecked(str(state.get("display_mode") or "local") != "full")
                local_act.triggered.connect(lambda _c=False: _set_display_mode("local"))
                range_menu.addAction(local_act)
                full_act = QAction("全屏可见", range_menu)
                full_act.setCheckable(True)
                full_act.setChecked(str(state.get("display_mode") or "local") == "full")
                full_act.triggered.connect(lambda _c=False: _set_display_mode("full"))
                range_menu.addAction(full_act)
                menu.addSeparator()
                menu.addAction("隐藏部件…", _enter_part_mode)
                menu.addAction("恢复部件", _restore_parts)
                menu.addSeparator()
                fillers.append(lazy_submenu(menu, "播放动画", _fill_anim_menu, _log_line))
                # 换装：只有模型真的带服装参数通道（Clothes* + Exp3 状态）时才出现，
                # 普通模型不会多出一条空菜单。
                if state.get("outfits"):
                    fillers.append(lazy_submenu(menu, "换装", _fill_outfit_menu, _log_line))
                menu.addSeparator()
                menu.addAction("隐藏", pet.hide)
                menu.addAction("关闭", _shutdown_pet)
                menu.addAction("全部关闭", _shutdown_all_pets)
                menu.addSeparator()
                _root_now = load_ui_cfg().get("model_root") or ""
                if _root_now and os.path.isdir(_root_now):
                    menu.addAction("选择模型目录…（当前：%s）" % os.path.basename(_root_now),
                                   _pick_model)
                else:
                    menu.addAction("选择模型目录…", _pick_model)
                fillers.append(lazy_submenu(menu, "切换模型", _fill_model_submenu, _log_line))
                menu.addAction("重新加载渲染器", _reload_renderer)
                if expand_all:
                    for _sub, _fill in fillers:
                        _fill()
                return menu

            def _on_menu_hidden(self):
                """菜单收起：解除"展开中"状态，并给半秒缓冲再恢复避让。"""
                state["menu_open"] = False
                state["avoid_grace_until"] = time.monotonic() + 0.5
                try:
                    QTimer.singleShot(0, _repin_windows)
                except Exception:
                    pass

            def contextMenuEvent(self, e):
                # 用 popup()，**不**用 exec_()：exec_ 会进入嵌套模态循环并抓走鼠标，
                # 菜单一旦没显示出来（置顶的桌宠窗口把它盖住、或 Windows 把激活抢走），
                # 整个窗口就被那条循环锁死 —— 用户看到的就是「右键之后窗口未响应」。
                # popup() 不阻塞事件循环：再右键一次就收起，点空白就关掉，
                # 桌宠永远不会被菜单锁住。
                m = self._menu
                if m is None:
                    m = QMenu(self)
                    # 桌宠窗口带 WindowStaysOnTopHint，菜单也必须置顶，否则会弹到
                    # 半透明桌宠窗口下面（看得见一半、点不到，像卡死）
                    m.setWindowFlag(Qt.WindowStaysOnTopHint, True)
                    m.aboutToHide.connect(self._on_menu_hidden)
                    self._menu = m
                if m.isVisible():
                    m.close()                       # 已展开 → 再右键 = 收起
                    self._on_menu_hidden()
                    e.accept()
                    return
                self._build_context_menu(m)
                state["menu_open"] = True
                state["avoid_grace_until"] = 0.0
                m.popup(e.globalPos())
                # popup() 返回后窗口才真正创建；下一轮事件循环把它补到
                # topmost 最上层，避免被 Electron 模型窗盖住。
                QTimer.singleShot(0, _pin_menu_topmost)
                e.accept()

        pet = PetWidget()

        _GWL_EXSTYLE = -20
        _WS_EX_TOPMOST = 0x00000008

        def _zdbg(msg):
            """置顶相关的排障日志（PET_ZDEBUG=1 时写 _js_console.log）。

            多模型"抢占置顶"只能靠时间线定位是谁在抬窗，平时不用开。
            """
            try:
                if (os.environ.get("PET_ZDEBUG") or "").strip() in ("1", "true", "yes"):
                    _log_line("ZDBG", msg)
            except Exception:
                pass

        def _hwnd_is_topmost(hwnd):
            """查扩展样式判断窗口是否已在 topmost 组里（只读，不改状态）。"""
            try:
                user32 = ctypes.windll.user32
                user32.GetWindowLongW.restype = ctypes.c_long
                user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
                ex = int(user32.GetWindowLongW(int(hwnd), _GWL_EXSTYLE))
                return bool(ex & _WS_EX_TOPMOST)
            except Exception:
                return False

        def _set_hwnd_topmost(hwnd, force=False):
            """把窗口放进 topmost 组；已经 topmost 就什么都不做。

            原来无条件 SetWindowPos(HWND_TOPMOST) 会顺带把窗口抬到 topmost 组的
            最上层 —— 多模型同时打开时，每个 worker 的补置顶定时器都在抢这个
            最上层，表现就是模型互相盖、闪烁。现在只在置顶属性真的丢了时补。
            """
            try:
                if not force and _hwnd_is_topmost(hwnd):
                    return True
                user32 = ctypes.windll.user32
                user32.SetWindowPos.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_uint,
                ]
                user32.SetWindowPos.restype = ctypes.c_bool
                user32.SetWindowPos(
                    int(hwnd), -1, 0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010)   # NOSIZE|NOMOVE|NOACTIVATE
                _zdbg("SetWindowPos(TOP) hwnd=%d force=%s" % (int(hwnd), force))
                return True
            except Exception:
                return False

        def _place_above(hwnd, ref_hwnd):
            """把 hwnd 放到 ref_hwnd 的正上方（相对插入，不抢 topmost 组最顶层）。

            用于 Qt 交互覆盖层：它只需要压在自己这个模型的 Electron 窗上面，
            不需要（也不该）跑到别的模型头上。

            坑：SetWindowPos 的第二个参数语义是「排在我**后面**（更靠下）的那个
            窗口」。直接传 ref 会把自己插到 ref **下面** —— Qt 覆盖层落到模型窗
            之下，热区圆点和序号就被模型盖住了（这正是"标志和文字看不见"）。
            所以反过来：把 ref 压到 hwnd 下方，再复核一次 z 序。
            """
            try:
                user32 = ctypes.windll.user32
                user32.SetWindowPos.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_uint,
                ]
                user32.SetWindowPos.restype = ctypes.c_bool
                user32.SetWindowPos(
                    int(ref_hwnd), int(hwnd), 0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010)   # NOSIZE|NOMOVE|NOACTIVATE
                ok = _is_above(hwnd, ref_hwnd)
                _zdbg("place_above hwnd=%d ref=%d ok=%s" % (int(hwnd), int(ref_hwnd), ok))
                return ok
            except Exception:
                return False

        def _is_above(hwnd_a, hwnd_b):
            """全局 z 序里 a 是否排在 b 前面（更靠上层）。"""
            try:
                user32 = ctypes.windll.user32
                user32.GetTopWindow.restype = ctypes.c_void_p
                user32.GetTopWindow.argtypes = [ctypes.c_void_p]
                user32.GetWindow.restype = ctypes.c_void_p
                user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                a = int(hwnd_a)
                b = int(hwnd_b)
                cur = user32.GetTopWindow(None)
                guard = 0
                while cur and guard < 4096:
                    guard += 1
                    h = int(cur)
                    if h == a:
                        return True
                    if h == b:
                        return False
                    cur = user32.GetWindow(cur, 2)      # GW_HWNDNEXT
                return False
            except Exception:
                return False

        def _electron_hwnds():
            """当前 Electron 主进程的可见顶层窗（通常 1 个）。"""
            out = []
            try:
                proc = getattr(self, "_electron_proc", None)
                if proc is None or proc.poll() is not None:
                    return out
                target_pid = int(proc.pid)
                user32 = ctypes.windll.user32

                @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                def _cb(hwnd, _lparam):
                    p = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                    if int(p.value) == target_pid and user32.IsWindowVisible(hwnd):
                        out.append(int(hwnd))
                    return True

                user32.EnumWindows(_cb, 0)
            except Exception:
                pass
            return out

        def _pin_qt_topmost(force=False):
            """把 Qt 交互覆盖层压到自己这个模型的 Electron 之上（仍保持透明）。

            Electron 是后启动的 topmost 窗口，默认会盖在 Qt 上；那样鼠标
            落在 Electron 的整窗 ignore 区，Qt 的剪影 mask 根本收不到事件。
            Qt 必须在上层负责点击/拖动，Electron 只负责把模型画在下面透出来。

            关键：用"插到自己 Electron 正上方"的相对插法，而不是抬到 topmost
            组最顶层 —— 后者会把别的模型压下去（多模型互相抢占置顶）。
            """
            if not _electron_mode:
                return
            try:
                if not pet.isVisible():
                    return
                qt = int(pet.winId())
                els = _electron_hwnds()
                if els:
                    # 两个窗都必须在 topmost 带里，才谈得上"谁压谁"；
                    # 已经在带里就什么都不做（不抬最上层，避免多模型抢）。
                    for el in els:
                        _set_hwnd_topmost(el, force=force)
                    if not _is_above(qt, els[0]):
                        _zdbg("qt not above electron -> reinsert qt=%d el=%d" % (qt, els[0]))
                        _set_hwnd_topmost(qt)
                        if not _place_above(qt, els[0]):
                            # 相对插入没成功（极少见：被别的 topmost 程序打断），
                            # 退化成"抬到 topmost 组最上层"，保证热区/点击可见可用。
                            _zdbg("place_above failed -> force topmost qt")
                            _set_hwnd_topmost(qt, force=True)
                    return
                _set_hwnd_topmost(qt, force=force)
            except Exception:
                pass

        def _pin_electron_topmost(force=False):
            """Electron 的 alwaysOnTop 在 setBounds 后可能被 Windows 清掉，这里补刀。

            只按当前 Electron 主进程 PID 找它的顶层窗，不碰别的窗口；
            已经是 topmost 就不动（避免每 2s 把自己抬到 topmost 组最上层，
            那就是多模型互相抢占的根源）。
            """
            if not _electron_mode:
                return
            for hwnd in _electron_hwnds():
                _set_hwnd_topmost(hwnd, force=force)

        def _raise_own_windows():
            """用户点/拖自己的模型时，把这个模型整体抬到最前（用户驱动，非周期）。

            这是唯一被允许"抢占"的时机：用户明确和某个模型交互，它就该在最上面。
            """
            if not _electron_mode:
                return
            try:
                for hwnd in _electron_hwnds():
                    _set_hwnd_topmost(hwnd, force=True)
                _pin_qt_topmost(force=True)
                _zdbg("raise_own (user click)")
            except Exception:
                pass

        def _pin_menu_topmost():
            """右键菜单打开时压在模型/交互层之上。

            QMenu 虽然带 WindowStaysOnTopHint，但 Electron 模型窗也是
            topmost；谁最后被 SetWindowPos 谁在上面。菜单弹出后必须再补
            一次，否则模型置顶会把菜单盖住。
            """
            try:
                if not state.get("menu_open"):
                    return
                m = getattr(pet, "_menu", None)
                if m is None or not m.isVisible():
                    return
                # 父菜单先补，子菜单后补；子菜单最后置顶，保证展开项在最上面。
                _set_hwnd_topmost(m.winId())
                for sub in m.findChildren(QMenu):
                    if sub.isVisible():
                        _set_hwnd_topmost(sub.winId())
            except Exception:
                pass

        class _MenuTopmostFilter(QObject):
            """QMenu 每次 Show（含子菜单展开）后补一次 topmost。"""

            def eventFilter(self, obj, ev):
                try:
                    if ev.type() == QEvent.Show and isinstance(obj, QMenu):
                        QTimer.singleShot(0, _pin_menu_topmost)
                except Exception:
                    pass
                return False

        _menu_topmost_filter = _MenuTopmostFilter(pet)
        pet._menu_topmost_filter = _menu_topmost_filter
        app.installEventFilter(_menu_topmost_filter)

        # === 全屏 Stage 布局 ===
        def _display_clip():
            if str(state.get("display_mode") or "local") == "full":
                return (screen.x(), screen.y(), screen.width(), screen.height())
            return tuple(int(v) for v in pet.stage_rect())

        def _sync_layout():
            """把 Stage/Clip 下发给渲染层；不触碰 OS 窗口几何。"""
            # 字幕按"相对 Stage 中心"落位，Stage 一动就必须重画一次：
            # 否则字幕条会停在旧坐标，看起来就是"字幕固定在屏幕上、不跟模型走"。
            # 放在最前面：直出/本地模式下也要跟着动。
            try:
                if getattr(pet, "_sub_text", "") and pet.isVisible():
                    pet.update()
            except Exception:
                pass
            if not _electron_mode:
                return
            if _PERF["on"]:
                _PERF["layout_n"] += 1
            stage = tuple(int(v) for v in pet.stage_rect())
            clip = _display_clip()
            ep = getattr(self, "_electron_page", None)
            if ep is not None:
                ep.set_layout(stage, clip, state.get("display_mode") or "local")
                return
            try:
                page.runJavaScript(
                    "setPetCommand(" + json.dumps({
                        "type": "setLayout", "stage": list(stage),
                        "clip": list(clip),
                        "mode": state.get("display_mode") or "local",
                    }) + ")")
            except Exception:
                pass

        # Stage 移动/缩放后，旧 alpha 剪影和双击热区坐标都还停在旧位置。
        # 必须等这一轮布局稳定后重采一次，否则鼠标命中的是“上一个位置”
        # 的模型，表现为点不到、拖不动。
        _hit_refresh_timer = QTimer()
        _hit_refresh_timer.setSingleShot(True)

        def _reusable_since_capture():
            """当前 Stage 相对"上次抓帧/建热区"能否直接复用（纯平移**或**缩放）。

            剪影格子、alpha 网格的热区、双击热区种子全都按"发布那一刻的 Stage
            几何"量出来，但坐标本身是**整块画布归一化**的；_apply_interaction_mask
            每帧都会按
                屏幕x = 当前Stage中心x + (格子画布x - 发布时Stage中心x) × k
            （k = 当前Stage高 / 发布时Stage高，两轴共用一个 k）把它们搬到现在
            的 Stage 位置 —— 渲染层就是"按 Stage 高度定模型缩放、把模型画在 Stage
            中心"，所以**同一个模型**一次抓帧就能一直用下去（拖动、缩放都在同一套
            映射里）。换模型则必须重抓：另一只模型的轮廓长度不一样。

            旧实现只认"尺寸差 ≤2px"（纯平移），缩放一结束就重抓一帧 + 重建热区：
            readPixels 是同步 GPU→CPU，滚动缩放的收尾正好撞进这条路，表现就是
            "缩放停手后整段卡住几百毫秒、画面尺寸一口气跳过去"。
            """
            try:
                if (os.environ.get("PET_NO_GEOM_SKIP") or "").strip() in ("1", "true", "yes"):
                    return False      # 诊断开关：强制走完整重算做对照
                cm = state.get("_cm_stage")
                _am_obj = state.get("alpha_mask") or {}
                am = _am_obj.get("stage")
                if not state.get("_cm_cells") or not am:
                    return False
                _mu = str(state.get("model_url") or "")
                if str(state.get("_cm_model") or "") != _mu:
                    return False
                # alpha 剪影也必须是**这一个模型**的：旧剪影留着会被当成新模型的
                # 轮廓用，命中区就整体错位（可见部位穿透）。没打过标记的旧数据
                # （老版本抓的）一律视为不可复用，重抓一次最稳。
                if str(_am_obj.get("model") or "") != _mu:
                    return False
                for ref in (cm, am):
                    if not ref or len(list(ref)) != 4:
                        return False
                    # 只挡住明显坏掉的几何（0/负尺寸）
                    if float(list(ref)[2]) < 2 or float(list(ref)[3]) < 2:
                        return False
                # Stage **尺寸**变了就必须重抓：尺寸一变，模型按 Stage 高度重新
                # 缩放、Stage 变宽还会露出原先被裁掉的一圈（"补宽缓动"就是这个
                # 场景：模型先按窄 Stage 显示、随后 Stage 拉宽，左右补出新的实体）。
                # 旧抓帧的剪影里根本没有那一圈，靠等比搬只能把"旧的一圈"摆对，
                # 新露出来的部位仍然是空的 —— 那边点下去就穿透。
                # 纯平移（尺寸没变）可以放心复用：换算退化成"整体平移"。
                _st_now = pet.stage_rect()
                for ref in (cm, am):
                    if (abs(float(list(ref)[2]) - float(_st_now[2])) > 2
                            or abs(float(list(ref)[3]) - float(_st_now[3])) > 2):
                        return False
                return True
            except Exception:
                return False

        def _refresh_hit_geometry():
            # 拖动途中绝不抓帧：readPixels 是同步 GPU→CPU（全屏 8.3MB，实测
            # 300~1200ms），抓一次就等于把渲染层冻住，模型在用户眼里"卡住不动"。
            # 拖动期间的命中区由包围盒粗掩膜顶着（见 _apply_interaction_mask），
            # 松手后由 mouseReleaseEvent 走一次确定性重算。
            if getattr(pet, "_dragging", False) or getattr(pet, "_dragging_sub", False):
                # 只记"几何过期"，**不能**写 _geom_pending：_poll_cmds 每 10ms
                # 见到 pending 就会重算一次双击热区，拖动期间等于 100 次/秒
                # 的 JS 抓帧请求，比不跳过更慢。松手时 mouseReleaseEvent
                # 会走一次确定性重算。
                state["_geom_stale"] = True
                return
            # 拖动/挪位/缩放只要"模型没换"，复用上一套格子就行。原实现每次松手
            # 都重抓一帧 + 重建热区（getClickMap 采样 ~900 格 + 逐格部件回溯，
            # 实测 200~450ms 冻结），而平移/缩放场景下结果只差一个位移+缩放比
            # —— 那个映射掩膜本来就在算（见 _apply_interaction_mask）。
            if _reusable_since_capture():
                if _PERF["on"]:
                    _PERF["geom_skip_n"] += 1
                state["_geom_stale"] = False
                state.pop("_geom_pending", None)
                _request_model_rect()      # 只有这一项必须刷（纯算术，不抓帧）
                return
            # 两路抓帧必须串行：alpha 剪影先抓，回包后再重建双击热区。
            # 并发抓帧会把 Electron 和 worker 一起带崩（见 _request_alpha_mask）。
            state["_geom_pending"] = time.time()
            if os.environ.get("PET_SKIP_ALPHA_REFRESH"):
                _consume_geom_pending()
                return
            # 上一帧还没回包就不再插一发：并发抓帧会连带把 worker 带崩。
            # pending 已经记下，等它回包后 _poll_cmds / _consume 会补算。
            if state.get("_alpha_busy"):
                return
            try:
                _request_alpha_mask()
            except Exception:
                pass
            # 抓不到帧（非直连/页面未就绪）时不 busy，立刻补一次热区地图
            if not state.get("_alpha_busy"):
                _consume_geom_pending()

        def _consume_geom_pending():
            t = state.pop("_geom_pending", None)
            if t is None:
                return
            if os.environ.get("PET_SKIP_CM_REFRESH"):
                return
            try:
                _build_click_map()
            except Exception:
                pass

        _hit_refresh_timer.timeout.connect(_refresh_hit_geometry)

        def _schedule_hit_refresh():
            # 连续缩放是 ~60fps 调 _set_stage_rect，这里做 220ms 防抖：
            # 只在手势停下来后抓一帧，不把 readPixels 塞进动画链路。
            # 拖动中干脆不排期：防抖计时器会在"手停一下再走"的空隙里触发，
            # 恰好把一次 readPixels 插进拖动中间（就是拖动时的突然卡顿）。
            if getattr(pet, "_dragging", False) or getattr(pet, "_dragging_sub", False):
                state["_geom_stale"] = True
                try:
                    _hit_refresh_timer.stop()
                except Exception:
                    pass
                return
            _hit_refresh_timer.start(220)

        def _set_stage_rect(x, y, w, h):
            w = max(80, int(w))
            h = max(80, int(h))
            _st0 = time.monotonic() if _PERF["on"] else 0.0
            if _PERF["on"]:
                _PERF["drag_n"] += 1
            state["stage_rect"] = (int(x), int(y), w, h)
            # 快照一份给 /state：全屏模式下 __geom__ 要等渲染层空闲，工具侧
            # 只需要"当前位置"时不该被 JS 往返卡住。
            _WSTATE["stage_rect"] = state["stage_rect"]
            _sync_layout()
            _schedule_hit_refresh()
            if _PERF["on"]:
                _PERF["drag_ms"] += (time.monotonic() - _st0) * 1000.0

        # === 切模型补宽：缓动"外拉"而不是一次跳过去 ===
        # 模型换掉后包围盒往往比当前 Stage 宽。旧实现直接 _set_stage_rect 一次改到
        # 目标宽度：用户看到的是"先只露出中间一条，然后画面一下子全部弹出来"。
        # 这里改成 0.3s 的 easeInOutCubic 外拉 —— 中心固定、宽度逐帧逼近，配合
        # 渲染层每帧 fit，观感是模型被慢慢拉开，而不是突然撑开。
        _wid_anim = {"active": False, "i": 0, "n": 1, "from": 0, "to": 0}

        def _wid_tick():
            if not _wid_anim["active"]:
                return
            try:
                n = max(1, int(_wid_anim["n"] or 1))
                _wid_anim["i"] = min(n, int(_wid_anim["i"] or 0) + 1)
                k = _wid_anim["i"] / float(n)
                ease = 3 * k * k - 2 * k * k * k        # easeInOutCubic
                w = int(round(_wid_anim["from"]
                              + (_wid_anim["to"] - _wid_anim["from"]) * ease))
                sx, sy, sw, sh = pet.stage_rect()
                # 以"此刻的中心"为基准：动画期间用户拖出来的位移不会被下一拍打回
                _set_stage_rect(int(sx + sw // 2 - w // 2), int(sy), max(80, w), int(sh))
                if _wid_anim["i"] >= n:
                    _wid_anim["active"] = False
            except Exception:
                _wid_anim["active"] = False

        _wid_timer = QTimer()
        _wid_timer.timeout.connect(_wid_tick)

        def _animate_stage_width(target_w, dur=0.30):
            """把 Stage 宽度缓动到 target_w（中心固定）；动画中被再次调用则改目标续拉。"""
            target_w = max(80, int(target_w))
            try:
                sx, sy, sw, sh = pet.stage_rect()
            except Exception:
                return
            if abs(target_w - int(sw)) <= 2:
                _wid_anim["active"] = False
                return
            _wid_anim.update({
                "active": True, "i": 0, "from": int(sw), "to": target_w,
                "n": max(6, int(round(float(dur) / 0.016))),
            })
            if not _wid_timer.isActive():
                _wid_timer.start(16)

        def _move_stage_norm(nx, ny):
            """按 tool.py 的归一化坐标移动 Stage（不是移动全屏窗口）。"""
            try:
                sx, sy, sw, sh = pet.stage_rect()
                # 允许越界位置：负数/大于 1 表示模型有一部分在屏幕外。
                nx = max(-1.0, min(2.0, float(nx)))
                ny = max(-1.0, min(2.0, float(ny)))
                x = screen.x() + int(round(nx * max(0, screen.width() - sw)))
                y = screen.y() + int(round(ny * max(0, screen.height() - sh)))
                _set_stage_rect(x, y, sw, sh)
            except Exception:
                pass

        _model_mode_cfg = {"url": str(state.get("model_url") or "")}

        def _apply_model_display_mode(url):
            _model_mode_cfg["url"] = str(url or "")
            modes = load_ui_cfg().get("display_modes") or {}
            raw = str(modes.get(_model_mode_cfg["url"], "local") or "local").lower()
            state["display_mode"] = "full" if raw in ("full", "all", "screen") else "local"
            _WSTATE["display_mode"] = state["display_mode"]
            _sync_layout()
            try:
                pet._apply_interaction_mask()
            except Exception:
                pass

        def _set_display_mode(mode, persist=True):
            mode = "full" if str(mode) == "full" else "local"
            state["display_mode"] = mode
            _WSTATE["display_mode"] = mode
            if persist:
                modes = dict(load_ui_cfg().get("display_modes") or {})
                modes[_model_mode_cfg["url"]] = mode
                save_ui_cfg(display_modes=modes)
            _sync_layout()
            try:
                pet._apply_interaction_mask()
            except Exception:
                pass

        def _sync_stage_to_model():
            """模型加载后按包围盒宽高比扩 Stage 宽度，高度不变。"""
            try:
                aspect = float(state.get("model_aspect") or 0.0)
            except Exception:
                aspect = 0.0
            if aspect <= 0:
                return
            sx, sy, sw, sh = pet.stage_rect()
            need_w = int(max(sw, sh * aspect * 0.95 + 24))
            if abs(need_w - sw) <= 2:
                return
            # 走缓动：直接改宽度会让画面"一下子全出来"
            _animate_stage_width(need_w)

        pet._stage_hook = _sync_layout

        # 部件隐藏交互控件（点击选中部件后显示「确认隐藏 / 取消」）
        part_hint = QLabel("点击要隐藏的部件", pet)
        part_hint.setStyleSheet(
            "QLabel{color:#fff;background:rgba(20,24,34,200);border-radius:8px;"
            "padding:4px 10px;font-size:12px;}")
        part_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        part_hint.adjustSize()
        part_hint.hide()

        part_ok = QPushButton("确认隐藏", pet)
        part_cancel = QPushButton("取消", pet)
        for _b in (part_ok, part_cancel):
            _b.setStyleSheet(
                "QPushButton{color:#fff;background:rgba(30,36,50,225);"
                "border:1px solid rgba(120,160,255,150);border-radius:8px;"
                "padding:4px 10px;font-size:12px;}"
                "QPushButton:hover{background:rgba(60,80,120,240);}")
            _b.setFocusPolicy(Qt.NoFocus)
            _b.adjustSize()
            _b.hide()

        def _reposition_part_ui(anchor=None):
            """把「提示 + 确认隐藏/取消」贴到鼠标点击处附近，方便直接点。

            anchor = 窗口内坐标 (x, y)，默认取最近一次点选部件的位置。
            优先放在点击点下方（不遮住刚选中的部件），下方放不下就翻到上方，
            最后按窗口边界钳制 —— 贴着鼠标走，但不会跑出窗口。
            """
            w, h = pet.width(), pet.height()
            sx, sy, sw, sh = pet.stage_local_rect()
            part_hint.adjustSize()
            part_ok.adjustSize()
            part_cancel.adjustSize()
            bw = max(part_hint.width(), part_ok.width() + 8 + part_cancel.width())
            bh = part_hint.height() + 6 + part_ok.height()

            if anchor is None:
                anchor = state.get("part_anchor")
            if anchor:
                ax, ay = int(anchor[0]), int(anchor[1])
                x = ax - bw // 2
                y = ay + 18                  # 点击点下方 18px
                if y + bh > h - 4:           # 下方不够 → 翻到点击点上方
                    y = ay - 18 - bh
                y = max(4, min(y, h - 4 - bh))
            else:                            # 还没点选：Stage 顶部居中提示
                x = sx + (sw - bw) // 2
                y = sy + 8
            x = max(4, min(x, w - 4 - bw))

            part_hint.move(x + (bw - part_hint.width()) // 2, y)
            by = y + part_hint.height() + 6
            btn_total = part_ok.width() + 8 + part_cancel.width()
            part_ok.move(x + (bw - btn_total) // 2, by)
            part_cancel.move(part_ok.x() + part_ok.width() + 8, by)

        pet.show()

        # === 模型渲染 QWebEngineView ===
        # direct：顶层透明窗直接显示，和 PetWidget 覆盖层同几何；不再回传像素。
        # frame ：旧离屏渲染，readPixels 回传后由 PetWidget 用 QPainter 绘制。
        _w0, _h0 = int(BASE_W * scale), int(BASE_H * scale)
        _rw, _rh = (_w0, _h0) if _direct_mode else _fit_render_size(_w0, _h0)
        state["cur_render"] = (_rw, _rh)
        render_view = QWebEngineView(pet) if (_direct_mode and _direct_child) else QWebEngineView()
        if _electron_mode:
            # 只保留一个 1x1 的空白 QWebEngine 作为 QWebChannel/page 占位；
            # 真正画面由 Electron 透明窗显示，避免两份 Live2D 渲染。
            render_view.resize(1, 1)
            render_view.move(-3000, 0)
        elif _direct_mode:
            if _direct_child:
                # 子控件模式：交给父窗口的透明合成通道，避免顶层原生窗自带黑底。
                render_view.setGeometry(0, 0, _w0, _h0)
                render_view.setAttribute(Qt.WA_TranslucentBackground, True)
                render_view.setAttribute(Qt.WA_NoSystemBackground, True)
                render_view.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                render_view.setFocusPolicy(Qt.NoFocus)
                render_view.show()
                render_view.lower()
            else:
                render_view.setWindowFlags(
                    Qt.FramelessWindowHint |
                    Qt.WindowStaysOnTopHint |
                    Qt.NoDropShadowWindowHint |
                    Qt.WindowTransparentForInput
                )
                # 色键模式不能同时开 WA_TranslucentBackground（那会走逐像素
                # alpha，Windows 上给 QWebEngine 的结果是黑底）。
                render_view.setAttribute(Qt.WA_NoSystemBackground, True)
                render_view.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                render_view.setFocusPolicy(Qt.NoFocus)
                render_view.setGeometry(pet.geometry())
                render_view.show()
                pet.raise_()          # 覆盖层必须在模型窗之上，才能接收拖动/右键/热区
        else:
            render_view.resize(_rw, _rh)
            render_view.move(-_rw - 2000, 0)
            render_view.show()

        class SilentPage(QWebEnginePage):
            def javaScriptConsoleMessage(self, level, msg, line, source):
                # 诊断：写入 _js_console.log（超过 4MB 时轮转为 .old），不影响渲染
                try:
                    try:
                        if os.path.getsize(_JS_LOG_PATH) > 4 * 1024 * 1024:
                            os.replace(_JS_LOG_PATH, _JS_LOG_PATH + ".old")
                    except OSError:
                        pass
                    with open(_JS_LOG_PATH, "a", encoding="utf-8") as _f:
                        # 多实例：每行带实例标签，避免多个 worker 混写同一条日志无法区分
                        _f.write("%s[%s] L%s %s (%s:%s)\n" % (
                            _log_tag(), time.strftime("%H:%M:%S"), level, str(msg)[:2000],
                            source, line))
                except Exception:
                    pass

        def _make_render_page(view):
            """按统一配置新建渲染页面（离屏、透明、开 WebGL）。"""
            p = SilentPage(view)
            st = p.settings()
            st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
            st.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
            st.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
            st.setAttribute(QWebEngineSettings.WebGLEnabled, True)
            st.setAttribute(QWebEngineSettings.ShowScrollBars, False)
            # 配音：离屏渲染页永远拿不到真实用户手势，Chromium 的自动播放策略会把
            # HTMLAudio.play() 直接拒掉 —— 表现就是「有台词、字幕也显示，就是不出声」。
            # 这里明确关掉「播放需要用户手势」，让动作自带的配音能自己响。
            try:
                st.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
            except Exception:
                pass
            p.setBackgroundColor(
                QColor(0, 0, 0) if (_direct_mode and not _direct_child and _colorkey)
                else QColor(0, 0, 0, 0))
            return p

        page = _make_render_page(render_view)
        render_view.setPage(page)
        if _direct_mode and not _direct_child and _colorkey:
            def _apply_colorkey():
                try:
                    hwnd = int(render_view.winId())
                    u = ctypes.windll.user32
                    GWL_EXSTYLE, WS_EX_LAYERED, LWA_COLORKEY = -20, 0x80000, 1
                    style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
                    # COLORREF = 0x00BBGGRR；页面底色 #000000 → 0x00000000
                    u.SetLayeredWindowAttributes(hwnd, 0x00000000, 0, LWA_COLORKEY)
                    print("[direct] colorkey applied hwnd=%s" % hwnd)
                except Exception as e:
                    print("[direct] colorkey failed:", e)
            QTimer.singleShot(0, _apply_colorkey)
            QTimer.singleShot(1500, _apply_colorkey)
        if _direct_mode:
            # Windows 上顶层 QWebEngineView 默认仍带不透明黑底：只在 page 上设
            # 透明色不够，view 自身的 backing store / 样式表也要一起透明。
            try:
                page.setBackgroundColor(QColor(0, 0, 0, 0))
                render_view.setAutoFillBackground(False)
                render_view.setStyleSheet(
                    "QWebEngineView{background:transparent;border:0;}"
                    "QWidget{background:transparent;}")
                _pal = render_view.palette()
                _pal.setColor(_pal.Window, QColor(0, 0, 0, 0))
                _pal.setColor(_pal.Base, QColor(0, 0, 0, 0))
                render_view.setPalette(_pal)
            except Exception:
                pass
        # 配音口型等"窗口自身"需要往渲染页推命令的地方，用这个引用。
        # 注意要挂到 PetWidget 实例（pet）上：tts_lip_* 是 PetWidget 的方法，
        # 挂 self（窗口外套）等于挂错对象，口型会静默失效。
        try:
            pet._cmd_page = page
            # 备用通道：直接 page.runJavaScript 在个别时机（页面刚重建 / 主线程正忙）
            # 会抛异常，异常一旦被吞掉，内置动画就"看起来没播"。命令队列是同一套
            # 生效路径（/cmd 走的就是它），所以留一条兜底，保证内置动画必达。
            pet._send_cmd = self.send_command
            state["send_cmd"] = self.send_command
        except Exception:
            pass

        # === QWebChannel 桥：JS 推帧 → Python 解码 → 宠物窗口重绘 ===
        class Bridge(QObject):
            frameReady = pyqtSignal(str)
            frameRaw = pyqtSignal(bytes, int, int, int, int, int, int, int)
            # 原始 RGBA 帧（HTTP 通道 → Qt 线程）；后 5 个 int 是 cw/ch/ox/oy/seq
            modelReady = pyqtSignal()
            modelError = pyqtSignal(str)              # 模型加载/切换失败原因

            @pyqtSlot(str)
            def pushFrame(self, dataUrl: str):
                self.frameReady.emit(dataUrl)

            @pyqtSlot(bytes, int, int)
            def pushFrameRaw(self, data, w, h):      # 预留：需要时也可走 QWebChannel
                self.frameRaw.emit(data, w, h, 0, 0, 0, 0, 0)

            @pyqtSlot()
            def notifyModelReady(self):
                """JS 侧模型加载/切换完成时回调，隐藏加载泡泡。"""
                self.modelReady.emit()

            @pyqtSlot(str)
            def notifyModelError(self, message: str):
                """JS 侧模型加载/切换失败时回调，隐藏泡泡并记录失败原因。"""
                self.modelError.emit(str(message or ""))

        bridge = Bridge()
        bridge.frameReady.connect(pet.setFrame)

        # === 原始 RGBA 帧服务器（file:// 页面 → 127.0.0.1 POST，无预检）===
        _frames_enabled = {"v": True}

        def _accept_frame(data, w, h, cw=0, ch=0, ox=0, oy=0, seq=0):
            # 直出模式不接收任何帧：旧帧服务器/残留页面若误推，也不能盖到透明覆盖层上。
            if _direct_mode:
                return False
            # HTTP 线程 → pyqtSignal 队列投递到 Qt 线程（Qt 自动跨线程排队）。
            # 几何参数和 seq 随信号一起排队，避免"帧 A 的几何被帧 B 覆盖"的错配。
            if not _frames_enabled["v"]:
                return False
            try:
                bridge.frameRaw.emit(data, w, h, cw, ch, ox, oy, int(seq or 0))
                return True
            except Exception:
                return False

        _frame_token = uuid.uuid4().hex[:12]
        if _direct_mode:
            # 直出模式不需要帧服务器：页面自己合成上屏，零 readPixels / 零 HTTP。
            _frame_port = 0
            frame_server = None
            self._frame_server = None
        else:
            frame_server = FrameServer(_accept_frame)
            _frame_port = frame_server.start(token=_frame_token)
            self._frame_server = frame_server

        def _on_frame_raw(data, w, h, cw=0, ch=0, ox=0, oy=0, seq=0):
            try:
                pet.setFrameRaw(data, w, h, cw, ch, ox, oy)
                self.frame_count += 1
                _quality_tick()
            finally:
                # 必须在 Qt 线程内 ack：HTTP 线程等到这里才回 204，渲染器才会推下一帧。
                fs = getattr(self, "_frame_server", None)
                if fs is not None:
                    fs.ack(seq)
            # 诊断：滚动保存最近 10 帧（含帧号与时间），用于看"当前"实际像素
            if _DUMP_FRAMES and self.frame_count % 15 == 0:
                try:
                    os.makedirs(_FRAME_DUMP_DIR, exist_ok=True)
                    img = QImage(bytes(data), w, h, w * 4,
                                 QImage.Format_RGBA8888_Premultiplied).mirrored(False, True)
                    img.save(os.path.join(_FRAME_DUMP_DIR, "last.png"))
                    slot = _FRAME_DUMP_STATE["n"] % 10
                    _FRAME_DUMP_STATE["n"] += 1
                    files = _FRAME_DUMP_STATE.setdefault("files", {})
                    old = files.get(slot)
                    if old and os.path.exists(old):
                        try:
                            os.remove(old)
                        except Exception:
                            pass
                    path = os.path.join(_FRAME_DUMP_DIR, "s%02d_f%06d_%dx%d.png"
                                        % (slot, self.frame_count, w, h))
                    img.save(path)
                    files[slot] = path
                    with open(os.path.join(_FRAME_DUMP_DIR, "last.txt"), "w",
                              encoding="utf-8") as _f:
                        _f.write("%d %dx%d (%dx%d+%d+%d) %s"
                                 % (self.frame_count, w, h, cw, ch, ox, oy,
                                    time.strftime("%H:%M:%S")))
                except Exception:
                    pass

        bridge.frameRaw.connect(_on_frame_raw)

        # === 加载提示泡泡的控制 ===
        # 模型加载/切换完成 → 隐藏泡泡；15s 兜底超时（防止 JS 回调意外丢失）
        _loading_timeout = QTimer()
        _loading_timeout.setSingleShot(True)

        def _snap_render_state():
            """诊断：就绪后把页面内 toDataURL 快照 + GL 状态落盘（每次覆盖）。

            碎块只能靠现场取证区分「渲染本身错」还是「推帧读错」：
            snap_ready.png = 页面自己导出的画面，_frames/last.png = readPixels 帧。
            """
            def _cb(s):
                try:
                    d = json.loads(s) if isinstance(s, str) and s else {}
                except Exception:
                    return
                url = d.pop("dataUrl", "")
                print("[render] snap", json.dumps(d, ensure_ascii=False))
                if url and "," in url:
                    try:
                        os.makedirs(_FRAME_DUMP_DIR, exist_ok=True)
                        with open(os.path.join(_FRAME_DUMP_DIR, "snap_ready.png"), "wb") as f:
                            f.write(base64.b64decode(url.split(",", 1)[1]))
                    except Exception:
                        pass
            try:
                page.runJavaScript(
                    "(function(){try{var gl=app.renderer.gl;"
                    "var n=-1;try{var m=app.stage.children[0];"
                    "n=m.internalModel.coreModel.getDrawableCount()}catch(e){}"
                    "return JSON.stringify({ctx:app.renderer.CONTEXT_UID,"
                    "err:gl.getError(),lost:gl.isContextLost(),kids:app.stage.children.length,"
                    "drawables:n,cw:app.view.width,ch:app.view.height,"
                    "dataUrl:app.view.toDataURL('image/png')});"
                    "}catch(e){return ''}})()", _cb)
            except Exception:
                pass

        def _fit_window_to_model():
            """模型就绪后读取包围盒，把 Stage 宽度补到模型宽度（高度保持固定）。"""
            if not _direct_mode or page is None:
                return

            def _cb(res):
                try:
                    data = json.loads(res) if isinstance(res, str) else (res or {})
                    if not isinstance(data, dict) or data.get("fallback"):
                        return
                    mw, mh = float(data.get("w") or 0), float(data.get("h") or 0)
                    if mw <= 1 or mh <= 1:
                        return
                    # 记录模型可见矩形（归一化到整块画布），供 Qt 覆盖层裁鼠标命中区。
                    try:
                        mx = float(data.get("x") or 0)
                        my = float(data.get("y") or 0)
                        cw = float(data.get("cw") or 0) or float(pet.width())
                        ch = float(data.get("ch") or 0) or float(pet.height())
                        if cw > 1 and ch > 1:
                            state["model_rect_norm"] = (
                                mx / cw, my / ch, mw / cw, mh / ch)
                            _WSTATE["model_rect_norm"] = state["model_rect_norm"]
                            _stq = state.get("stage_rect")
                            if _stq and len(_stq) >= 4:
                                state["model_rect_stage"] = (int(_stq[0]), int(_stq[1]))
                                _WSTATE["model_rect_stage"] = state["model_rect_stage"]
                            state["_mask_dirty"] = True
                    except (TypeError, ValueError):
                        pass
                    state["model_aspect"] = mw / mh
                    need = max(int(BASE_W * state["scale"]),
                               int(BASE_H * state["scale"] * state["model_aspect"] * 0.95) + 24)
                    if abs(need - int(pet.stage_rect()[2])) < 4:
                        return
                    # 回调在 Electron 桥线程里，不能直接动 Qt 控件；
                    # 记下目标宽度，交给主线程的 _poll_cmds 应用。
                    state["pending_model_width"] = int(need)
                except (TypeError, ValueError, RuntimeError):
                    pass

            try:
                page.runJavaScript(
                    f"setPetCommand({json.dumps({'type': 'getModelRect'})})", _cb)
            except Exception:
                pass

        def _request_alpha_mask():
            """取一帧模型 alpha 剪影，供 Qt 覆盖层做逐块鼠标命中区。

            只在模型就绪/切换后各抓一次；不进入每帧显示链路。失败时
            保留包围盒命中，不会让模型变成完全点不动。

            抓帧会做一次 canvas readPixels，和「重建双击热区」里的截图是
            同一个渲染器上的重活：**两路抓帧并发时 Electron 侧会连带把
            worker 一起带崩**（实测 full 模式松手 0.4s 后进程静默消失，
            Qt5Core 0xC0000409，日志无栈）。所以这里登记 `_alpha_busy`，
            让另一路等它回包后再发起。
            """
            if not _direct_mode or page is None:
                return

            def _alpha_retry():
                """抓帧失败（页面刚重建、GL 上下文还没画过一帧）→ 过一会儿再抓。

                旧实现只在"模型就绪"那一刻抓一次，抓不到就永远留着一份**上一次
                的**剪影：模型/几何都换了，命中区却还是旧轮廓 —— 表现就是模型上
                大片看得见的部位鼠标穿透。失败重试几次，保证每只新模型最终都有
                自己的剪影。
                """
                try:
                    _try = int(state.get("_alpha_try") or 0) + 1
                except (TypeError, ValueError):
                    _try = 1
                state["_alpha_try"] = _try
                if _try > 6 or not state.get("ready"):
                    return
                try:
                    _hit_refresh_timer.start(600)     # 防抖计时器会重新走一次抓帧
                    state["_geom_stale"] = True
                except Exception:
                    pass

            def _cb(res):
                try:
                    data = json.loads(res) if isinstance(res, str) else (res or {})
                    if not isinstance(data, dict) or not data.get("ok"):
                        _alpha_retry()
                        return
                    bits = data.get("bits")
                    if not isinstance(bits, list) or not bits:
                        _alpha_retry()
                        return
                    _stg = data.get("stage")
                    try:
                        _stg = [int(round(float(v))) for v in list(_stg)[:4]]
                    except (TypeError, ValueError):
                        _stg = None
                    state["alpha_mask"] = {
                        "cols": int(data.get("cols") or 0),
                        "rows": int(data.get("rows") or 0),
                        "bits": bits,
                        # 抓帧那一刻的 Stage：之后拖动/缩放要按它把网格搬到现在位置，
                        # 否则剪影会停在旧几何上（点上去就穿透）。
                        # **必须用渲染层回传的值，不能在这里重读 stage_rect()**：
                        # 快速来回拖动时回包已经晚于抓帧，这十几像素的差会让整块
                        # 命中区偏移（松手后模型看得见、却点不到也右键不到）。
                        "stage": (_stg if (_stg and len(_stg) == 4 and _stg[2] > 1)
                                  else list(pet.stage_rect())),
                        # 这份剪影属于哪一个模型：换模型后旧剪影必须作废，
                        # 否则会把命中区裁成上一只模型的轮廓（可见部位点不动）。
                        "model": str(state.get("model_url") or ""),
                    }
                    state["alpha_mask_ver"] = int(state.get("alpha_mask_ver") or 0) + 1
                    state["_mask_dirty"] = True
                    state["_alpha_try"] = 0
                except (TypeError, ValueError):
                    _alpha_retry()
                finally:
                    state["_alpha_busy"] = False

            state["_alpha_busy"] = True
            try:
                # 精细一点：96×100（每格 20×10.8px）在 1080p 上会把发梢、手指、
                # 裙摆细条整格抹掉，命中区就漏边 -> 点上去穿透到下面的窗口。
                # 200×160（每格 9.6×6.75px）够细，JSON 也只有 32KB 左右。
                page.runJavaScript(
                    f"setPetCommand({json.dumps({'type': 'getAlphaMask', 'cols': 200, 'rows': 160})})",
                    _cb)
            except Exception:
                state["_alpha_busy"] = False

        def _on_model_ready():
            _loading_timeout.stop()
            pet.hide_loading()
            # 模型(重新)打开 / 切换：字幕回默认位置。拖动记下的相对偏移只属于
            # "上一次打开"，不跨模型、不跨次沿用（用户要求：每次打开都在默认位置）。
            # 同时**整条字幕一起收掉**：旧代码只清几何、留着文本，于是新模型就绪后
            # 掩膜换成新剪影，而那条旧字还按旧位置重绘 —— 落在剪影外的部分被
            # setMask 裁掉（"字幕被剪影"），又因为文本还在、到期时间还远，
            # 它就一直挂在那儿（"字幕不消失"）。
            try:
                pet._sub_rel = None
                pet._sub_clear()
            except Exception:
                pass
            self._model_ready = True
            state["ready"] = True          # 交互解禁：只有这一之后才接受拖动/滚轮
            self._model_error = None
            self._model_ready_event.set()
            # 首次加载也要按模型包围盒补窗宽，否则宽模型会被窗口左右裁掉。
            try:
                _fit_window_to_model()
            except Exception:
                pass
            # 包围盒拿到后再抓一次 alpha 剪影，把模型轮廓内的空白也排除掉。
            try:
                _request_alpha_mask()
            except Exception:
                pass
            # 切换模型 = 重载渲染器文档：新文档默认 push=on、画布=视图尺寸，
            # 这里按当前可见性/缩放重新对齐（隐藏中的窗口不该空转推帧）
            if getattr(self, "_reload_pending_push", False):
                self._reload_pending_push = False
                try:
                    # 以窗口此刻的真实可见性为准，不能用创建页面时的快照：
                    # 首次 show 时页面往往还没加载完，快照可能是 False，
                    # 于是模型就绪后把刚打开的页面停推，表现为 frames 一直为 0。
                    vis = bool(pet.isVisible())
                    self._visible = vis
                    _set_push(vis)
                except Exception:
                    pass
                try:
                    _sync_render_size(state["scale"])
                except Exception:
                    pass
                try:
                    _sync_direct_geometry()
                except Exception:
                    pass
                try:
                    _fit_window_to_model()
                except Exception:
                    pass
                # 切模型/重新适配窗口后，Electron 模型窗可能重新压到覆盖层上面；
                # 统一抬一次，保证字幕/加载泡泡/热区提示始终可见。
                try:
                    pet.raise_()
                except Exception:
                    pass
            # 诊断：就绪 2.5s 后存一张「页面内 toDataURL」快照 + GL 状态。
            # 现场碎块时用它和 _frames/last.png（readPixels 帧）对比，即可判断
            # 是渲染本身错，还是推帧读错。
            QTimer.singleShot(2500, _snap_render_state)
            # 就绪那一刻量到的包围盒可能还是模型内部画布（正方形）的盒子：
            # 尺寸一落地就要再量一次，Stage 宽度才会按真实宽高比扩展，
            # 不给"模型比 Stage 宽、超出的地方点不到"留窗口期。
            for _ms in (900, 2200):
                QTimer.singleShot(_ms, lambda: _fit_window_to_model())
            # 右键菜单「播放动画」/ 双击部位判定都依赖动作清单，就绪后缓存一份。
            # 直出模式没有再等 400ms 的必要：模型已 ready，立即拉一次资源清单，
            # 触发 clickmap（热区）构建；异步回调，不阻塞主线程。
            try:
                _refresh_assets()
            except Exception:
                pass
            # 换装通道：换模型后必须重扫（纯读文件，几毫秒），否则菜单里还挂着
            # 上一个模型的通道；扫不到就不显示「换装」。
            try:
                _refresh_outfits()
            except Exception:
                pass

        def _on_model_error(message):
            _loading_timeout.stop()
            pet.hide_loading()
            self._model_error = str(message or "模型加载失败")
            self._model_ready = False
            self._model_ready_event.set()

        bridge.modelReady.connect(_on_model_ready)
        bridge.modelError.connect(_on_model_error)
        _loading_timeout.timeout.connect(pet.hide_loading)

        def _show_loading(text="加载中"):
            # 每次加载/切换都复位就绪状态，供 tool.py wait_model_ready 判断
            self._model_error = None
            self._model_ready = False
            self._model_ready_event.clear()
            # 交互护栏：没加载完之前，这个覆盖层不接受拖动/滚轮。
            # 否则整块屏幕（遮罩还没按剪影裁好）都算"点在模型上"，随便在哪拖一下
            # 就把还没画出来的模型挪走、滚一下就把它的缩放改了。
            state["ready"] = False
            # 旧模型的动画清单必须立刻作废：切模型后 menu 是同步读 state["assets"] 的，
            # 留着上一份会让"播放动画"显示别的模型的列表（或干脆还是加载中）。
            state["assets"] = None
            _WSTATE["assets_ready"] = False
            try:
                _assets_try["n"] = 0     # 首次调用时它在更后面定义，忽略即可
            except NameError:
                pass
            pet.show_loading(text)
            # 加载期命中区只留气泡那一块（见 _apply_interaction_mask 的 ready 分支）：
            # 必须立刻算一次，否则窗口还按上一份（或整窗）命中区接鼠标。
            try:
                pet._apply_interaction_mask()
            except Exception:
                pass
            _loading_timeout.start(15000)

        _show_loading("加载中")   # 初始模型加载提示

        channel = QWebChannel(page)
        channel.registerObject("bridge", bridge)
        page.setWebChannel(channel)

        def _set_push(on):
            """隐藏时停推、显示时恢复：省掉整条 readPixels+POST 链路的空转。"""
            _frames_enabled["v"] = bool(on)
            self._visible = bool(on)   # 供 tool.py 判断「模型当前是否可见」
            if _direct_mode:
                try:
                    if _electron_mode:
                        _ep = getattr(self, "_electron_page", None)
                        if _ep is not None:
                            _ep._post("/visible", {"on": bool(on)}, timeout=2.0)
                    elif _direct_child:
                        render_view.setGeometry(0, 0, pet.width(), pet.height())
                    else:
                        render_view.setGeometry(pet.geometry())
                    if not _electron_mode:
                        render_view.setVisible(bool(on))
                    if on and not _direct_child and not _electron_mode:
                        pet.raise_()   # 覆盖层保持在模型窗之上
                except Exception:
                    pass
            if page is None:           # 切换中的空窗（旧页已销毁、新页未就绪）
                return
            try:
                page.runJavaScript(
                    f"setPetCommand({json.dumps({'type': 'setPush', 'on': bool(on)})})")
            except Exception:
                pass

        def _sync_direct_geometry():
            """移动/缩放时同步模型窗；覆盖层始终在上。"""
            if not _direct_mode:
                return
            if _electron_mode:
                try:
                    _sync_layout()
                except Exception:
                    pass
                return
            try:
                if _direct_child:
                    render_view.setGeometry(0, 0, pet.width(), pet.height())
                else:
                    render_view.setGeometry(pet.geometry())
                if pet.isVisible():
                    render_view.show()
                    if not _direct_child:
                        pet.raise_()
            except Exception:
                pass

        # 窗口显示/隐藏（含右键"隐藏"、__close__）都同步帧推送/模型窗
        pet.on_visibility_change = _set_push
        pet.on_geometry_change = _sync_direct_geometry
        # 首帧 showEvent 发生在本行之前（pet.show() 在更早处调用），补一次同步，
        # 否则 is_visible() / 模型窗会一直停在初始状态。
        self._visible = pet.isVisible()
        _set_push(self._visible)

        # 加载 renderer.html，把 model URL + 帧端点端口通过 query 传入
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderer.html")

        def _renderer_url(model_url_arg, scale_arg):
            """renderer.html 的完整 URL（model / scale / framePort 走 query）。"""
            u = QUrl.fromLocalFile(html_path)
            u.setQuery(f"model={model_url_arg}&scale={scale_arg}&framePort={_frame_port}"
                       f"&ftoken={_frame_token}&direct={'1' if _direct_mode else '0'}")
            return u

        url = _renderer_url(model_url, scale)
        _cur_model = {"arg": model_url, "scale": scale}
        if _electron_mode:
            try:
                _log_path = os.path.join(
                    _WORKERS_DIR, "electron_%d.log" % os.getpid())
                _init_stage = tuple(int(v) for v in pet.stage_rect())
                _init_clip = ((screen.x(), screen.y(), screen.width(), screen.height())
                              if str(state.get("display_mode") or "local") == "full"
                              else _init_stage)
                _proc, _port = _start_electron_display(
                    model_url, screen.x(), screen.y(), screen.width(), screen.height(),
                    state["scale"], _log_path,
                    layout={"stage": _init_stage, "clip": _init_clip,
                            "mode": state.get("display_mode") or "local"})
                self._electron_proc = _proc
                self._electron_page = _ElectronPage(_port)
                page = self._electron_page
                pet._cmd_page = page
                state["send_cmd"] = self.send_command
                _log_line("INFO", "Electron 显示窗已启动 port=%d（全屏 %dx%d）"
                          % (_port, screen.width(), screen.height()))
                # Electron 首次创建会被 Windows 夹到工作区高度（任务栏 48px），
                # 必须在页面起来后显式 setBounds 到整块显示器，否则全屏窗口
                # 底部会缺一条、Stage 的 clip 也会跟着算错。
                def _force_full_bounds():
                    try:
                        _ep = getattr(self, "_electron_page", None)
                        if _ep is not None:
                            _ep.set_bounds(screen.x(), screen.y(),
                                           screen.width(), screen.height())
                    except Exception:
                        pass
                    _pin_electron_topmost()
                    # Electron 是后启动的，默认会压在 Qt 覆盖层上面；把 Qt
                    # raise 回最前，鼠标事件才会落到带剪影 mask 的覆盖层，
                    # 同时 Electron 模型仍从透明区透出来。
                    try:
                        if pet.isVisible():
                            pet.raise_()
                            _pin_qt_topmost()
                    except Exception:
                        pass
                for _ms in (0, 250, 900, 1800):
                    QTimer.singleShot(_ms, _force_full_bounds)
                # 全屏窗口几何固定，只补发 Stage 布局；不再逐次 setBounds。
                for _ms in (0, 300, 900, 1800):
                    QTimer.singleShot(_ms, _sync_layout)
                # Electron 是独立顶层窗，原生透明覆盖层（字幕/加载泡泡/热区）
                # 必须压在它上面，否则会被模型窗的插画背景整块盖住。
                # Electron 进程刚起时窗口还没创建完，延迟两次抬升覆盖层。
                QTimer.singleShot(1200, pet.raise_)
                QTimer.singleShot(3500, pet.raise_)

                def _watch_electron_model():
                    """后台探测 Electron 页面里的 model；就绪后由主循环切换状态。"""
                    _gen = int(state.get("electron_watch_gen") or 0) + 1
                    state["electron_watch_gen"] = _gen

                    def _run():
                        deadline = time.monotonic() + 35.0
                        while time.monotonic() < deadline:
                            if state.get("electron_watch_gen") != _gen:
                                return
                            try:
                                res = page._post(
                                    "/eval",
                                    {"js": "typeof setPetCommand === 'function' && typeof model !== 'undefined' && model ? 1 : 0"},
                                    timeout=2.0)
                                if res.get("result"):
                                    if state.get("electron_watch_gen") == _gen:
                                        state["_electron_model_loaded"] = True
                                    return
                            except Exception:
                                pass
                            time.sleep(0.3)

                    threading.Thread(target=_run,
                                     name="electron-ready", daemon=True).start()

                _watch_electron_model()
            except Exception as e:
                self._model_error = "Electron 显示启动失败: %s" % e
                self._model_ready = False
                self._model_ready_event.set()
                _log_line("ERROR", "Electron 启动失败: %r" % (e,))
        else:
            render_view.load(url)

        # === 切换模型 = 让渲染页自己重建渲染器（Qt 侧对象全程不动）===
        # 两条已复现的死路：
        #   ① 同一 WebContents 内 page.load() 重载 → Chromium 合成器复用已回收的
        #      shared image（"non-existent mailbox"），第 2 次起画面碎块；
        #   ② 销毁 QWebEngineView / QWebEnginePage 再建新页 → Qt5Core fail-fast，
        #      进程直接被杀（0xc0000409，Windows 事件日志已证实两次）。
        # 所以切换只调页面内的 __rebuildRenderer：销毁「模型 + pixi Application +
        # canvas」后重建新 canvas 与新 WebGL 上下文。GL 状态每次干净（不碎），
        # Qt 对象一个都不销毁（不崩）。实验 F：同一页面连续 6 次重建全部正确。
        SWAP_SETTLE_MS = 600      # 重建后等模型就绪，再对齐一次画布
        _swap_gen = {"v": 0}

        def _swap_render_view(model_url_arg, scale_arg):
            """切换模型：发一条 JS 命令让渲染页重建渲染器。"""
            self._reload_pending_push = True
            self._model_ready = False
            self._model_ready_event.clear()
            state["_electron_model_loaded"] = False
            _cur_model["arg"] = model_url_arg
            _cur_model["scale"] = scale_arg
            state["model_url"] = str(model_url_arg)
            # 换模型 = 剪影/格子全部作废。它们是**上一只模型**的形状：
            # 旧的 alpha 网格 + 旧的热区格子留着，只会被搬成新模型的轮廓，
            # 命中区就缩成一小团，新模型上大片看得见的部位点下去会穿透到桌面
            # （这正是"模型一些可见部分鼠标穿透"的主因）。清空后由
            # _refresh_hit_geometry → _request_alpha_mask / _build_click_map
            # 重新抓一帧属于新模型的剪影。
            state["alpha_mask"] = None
            state["alpha_mask_ver"] = int(state.get("alpha_mask_ver") or 0) + 1
            state["_alpha_try"] = 0
            state["_cm_cells"] = 0
            state["_cm_model"] = ""
            state["_cm_stage"] = None
            state["_cm_seq"] = int(state.get("_cm_seq") or 0) + 1
            state["_mask_dirty"] = True
            state["_geom_stale"] = True
            try:
                _apply_model_display_mode(model_url_arg)
            except Exception:
                pass
            _show_loading("切换模型中")
            _swap_gen["v"] += 1
            gen = _swap_gen["v"]
            if page is None:
                self._model_error = "渲染页不存在，请重启桌宠"
                self._model_ready = False
                self._model_ready_event.set()
                return
            try:
                page.runJavaScript(
                    "window.__rebuildRenderer(%s); 1" % json.dumps(model_url_arg))
                if _electron_mode:
                    _watch_electron_model()
            except Exception as e:
                self._model_error = f"切换失败: {e}"
                self._model_ready = False
                self._model_ready_event.set()
                return

            def _later():
                if gen != _swap_gen["v"]:
                    return                  # 期间又切了模型，这次的结果作废
                try:
                    _sync_render_size(state["scale"])
                except Exception:
                    pass
            QTimer.singleShot(SWAP_SETTLE_MS, _later)

        def _reload_renderer():
            """重建渲染器（页面内重建 GL 层，用于画面异常时恢复）。

            注意：这不会重新加载 renderer.html 的源码 —— 改过前端代码后必须重启
            插件才生效（重载文档会踩合成器 shared image 的坑，画面必碎）。
            """
            try:
                _exit_part_mode()
            except Exception:
                pass
            try:
                _swap_render_view(_cur_model["arg"], _cur_model["scale"])
            except Exception as e:
                print("[render] 重建渲染器失败:", e)

        # === 缩放几何：全屏窗口固定，只动 Stage ===
        # 每一帧只改 Stage 的屏幕矩形并下发 setLayout；OS 窗口从不 resize，
        # 因此不存在窗口位置/内容尺寸异步追赶导致的“偏移后回弹”。
        # anchor=None → 保持 Stage 中心固定（智能体控制缩放用）
        # anchor=QPoint → 保持该屏幕点固定（滚轮缩放以鼠标为基点）
        def _scale_geom_for(s, anchor, frac, geom0):
            """按缩放值/锚点算目标 Stage 几何（屏幕坐标）。"""
            s = max(MIN_SCALE, min(MAX_SCALE, s))
            old_x, old_y, old_w, old_h = geom0
            w, h = int(BASE_W * s), int(BASE_H * s)
            if _direct_mode:
                try:
                    _aspect = state.get("model_aspect")
                    if _aspect:
                        w = max(w, int(BASE_H * s * float(_aspect) * 0.95) + 16)
                except (TypeError, ValueError):
                    pass
            if anchor is None:
                ax = old_x + old_w // 2
                ay = old_y + old_h // 2
                fx, fy = 0.5, 0.5
            else:
                ax, ay = anchor.x(), anchor.y()
                if frac is not None:
                    fx, fy = frac
                else:
                    fx = (ax - old_x) / old_w if old_w else 0.5
                    fy = (ay - old_y) / old_h if old_h else 0.5
            px = int(round(ax - fx * w))
            py = int(round(ay - fy * h))
            if anchor is None:
                if w <= screen.width():
                    px = max(screen.x(), min(px, screen.x() + screen.width() - w))
                else:
                    px = int(screen.x() + (screen.width() - w) / 2)
                if h <= screen.height():
                    py = max(screen.y(), min(py, screen.y() + screen.height() - h))
                else:
                    py = int(screen.y() + (screen.height() - h) / 2)
            else:
                m = 80
                px = max(screen.x() - w + m,
                         min(px, screen.x() + screen.width() - m))
                py = max(screen.y() - h + m,
                         min(py, screen.y() + screen.height() - m))
            return (px, py, w, h)

        def _apply_scale_geom(s, anchor=None, notify=True, frac=None):
            s = max(MIN_SCALE, min(MAX_SCALE, s))
            state["scale"] = s
            g = _scale_geom_for(s, anchor, frac, pet.stage_rect())
            _set_stage_rect(g[0], g[1], g[2], g[3])
            # 仅动画结束时通知 tool.py 同步最终缩放
            if notify and self.on_scale_change:
                self.on_scale_change(s)

        def _render_size_for(s):
            """目标缩放的理想渲染画布尺寸（见 _fit_render_size：只超采样不降采样）。"""
            w, h = int(BASE_W * s), int(BASE_H * s)
            return _fit_render_size(w, h)

        def _sync_render_size(s):
            """动画结束后把画布一次性对齐到当前缩放。

            全屏直出画布固定为显示器尺寸，不再需要独立调渲染缓冲。
            frame 回退模式仍保留原逻辑。"""
            if _direct_mode:
                return
            if page is None:      # 切换空窗：新页建好后由 _on_model_ready 再对齐
                return
            _rw, _rh = _render_size_for(s)
            _prev = state.get("cur_render")
            if _prev and abs(_rw - _prev[0]) <= max(2, _prev[0] * 0.02) \
                    and abs(_rh - _prev[1]) <= max(2, _prev[1] * 0.02):
                return
            state["cur_render"] = (_rw, _rh)
            try:
                page.runJavaScript(
                    f"setPetCommand({json.dumps({'type': 'syncCanvas', 'w': _rw, 'h': _rh})})")
            except Exception:
                pass

        # === 帧率观测（只统计，不改画布） ===
        # 旧版按 fps 在 0.70~1.0 之间调画布倍率，倍率 <1 就是"模型发虚"的直接原因，
        # 而且每次调整都要重建渲染缓冲。清晰度现在是硬下限，掉帧靠缩小回传区域解决，
        # 所以这里只保留 fps 观测，供 tool.py / 日志取证。
        _fps_stat = {"n": 0, "t0": time.monotonic()}

        def _quality_tick():
            if not pet.isVisible():
                _fps_stat["n"] = 0
                _fps_stat["t0"] = time.monotonic()
                return
            _fps_stat["n"] += 1
            dt = time.monotonic() - _fps_stat["t0"]
            if dt > 3.0:
                # 采样中断（隐藏恢复/模型切换停推帧）：重置窗口
                _fps_stat["n"] = 0
                _fps_stat["t0"] = time.monotonic()
                return
            if dt < 2.0:
                return
            self.fps = _fps_stat["n"] / dt
            _fps_stat["n"] = 0
            _fps_stat["t0"] = time.monotonic()


        # === 平滑缩放动画（easeInOutCubic 插值，避免突变）===
        # 命令（show/setScale）用 200ms；滚轮用更短的 140ms 并累积目标，连续滚动时
        # 始终从「当前值」续接到新目标，不会一顿一顿地跳。
        anim = {"active": False, "from": state["scale"], "to": state["scale"],
                "anchor": None, "frac": None, "start": 0.0, "dur": 0.20,
                "n": 12, "i": 0}

        scale_timer = QTimer()

        def _animate_scale_to(to, anchor=None, dur=0.20):
            """从当前缩放平滑动画到目标缩放（滚轮/命令共用）。"""
            to = max(MIN_SCALE, min(MAX_SCALE, to))
            if abs(to - state["scale"]) < 0.004 and not anim["active"]:
                return
            anim["from"] = state["scale"]
            anim["to"] = to
            anim["anchor"] = anchor
            sx0, sy0, sw0, sh0 = pet.stage_rect()
            if anchor is not None:
                anim["frac"] = (((anchor.x() - sx0) / sw0) if sw0 else 0.5,
                                ((anchor.y() - sy0) / sh0) if sh0 else 0.5)
            else:
                anim["frac"] = (0.5, 0.5)
            anim["dur"] = max(0.08, float(dur))
            anim["start"] = time.monotonic()
            anim["geom0"] = (sx0, sy0, sw0, sh0)
            # "上一拍下发的几何" + "用户额外拖出来的位移（drift）"：动画的尺寸由
            # 缩放决定，但位置必须保留用户在动画期间拖出来的那段位移，否则拖动
            # 会被下一拍覆盖（= 缩放中/缩放后拖不动）。
            anim["last"] = (sx0, sy0, sw0, sh0)
            anim["drift"] = (0, 0)
            anim["n"] = max(6, int(round(anim["dur"] / 0.020)))
            anim["i"] = 0
            anim["active"] = True
            if not scale_timer.isActive():
                scale_timer.start(16)  # ~60fps 步进

        def _scale_tick():
            if not anim["active"]:
                return
            n = max(1, int(anim.get("n") or 12))
            anim["i"] = min(n, int(anim.get("i") or 0) + 1)
            k = anim["i"] / float(n)
            k2 = k * k
            ease = 3 * k2 - 2 * k * k2  # easeInOutCubic：平滑起步+平滑收尾
            s = anim["from"] + (anim["to"] - anim["from"]) * ease
            final = anim["i"] >= n
            state["scale"] = s
            g = _scale_geom_for(s, anim["anchor"], anim.get("frac"), anim["geom0"])
            # 用户在这一拍之前把 Stage 拖走了多少（纯平移分量）：缩放动画的目标
            # 几何是从 geom0 算的，不补这段位移，拖动就会被覆盖。
            _last = anim.get("last")
            _dr = anim.get("drift") or (0, 0)
            if _last is not None:
                try:
                    _cur = pet.stage_rect()
                    _dxm = float(_cur[0]) - float(_last[0])
                    _dym = float(_cur[1]) - float(_last[1])
                    if abs(_dxm) < 4000 and abs(_dym) < 4000:
                        _dr = (float(_dr[0]) + _dxm, float(_dr[1]) + _dym)
                        anim["drift"] = _dr
                except Exception:
                    _dr = anim.get("drift") or (0, 0)
            if _dr[0] or _dr[1]:
                g = (int(round(g[0] + _dr[0])), int(round(g[1] + _dr[1])), g[2], g[3])
            _set_stage_rect(g[0], g[1], g[2], g[3])
            anim["last"] = (int(g[0]), int(g[1]), int(g[2]), int(g[3]))
            if final:
                anim["active"] = False
                scale_timer.stop()
                if self.on_scale_change:
                    self.on_scale_change(s)
                _sync_render_size(s)
                # 缩放收尾补一次包围盒刷新（纯算术、不抓帧）：之后的贴边夹取
                # 和拖动粗掩膜都要用新的模型矩形。
                try:
                    _request_model_rect()
                except Exception:
                    pass

        def _finish_scale_anim():
            """把正在跑的缩放缓动立刻定格到目标值（按下/松手时调用）。"""
            if not anim.get("active"):
                return
            anim["i"] = max(1, int(anim.get("n") or 12))
            try:
                _scale_tick()
            except Exception:
                pass

        state["finish_scale_anim"] = _finish_scale_anim

        scale_timer.timeout.connect(_scale_tick)

        # === 滚轮缩放：累积目标 + 短过渡动画（等比步进，大倍率下手感一致）===
        # 动画期间画布不动（只在结束时一次性 syncCanvas），所以逐帧改窗口不会像旧版
        # 那样因异步画布 resize 产生反向抖动。
        _scale_sync_timer = QTimer()
        _scale_sync_timer.setSingleShot(True)
        _scale_sync_timer.timeout.connect(lambda: _sync_render_size(state["scale"]))

        def _scale_wheel(delta, anchor):
            """滚轮缩放：一格 = ×1.12 / ÷1.12，140ms 过渡，连续滚动自动续接。
            以鼠标位置为锚点：鼠标下的那一点保持不动。"""
            base = anim["to"] if anim["active"] else state["scale"]
            factor = 1.12 if delta > 0 else (1.0 / 1.12)
            to = max(MIN_SCALE, min(MAX_SCALE, base * factor))
            if abs(to - state["scale"]) < 0.004 and not anim["active"]:
                return
            # 模型就绪时可能还挂着"按包围盒补宽"的待办；缩放期间先丢掉，
            # 否则它会在动画中途把宽度改一次，造成横向跳一下。
            state.pop("pending_model_width", None)
            _wid_anim["active"] = False     # 补宽缓动也停掉，几何交给缩放动画
            _animate_scale_to(to, anchor, dur=0.14)
            _scale_sync_timer.start(220)   # 兜底：滚动停手后对齐画布分辨率

        # === 鼠标跟随（眼睛/头部追踪鼠标，类似桌宠软件）===
        def _toggle_track(checked):
            state["track"] = bool(checked)
            save_ui_cfg(track=bool(checked))
            state["was_near"] = False
            # 关闭跟随时发送零目标，JS 会平滑复位
            if not checked:
                state["track_look"] = (0.0, 0.0)
                _send_track_params(0, 0)

        def _send_track_params(dx, dy):
            """把鼠标偏移换算成眼睛/头部/身体参数，发送目标值给 JS。
            JS 用每帧指数平滑逼近目标（不重启 tween），因此平滑度与 FPS 无关。
            """
            # 分层幅度：眼球跟随最灵敏，头部次之，身体最小（有重量感、更自然）
            params = {
                "ParamEyeBallX": round(dx * 1.0, 4),       # 眼球左右
                "ParamEyeBallY": round(-dy * 1.0, 4),      # 眼球上下
                "ParamAngleX": round(dx * 16, 2),          # 头部左右转
                "ParamAngleY": round(-dy * 12, 2),         # 头部上下点
                "ParamAngleZ": round(-dx * dy * 4, 2),     # 头部轻微侧倾（斜向看时）
                "ParamBodyAngleX": round(dx * 7, 2),       # 身体左右扭转
                "ParamBodyAngleZ": round(dx * 3, 2),       # 身体侧倾
            }
            # 记下"这一次真正发出去的注视方向"。说话微动作每 50ms 会拿它重发一遍，
            # 这里不更新就会用上一次的残留值 —— 表现是「一开口头和眼睛就固定歪向一边」：
            # 鼠标早就不在那个方向了，微动作却把旧方向一直喂回去。
            state["track_look"] = (float(dx), float(dy))
            # 叠加说话时的程序化微动作（偏移量，由 TTS 服务每 50ms 推一次）。
            # 只叠加不覆盖：跟随仍然是主体，微动作只是"活着"的细节。
            for k, v in (state.get("gesture") or {}).items():
                try:
                    params[k] = round(float(params.get(k) or 0.0) + float(v), 3)
                except (TypeError, ValueError):
                    continue
            cmd = {"type": "setTrackTarget", "params": params}
            page.runJavaScript(
                f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
            )

        # 让 TTS 服务能"改完偏移量立刻重发一次目标"，不必等鼠标再动
        state["send_track"] = _send_track_params
        state["track_look"] = (0.0, 0.0)

        # === 程序化动作（模型没有 motion 文件时的兜底）===
        # 每个动作 = 几段偏移量序列，200ms 一步，最后一步清空回到基准。
        _PROC = {
            "nod":   [{"ParamAngleY": -4.0}, {"ParamAngleY": 1.0}, {}],
            "shake": [{"ParamAngleX": 5.0}, {"ParamAngleX": -5.0}, {}],
            "tilt":  [{"ParamAngleZ": 5.0}, {"ParamAngleZ": 5.0}, {}],
            "sway":  [{"ParamBodyAngleX": 3.5}, {"ParamBodyAngleX": -3.5}, {}],
            "point": [{"ParamBodyAngleZ": 3.0, "ParamAngleX": 4.0},
                      {"ParamBodyAngleZ": 3.0, "ParamAngleX": 4.0}, {}],
        }
        _proc_seq = {"token": 0}

        def _play_proc(name):
            steps = _PROC.get(name)
            if not steps:
                return False
            _proc_seq["token"] += 1
            token = _proc_seq["token"]

            def _step(i):
                if token != _proc_seq["token"]:
                    return                      # 被新动作/配音打断，旧的序列作废
                if i >= len(steps):
                    state["gesture"] = {}
                    _resend()
                    return
                state["gesture"] = dict(steps[i])
                _resend()
                QTimer.singleShot(200, lambda: _step(i + 1))

            _step(0)
            return True

        def _resend():
            send = state.get("send_track")
            if send is None:
                return
            try:
                lx, ly = state.get("track_look") or (0.0, 0.0)
                send(float(lx), float(ly))
            except Exception:
                pass

        state["play_proc"] = _play_proc

        # 目标值 Python 侧一阶低通（抑制光标抖动/高频小位移），JS 侧再做分层平滑
        _track_smooth = {"x": 0.0, "y": 0.0, "sent": (None, None),
                         "t": 0.0, "sent_t": 0.0}

        def _smooth_track(dx, dy):
            """低通 + 发送节流。

            节流是关键：runJavaScript 的跟踪命令和帧回传（readPixels + POST）
            走同一个渲染线程的 IPC 队列。原来按 50Hz 推目标，掉帧时命令排在
            帧后面，表现就是"眼睛转过去总是慢半拍"。30Hz 足够驱动 JS 侧的
            逐帧指数插值（插值按时间推进，与命令频率无关）。
            """
            now = time.monotonic()
            last = _track_smooth["t"] or (now - 0.02)
            k = 1.0 - math.exp(-max(0.001, now - last) / 0.055)   # τ≈55ms
            k = max(0.12, min(0.90, k))
            _track_smooth["t"] = now
            _track_smooth["x"] += (dx - _track_smooth["x"]) * k
            _track_smooth["y"] += (dy - _track_smooth["y"]) * k
            sx, sy = _track_smooth["x"], _track_smooth["y"]
            state["track_look"] = (sx, sy)     # 微动作叠加时要用同一份注视方向
            if now - _track_smooth["sent_t"] < 0.033:
                return
            px, py = _track_smooth["sent"]
            # 目标几乎没变就不打扰 JS（JS 仍在自己插值逼近旧目标）
            if px is not None and abs(sx - px) < 0.006 and abs(sy - py) < 0.006:
                return
            _track_smooth["sent"] = (sx, sy)
            _track_smooth["sent_t"] = now
            _send_track_params(sx, sy)

        def _follow_tick():
            if not state["track"]:
                return
            if not pet.isVisible():
                return
            if getattr(pet, "_dragging", False):
                return          # 拖拽时鼠标就是"手"，别再让头追手
            t = QCursor.pos()
            # mapFromGlobal 处理高 DPI 坐标转换
            local = pet.mapFromGlobal(t)
            # Stage 局部矩形（全屏窗口内）；跟随以模型 Stage 为中心，不是整屏。
            sx, sy, sw, sh = pet.stage_local_rect()
            nr_x = max(300, sw // 2)
            nr_y = max(300, sh // 2)
            near = (sx - nr_x <= local.x() <= sx + sw + nr_x and
                    sy - nr_y <= local.y() <= sy + sh + nr_y)
            if near:
                state["was_near"] = True
                # 归一化偏移（-1~1），以 Stage 半宽/半高为分母
                hw = max(1, sw // 2)
                hh = max(1, sh // 2)
                dx = max(-1.0, min(1.0, (local.x() - (sx + hw)) / hw))
                dy = max(-1.0, min(1.0, (local.y() - (sy + hh)) / hh))
                _smooth_track(dx, dy)
            elif state["was_near"]:
                # 鼠标离开：平滑复位到正视前方（清零平滑器，避免回位中途被旧值拖住）
                state["was_near"] = False
                _track_smooth["x"] = 0.0
                _track_smooth["y"] = 0.0
                _track_smooth["sent"] = (0.0, 0.0)
                _send_track_params(0, 0)

        def _combined_tick():
            _follow_tick()
            _avoid_tick()

        follow_timer = QTimer()
        follow_timer.timeout.connect(_combined_tick)
        follow_timer.start(20)  # ~50fps（跟随目标推送 + 避让位置动画）

        # === 鼠标避让（标记原始占据区域，鼠标进入则让开，离开后回位）===
        # 推开后鼠标与模型边缘的安全间距（越大越"躲得干净"，也越容易被屏幕边限制）
        AVOID_PUSH_MARGIN = 24
        MOVE_ANIM_DUR = 0.18     # 移动动画时长（秒）

        move_anim = {"active": False, "from": (0, 0), "to": (0, 0),
                     "start": 0.0, "on_done": None}

        def _animate_move_to(tx, ty, on_done=None):
            """平滑移动 Stage 到目标屏幕坐标（窗口不动）。"""
            sx, sy, sw, sh = pet.stage_rect()
            if abs(tx - sx) < 2 and abs(ty - sy) < 2:
                return
            move_anim["from"] = (sx, sy)
            move_anim["to"] = (tx, ty)
            move_anim["start"] = time.monotonic()
            move_anim["active"] = True
            move_anim["on_done"] = on_done

        def _move_tick():
            if not move_anim["active"]:
                return
            # 右键菜单展开期间窗口不能动：菜单是弹在固定屏幕坐标上的，
            # 窗口一挪菜单就跟脱开，用户会觉得"点了没反应"。
            if state.get("menu_open"):
                return
            dur = MOVE_ANIM_DUR
            k = min(1.0, (time.monotonic() - move_anim["start"]) / dur)
            k2 = k * k
            ease = 3 * k2 - 2 * k * k2  # easeInOutCubic
            fx, fy = move_anim["from"]
            tx, ty = move_anim["to"]
            cx = int(round(fx + (tx - fx) * ease))
            cy = int(round(fy + (ty - fy) * ease))
            sx, sy, sw, sh = pet.stage_rect()
            _set_stage_rect(cx, cy, sw, sh)
            final = k >= 1.0
            if final:
                move_anim["active"] = False
                cb = move_anim["on_done"]
                move_anim["on_done"] = None
                if cb:
                    cb()

        move_timer = QTimer()
        move_timer.timeout.connect(_move_tick)

        def _clamp_pos(x, y, vr=None):
            """把 Stage 位置限制在屏幕内，默认保证整个 Stage 可见；
            提供 vr=(lx,ly,w,h) 则保证**模型可见部分**完整在屏幕内。
            """
            _sx, _sy, w, h = pet.stage_rect()
            if vr:
                # 模型可见部分的屏幕 rect = (x+lx, y+ly, w, h)
                lx, ly, vw, vh = vr
                x = max(screen.x() - lx,
                        min(x, screen.x() + screen.width() - lx - vw))
                y = max(screen.y() - ly,
                        min(y, screen.y() + screen.height() - ly - vh))
            else:
                x = max(screen.x(), min(x, screen.x() + screen.width() - w))
                y = max(screen.y(), min(y, screen.y() + screen.height() - h))
            return x, y

        def _notify_pos():
            """通知 tool.py 当前宠物位置（Stage 归一化坐标）。"""
            if self.on_pos_change:
                _sx, _sy, w, h = pet.stage_rect()
                sx = screen.width() - w
                sy = screen.height() - h
                nx = (_sx - screen.x()) / sx if sx else 0.5
                ny = (_sy - screen.y()) / sy if sy else 0.5
                # 允许模型移出屏幕，归一化位置不再锁死在 0~1；
                # 留一个较宽的安全范围，避免异常坐标把模型彻底拖丢。
                nx = max(-1.0, min(2.0, nx))
                ny = max(-1.0, min(2.0, ny))
                self.on_pos_change(nx, ny)

        # === ZoneMarker：开启避让时在桌宠**可见部分**位置创建的标记区域 ===

        class ZoneMarker(QWidget):
            """不可见区域标记窗口 — 尺寸=模型可见 rect（非整个透明窗口）。"""
            def __init__(self, x, y, w, h):
                super().__init__()
                self.setWindowFlags(
                    Qt.FramelessWindowHint |
                    Qt.WindowStaysOnTopHint |
                    Qt.Tool
                )
                self.setAttribute(Qt.WA_TranslucentBackground, True)
                self.setAttribute(Qt.WA_NoSystemBackground, True)
                self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                self.setGeometry(x, y, w, h)

            def paintEvent(self, e):
                pass

        def _create_avoid_zone_from_visible_rect(lx, ly, w, h):
            """用可见 rect（相对 Stage）创建 ZoneMarker，位置基于开启瞬间的 Stage 位置。"""
            old = state.get("avoid_zone")
            if old:
                old.close()

            orig_x, orig_y = state["avoid_orig_win_pos"]
            screen_x = orig_x + lx
            screen_y = orig_y + ly
            zone = ZoneMarker(screen_x, screen_y, w, h)
            zone.show()
            zone.lower()
            state["avoid_zone"] = zone
            state["avoid_visible_rect"] = (lx, ly, w, h)
            state["avoid_pushing"] = False
            state["avoid_push_dir"] = None
            state["avoid_target"] = None

        def _query_visible_then_start_avoid():
            """向 JS 查模型可见 rect，拿到后创建 ZoneMarker。
            先用 Stage 中心 70% 做合理 fallback，JS 回调到达后再替换。
            """
            sx, sy, sw, sh = pet.stage_rect()
            fw = max(int(sw * 0.7), 50)
            fh = max(int(sh * 0.7), 50)
            flx = (sw - fw) // 2
            fly = (sh - fh) // 2
            _create_avoid_zone_from_visible_rect(flx, fly, fw, fh)

            js = f"setPetCommand({json.dumps({'type': 'getModelRect'}, ensure_ascii=False)})"

            def _cb(result):
                try:
                    data = json.loads(result) if isinstance(result, str) else {}
                except (json.JSONDecodeError, TypeError):
                    data = {}
                if not data.get("fallback") and "w" in data and "h" in data:
                    # getModelRect 返回整块画布坐标；Qt 全屏窗口 = 画布，
                    # 这里只把它换算成相对 Stage 的局部坐标。
                    _x = float(data.get("x", 0)) - screen.x()
                    _y = float(data.get("y", 0)) - screen.y()
                    sx0, sy0 = sx - screen.x(), sy - screen.y()
                    lx, ly = _x - sx0, _y - sy0
                    w, h = float(data["w"]), float(data["h"])
                    if state["avoid"] and state.get("avoid_orig_win_pos"):
                        _create_avoid_zone_from_visible_rect(
                            int(lx), int(ly), int(w), int(h))

            page.runJavaScript(js, _cb)

        def _toggle_dblclick(checked):
            """双击部位触发动效开关（关掉后双击只当作普通点击）。"""
            state["dblclick"] = bool(checked)
            save_ui_cfg(dblclick=bool(checked))

        def _toggle_avoid(checked):
            """切换鼠标避让开关。开启时**立即**锁定 Stage 原始位置，然后异步查可见 rect。"""
            state["avoid"] = bool(checked)
            if checked:
                # 立即锁定 Stage 原始位置（在任何 move 发生之前）
                sx, sy, _sw, _sh = pet.stage_rect()
                state["avoid_orig_win_pos"] = (sx, sy)
                _query_visible_then_start_avoid()
            else:
                # 关闭回位：始终回到 avoid_orig_win_pos
                zone = state.get("avoid_zone")
                sx, sy, _sw, _sh = pet.stage_rect()
                orig_x, orig_y = state.get("avoid_orig_win_pos", (sx, sy))
                if zone:
                    zone.close()
                state["avoid_zone"] = None
                state["avoid_visible_rect"] = None
                state["avoid_orig_win_pos"] = None
                state["avoid_pushing"] = False
                state["avoid_push_dir"] = None
                state["avoid_target"] = None
                state["avoid_burst"] = 0
                state["avoid_hold"] = False

                if sx != orig_x or sy != orig_y:
                    if not move_timer.isActive():
                        move_timer.start(16)
                    _animate_move_to(orig_x, orig_y, on_done=_notify_pos)

        _pos_notify_counter = 0
        _pixel_hit_logged = False

        def _check_pixel_alpha(cursor_pos):
            """检测鼠标位置对应像素是否有不透明内容（alpha > 20）。
            返回 True 表示"鼠标碰到了桌宠可见像素"，忽略周围透明 padding。

            帧现在只回传模型实际占用的裁剪区域，所以坐标换算不再用
            "img 尺寸 vs 窗口"（那会把裁剪区域当整幅画布），统一走
            PetWidget.canvas_norm / alpha_at_norm（它们内部处理了裁剪偏移与行序）。
            """
            try:
                local = QPoint(pet.mapFromGlobal(cursor_pos))
            except Exception:
                local = QPoint(cursor_pos.x() - pet.x(), cursor_pos.y() - pet.y())
            norm = pet.canvas_norm(local)
            if not norm:
                return False
            return pet.alpha_at_norm(norm[0], norm[1]) > 20


        def _clear_dist_sq(px, py, vr, cursor):
            """鼠标到模型可见 rect 的距离²（>= AVOID_PUSH_MARGIN² 视为已躲开）。"""
            return avoid_dist_sq(px, py, vr, (cursor.x(), cursor.y()))

        def _plan_avoid(vr, cursor, orig):
            """规划这次避让要挪到哪个位置（算法见 avoid_plan）。"""
            tx, ty, dir_x, dir_y = avoid_plan(
                vr, (cursor.x(), cursor.y()), orig,
                screen.width(), screen.height(), AVOID_PUSH_MARGIN)
            state["avoid_push_dir"] = (dir_x, dir_y)
            return tx, ty

        # === 躲开 vs 能被点：鼠标慢速靠近 / 停在模型上时不躲 ===
        # 否则模型永远躲、鼠标追不上，右键就关不掉避让（死循环）。
        AVOID_FREE_SPEED = 300    # px/s：低于此速度视为"想凑近/停留"，让开位置不再躲
        _cursor_track = {"pos": None, "t": 0.0}

        def _cursor_speed(cursor):
            """两次 tick 之间的鼠标速度（px/s）；鼠标不动则为 0。"""
            now = time.monotonic()
            last_pos = _cursor_track["pos"]
            last_t = _cursor_track["t"]
            if last_pos is None or last_t <= 0:
                speed = 0.0
            else:
                dt = now - last_t
                dist = math.hypot(cursor.x() - last_pos[0], cursor.y() - last_pos[1])
                speed = (dist / dt) if dt > 0.001 else 0.0
            _cursor_track["pos"] = (cursor.x(), cursor.y())
            _cursor_track["t"] = now
            return speed

        def _avoid_tick():
            """像素级检测：鼠标碰到桌宠可见像素则避让；同时离开桌宠和原位置 zone 才回位。"""
            nonlocal _pos_notify_counter, _pixel_hit_logged
            if not state["avoid"]:
                return
            if state.get("avoid_hold"):
                return
            # 右键菜单展开期间模型不能跑：否则菜单还开着、模型已经从鼠标底下
            # 溜走，用户点不到任何条目，看起来就是"窗口卡住了"。
            if state.get("menu_open"):
                return
            # 菜单刚收起：留半秒缓冲，避免刚点完菜单项模型就被弹开
            if time.monotonic() < float(state.get("avoid_grace_until") or 0.0):
                return
            # 部件隐藏模式：鼠标要停在模型/确认按钮上点选，窗口不能跑
            if state.get("part_mode"):
                return
            if not pet.isVisible():
                return
            if getattr(pet, '_dragging', False):
                return

            cursor = QCursor.pos()
            speed = _cursor_speed(cursor)

            # ① 像素级精确检测：鼠标是否真的在桌宠可见像素上
            on_pet = _check_pixel_alpha(cursor)
            if on_pet and not _pixel_hit_logged:
                _pixel_hit_logged = True
                img = getattr(pet, '_img', None)
                iw, ih = (img.width(), img.height()) if img and not img.isNull() else (0, 0)
                print(f"[avoid] pixel hit: img={iw}x{ih} win={pet.width()}x{pet.height()} "
                      f"cursor=({cursor.x()},{cursor.y()}) pet=({pet.x()},{pet.y()}) "
                      f"speed={speed:.0f}px/s")

            # ② 已经躲过一次 + 鼠标慢速靠近/停在模型上：不再躲，让用户能右键（关避让）或拖动。
            #    首次快速划过时照常躲开（避让保持灵敏）；躲开后慢慢凑近就不跑，
            #    否则模型永远躲、鼠标追不上，右键就关不掉避让（死循环）。
            if on_pet and state["avoid_pushing"] and speed < AVOID_FREE_SPEED:
                return

            # ③ 固定 zone（原位置区域），仅用于回位滞回区
            zone = state.get("avoid_zone")
            _sx, _sy, _sw, _sh = pet.stage_rect()
            zone_rect = zone.geometry() if zone else QRect(
                _sx, _sy, _sw, _sh)
            in_zone = zone_rect.contains(cursor)

            if on_pet:
                # === 推开阶段 ===
                vr = state.get("avoid_visible_rect") or (0, 0, _sw, _sh)
                orig = state.get("avoid_orig_win_pos") or (_sx, _sy)

                if not state["avoid_pushing"]:
                    state["avoid_pushing"] = True
                    state["avoid_target"] = None
                    move_anim["active"] = False
                    move_timer.stop()

                    # 兜底：连续躲 4 次还想抓它 → 暂停避让 2 秒，保证一定能右键关掉
                    state["avoid_burst"] = state.get("avoid_burst", 0) + 1
                    if state["avoid_burst"] >= 4:
                        state["avoid_burst"] = 0
                        state["avoid_hold"] = True

                        def _release_hold():
                            state["avoid_hold"] = False
                        QTimer.singleShot(2000, _release_hold)

                # 当前位置仍压着鼠标（含贴屏幕边被 clamp 住、推不到位的情况）→ 重新规划
                if state.get("avoid_target") is None or \
                        _clear_dist_sq(_sx, _sy, vr, cursor) < AVOID_PUSH_MARGIN ** 2:
                    state["avoid_target"] = _plan_avoid(vr, cursor, orig)

                tx, ty = state["avoid_target"]
                if tx != _sx or ty != _sy:
                    _set_stage_rect(tx, ty, _sw, _sh)

                _pos_notify_counter += 1
                if _pos_notify_counter >= 10:
                    _pos_notify_counter = 0
                    _notify_pos()

            else:
                # === 回位条件：鼠标同时离开桌宠和 zone ===
                if state["avoid_pushing"] and not in_zone:
                    state["avoid_pushing"] = False
                    state["avoid_push_dir"] = None
                    state["avoid_target"] = None
                    state["avoid_burst"] = 0
                    orig_x, orig_y = state.get("avoid_orig_win_pos", (_sx, _sy))
                    if _sx != orig_x or _sy != orig_y:
                        if not move_timer.isActive():
                            move_timer.start(16)
                        _animate_move_to(orig_x, orig_y, on_done=_notify_pos)

        # === 部件隐藏交互（右键「隐藏部件」→ 点击选中 → 确认/取消）===
        def _exit_part_mode():
            state["part_mode"] = False
            state["part_pending"] = None
            try:
                page.runJavaScript(
                    "setPetCommand(" + json.dumps({"type": "clearHighlight"}) + ")")
            except Exception:
                pass
            part_hint.hide()
            part_ok.hide()
            part_cancel.hide()
            state["part_anchor"] = None
            pet.unsetCursor()

        def _enter_part_mode():
            state["part_mode"] = True
            state["part_pending"] = None
            state["part_anchor"] = None      # 进入时还没有点选位置 → 顶部居中提示
            pet.setCursor(Qt.CrossCursor)
            part_ok.hide()
            part_cancel.hide()
            part_hint.setText("点击要隐藏的部件（重叠处同一位置再点可切换下层）")
            part_hint.adjustSize()
            part_hint.show()
            _reposition_part_ui()

        def _pick_part_at(pos):
            if not state.get("part_mode"):
                return
            try:
                img = pet._img
                # 直出模式没有 Qt 帧缓冲，命中判定完全走 Electron 画布的
                # pickPart；这里不能再因为 img 为空就直接返回，否则隐藏
                # 部件模式下所有点击都会被静默吞掉。
                if not pet._direct_mode and (img is None or img.isNull()):
                    return
                # 归一化坐标直接走 PetWidget（内部按画布 + 裁剪偏移换算）
                norm = pet.canvas_norm(pos)
                if not norm:
                    return
                nx, ny = norm
                if nx < 0 or nx > 1 or ny < 0 or ny > 1:
                    return
            except Exception:
                return

            # 记下这次点击的位置：确认/取消按钮随后贴着这里弹出
            state["part_anchor"] = (pos.x(), pos.y())

            def _cb(result):
                try:
                    data = json.loads(result) if isinstance(result, str) else result
                except Exception:
                    data = None
                if not isinstance(data, dict):
                    data = {}
                if not data.get("ok"):
                    part_hint.setText(str(data.get("error") or "未选中部件"))
                    part_hint.adjustSize()
                    part_hint.show()
                    _reposition_part_ui()
                    return
                state["part_pending"] = data
                name = str(data.get("drawableId") or data.get("partId") or "部件")
                if len(name) > 18:
                    name = name[:17] + "…"
                layer_count = int(data.get("layerCount") or 1)
                layer_index = int(data.get("layerIndex") or 1)
                part_ok.setText("确认隐藏 " + name)
                part_ok.adjustSize()
                if layer_count > 1:
                    part_hint.setText(
                        f"已选中 {name}（第 {layer_index}/{layer_count} 层，"
                        f"同一位置再点一次切换下层）")
                else:
                    part_hint.setText(f"已选中 {name}")
                part_hint.adjustSize()
                part_hint.show()
                part_ok.show()
                part_cancel.show()
                _reposition_part_ui()

            cmd = {"type": "pickPart", "nx": float(nx), "ny": float(ny)}
            page.runJavaScript("setPetCommand(" + json.dumps(cmd) + ")", _cb)

        def _confirm_hide_part():
            data = state.get("part_pending")
            if not data:
                return
            cmd = {"type": "hideDrawable", "drawableIndex": data.get("drawableIndex"),
                   "drawableId": data.get("drawableId")}
            page.runJavaScript("setPetCommand(" + json.dumps(cmd, ensure_ascii=False) + ")")
            # 保持选中模式，可继续点选其他部件
            state["part_pending"] = None
            part_ok.hide()
            part_cancel.hide()
            part_hint.setText("点击要隐藏的部件（重叠处同一位置再点可切换下层）")
            part_hint.adjustSize()
            part_hint.show()
            _reposition_part_ui()

        def _restore_parts():
            _exit_part_mode()
            page.runJavaScript(
                "setPetCommand(" + json.dumps({"type": "showAllDrawables"}) + ")")

        part_ok.clicked.connect(_confirm_hide_part)
        part_cancel.clicked.connect(_exit_part_mode)

        # === 双击反馈标签（提示这次双击命中了哪个部位、播了什么动画）===
        zone_hint = QLabel("", pet)
        zone_hint.setStyleSheet(
            "QLabel{color:#fff;background:rgba(20,24,34,215);border-radius:8px;"
            "padding:3px 9px;font-size:12px;}")
        zone_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        zone_hint.adjustSize()
        zone_hint.hide()
        pet._hint_widget = zone_hint        # 让覆盖层 setMask 把它并进命中区
        _hint_seq = {"n": 0}

        def _flash_zone_hint(text):
            """在鼠标上方闪一条小提示，0.9s 后自动消失（连点时只保留最新一条）。"""
            _hint_seq["n"] += 1
            seq = _hint_seq["n"]
            zone_hint.setText(text)
            zone_hint.adjustSize()
            cur = pet.mapFromGlobal(QCursor.pos())
            x = max(4, min(pet.width() - zone_hint.width() - 4,
                           cur.x() - zone_hint.width() // 2))
            y = max(4, cur.y() - zone_hint.height() - 18)
            zone_hint.move(x, y)
            zone_hint.show()
            zone_hint.raise_()
            try:
                pet._apply_interaction_mask()
            except Exception:
                pass

            def _hide():
                if _hint_seq["n"] == seq:
                    zone_hint.hide()
                    try:
                        pet._apply_interaction_mask()
                    except Exception:
                        pass
            QTimer.singleShot(900, _hide)

        # === 右键菜单「播放动画」===
        # 资源列表来自 getAssets：模型加载完成时刷新一次、切模型时再刷一次，
        # 菜单打开时直接用缓存（QMenu 是同步构建的，没法在里面等异步回调）。
        def _send_js(cmd, callback=None):
            js = "setPetCommand(" + json.dumps(cmd, ensure_ascii=False) + ")"
            if callback:
                page.runJavaScript(js, callback)
            else:
                page.runJavaScript(js)

        _assets_try = {"n": 0}

        def _model_key(u):
            """模型 URL → 可比较的路径键（去转义/去协议/去 query）。"""
            try:
                p = urllib.parse.unquote(str(u or ""))
                for pre in ("file:///", "file://", "http://", "https://"):
                    if p.lower().startswith(pre):
                        p = p[len(pre):]
                        break
                p = p.split("?")[0].split("#")[0].replace("\\", "/")
                return os.path.normcase(os.path.normpath(p))
            except Exception:
                return ""

        def _same_model_url(a, b):
            ka, kb = _model_key(a), _model_key(b)
            if not ka or not kb:
                return True          # 拿不到就別拦（宁可放过，不要永久重试）
            if ka == kb:
                return True
            # http 服务地址和本地路径前缀不同，但模型文件名一致 → 同一个模型
            return os.path.basename(ka) == os.path.basename(kb)

        def _assets_ok(data):
            """这份 getAssets 结果能不能当作「当前模型」的动画清单。

            页面刚起来 / 模型还在加载时，getAssets 返回的是**合法** JSON：
            model_ready=false、motions 为空、model 还是上一个模型。旧逻辑只看
            "不是 error" 就照单全收（还会置 assets_ready=True），于是右键
            「播放动画」长时间只有"（模型还在加载…）"，要等用户再开一次菜单
            手动刷新才出现 —— 这正是"自带动画很久才显示"的原因。
            """
            if not isinstance(data, dict) or data.get("error"):
                return False
            if data.get("switch_busy"):
                return False
            if not data.get("model_ready"):
                return False
            if not _same_model_url(state.get("model_url"), data.get("model")):
                return False
            # 模型声称有动作却给了空列表 = 还没解析出来，继续等
            if data.get("has_motions") and not (data.get("motions") or []):
                return False
            return True

        def _refresh_assets(cb=None):
            def _done(res):
                data = None
                try:
                    data = json.loads(res) if isinstance(res, str) else res
                except Exception:
                    data = None
                if _assets_ok(data):
                    _assets_try["n"] = 0
                    state["assets"] = data
                    state["assets_at"] = time.monotonic()
                    _WSTATE["assets_ready"] = True
                    # 资源清单到手 = 知道有哪些动画了 → 顺手给每个动画分一个热区
                    _cm_try["n"] = 0
                    _build_click_map(data)
                elif _assets_try["n"] < 20:
                    # Electron 页面加载完到 setPetCommand 可调用之间有数秒空窗，
                    # 大模型的 motions 解析也可能晚一两秒 —— 退避重试到 20 次
                    # （约 0.5s→1s，总计 ~15s），别让清单永久空着。
                    _assets_try["n"] += 1
                    delay = 0.5 if _assets_try["n"] <= 8 else 1.0
                    threading.Timer(delay, lambda: _refresh_assets(cb)).start()
                if cb:
                    try:
                        cb(state.get("assets"))
                    except Exception:
                        pass

            try:
                _send_js({"type": "getAssets"}, _done)
            except Exception:
                # 页面还在切换（旧页已销毁）会直接抛异常：同样退避重试，
                # 否则异常一出就再也没有下一次拉取。
                if _assets_try["n"] < 20:
                    _assets_try["n"] += 1
                    threading.Timer(0.5, lambda: _refresh_assets(cb)).start()

        # === 换装（服装参数通道）===
        # 模型自带的衣装做成参数：Clothes1 长短裙 / Clothes2 大小变 / Clothes3 胸口衣服 /
        # Clothes4 鞋子 / Clothes5 尾巴…… 每个通道的候选状态由 `Exp3/*.exp3.json`
        # 给出（作者写的就是"裙子切换 / 脱鞋 / 体型2"这类切换动作）。
        # 换装值写进渲染层的 `_outfitLock`，由它每帧强压 —— 否则播个动作就穿回去了。
        # === 换装部位判定：中键"点哪换哪"===
        # 鼠标指向哪个部位，就只切那个部位相关的服装通道；判不出部位才退回全局轮转。
        # 位置用 state["model_rect_norm"]（模型可见包围盒，已归一化到画布）换算 ——
        # 纯算术、不抓帧，所以屏外/没有剪影也能判。
        _ZONE_LABEL = {"head": "头部", "chest": "上身", "waist": "腰臀",
                       "leg": "腿", "foot": "脚", "hand": "手"}
        _ZONE_YBANDS = (("head", 0.00, 0.24), ("chest", 0.24, 0.46),
                        ("waist", 0.46, 0.66), ("leg", 0.66, 0.88),
                        ("foot", 0.88, 1.01))
        # 通道名/参数名 → 部位。顺序有意义："手套"必须排在"衣"前面，
        # "裤袜"必须排在"袜"前面，否则会被更宽的词先吃掉。
        _ZONE_WORDS = (
            ("hand", ("手套", "手", "袖", "腕", "指")),
            ("foot", ("鞋", "靴", "脚", "趾")),
            ("leg", ("裤袜", "丝袜", "袜", "裤", "腿")),
            ("waist", ("裙", "腰", "尾", "披风", "围巾", "腰带")),
            ("head", ("帽", "头", "发", "耳", "眼镜", "面具", "发饰")),
            ("chest", ("毛衣", "外套", "内衣", "泳装", "衣", "胸", "领", "羽织")),
        )

        def _outfit_zone(ch):
            """通道 → 部位键（判不出返回空串）。"""
            nm = "%s %s" % (ch.get("label") or "", ch.get("param") or "")
            for zone, words in _ZONE_WORDS:
                for w in words:
                    if w in nm:
                        return zone
            return ""

        def _zone_at_norm(nx, ny):
            """画布归一化坐标 → 部位键。

            刻意**不用** state["model_rect_norm"]：那是 renderer 上报的
            `model.x - w/2` / cw，对带 fit 变换的模型会给出画布外的值
            （实测大肥鱼报 x=3179、cw=1920），拿它算相对位置必然判错。
            改用窗口归一化坐标直接分档 —— applyFit 是"按高度贴满舞台"的，
            模型上下沿基本就是舞台上下沿，纵向比例直接可当部位比例。
            """
            try:
                nx, ny = float(nx), float(ny)
            except (TypeError, ValueError):
                return ""
            if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                return ""
            # 手在身体两侧：竖直方向落在中段、水平方向靠近外沿
            if 0.20 <= ny <= 0.80 and (nx < 0.20 or nx > 0.80):
                return "hand"
            for zone, lo, hi in _ZONE_YBANDS:
                if lo <= ny < hi:
                    return zone
            return ""

        def _refresh_outfits():
            """按当前模型重新扫一遍换装通道（切模型/重新加载后调用）。"""
            prev = state.get("outfit_sel") or {}
            channels = []
            try:
                mf = local_model_json_of_url(state.get("model_url"))
                if mf:
                    channels = scan_outfits(mf)
            except Exception as exc:
                _log_line("WARN", "换装扫描失败: %r" % (exc,))
                channels = []
            state["outfits"] = channels
            sel = {}
            for ch in channels:
                pid = ch["param"]
                try:
                    old = int(prev.get(pid, 0))
                except Exception:
                    old = 0
                if not (0 <= old < len(ch["states"])):
                    old = 0
                sel[pid] = old
            state["outfit_sel"] = sel
            state["outfit_cursor"] = 0
            if channels:
                _log_line("INFO", "换装通道 %d 个：%s"
                          % (len(channels), " / ".join(c["label"] for c in channels)))
                _apply_outfit_ranges_async(channels)
            return channels

        def _apply_outfit_ranges_async(channels):
            """问一次 getParams 的真实值域，把参数型通道的档位改成整数枚举。

            纯异步（渲染层就绪后 1~2ms 回包）：拿不到就保留"按名字猜"的兜底档位，
            所以宁可先给个能用但不精确的菜单，也不阻塞就绪流程。
            """
            if not any(c.get("source") == "param" for c in (channels or [])):
                return

            def _cb(res):
                try:
                    data = json.loads(res) if isinstance(res, str) else res
                    if not isinstance(data, dict):
                        return
                    inner = data.get("result")
                    if not isinstance(inner, dict):
                        inner = data
                    plist = inner.get("params")
                    if not isinstance(plist, list):
                        return
                    if not apply_outfit_ranges(channels, plist):
                        return
                    sel = state.get("outfit_sel") or {}
                    for ch in channels:
                        if ch.get("source") != "param":
                            continue
                        idx = int(sel.get(ch["param"], 0) or 0)
                        if not (0 <= idx < len(ch["states"])):
                            sel[ch["param"]] = 0
                    state["outfit_sel"] = sel
                    _log_line("INFO", "换装档位已按真实值域修正：%s"
                              % " / ".join("%s×%d" % (c["label"], len(c["states"]))
                                           for c in channels
                                           if c.get("source") == "param")[:300])
                except Exception:
                    pass

            try:
                _send_js({"type": "getParams"}, _cb)
            except Exception:
                pass

        def _outfit_cur_name(ch):
            idx = int((state.get("outfit_sel") or {}).get(ch["param"], 0) or 0)
            try:
                return str(ch["states"][idx]["name"])
            except Exception:
                return "默认"

        def _set_outfit_state(ch, idx, quiet=False):
            """把某个通道切到第 idx 个状态（写入并锁定，同步提示）。"""
            try:
                idx = int(idx) % len(ch["states"])
            except Exception:
                return False
            st = ch["states"][idx]
            try:
                _send_js({
                    "type": "applyOutfit",
                    "params": {ch["param"]: float(st["value"])},
                    "names": {ch["param"]: str(st["name"])},
                })
            except Exception:
                return False
            (state.get("outfit_sel") or {})[ch["param"]] = idx
            _after_outfit_change()
            if not quiet:
                _flash_zone_hint("换装：%s → %s" % (ch["label"], st["name"]))
            return True

        def _after_outfit_change():
            """换装改了剪影：作废抓帧缓存 + 旧热区格子，下一拍按新剪影重算。

            不刷的话，裙子变长/尾巴冒出来之后，新长出来的那截"看得见却点不到"，
            而且平移缩放还会一直复用旧格子（_reusable_since_capture 只比模型 URL，
            换装不换 URL，它看不出来剪影已经变了）。
            """
            try:
                _send_js({"type": "invalidateHitFrame"})
            except Exception:
                pass
            state["_cm_cells"] = 0
            state["_cm_model"] = ""
            try:
                _schedule_hit_refresh()
            except Exception:
                pass

        def _cycle_outfit(nx=None, ny=None):
            """中键换装：**指向哪个部位就切那个部位的通道**。

            同一部位有多个通道（例如左右手各一个手套参数）时，每次点轮到下一个；
            该部位没有对应通道时提示一句、不动手；完全判不出部位（没有包围盒、
            点在模型外）才退回原来的全局轮转。
            """
            chs = state.get("outfits") or []
            if not chs:
                return False
            zone = ""
            try:
                if nx is not None and ny is not None:
                    zone = _zone_at_norm(nx, ny)
            except Exception:
                zone = ""
            if zone:
                group = [c for c in chs if _outfit_zone(c) == zone]
                if group:
                    zc = state.setdefault("outfit_zone_cursor", {})
                    cur = int(zc.get(zone, 0) or 0) % len(group)
                    ch = group[cur]
                    nxt = (int((state.get("outfit_sel") or {}).get(ch["param"], 0) or 0) + 1)
                    _set_outfit_state(ch, nxt)
                    zc[zone] = (cur + 1) % len(group)
                    return True
                _flash_zone_hint("%s没有可换的服装" % _ZONE_LABEL.get(zone, zone))
                return False
            cur = int(state.get("outfit_cursor") or 0) % len(chs)
            ch = chs[cur]
            nxt = (int((state.get("outfit_sel") or {}).get(ch["param"], 0) or 0) + 1)
            _set_outfit_state(ch, nxt)
            state["outfit_cursor"] = (cur + 1) % len(chs)
            return True

        def _reset_outfits():
            """全部通道回默认（相当于把衣服穿回去）。"""
            chs = state.get("outfits") or []
            for ch in chs:
                _set_outfit_state(ch, 0, quiet=True)
            try:
                _send_js({"type": "applyOutfit", "clear": True})
            except Exception:
                pass
            _flash_zone_hint("换装：已恢复默认")

        def _fill_outfit_menu(sub):
            """右键菜单「换装」：通道 → 状态，当前状态打 ✓。"""
            sub.clear()
            chs = state.get("outfits") or []
            if not chs:
                empty = sub.addAction("当前模型没有可换装的服装参数")
                empty.setEnabled(False)
                return
            for ch in chs:
                cur = int((state.get("outfit_sel") or {}).get(ch["param"], 0) or 0)
                head = sub.addMenu("%s（%s）" % (ch["label"], _outfit_cur_name(ch)))
                for i, st in enumerate(ch["states"]):
                    mark = "✓ " if i == cur else "   "
                    head.addAction(
                        "%s%s%s" % (mark, st["name"],
                                    "" if abs(float(st["value"])) < 1e-6
                                    else "（%.4g）" % float(st["value"])),
                        lambda _c=False, chh=ch, ii=i: _set_outfit_state(chh, ii))
            sub.addSeparator()
            sub.addAction("下一个换装（也可中键点模型部位）", lambda _c=False: _cycle_outfit())
            sub.addAction("全部恢复默认", lambda _c=False: _reset_outfits())

        def _builtin_label(bid):
            for item in ((state.get("assets") or {}).get("builtin_animations") or []):
                if item.get("id") == bid:
                    return item.get("label") or bid
            return BUILTIN_ANIM_LABEL.get(bid, bid)

        def _motion_sound_ok(group, index):
            """该动作声明的配音文件在本地是否真的存在（不存在就别显示字幕）。"""
            assets = state.get("assets") or {}
            dirp = local_dir_of_model_url(assets.get("model"))
            if not dirp:
                return True                      # 本地目录未知 → 交给渲染器自己判断
            for grp in (assets.get("motions") or []):
                if str(grp.get("group") or "") != str(group):
                    continue
                names = grp.get("sound_files") or []
                try:
                    i = int(index or 0)
                except Exception:
                    return True
                if not (0 <= i < len(names)):
                    return True
                nm = os.path.basename(str(names[i] or "").replace("\\", "/")).lower()
                if not nm:
                    return True
                if nm in audio_name_index(dirp):
                    return True
                _log_line("WARN", "配音缺失、不显示字幕: %s#%s %s" % (group, i, nm))
                return False
            return True

        def _sound_diag_text():
            """配音诊断：声明了几条、文件在不在、路径能不能解析（用户看得懂的那份）。"""
            assets = state.get("assets") or {}
            motions = assets.get("motions") or []
            dirp = local_dir_of_model_url(assets.get("model")) or ""
            jsonp = local_model_json_of_url(assets.get("model")) or ""
            n_all = sum(len(g.get("files") or []) for g in motions)
            n_decl = sum(len([f for f in (g.get("sound") or []) if f]) for g in motions)
            idx = audio_name_index(dirp) if dirp else {}
            miss = []
            for g in motions:
                names = g.get("sound_files") or []
                for i, f in enumerate(g.get("sound") or []):
                    if not f:
                        continue
                    nm = os.path.basename(str(names[i] if i < len(names) else "").replace("\\", "/")).lower()
                    if nm and nm not in idx:
                        miss.append("%s#%d %s" % (g.get("group") or "(空)", i, nm))
            lines = ["模型：%s" % (os.path.basename(jsonp) or "(未知)"),
                     "声明配音：%d / %d 个动作" % (n_decl, n_all),
                     "模型目录里找到音频文件：%d 个" % len(idx)]
            if n_decl:
                lines.append("配音文件已找到：%d 条；找不到：%d 条" % (n_decl - len(miss), len(miss)))
            if miss:
                lines.append("找不到的动作：" + "；".join(miss[:8]) + (" 等" if len(miss) > 8 else ""))
            lines.append("")
            if n_decl:
                lines.append("播放时仍然没听到声音？看插件目录下的 _js_console.log，"
                             "搜 [snd] / SoundManager：会写明是文件 404 还是被浏览器自动播放策略拦下。")
            else:
                lines.append("该模型的 model3.json 没有声明 Sound —— 播不出配音，也不会显示字幕。"
                             + ("（目录里有 %d 个音频文件，但没接到任何动作上）" % len(idx) if idx else ""))
            return "\n".join(lines)

        def _show_sound_diag(_checked=False):
            try:
                QMessageBox.information(pet, "配音诊断", _sound_diag_text())
            except Exception as _e:
                _log_line("WARN", "配音诊断失败: %s" % _e)

        def _motion_text(group, index):
            """某个动作的台词（model3.json 里 Sound 旁边的 Text）；没配音就没台词。"""
            assets = state.get("assets") or {}
            if not _motion_sound_ok(group, index):
                return ""
            g = str(group)
            for grp in (assets.get("motions") or []):
                if str(grp.get("group") or "") != g:
                    continue
                flags = grp.get("sound") or []
                texts = grp.get("texts") or []
                try:
                    i = int(index or 0)
                except Exception:
                    return ""
                if 0 <= i < len(flags) and flags[i] and i < len(texts):
                    return " ".join(str(texts[i] or "").split())
                return ""
            return ""

        def _probe_sound_dur():
            """播完立即查音频时长：字幕跟着配音一起结束（查不到就用默认 3.6s）。"""
            def _on(res):
                data = {}
                try:
                    data = json.loads(res) if isinstance(res, str) else (res or {})
                except Exception:
                    data = {}
                try:
                    dur = float(data.get("duration") or 0)
                except Exception:
                    dur = 0.0
                if dur > 0.4:
                    pet.subtitle_extend(min(14.0, dur + 0.35))
            _send_js({"type": "getAudioState"}, _on)

        def _play_anim(builtin=None, group=None, index=None, loop=None):
            cmd = {"type": "playMotion", "priority": "force",
                   "loop": bool(state.get("anim_loop")) if loop is None else bool(loop)}
            if builtin:
                cmd["builtin"] = str(builtin)
            if group is not None:
                cmd["group"] = str(group)
            if index is not None:
                cmd["index"] = int(index)
            _send_js(cmd)
            # 带配音的动作 → 底部显示台词；其余动作把上一条字幕清掉，避免串台词
            if group is not None and index is not None:
                txt = _motion_text(group, index)
                if txt:
                    pet.show_subtitle(txt, 3.6)
                    QTimer.singleShot(420, _probe_sound_dur)
                else:
                    pet.show_subtitle("", 0)
            else:
                pet.show_subtitle("", 0)

        def _motion_label(f):
            """motions/touch_head.motion3.json → touch_head。"""
            base = os.path.basename(str(f or ""))
            for suf in (".motion3.json", ".motion3", ".json"):
                if base.lower().endswith(suf):
                    base = base[: -len(suf)]
                    break
            return base or "未命名"

        def _all_hotspots():
            """全部热区在**当前画布**上的坐标（拖动/缩放后跟着模型一起走）。

            种子存的是抓帧那一刻的坐标，直接拿去画就会停在旧位置 ——
            拖动之后表现为"圆点集体脱离模型"。统一过一遍 _seed_to_current。
            """
            seeds = (state.get("click_map") or {}).get("seeds") or []
            pts = []
            for s in seeds:
                try:
                    cur = _seed_to_current(s)
                except Exception:
                    cur = None
                if cur:
                    pts.append((cur[0], cur[1], s.get("n")))
            return pts

        def _fill_anim_menu(anim_menu):
            """填充「播放动画」子菜单（由 lazy_submenu 在展开时才调用，不压右键事件）。"""
            assets = state.get("assets")
            if not assets:
                empty = anim_menu.addAction("（模型还在加载…）")
                empty.setEnabled(False)
                anim_menu.addAction("刷新列表", lambda _c=False: _refresh_assets())
                _refresh_assets()
                return

            # 热区编号：菜单里每个动画前面的 #n 就是模型上那个点的号码
            seeds = (state.get("click_map") or {}).get("seeds") or []

            def _no(**kw):
                for s in seeds:
                    if all(s.get(k) == v for k, v in kw.items()):
                        return s.get("n")
                return None

            def _tag(n, text):
                return ("#%d %s" % (n, text)) if n else text

            builtin = assets.get("builtin_animations") or []
            motions = assets.get("motions") or []
            if builtin:
                sub = anim_menu.addMenu("内置动画")
                for item in builtin:
                    aid = item.get("id")
                    sub.addAction(_tag(_no(kind="builtin", id=aid), item.get("label") or aid),
                                  lambda _c=False, i=aid: _play_anim(builtin=i))
            if motions:
                total = assets.get("motion_total") or sum(
                    (g.get("count") or len(g.get("files") or [])) for g in motions)
                snd_total = assets.get("sound_total")
                if snd_total is None:
                    snd_total = sum(len([x for x in (g.get("sound") or []) if x])
                                    for g in motions)
                title = "模型动作（%d 组 / %d 个%s）" % (
                    len(motions), total,
                    " / %d 个带配音" % snd_total if snd_total else "")
                sub_m = anim_menu.addMenu(title)
                hit_ids = _hit_areas_of(assets.get("model"))
                if hit_ids:
                    # 作者在 model3.json 里声明了 HitAreas（Cubism 标准交互区）：
                    # 双击热区优先按这些区来铺，而不是我们瞎猜部位。
                    sub_m.addAction("作者声明的交互区：%s（双击热区按这些区铺）"
                                    % " / ".join((x.get("name") or x["id"]) for x in hit_ids[:6])
                                    ).setEnabled(False)
                    sub_m.addSeparator()
                if snd_total:
                    sub_m.addAction("♪ = 该动画带配音，播放时会出声").setEnabled(False)
                    sub_m.addAction("配音诊断（哪些配音真的能出声）", _show_sound_diag)
                    sub_m.addSeparator()
                else:
                    # 没有任何声明配音的动作时，别说"♪ = 带配音"（会让人以为哪条漏标了）；
                    # 若目录里其实躺着音频文件，就直接讲清楚"素材在、没接线"。
                    try:
                        mdir = local_dir_of_model_url(assets.get("model"))
                        n_aud = count_audio_files(mdir) if mdir else 0
                    except Exception:
                        n_aud = 0
                    if n_aud:
                        sub_m.addAction(
                            "该模型有 %d 个音频文件，但 model3.json 没声明 Sound，播不出声"
                            % n_aud).setEnabled(False)
                        sub_m.addSeparator()
                for g in motions:
                    gname = g.get("group") or ""
                    files = g.get("files") or []
                    flags = g.get("sound") or []
                    snames = g.get("sound_files") or []
                    texts = g.get("texts") or []
                    gsnd = g.get("sound_count") or 0
                    title = ("%s（%d%s）" % ("默认分组" if not gname else gname,
                                            g.get("count") or len(files),
                                            "，%d 带配音" % gsnd if gsnd else ""))
                    gmenu = sub_m.addMenu(title)
                    for i, f in enumerate(files[:40]):
                        voiced = bool(flags[i]) if i < len(flags) else False
                        sname = snames[i] if (voiced and i < len(snames)) else ""
                        text = "%d. %s%s" % (i + 1, "♪ " if voiced else "", _motion_label(f))
                        act = gmenu.addAction(
                            _tag(_no(kind="motion", group=gname, index=i), text),
                            lambda _c=False, gg=gname, ii=i:
                            _play_anim(group=gg, index=ii))
                        tip = []
                        if sname:
                            tip.append("配音：%s" % sname)
                        tline = texts[i] if (voiced and i < len(texts)) else ""
                        if tline:
                            tip.append("台词：%s" % tline)
                        if tip:
                            act.setToolTip("\n".join(tip))
                    if len(files) > 40:
                        more = gmenu.addAction(
                            "…其余 %d 个（该模型动作太多，未全部列出）" % (len(files) - 40))
                        more.setEnabled(False)
            if not builtin and not motions:
                empty = anim_menu.addAction("（该模型没有可用动作）")
                empty.setEnabled(False)

            anim_menu.addSeparator()
            loop_act = anim_menu.addAction("循环播放")
            loop_act.setCheckable(True)
            loop_act.setChecked(bool(state.get("anim_loop")))

            def _toggle_loop(checked):
                state["anim_loop"] = bool(checked)
                save_ui_cfg(anim_loop=bool(checked))
            loop_act.toggled.connect(_toggle_loop)

            sub_act = anim_menu.addAction("显示配音字幕")
            sub_act.setCheckable(True)
            sub_act.setChecked(bool(state.get("subtitles", True)))
            sub_act.setToolTip("带配音的动作播放时，在模型底部显示台词（默认开启）")

            def _toggle_sub(checked):
                state["subtitles"] = bool(checked)
                save_ui_cfg(subtitles=bool(checked))
                if not checked:
                    pet.show_subtitle("", 0)
                else:
                    pet.update()
            sub_act.toggled.connect(_toggle_sub)
            def _reset_sub_pos(_checked=False):
                """清掉本次打开拖出来的相对偏移，字幕回到默认（贴模型底部居中）。

                不再写盘：偏移本来就是"只在本次打开内"的一次性状态。
                """
                pet._sub_rel = None
                pet._sub_rect = QRectF()
                pet.update()
            anim_menu.addAction("重置字幕位置", _reset_sub_pos)
            anim_menu.addAction("停止动画", lambda _c=False: _send_js({"type": "stopMotion"}))
            anim_menu.addAction("刷新列表", lambda _c=False: _refresh_assets())

            anim_menu.addSeparator()
            hs = anim_menu.addAction("显示双击热区（3 秒）")
            hs.setEnabled(bool(seeds))
            hs.triggered.connect(
                lambda _c=False: pet.show_hotspots(_all_hotspots(), 3.0))
            anim_menu.addAction("重建双击热区", lambda _c=False: _build_click_map())

        # === 双击部位 → 不同动画 ===
        # 部件名（cdi3 里的中文名）各模型写法不统一，所以两级判定：
        #   1) 命中到的部件名里出现关键词 → 精确到部位
        #   2) 名字对不上 → 按"模型可见区域里的相对高度"分三段（任何模型都能用）
        # 每个部位再优先找一个名字里带 hint 的模型动作（touch_head / tapBody…），
        # 找不到才退回内置动画 —— 这样带 touch 动作的模型（碧蓝航线那类）手感更好。
        DBLCLICK_NAME_RULES = (
            ("face", ("眼", "瞳", "眉", "嘴", "牙", "鼻", "脸", "腮"), "look_around", "eyes"),
            ("hand", ("手", "指", "臂", "掌", "肩", "袖"), "cheer", "hand"),
            ("head", ("头", "发", "角", "鬓", "辫", "耳"), "tilt", "head"),
            ("body", ("胸", "身体", "腰", "裙", "衣", "脖", "带", "绳", "腿", "脚", "鞋"),
             "sway", "body"),
        )
        DBLCLICK_HEIGHT_RULES = (
            (0.34, "head", "tilt", "head"),
            (0.68, "body", "sway", "body"),
        )
        DBLCLICK_LOWER = ("lower", "sway", "body")
        ZONE_LABEL = {"face": "摸脸", "hand": "握手", "head": "摸头",
                      "body": "摸身体", "lower": "摸腿"}

        # 每个 zone 的"合理高度上限"：长发/翅/披风一类部件会一路垂到模特下半身，
        # 只按名字命中会把摸身体、摸腿全判成摸头。超过上限就跳过这条名字继续看。
        ZONE_MAX_Y = {"face": 0.50, "head": 0.45, "hand": 1.01, "body": 1.01}

        def _zone_for_point(part_names, rel_y):
            """部位名（按命中层级从高到低）→ (zone, 内置动画, 动作名关键词)。

            先看**最上层**命中的部件：点到的像素上可能压着头发、衣服好几层，
            用最上层判断才符合"我看到的是哪个部位"。都认不出来才退回高度分段。
            """
            for nm in part_names:
                if not nm:
                    continue
                for zone, kws, builtin, hint in DBLCLICK_NAME_RULES:
                    if any(k in nm for k in kws):
                        if rel_y <= ZONE_MAX_Y.get(zone, 1.01):
                            return zone, builtin, hint
                        break      # 这条名字在这个高度上说不通，换下一个名字
            for limit, zone, builtin, hint in DBLCLICK_HEIGHT_RULES:
                if rel_y < limit:
                    return zone, builtin, hint
            return DBLCLICK_LOWER

        def _find_zone_motion(hint):
            """在模型自带动作里找名字含 hint 的那个（组名 + 文件名一起比对）。"""
            assets = state.get("assets") or {}
            for g in (assets.get("motions") or []):
                gname = g.get("group") or ""
                for i, f in enumerate(g.get("files") or []):
                    nm = (gname + " " + _motion_label(f)).lower()
                    if hint and hint in nm:
                        return gname, i, _motion_label(f)
            return None

        # === 双击热区地图：每个动画独占一个位置 ===
        # 旧做法是"部位→固定动画"，一个部位只挂一种反应，模型自带的十几个动作
        # 根本点不到。现在给**每个动画**分一个独立触发点：
        #   ① 名字能看出部位的（touch_head / pat_body / 眼…）→ 落在对应部位上
        #   ② 看不出语义的（mission / login / 1 / 2…）→ 最远点采样随机铺在剩下的
        #      可见格子里，天然彼此不重合、也不挤成一团
        # 双击落在谁的"领地"（最近邻种子点）就播谁 —— 所以每个动画都点得到。
        MAX_HOTSPOTS = 48        # 一块模型上能分出的上限（再多只会互相压住）
        BUILTIN_ZONE = {"nod": "head", "shake": "head", "tilt": "head",
                        "sway": "body", "look_around": "face", "cheer": "hand"}
        NAME_ZONE_HINTS = (
            ("head", ("head", "hair", "头", "发")),
            ("face", ("face", "eye", "cheek", "mouth", "kiss", "脸红", "脸", "眼", "嘴", "眉")),
            ("hand", ("hand", "arm", "finger", "手", "臂", "指")),
            ("body", ("body", "chest", "skirt", "waist", "breast",
                      "衣", "身", "胸", "裙", "腰")),
            ("lower", ("leg", "foot", "thigh", "腿", "脚", "鞋")),
        )
        _cm_try = {"n": 0}

        def _hint_of_name(name):
            """动作名 → 部位 hint（"touch_head" → head）；认不出语义返回 None。"""
            s = str(name or "").lower()
            for zone, kws in NAME_ZONE_HINTS:
                if any(k in s for k in kws):
                    return zone
            return None

        def _hit_areas_of(model_url):
            """当前模型作者声明的交互区 Id 列表（没有就返回空 → 走自动铺点）。"""
            try:
                return model_hit_areas(local_model_json_of_url(model_url))
            except Exception:
                return []

        def _hotspot_entries(assets):
            """待分配热区的动画清单：模型自带动作在前，内置动画在后。"""
            out = []
            for g in (assets.get("motions") or []):
                gname = g.get("group") or ""
                flags = g.get("sound") or []
                for i, f in enumerate(g.get("files") or []):
                    if not str(f or "").strip():
                        continue      # 空条目（个别模型有 File:""）：播不出来，别占热区
                    lbl = _motion_label(f)
                    out.append({"kind": "motion", "group": gname, "index": i,
                                "label": lbl, "hint": _hint_of_name(gname + " " + lbl),
                                "snd": bool(flags[i]) if i < len(flags) else False})
            for it in (assets.get("builtin_animations") or []):
                bid = it.get("id")
                out.append({"kind": "builtin", "id": bid,
                            "label": it.get("label") or bid,
                            "hint": BUILTIN_ZONE.get(bid)})
            return out

        def _seeds_minsep(seeds, asp=1.0):
            best = 9.9
            for i in range(len(seeds)):
                for j in range(i + 1, len(seeds)):
                    dx = seeds[i]["nx"] - seeds[j]["nx"]
                    dy = (seeds[i]["ny"] - seeds[j]["ny"]) * asp
                    d = (dx * dx + dy * dy) ** 0.5
                    if d < best:
                        best = d
            return best

        def _assign_hotspots(entries, cells, asp, regions=None):
            """每个动画挑一个格子：语义优先归位，其余贪心最远点采样（互不重合）。

            regions: 作者声明的交互区 [{id, zone, nx0, ny0, nx1, ny1}]。有的话**优先**
            把动画落在作者画定的区里（同一个区里也按最远点分层铺开，不重合）；
            只有区里挤不下才退回全局最远点。地区里没格子（区在画布外）同理会退回。
            """
            ys = [float(c["y"]) for c in cells]
            y0, y1 = min(ys), max(ys)
            span = max(1e-6, y1 - y0)
            for c in cells:
                nm = str(c.get("p") or "")
                c["z"] = _zone_for_point([nm], (float(c["y"]) - y0) / span)[0]
            regions = [r for r in (regions or []) if r.get("nx1", 0) > r.get("nx0", 0)]
            for c in cells:
                c["_reg"] = -1
                for ri, r in enumerate(regions):
                    if (r["nx0"] <= float(c["x"]) <= r["nx1"]
                            and r["ny0"] <= float(c["y"]) <= r["ny1"]):
                        c["_reg"] = ri
                        break

            chosen = []

            def _mind(c):
                best = 2.0
                for s in chosen:
                    dx = float(c["x"]) - s["nx"]
                    dy = (float(c["y"]) - s["ny"]) * asp
                    d = (dx * dx + dy * dy) ** 0.5
                    if d < best:
                        best = d
                return best

            def _pick(pool):
                best, bd = None, -1.0
                for c in pool:
                    if c.get("_used"):
                        continue
                    d = _mind(c)
                    if d > bd:
                        bd, best = d, c
                return best, bd

            if len(entries) <= MAX_HOTSPOTS:
                ordered = entries
            else:
                # 超出上限：语义明确的动画优先保留，剩下的名额给无语义动画
                ordered = ([e for e in entries if e.get("hint")]
                           + [e for e in entries if not e.get("hint")])[:MAX_HOTSPOTS]
            if regions:
                # 有作者交互区时，先把"部位明确"的动画放进对应区，
                # 再让无语义的动画去填剩下的区内格子（否则区内会被先到先得占光）
                ordered = ([e for e in ordered if e.get("hint")]
                           + [e for e in ordered if not e.get("hint")])

            def _entry_reg(e):
                """动作名/组名里带着作者交互区的 Id → 那个区（如 TapTouchHead → TouchHead）。

                碧蓝航线这类模型的动作组就叫 TapTouchHead / Tappanci / Tapdamuzi，
                与 HitAreas 一一对应，这种 1:1 的关系比"按部位猜"更贴作者设计。
                """
                s = (str(e.get("group") or "") + " " + str(e.get("label") or "")).lower()
                best, blen = -1, 0
                for ri, r in enumerate(regions):
                    rid = str(r.get("id") or "").lower()
                    if len(rid) > blen and rid and rid in s:
                        best, blen = ri, len(rid)
                return best
            for e in ordered:
                hint = e.get("hint")
                c, d0 = None, -1.0
                if regions:
                    ri = _entry_reg(e)
                    if ri >= 0:
                        # ① 动作名对得上某个作者区 → 就落在这个区里
                        pool = [c2 for c2 in cells if c2.get("_reg") == ri]
                        c, d0 = _pick(pool) if pool else (None, -1.0)
                        if c is not None and d0 < 0.02:      # 这个区挤满了
                            c = None
                    if c is None:
                        # ② 作者区里与动画部位相符的（没 hint 就任取作者区）
                        pool = [c2 for c2 in cells if c2.get("_reg", -1) >= 0 and
                                (not hint or regions[c2["_reg"]].get("zone") == hint)]
                        c, d0 = _pick(pool) if pool else (None, -1.0)
                        if c is not None and d0 < 0.02:
                            c = None
                if c is None:
                    pool = [c2 for c2 in cells if c2["z"] == hint]
                    c, d0 = _pick(pool) if pool else (None, -1.0)
                if c is None or d0 < 0.02:      # 该部位没格子 / 已挤满 → 全局最远点
                    c, d0 = _pick(cells)
                if c is None:
                    break
                c["_used"] = True
                z = str(c.get("z") or hint or "body")
                if c.get("_reg", -1) >= 0:
                    z = str(regions[c["_reg"]].get("zone") or z)
                seed = {"nx": round(float(c["x"]), 4), "ny": round(float(c["y"]), 4),
                        "zone": z, "part": str(c.get("p") or ""), "n": len(chosen) + 1}
                if c.get("_reg", -1) >= 0:
                    seed["reg"] = str(regions[c["_reg"]].get("id") or "")
                seed.update({k: e.get(k) for k in ("kind", "group", "index", "id", "label", "snd")})
                chosen.append(seed)
            return chosen

        def _request_model_rect():
            """只问一次"模型在画布里的可见矩形"，纯 JS 算术、不抓帧（≈1ms）。

            拖动只平移的那条快路径上，热区/剪影都复用旧格子（靠 _cm_stage
            换算位移），唯一必须刷新的是模型包围盒 —— move_stage_to 里
            "至少露 24px" 的夹边全靠它算。
            """
            def _rect_cb(res):
                try:
                    d = json.loads(res) if isinstance(res, str) else (res or {})
                    mw, mh = float(d.get("w") or 0), float(d.get("h") or 0)
                    if mw <= 1 or mh <= 1:
                        return
                    cw = float(d.get("cw") or 0) or float(pet.width())
                    ch = float(d.get("ch") or 0) or float(pet.height())
                    if cw > 1 and ch > 1:
                        state["model_rect_norm"] = (
                            float(d.get("x") or 0) / cw,
                            float(d.get("y") or 0) / ch,
                            mw / cw, mh / ch)
                        _WSTATE["model_rect_norm"] = state["model_rect_norm"]
                        # 记下"这份包围盒是按哪个 Stage 原点量出来的"：
                        # move_stage_to 要按当前几何算贴边极限，拖动期间 norm 不会
                        # 每帧刷新，必须靠这个原点把位移补回去（否则夹边漂移）。
                        _sq = state.get("stage_rect")
                        if _sq and len(_sq) >= 4:
                            state["model_rect_stage"] = (int(_sq[0]), int(_sq[1]))
                            _WSTATE["model_rect_stage"] = state["model_rect_stage"]
                        # 顺手用这份「真实可见矩形」校正 model_aspect。
                        # 模型刚就绪时 getModelRect 可能还报着内部画布
                        # （正方形）的包围盒：Stage 宽度就会按 1:1 算成
                        # 很窄，而 applyFit 只按高度贴合，模型比 Stage 宽
                        # 出一截 —— 那一截看得见却不在命中区里，点下去
                        # 直接穿透到下面的窗口。
                        try:
                            _asp = mw / mh
                            _old = float(state.get("model_aspect") or 0.0)
                            if _old <= 0 or abs(_asp - _old) / _asp > 0.025:
                                state["model_aspect"] = _asp
                                _need = max(
                                    int(BASE_W * state["scale"]),
                                    int(BASE_H * state["scale"] * _asp * 0.95) + 24)
                                if abs(_need - int(pet.stage_rect()[2])) > 10:
                                    # 交给主线程 _poll_cmds 应用（这里在
                                    # Electron 桥线程，不能直接动 Qt 控件）
                                    state["pending_model_width"] = int(_need)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                except (TypeError, ValueError, AttributeError):
                    pass

            try:
                _send_js({"type": "getModelRect"}, _rect_cb)
                if _PERF["on"]:
                    _PERF["rect_n"] += 1
            except Exception:
                pass

        def _build_click_map(assets=None):
            """向 JS 要一张"可见网格"，给每个动画分配触发点。异步，不卡界面。"""
            assets = assets or state.get("assets")
            if not assets:
                return
            entries = _hotspot_entries(assets)
            if not entries:
                state["click_map"] = None
                return

            def _finish(cells, asp, ms, regions, stg=None):
                if len(entries) > MAX_HOTSPOTS:
                    _log_line("WARN", "动画 %d 个，超过热区上限 %d：只给前 %d 个分配了位置"
                              % (len(entries), MAX_HOTSPOTS, MAX_HOTSPOTS))
                seeds = _assign_hotspots(entries, cells, asp, regions)

                def _publish(final_seeds):
                    # 命中区（Qt 覆盖层的 setMask）要用**格子列表**本身：
                    # click_map["cells"] 是格子**数量**，不能拿来铺 mask。
                    state["_cm_cells"] = cells
                    # 记下这套格子是哪次模型建出来的：拖动只平移时靠它判断
                    # "能不能直接复用"，换模型必须重算。用 model_url 而不是
                    # assets["model"]，两边才是同一份标识、能直接比。
                    state["_cm_model"] = str(state.get("model_url") or "")
                    # 每次重算都要换序号：格子数量可能一个不变，靠数量当 key
                    # 会让 setMask 被判成"没变化"，模型一移动命中区就停在原地。
                    state["_cm_seq"] = int(state.get("_cm_seq") or 0) + 1
                    # 格子是按渲染层抓帧那一刻的 Stage 量出来的，就用那个值当
                    # 命中区的换算基准。**不能**在这里重读 stage_rect()：快速
                    # 来回拖动时，JS 回包已经比抓帧晚了几十毫秒/十几像素，
                    # 基准一错，整块命中区就被搬歪，松手后模型看得见却点不到、
                    # 右键也落到桌面（且不会有下一次重算来纠正）。
                    try:
                        if stg and len(list(stg)) == 4 and float(list(stg)[2]) > 1:
                            state["_cm_stage"] = [int(round(float(v)))
                                                  for v in list(stg)[:4]]
                        else:
                            state["_cm_stage"] = list(pet.stage_rect())
                    except Exception:
                        state["_cm_stage"] = None
                    # 顺手把模型可见矩形刷新一遍：它归一化到**整块画布**，
                    # Stage 一移动旧值就偏了。move_stage_to 的"至少露 24px"
                    # 边界是按它算的，不刷新会把模型夹在错误的位置（拖完就推不动）。
                    try:
                        _request_model_rect()
                    except Exception:
                        pass
                    state["click_map"] = {"model": assets.get("model"), "seeds": final_seeds,
                                          "cells": len(cells), "at": time.monotonic(),
                                          "grid_ms": ms, "regions": [r.get("id") for r in (regions or [])],
                                          "cells_raw": state.get("_cm_raw"),
                                          "cols": state.get("_cm_cols"),
                                          "rows": state.get("_cm_rows"),
                                          "cell_px": state.get("_cm_cell_px"),
                                          # 作者交互区的矩形留着：双击时先看点在哪个区里，
                                          # 再在该区的热区里挑（不让相邻区的点抢走）。
                                          "boxes": [{"id": r.get("id"), "zone": r.get("zone"),
                                                     "nx0": r.get("nx0"), "ny0": r.get("ny0"),
                                                     "nx1": r.get("nx1"), "ny1": r.get("ny1")}
                                                    for r in (regions or [])],
                                          "min_sep": round(_seeds_minsep(final_seeds, asp), 4)}
                    _WSTATE["click_seeds"] = len(final_seeds)
                    # 诊断用：列出每个热区落在画布上的归一化坐标，便于核对
                    # 是否落在模型可见像素上（不影响点击逻辑）。
                    _WSTATE["click_seed_list"] = [
                        {"n": s.get("n"), "nx": s.get("nx"), "ny": s.get("ny"),
                         "label": s.get("label"), "zone": s.get("zone")}
                        for s in final_seeds]
                    # 诊断用：热区坐标是抓帧那一刻的坐标系，外部要核对"当前该在
                    # 哪里"必须知道抓帧/当前两套 Stage 与画布尺寸（只读，不参与判定）。
                    try:
                        _WSTATE["click_cm"] = {
                            "cap_stage": state.get("_cm_stage"),
                            "cur_stage": [int(v) for v in list(pet.stage_rect())[:4]],
                            "canvas": [int(pet.width() or 0), int(pet.height() or 0)]}
                    except Exception:
                        _WSTATE["click_cm"] = None
                    _log_line("LOG", "clickmap: %d 动画 / %d 格 / %d 个热区 / %d 作者交互区 / %sms / 最小间距 %.3f"
                              % (len(entries), len(cells), len(final_seeds), len(regions or []), ms,
                                 state["click_map"]["min_sep"]))

                # 格子已经按 alpha 过滤过，但热区在模型动作/切模后可能落到已
                # 变透明的边缘。发布前用当前帧逐点复核一次；不透明度不足的
                # 热区吸附到最近的实心格，避免"点不到"。
                def _check_visible(res):
                    data = {}
                    try:
                        data = json.loads(res) if isinstance(res, str) else (res or {})
                    except Exception:
                        data = {}
                    pts = data.get("points") if isinstance(data, dict) else None
                    if not isinstance(pts, list) or len(pts) != len(seeds):
                        _publish(seeds)
                        return
                    used = set()
                    moved = 0
                    for i, s in enumerate(seeds):
                        try:
                            a = int((pts[i] or {}).get("a", -1))
                        except Exception:
                            a = -1
                        if a >= 24:
                            used.add((round(float(s["nx"]), 4), round(float(s["ny"]), 4)))
                            continue
                        best, bd = None, 1e9
                        for c in cells:
                            if int(c.get("a") or 0) < 40:
                                continue
                            key = (round(float(c["x"]), 4), round(float(c["y"]), 4))
                            if key in used:
                                continue
                            dx = float(c["x"]) - float(s["nx"])
                            dy = (float(c["y"]) - float(s["ny"])) * asp
                            d = dx * dx + dy * dy
                            if d < bd:
                                bd, best = d, c
                        if best is None:
                            continue
                        s["nx"] = round(float(best["x"]), 4)
                        s["ny"] = round(float(best["y"]), 4)
                        used.add((s["nx"], s["ny"]))
                        moved += 1
                    if moved:
                        _log_line("WARN", "clickmap: %d 个热区落在透明处，已吸附到最近实心格" % moved)
                    _publish(seeds)

                _send_js({"type": "alphaAt",
                          "points": [{"nx": s["nx"], "ny": s["ny"]} for s in seeds]},
                         _check_visible)

            def _regions_from_boxes(data, hits):
                """HitAreas → 屏幕归一化矩形（退化/画布外/纯道具区也收，靠挤满再退回）。"""
                out = []
                by_id = {h["id"]: h for h in hits}
                for r in ((data or {}).get("regions") or []):
                    hid = str(r.get("id") or "")
                    if not hid:
                        continue
                    try:
                        x0 = max(0.0, min(1.0, float(r.get("nx0"))))
                        x1 = max(0.0, min(1.0, float(r.get("nx1"))))
                        y0 = max(0.0, min(1.0, float(r.get("ny0"))))
                        y1 = max(0.0, min(1.0, float(r.get("ny1"))))
                    except Exception:
                        continue
                    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
                        continue
                    nm = str((by_id.get(hid) or {}).get("name") or r.get("name") or "")
                    # zone 先按作者起的名字/Id（含英文 TouchHead→head）判，再退回部件名+高度
                    zone = (_hint_of_name(hid) or _hint_of_name(nm)
                            or _zone_for_point([hid, nm], (y0 + y1) * 0.5)[0])
                    try:
                        cx = float(r.get("cx"))
                        cy = float(r.get("cy"))
                    except Exception:
                        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                    if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
                        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                    out.append({"id": hid, "name": nm, "zone": zone,
                                "nx0": x0, "ny0": y0, "nx1": x1, "ny1": y1,
                                "cx": round(cx, 4), "cy": round(cy, 4)})
                return out

            def _on_grid(res):
                data = {}
                try:
                    data = json.loads(res) if isinstance(res, str) else (res or {})
                except Exception:
                    data = {}
                cells = [c for c in (data.get("cells") or [])
                         if isinstance(c, dict) and "x" in c and "y" in c]
                if not data.get("ok") or not cells:
                    # 首帧还没到（模型刚就绪）→ 稍后自动重试，最多 6 次
                    _cm_try["n"] += 1
                    if _cm_try["n"] <= 6:
                        # 回调运行在 Electron 页面的后台线程，不能用 Qt 定时器；
                        # 用普通线程定时器重试，避免 "Timers can only be used ..." 警告。
                        threading.Timer(0.7, lambda: _build_click_map(assets)).start()
                    else:
                        _log_line("WARN", "clickmap failed: %r" % (str(res)[:160],))
                    return
                _cm_try["n"] = 0
                asp = float(data.get("ch") or 1) / max(1.0, float(data.get("cw") or 1))
                ms = data.get("ms")
                state["_cm_raw"] = int(data.get("cells_raw") or 0)
                state["_cm_cols"] = data.get("cols")
                state["_cm_rows"] = data.get("rows")
                state["_cm_cell_px"] = data.get("cell_px")
                state["_cm_band"] = data.get("band")
                stg = data.get("stage")          # 抓格子的那一刻的 Stage（屏幕坐标）
                hit_ids = _hit_areas_of(assets.get("model"))
                if not hit_ids:
                    _finish(cells, asp, ms, None, stg)
                    return

                def _on_boxes(bs):
                    bd = {}
                    try:
                        bd = json.loads(bs) if isinstance(bs, str) else (bs or {})
                    except Exception:
                        bd = {}
                    regions = _regions_from_boxes(bd, hit_ids)
                    miss = bd.get("misses") or []
                    if miss:
                        _log_line("WARN", "HitAreas 里这几个没找到对应网格，忽略：%r" % (miss[:8],))
                    if not regions:
                        _log_line("WARN", "作者声明了 HitAreas，但一个能用的区都没算出来，退回自动铺点")
                    # 作者画定的每个交互区至少留一个可点位置：网格是自适应方格，
                    # 但窄小的区（手指、脸、饰品）可能一格都压不住。用区中心补一格，
                    # 否则该区的动作只能退到全局最远点 —— 跑到别的部位上去，
                    # 就是用户说的"作者的交互逻辑没实现"。
                    if regions:
                        has_reg = set()
                        for c in cells:
                            for ri, r in enumerate(regions):
                                if (r["nx0"] <= float(c["x"]) <= r["nx1"]
                                        and r["ny0"] <= float(c["y"]) <= r["ny1"]):
                                    has_reg.add(ri)
                                    break
                        for ri, r in enumerate(regions):
                            if ri in has_reg:
                                continue
                            cells.append({"x": r.get("cx"), "y": r.get("cy"), "a": 200,
                                          "p": "", "o": -1, "synth": True})
                        if len(cells) > 0 and len(has_reg) < len(regions):
                            _log_line("LOG", "clickmap: %d/%d 个作者交互区没有网格点，已用区中心兜底"
                                      % (len(regions) - len(has_reg), len(regions)))
                    _finish(cells, asp, ms, regions, stg)

                _send_js({"type": "hitBoxes", "ids": hit_ids}, _on_boxes)

            # 网格不再固定 18×26 铺满整块画布：local 模式下 Stage 只占屏幕一小块，
            # 固定粗网格会让所有热区塌进同一列（表现为"停在屏幕固定区域"）。
            # 这里只给"目标格数"，由渲染层按 Stage 面积自适应算行列。
            _send_js({"type": "getClickMap", "cells": 900}, _on_grid)

        # === 热区坐标换算：抓帧坐标 ⇄ 当前画布坐标 ===
        # 热区 seed 的 nx/ny 是"抓帧那一刻"的画布归一化坐标。模型一拖动/缩放，
        # 同一块像素在画布上的位置就整体平移 + 缩放了，但判定链一直是拿"现在的
        # 点击点"直接和"当时的 nx/ny"比距离；而掩膜那条路
        # （_apply_interaction_mask）一直在搬这些小方块 —— 两条链各算各的，
        # 结果就是拖完 / 缩完以后热区脱离模型：原部位点不动，旧位置反而还能触发。
        # 这里补上和掩膜完全一致的一套换算：cur = a × cap + b
        #   a = 当前 Stage 尺寸 / 抓帧 Stage 尺寸        （缩放比）
        #   b = (当前 Stage 原点 − 抓帧 Stage 原点 × a) / 画布尺寸  （位移）
        def _cm_map():
            """抓帧坐标 → 当前坐标的归一化仿射 (ax, bx, ay, by)；拿不到几何返回 None。"""
            try:
                cs = state.get("_cm_stage")
                if not cs or len(list(cs)) < 4:
                    return None
                cs = [float(v) for v in list(cs)[:4]]
                st = [float(v) for v in list(pet.stage_rect())[:4]]
                if cs[2] <= 1 or cs[3] <= 1:
                    return None
                pw = float(pet.width() or 1)
                ph = float(pet.height() or 1)
                ax = st[2] / cs[2]
                ay = st[3] / cs[3]
                if ax <= 1e-6 or ay <= 1e-6:
                    return None
                bx = (st[0] - cs[0] * ax) / pw
                by = (st[1] - cs[1] * ay) / ph
                return ax, bx, ay, by
            except Exception:
                return None

        def _click_to_capture(nx, ny):
            """当前画布上的点 → 抓帧那一刻的坐标系（热区、作者交互区都存那套）。"""
            m = _cm_map()
            if not m:
                return float(nx), float(ny)
            ax, bx, ay, by = m
            return ((float(nx) - bx) / ax, (float(ny) - by) / ay)

        def _seed_to_current(s):
            """热区 → 当前画布归一化坐标（画提示圆点、自检输出用）。"""
            try:
                nx, ny = float(s["nx"]), float(s["ny"])
            except Exception:
                return None
            m = _cm_map()
            if not m:
                return nx, ny
            ax, bx, ay, by = m
            return (ax * nx + bx, ay * ny + by)

        def _seed_dist(nx, ny, s):
            """点到某个热区种子的距离（按窗口像素比例算，y 用画布纵横比拉伸）。"""
            try:
                cw, ch = pet._canvas_geom()[:2]
            except Exception:
                cw, ch = 1, 1
            dx = float(s["nx"]) - nx
            dy = (float(s["ny"]) - ny) * (float(ch or 1) / float(cw or 1))
            return (dx * dx + dy * dy) ** 0.5

        def _nearest_seed(nx, ny):
            """当前模型的热区里离点击点最近的那个（它的"领地"覆盖这块模型）。

            比较一律在**抓帧坐标系**里做：点击点先按"Stage 位移 + 缩放比"
            换算回去（_click_to_capture），热区本身还是原坐标。
            """
            cm = state.get("click_map") or {}
            seeds = cm.get("seeds") or []
            if not seeds:
                return None
            url = (state.get("assets") or {}).get("model")
            if cm.get("model") and url and cm.get("model") != url:
                return None            # 地图还是上一个模型的，等重建
            cx, cy = _click_to_capture(nx, ny)
            pool = seeds
            # 作者画了 HitAreas 时优先"点在哪个区里就用哪个区的热区"：
            # 相邻两个区可能重叠/紧挨，纯最近邻会把头部的点击判给身体。
            # 区的矩形也是抓帧那一刻量出来的，所以同样用换算后的点去比。
            boxes = cm.get("boxes") or []
            if boxes:
                ids = set()
                for b in boxes:
                    try:
                        if (float(b["nx0"]) <= cx <= float(b["nx1"])
                                and float(b["ny0"]) <= cy <= float(b["ny1"])):
                            ids.add(str(b.get("id") or ""))
                    except Exception:
                        continue
                if ids:
                    sub = [s for s in seeds if str(s.get("reg") or "") in ids]
                    if sub:
                        pool = sub
            best, bd = None, 1e9
            for s in pool:
                d = _seed_dist(cx, cy, s)
                if d < bd:
                    bd, best = d, s
            return best

        def _dblclick_at(nx, ny):
            """双击判定核心：只吃画布归一化坐标（像素入口与自检入口共用）。

            热区存的是抓帧那一刻的坐标，判定全程换算回那套坐标系再比较，
            所以拖动 / 缩放之后原部位照样点得中。
            """
            # ① 首选：热区地图 —— 每个动画独占一个互不重合的位置，
            #    双击落在谁的"领地"（最近邻）就播谁，因此每个动画都点得到。
            seed = _nearest_seed(nx, ny)
            _cx, _cy = _click_to_capture(nx, ny)
            # alpha 判的是**当前帧**的剪影 → 用当前坐标
            _alpha = pet.alpha_at_norm(nx, ny)
            if _alpha < 24:
                # 点在透明像素上：只有几乎正压在标记圆点上才算"点到了那个动画"，
                # 否则模型每个动作一动，贴在发梢/轮廓上的热区就会点不动。
                if not seed or _seed_dist(_cx, _cy, seed) > 0.022:
                    return {"hit": None, "why": "transparent"}   # 空白处：不响应

            if seed:
                if seed["kind"] == "motion":
                    _play_anim(group=seed.get("group"), index=seed.get("index"), loop=False)
                else:
                    _play_anim(builtin=seed.get("id"), loop=False)
                _flash_zone_hint("%s · %s" % (ZONE_LABEL.get(seed["zone"], seed["zone"]),
                                               ("♪ " if seed.get("snd") else "")
                                               + str(seed["label"])))
                _cur = _seed_to_current(seed)
                pet.show_hotspots([(_cur[0], _cur[1], seed["n"])] if _cur else [], 1.2)
                _log_line("LOG", "dblclick #%d %s（%s/%s）@(%.3f, %.3f)"
                          % (seed["n"], seed["label"], seed["zone"], seed["part"], nx, ny))
                return {"hit": "seed", "n": seed["n"], "label": seed["label"],
                        "zone": seed["zone"], "part": seed["part"],
                        "kind": seed["kind"], "group": seed.get("group"),
                        "index": seed.get("index"), "id": seed.get("id")}

            # ② 兜底（热区地图还没建好，例如模型刚加载）：走部位名 + 高度分段
            def _cb(res):
                data = {}
                try:
                    data = json.loads(res) if isinstance(res, str) else (res or {})
                except Exception:
                    data = {}
                names = [h.get("partName") for h in (data.get("parts") or [])
                         if isinstance(h, dict) and h.get("partName")]
                zone, builtin, hint = _zone_for_point(names, pet.bounds_rel_y(ny))
                motion = _find_zone_motion(hint) if (state.get("assets") or {}).get("motions") else None
                if motion:
                    _play_anim(group=motion[0], index=motion[1], loop=False)
                    label = motion[2]
                else:
                    _play_anim(builtin=builtin, loop=False)
                    label = _builtin_label(builtin)
                _flash_zone_hint("%s · %s" % (ZONE_LABEL.get(zone, zone), label))

            _send_js({"type": "hitTestParts", "nx": nx, "ny": ny}, _cb)
            # 模型列表可能还没缓存，顺手补一次，下次双击判定更准
            if not state.get("assets"):
                _refresh_assets()
            return {"hit": "fallback", "nx": nx, "ny": ny}

        def _on_double_click(pos):
            norm = pet.canvas_norm(pos)
            if not norm:
                return
            _dblclick_at(norm[0], norm[1])

        # === 右键菜单动作 ===
        def _request_self_close(all_instances=False):
            """右键「关闭」= 真正关掉这个模型（释放内存），不是只隐藏。

            worker 自己删不掉父进程注册表里的条目，所以先回调父进程，由父进程
            走 _destroy → /shutdown → 结束进程。父进程不通（独立运行 / 插件已退出）
            时直接退出进程：内存照样还给系统，父进程侧的死实例由 _prune_dead 自愈。
            all_instances=True 时让父进程把所有模型一起关掉（右键「全部关闭」）。
            """
            port = int(_WSTATE.get("parent_port") or _PARENT_PORT or 0)
            if not port:
                self.close_window()     # 独立运行（没有父进程）：沿用旧的隐藏语义
                return

            def _post():
                endpoint = "/worker_close_all" if all_instances else "/worker_self_close"
                payload = json.dumps({"pid": os.getpid(),
                                      "model": _WSTATE.get("model") or ""}).encode("utf-8")
                req = urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (port, endpoint),
                    data=payload, method="POST",
                    headers={"Content-Type": "application/json; charset=utf-8"})
                # 全部关闭时父进程要逐个收走（每个最多 6 秒），超时给宽松一点
                timeout = 22.0 if all_instances else 5.0
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        r.read()
                except Exception:
                    _log_line("WARN", "worker 回调父进程关闭失败，直接退出（内存已释放）")
                    # 自己退出前必须先把 Electron 显示壳收掉，否则屏幕上会留下
                    # 一个孤儿透明窗继续显示模型（"关闭了模型还在"）。
                    _stop_electron_display()
                    os._exit(0)

            threading.Thread(target=_post, daemon=True).start()

        def _shutdown_pet():
            _request_self_close()

        def _shutdown_all_pets():
            _request_self_close(all_instances=True)

        def _apply_model_file(model_file, root=None):
            """切到指定 model3.json（走「重载渲染器文档」，即时切换那条已在部分模型上炸模）。"""
            try:
                _exit_part_mode()
            except Exception:
                pass
            model_dir = str(Path(model_file).parent)
            try:
                http_url = serve_model(model_dir)
            except Exception:
                return
            new_model_url = model_arg_for_serve(model_file)
            if not new_model_url:
                return
            self.send_command({"type": "__reload_model__", "modelUrl": new_model_url})
            if self.on_model_switch:
                self.on_model_switch(model_file, http_url)
            save_ui_cfg(last_model=model_file, model_root=root)

        # === 全局快捷键的落地动作（钩子只负责入队，真正干活在主线程这里）===
        def _current_model_file():
            try:
                return local_model_json_of_url(state.get("model_url")) or ""
            except Exception:
                return ""

        def _cycle_model(step):
            """Ctrl+←/→：在「模型目录」里按顺序换上一个/下一个模型。

            槽位不动（位置/缩放/跟随沿用），等于右键「切换模型」里点下一条，
            只是不用展开菜单、也不用记住自己在列表第几个。
            """
            # 换模型要重建渲染页（~2s）：重载途中 ready 是 False，直接忽略，
            # 免得连按把一串 __reload_model__ 堆在队列里。
            if not state.get("ready"):
                _log_line("WARN", "快捷键切模型被丢弃：模型还没就绪")
                return False
            now = time.monotonic()
            if now - float(state.get("_model_cycle_at") or 0.0) < 2.0:
                _log_line("WARN", "快捷键切模型被节流（2s 内只切一次）")
                return False
            root = load_ui_cfg().get("model_root") or ""
            items = scan_models(root)
            if len(items) < 2:
                _log_line("WARN", "快捷键切模型被丢弃：model_root=%r 下只有 %d 个模型"
                          % (root, len(items)))
                _flash_zone_hint("模型目录里只有一个模型")
                return False
            cur = _current_model_file()
            idx = -1
            for i, (_nm, pth) in enumerate(items):
                try:
                    if os.path.normcase(os.path.abspath(pth)) == os.path.normcase(
                            os.path.abspath(cur)):
                        idx = i
                        break
                except Exception:
                    continue
            if idx < 0:
                nxt = items[0] if step > 0 else items[-1]
            else:
                nxt = items[(idx + (1 if step > 0 else -1)) % len(items)]
            state["_model_cycle_at"] = now
            _flash_zone_hint("切换模型：%s" % nxt[0])
            _log_line("INFO", "快捷键切模型 → %s" % nxt[0])
            _apply_model_file(nxt[1], root=root)
            return True

        def _flat_actions():
            """把「内置动画 + 模型动作（分组/条目）」摊平成一条有序列表。"""
            assets = state.get("assets") or {}
            out = []
            for item in (assets.get("builtin_animations") or []):
                aid = item.get("id")
                if aid:
                    out.append({"kind": "builtin", "id": aid,
                                "label": item.get("label") or aid})
            for g in (assets.get("motions") or []):
                gname = g.get("group") or ""
                for i, f in enumerate(g.get("files") or []):
                    out.append({
                        "kind": "motion", "group": gname, "index": i,
                        "label": "%s%s" % (_motion_label(f),
                                           "" if not gname else "（%s）" % gname),
                    })
            return out

        def _cycle_action(step):
            """Ctrl+↑/↓：在内置动画 + 模型动作里按顺序播上一个/下一个。"""
            acts = _flat_actions()
            if not acts:
                _log_line("WARN", "快捷键切动作被丢弃：动作列表为空（assets 未就绪）")
                _flash_zone_hint("模型还没加载完 / 没有动作")
                return False
            cur = int(state.get("anim_cursor") or 0)
            if not (0 <= cur < len(acts)):
                cur = 0
            idx = (cur + (1 if step > 0 else -1)) % len(acts)
            state["anim_cursor"] = idx
            it = acts[idx]
            if it["kind"] == "builtin":
                _play_anim(builtin=it["id"])
            else:
                _play_anim(group=it["group"], index=it["index"])
            _flash_zone_hint("动作 %d/%d：%s" % (idx + 1, len(acts), it["label"]))
            _log_line("INFO", "快捷键切动作 %d/%d：%s"
                      % (idx + 1, len(acts), it["label"]))
            return True

        def _handle_hotkey(name):
            """键盘钩子投进来的 Ctrl+方向键（只在就绪后、且光标在模型上时才会到）。"""
            if not state.get("ready"):
                _log_line("WARN", "快捷键 %s 被丢弃：模型还没就绪" % name)
                return
            if name == "left":
                _cycle_model(-1)
            elif name == "right":
                _cycle_model(1)
            elif name == "up":
                _cycle_action(-1)
            elif name == "down":
                _cycle_action(1)

        # === 装全局快捷键钩子 ===
        def _cursor_on_me():
            """这次按键该不该归我，返回 (是否归我, 原因) —— 原因只用于诊断日志。

            注意：这个函数跑在键盘钩子的后台线程里，**绝对不能碰 Qt 对象**
            （isVisible/QCursor 都是 GUI 线程的东西，跨线程调是随机崩）。
            所以可见性读 _WSTATE 快照、光标用 Win32 GetCursorPos、几何读 state。
            """
            try:
                if not _WSTATE.get("visible"):
                    return False, "window-hidden"
                import ctypes
                from ctypes import wintypes as wt
                pt = wt.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                sx, sy, sw, sh = state.get("stage_rect") or (0, 0, 0, 0)
                pad = 40
                if (sx - pad <= pt.x <= sx + sw + pad
                        and sy - pad <= pt.y <= sy + sh + pad):
                    return True, "cursor-in-stage"
                # 只有一个桌宠时，快捷键不该要求用户先把鼠标放到模型上
                n = _visible_instance_count()
                if n <= 1:
                    return True, "only-instance"
                return False, "cursor-outside(n=%d)" % n
            except Exception as exc:
                return False, "error:%r" % (exc,)

        _inst_cnt = {"ts": 0.0, "n": 1}

        def _pid_alive(pid):
            """state_*.json 里 pid 还活着吗（死进程留下的僵尸文件不能算一个实例）。"""
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
                if not h:
                    return False
                try:
                    code = ctypes.c_ulong()
                    if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                        return True
                    return int(code.value) == 259              # STILL_ACTIVE
                finally:
                    k32.CloseHandle(h)
            except Exception:
                return True

        def _visible_instance_count():
            """有几个可见的桌宠窗口（读 _workers/state_*.json，3 秒缓存）。"""
            now = time.monotonic()
            if now - float(_inst_cnt["ts"] or 0.0) < 3.0:
                return int(_inst_cnt["n"])
            n = 0
            try:
                for fn in os.listdir(_WORKERS_DIR):
                    if not (fn.startswith("state_") and fn.endswith(".json")):
                        continue
                    try:
                        with open(os.path.join(_WORKERS_DIR, fn), "r", encoding="utf-8") as f:
                            d = json.load(f)
                    except Exception:
                        continue
                    pid = d.get("pid")
                    if pid and not _pid_alive(pid):
                        continue                      # 僵尸文件
                    if d.get("visible") is False:
                        continue                      # 明确隐藏（字段缺失按可见计）
                    n += 1
            except Exception:
                n = 1
            _inst_cnt["ts"] = now
            _inst_cnt["n"] = max(1, n)
            return int(_inst_cnt["n"])

        def _queue_hotkey(name):
            try:
                self._cmd_queue.put_nowait({"type": "__hotkey__", "key": str(name)})
            except Exception:
                pass

        try:
            _hk = _GlobalHotkeys(_cursor_on_me, _queue_hotkey, _log_line)
            state["hotkeys"] = _hk
            if _hk.start():
                _log_line("INFO", "全局快捷键已就绪：Ctrl+←/→ 切模型，Ctrl+↑/↓ 切动作")
            else:
                _log_line("WARN", "全局快捷键未安装（非 Windows 或钩子失败）")
        except Exception as _hk_exc:
            _log_line("WARN", "全局快捷键安装失败: %r" % (_hk_exc,))

        def _pick_model():
            """选择模型目录 → 记住它 → 立刻弹出该目录下的模型列表（超过一个时才弹）。

            三处改写都是为了不再卡 GUI 线程：
              1) 目录选择强制用 Qt 自己的对话框（DontUseNativeDialog）。原生
                 getExistingDirectory 在这个"置顶 + Qt::Tool + 无边框"的桌宠窗口
                 上偶尔会生成一个没有正常所有者的模态窗口 —— 对话框不显示，
                 但事件循环已经被它阻塞了，表现就是整个插件"未响应"。
              2) 扫目录（264 个模型实测 0.67s）挪到后台线程，主线程用 60ms 的
                 QTimer 轮询结果，选完目录后界面不会僵住。
              3) 多模型时的选择菜单用 popup() 弹出，不再 exec_()。
            """
            try:
                _exit_part_mode()
            except Exception:
                pass
            start = load_ui_cfg().get("model_root") or os.path.expanduser("~")
            if not os.path.isdir(start):
                start = os.path.expanduser("~")
            # 先把自己抬到前台，否则置顶窗口底下的对话框可能被抢走激活
            try:
                pet.raise_()
                pet.activateWindow()
            except Exception:
                pass
            dir_path = QFileDialog.getExistingDirectory(
                pet, "选择 Live2D 模型目录", start,
                QFileDialog.DontUseNativeDialog
            )
            if not dir_path:
                return
            save_ui_cfg(model_root=dir_path)

            # 后台扫描 + 主线程轮询（scan 是共享 dict，不阻塞 UI）
            scan = {"done": False, "items": [], "used": False}
            _log_line("INFO", "开始扫描模型目录：%s" % dir_path)

            def _worker():
                try:
                    items = scan_models(dir_path, ttl=0) or []
                    # 顺手把菜单里 ♪ 标记要读的配音信息也预热掉：264 个模型逐个
                    # 读 model3.json 实测好几秒，留在 GUI 线程填菜单照样会僵住。
                    for _n, _p in items:
                        try:
                            model_sound_info(_p)
                        except Exception:
                            pass
                    scan["items"] = items
                except Exception as exc:
                    _log_line("WARN", "扫描模型目录失败: %r" % (exc,))
                scan["done"] = True

            threading.Thread(target=_worker, name="model-scan", daemon=True).start()

            def _poll():
                if not scan["done"]:
                    QTimer.singleShot(60, _poll)
                    return
                if scan["used"]:
                    return
                scan["used"] = True
                items = scan["items"]
                if not items:
                    _log_line("WARN", "目录里没有 *.model3.json：%s" % dir_path)
                    return
                if len(items) == 1:
                    _apply_model_file(items[0][1], root=dir_path)
                    return
                pick = QMenu("选择模型", pet)
                pick.setWindowFlag(Qt.WindowStaysOnTopHint, True)
                _fill_model_menu(pick, items, root=dir_path)
                pet._pick_menu = pick        # 保活：popup 不阻塞，局部变量会被回收
                pick.popup(QCursor.pos())

            QTimer.singleShot(60, _poll)

        def _fill_model_menu(menu, items, root, last=None):
            """把 [(名, 路径)] 填进菜单；条目多时按首字母分组，避免菜单超出屏幕。

            配音标记直接写在条目上（不在菜单顶上放"♪ = …"的图例行 ——
            列表长起来会按首字母折叠，顶上的图例容易让人以为在说看得见的那几条）：
                ♪ 名字 · 配音 13/15          model3.json 声明了 Sound，播放会出声
                ♪ 名字 · 语音素材 12（未接入） 目录里有音频文件，但没声明 Sound
            """
            cfg_last = last if last is not None else (load_ui_cfg().get("last_model") or "")

            def _mark(name, path):
                try:
                    n_snd, n_all, n_aud = model_sound_info(path)
                except Exception:
                    return name, ""
                if n_snd:
                    return ("♪ %s · 配音 %d/%d" % (name, n_snd, n_all),
                            "播放该模型的动作会出声：%d/%d 个动作带配音（model3.json 已声明 Sound）"
                            % (n_snd, n_all))
                if n_aud:
                    return ("♪ %s · 语音素材 %d（未接入）" % (name, n_aud),
                            "目录里有 %d 个音频文件，但 model3.json 没声明 Sound，插件播不出声"
                            % n_aud)
                return name, ""

            marks = {p: _mark(n, p) for n, p in items}

            def _add(menu_, path):
                text, tip = marks[path]
                act = menu_.addAction(
                    text, lambda _c=False, p=path: _apply_model_file(p, root=root))
                if tip:
                    act.setToolTip(tip)
                return act

            if cfg_last:
                for name, path in items:
                    if path == cfg_last:
                        act = _add(menu, path)
                        act.setText("★ " + act.text())
                        act.setCheckable(True)
                        act.setChecked(True)
                        menu.addSeparator()
                        break
            rest = [(n, p) for n, p in items if p != cfg_last]
            if len(rest) > 40:
                groups = {}
                for name, path in rest:
                    key = name[0].upper() if name else "#"
                    groups.setdefault(key, []).append((name, path))
                for key in sorted(groups):
                    sub = menu.addMenu("%s（%d）" % (key, len(groups[key])))
                    for name, path in groups[key]:
                        _add(sub, path)
            else:
                for name, path in rest:
                    _add(menu, path)

        def _fill_model_submenu(sub):
            """填充「切换模型」子菜单（展开时才扫目录，不压右键事件）。

            sub 由 lazy_submenu 预先建好，这里只管往里填。
            """
            sub.clear()
            cfg = load_ui_cfg()
            root = cfg.get("model_root") or ""
            if not root:
                a = sub.addAction("先到「选择模型目录…」挑一个文件夹")
                a.setEnabled(False)
                return
            items = scan_models(root)
            if not items:
                a = sub.addAction("目录里没找到 *.model3.json")
                a.setEnabled(False)
                return
            sub.addAction("模型目录：%s" % os.path.basename(root)).setEnabled(False)
            sub.addSeparator()
            _fill_model_menu(sub, items, root=root)
            sub.addSeparator()
            sub.addAction("重新扫描模型", _rescan_models)

        def _rescan_models():
            _MODEL_CACHE.update({"ts": 0.0, "root": ""})
            _SOUND_CACHE.clear()
            _AUDIO_NAME_CACHE.clear()
            _PATH_INDEX_CACHE.clear()
            _SERVE_JSON_CACHE.clear()
            _warm_model_cache()     # 清完缓存立刻在后台重新预热，别让下一次右键再吃 0.67s

        def _warm_model_cache():
            """启动时在后台把模型目录扫一遍、把 model3.json / 音频信息预热进缓存。

            右键菜单里的「切换模型」要的是 scan_models + model_sound_info 的结果：
            264 个模型冷缓存实测 0.67s，热缓存 4ms。以前这 0.67s 是用户第一次
            右键时才付（所以"有时候"右键就卡死），现在挪到启动后的后台线程里，
            右键永远是热的。只写进程内缓存，不碰任何文件。
            """
            try:
                root = load_ui_cfg().get("model_root") or ""
                if not root or not os.path.isdir(root):
                    return
                t0 = time.monotonic()
                items = scan_models(root) or []
                for _name, path in items:
                    try:
                        model_sound_info(path)
                    except Exception:
                        pass
                _log_line("INFO", "模型缓存预热完成：%d 个模型 / %.0fms"
                          % (len(items), (time.monotonic() - t0) * 1000.0))
            except Exception as exc:
                _log_line("WARN", "模型缓存预热失败: %r" % (exc,))

        # 启动后立即后台预热（不起线程就会占 GUI 线程，等于把卡顿挪回右键）
        threading.Thread(target=_warm_model_cache, name="model-warm", daemon=True).start()

        # === 命令轮询（10ms，从 queue 取命令 → runJavaScript）===
        def _poll_cmds():
            try:
                # 可见性快照以窗口真实状态为准：show/hide 有太多入口（命令队列、
                # 菜单、tool.py、启动参数、快捷键），只靠分支里打标记必然会漏一个，
                # 而漏了就意味着快捷键线程以为窗口不在、把按键全丢掉。
                _vis = bool(pet.isVisible())
                if _vis != bool(_WSTATE.get("visible")):
                    _WSTATE["visible"] = _vis
                _p0 = time.monotonic() if _PERF["on"] else 0.0
                _tk = _p0
                _perf_tick()
                if _PERF["on"]:
                    _tn = time.monotonic()
                    _PERF["seg_pt"] += (_tn - _tk) * 1000.0
                    _tk = _tn
                _need_w = state.pop("pending_model_width", None)
                # alpha 抓帧回包（或超时）后才补重建热区，避免两路抓帧并发
                _gp = state.get("_geom_pending")
                if _gp is not None and (not state.get("_alpha_busy")
                                        or (time.time() - _gp) > 1.2):
                    _consume_geom_pending()
                if _need_w:
                    # 缓动外拉：一次性改宽度会让新模型先只显示一部分、随后整块弹出
                    _animate_stage_width(int(_need_w))
                # 字幕拖动/到期/清屏都会改变命中区；key 不变时 setMask 不会真的执行。
                # 顺带自愈：定时器被主线程拖慢而漏掉的到期字幕在这里兜掉，
                # 否则"字幕不消失"；文本没了却还留着几何的残块也在这里收干净。
                state.pop("_mask_dirty", None)     # 现在每轮都重建掩膜，标记位不再需要
                try:
                    pet._sub_housekeeping()
                    pet._apply_interaction_mask()
                except Exception:
                    pass
                if _PERF["on"]:
                    _tn = time.monotonic()
                    _PERF["seg_mask"] += (_tn - _tk) * 1000.0
                    _tk = _tn
                if (_electron_mode and not self._model_ready
                        and state.pop("_electron_model_loaded", False)):
                    _on_model_ready()
                while True:
                    cmd = self._cmd_queue.get_nowait()
                    cmd_type = cmd.get("type")

                    if cmd_type == "__hotkey__":
                        # 全局钩子只负责投递：真正切模型/切动作要在主线程做
                        _handle_hotkey(str(cmd.get("key") or ""))

                    if cmd_type == "__close__":
                        pet._shutting_down = True
                        _WSTATE["visible"] = False
                        try:
                            _ep = getattr(self, "_electron_page", None)
                            if _ep is not None:
                                _ep.close()
                            _eproc = getattr(self, "_electron_proc", None)
                            if _eproc is not None and _eproc.poll() is None:
                                _eproc.terminate()
                        except Exception:
                            pass
                        pet.hide()
                        pet.close()
                        # 不调用 app.quit()：QApplication/QWebEngine 是进程级单例，
                        # 退出并重建会在下次 show 时 access violation（QtWebEngine
                        # 不支持同进程二次初始化）。"关闭"= 隐藏窗口，资源保留待复用。
                        return

                    elif cmd_type == "show":
                        pet._shutting_down = False  # 复用窗口：恢复绘制
                        _WSTATE["visible"] = True   # 快捷键线程读这份快照
                        if "scale" in cmd:
                            _animate_scale_to(cmd["scale"])
                        if "x" in cmd and "y" in cmd:
                            _move_stage_norm(cmd["x"], cmd["y"])
                        pet.show()
                        try:
                            _ep = getattr(self, "_electron_page", None)
                            if _ep is not None:
                                _ep.show_window()
                        except Exception:
                            pass
                        _pin_electron_topmost()
                        pet.raise_()
                        _pin_qt_topmost()
                        page.runJavaScript(
                            f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
                        )

                    elif cmd_type == "hide":
                        pet.hide()
                        _WSTATE["visible"] = False
                        try:
                            _ep = getattr(self, "_electron_page", None)
                            if _ep is not None:
                                _ep.hide_window()
                        except Exception:
                            pass

                    elif cmd_type == "toggle":
                        if pet.isVisible():
                            pet.hide()
                            _WSTATE["visible"] = False
                        else:
                            pet.show()
                            _WSTATE["visible"] = True

                    elif cmd_type == "setPosition":
                        if "x" in cmd and "y" in cmd:
                            _move_stage_norm(cmd["x"], cmd["y"])

                    elif cmd_type == "setScale":
                        if "scale" in cmd:
                            _animate_scale_to(cmd["scale"])
                        page.runJavaScript(
                            f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
                        )

                    elif cmd_type == "__substale__":
                        # 诊断：把"字幕到期清屏"和"掩膜收窄"压进同一轮（生产里
                        # `_sub_housekeeping` 紧跟 `_apply_interaction_mask` 就是这个
                        # 顺序，主线程忙时最常见）。返回后事件循环才轮到 paint ——
                        # 若绘制被掩膜裁剪，背面缓冲就留下字幕像素，放大模型时"显影"。
                        try:
                            pet._sub_clear()
                            pet._apply_interaction_mask()
                            res = {"ok": True, "sub_rect": list(
                                pet._sub_rect.getRect()) if not pet._sub_rect.isEmpty() else []}
                        except Exception as _e:
                            res = {"ok": False, "error": repr(_e)}
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__busy__":
                        # 诊断：占住主线程，模拟"字幕定时器抢不到点"——拖屏/缩放时
                        # 主线程被 setMask、抓帧占着，_sub_timer 只能晚触发，最后由
                        # 下一轮 _sub_housekeeping 在**同一轮**里清字幕 + 收掩膜。
                        try:
                            _ms = int(cmd.get("ms") or 2000)
                            _t0 = time.monotonic()
                            while (time.monotonic() - _t0) * 1000.0 < _ms:
                                time.sleep(0.01)
                            res = {"ok": True, "ms": _ms}
                        except Exception as _e:
                            res = {"ok": False, "error": repr(_e)}
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "setDisplayMode":
                        _set_display_mode(cmd.get("mode") or "local", persist=False)

                    elif cmd_type == "__menu_close__":
                        # 自检入口：走一遍右键「关闭」的真实路径（回调父进程 → 结束本 worker）
                        try:
                            _request_self_close()
                            res = {"requested": True}
                        except Exception as _e:
                            res = {"error": repr(_e)}
                            _log_line("WARN", "menu close failed: %r" % (_e,))
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__menu_close_all__":
                        # 自检入口：走一遍右键「全部关闭」（父进程把所有模型一起收走）
                        try:
                            _request_self_close(all_instances=True)
                            res = {"requested": True}
                        except Exception as _e:
                            res = {"error": repr(_e)}
                            _log_line("WARN", "menu close-all failed: %r" % (_e,))
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__menu_dump__":
                        # 自检入口：真构建一遍右键菜单，回传条目树
                        # （验证 ♪ 标记 / 图例 / 禁用项 —— 与用户看到的同一份）
                        try:
                            _m = pet._build_context_menu(expand_all=True)
                            res = {"n": len(_m.actions()) if _m is not None else -1,
                                   "tree": menu_tree(_m)}
                            _log_line("INFO", "menu dump: %s 顶层条目" % res["n"])
                        except Exception as _e:
                            res = {"error": repr(_e)}
                            _log_line("ERROR", "menu dump failed: %r" % (_e,))
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__dblclick__":
                        # 自检入口：用画布归一化坐标跑一次真实的双击判定，
                        # 回传命中的热区（验证"每个动画都有可点的位置"）
                        try:
                            res = _dblclick_at(float(cmd.get("nx")), float(cmd.get("ny")))
                        except Exception as _e:
                            res = {"error": repr(_e)}
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__hotspots__":
                        # 自检入口：把全部双击热区一次性标在模型上（和右键菜单里
                        # 「显示双击热区（3 秒）」同一条绘制路径），便于截图核对
                        # "圆点和序号到底有没有被模型盖住"。
                        try:
                            pts = _all_hotspots()
                            secs = float(cmd.get("secs") or 3.0)
                            pet.show_hotspots(pts, secs)
                            res = {"shown": len(pts), "secs": secs,
                                   "overlay_visible": bool(pet.isVisible())}
                        except Exception as _e:
                            res = {"error": repr(_e)}
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__mask__":
                        # 自检入口：回报覆盖层 setMask 的命中区，以及给定屏幕点
                        # 是否落在里面（多模型并存"谁吃掉了点击"就靠它判定）。
                        _mk0 = time.monotonic() if _PERF["on"] else 0.0
                        try:
                            m = pet.mask()
                            pts = cmd.get("pts") or []
                            inside = []
                            for _p in pts[:4000]:
                                try:
                                    _x, _y = int(_p[0]), int(_p[1])
                                except Exception:
                                    continue
                                inside.append([_x, _y,
                                               bool(m.contains(QPoint(_x, _y)))])
                            res = {
                                "mask_empty": bool(m.isEmpty()),
                                "mask_bounds": list(m.boundingRect().getRect()),
                                "mask_rects": len(m.rects()) if hasattr(m, "rects") else -1,
                                "overlay": [pet.width(), pet.height()],
                                "stage": list(pet.stage_rect()),
                                "display_mode": state.get("display_mode"),
                                "pts_in_mask": inside,
                                "dragging": bool(pet._dragging),
                                # 命中区到底走的是哪一支（缩放后穿透就靠这几项分辨）：
                                # cells=剪影格子（可按 dx/dy/sx/sy 重映射）、
                                # alpha=旧的全屏 alpha 位图（**不跟随缩放**）、
                                # norm/None=包围盒兜底。
                                "cm_cells_n": len(state.get("_cm_cells") or []),
                                "cm_stage": state.get("_cm_stage"),
                                "cm_model": str(state.get("_cm_model") or "")[:9],
                                "cm_pending": bool(state.get("_geom_pending")),
                                "cm_stale": bool(state.get("_geom_stale")),
                                "alpha_stage": (state.get("alpha_mask") or {}).get("stage"),
                                "alpha_ver": state.get("alpha_mask_ver") or 0,
                                "has_alpha_bits": bool((state.get("alpha_mask") or {}).get("bits")),
                                "model_rect_norm": state.get("model_rect_norm"),
                                "scale": state.get("scale"),
                                "mask_key": (str(pet._mask_key[0]) if getattr(pet, "_mask_key", None) else None),
                            }
                        except Exception as _e:
                            res = {"error": repr(_e)}
                        if _PERF["on"]:
                            # pet.mask() 要向系统查一次窗口 region（GetWindowRgn），
                            # 复杂 region 上能慢到秒级 —— 单独计时，别和主线程
                            # 卡顿混在一起判断。
                            _PERF["probe_ms"] += (time.monotonic() - _mk0) * 1000.0
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__alpha__":
                        # 自检入口：批量查"这些屏幕点是不是模型可见像素"。
                        # 和 __mask__ 的 pts_in_mask 对照，就能算出命中区漏了哪些
                        # 看得见的像素（漏的地方点下去会穿透到下面的窗口）。
                        pts = []
                        for _p in (cmd.get("pts") or [])[:4000]:
                            try:
                                pts.append({"nx": float(_p[0]), "ny": float(_p[1])})
                            except Exception:
                                continue

                        def _alpha_cb(res=None):
                            try:
                                self._sync_result = (res if isinstance(res, str)
                                                     else json.dumps(res, ensure_ascii=False))
                            except Exception:
                                self._sync_result = '{"error":"bad"}'
                            self._sync_event.set()

                        try:
                            page.runJavaScript(
                                "setPetCommand(" + json.dumps(
                                    {"type": "alphaAt", "points": pts},
                                    ensure_ascii=False) + ")", _alpha_cb)
                        except Exception as _e:
                            self._sync_result = json.dumps({"error": repr(_e)}, ensure_ascii=False)
                            self._sync_event.set()

                    elif cmd_type == "__dragTo__":
                        # 诊断：不开鼠标、直接走 move_stage_to 的夹取逻辑走 N 步，
                        # 确定性验证"贴边极限"停在哪（鼠标拖测会被"抓手点落在桌面/
                        # 模型外"干扰，测出来的不是夹取值）。
                        try:
                            _dx = int(cmd.get("dx") or 0)
                            _dy = int(cmd.get("dy") or 0)
                            _n = max(1, int(cmd.get("steps") or 1))
                            _trail = []
                            for _i in range(_n):
                                _sx, _sy, _sw, _sh = pet.stage_rect()
                                pet.move_stage_to(_sx + _dx, _sy + _dy)
                                _trail.append(list(pet.stage_rect()))
                            _norm = state.get("model_rect_norm") or [0, 0, 1, 1]
                            _org = state.get("model_rect_stage") or pet.stage_rect()[:2]
                            res = {"ok": True, "stage_rect": list(pet.stage_rect()),
                                   "trail": _trail[-3:],
                                   "norm": list(_norm), "norm_stage": list(_org)[:2],
                                   "pw": pet.width(), "ph": pet.height()}
                        except Exception as _e:
                            res = {"ok": False, "error": repr(_e)}
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__fps__":
                        # 自检入口：渲染层真实帧率。直出/Electron 模式没有 Qt 帧到达率
                        # 可统计（帧不进 Python），只能在页面里数 requestAnimationFrame
                        # 的触发次数：这就是"这块窗一秒钟到底画了几帧"。
                        # 首次调用只装探针（返回 installed），之后每次回最近一个
                        # 满 1 秒窗口的 fps，另附当前未满窗口的瞬时值与 PIXI ticker 自评。
                        _js_fps = (
                            "(function(){try{"
                            "var now=(window.performance&&performance.now)?performance.now():Date.now();"
                            "var p=window.__fpsProbe;"
                            "if(!p){"
                            "p=window.__fpsProbe={t0:now,n:0,fps:0,wms:0};"
                            "var tick=function(){var q=window.__fpsProbe;if(!q)return;"
                            "var t=(window.performance&&performance.now)?performance.now():Date.now();"
                            "q.n++;var dt=t-q.t0;"
                            "if(dt>=1000){q.fps=q.n*1000/dt;q.wms=dt;q.n=0;q.t0=t;}"
                            "requestAnimationFrame(tick);};"
                            "requestAnimationFrame(tick);"
                            "return JSON.stringify({installed:true,fps:0});}"
                            "var dt=now-p.t0;"
                            "var cur=(dt>200)?(p.n*1000/dt):null;"
                            "var tk=null;try{tk=(app&&app.ticker)?app.ticker.FPS:null;}catch(e){}"
                            "return JSON.stringify({fps:p.fps,wms:p.wms,"
                            "fpsNow:(cur===null?null:Math.round(cur*10)/10),"
                            "pixi:(tk===null?null:Math.round(tk*10)/10)});"
                            "}catch(e){return JSON.stringify({error:String(e)});}})()"
                        )

                        def _fps_cb(res=None):
                            try:
                                self._sync_result = (res if isinstance(res, str)
                                                     else json.dumps(res, ensure_ascii=False))
                            except Exception:
                                self._sync_result = '{"error":"bad js result"}'
                            self._sync_event.set()

                        try:
                            page.runJavaScript(_js_fps, _fps_cb)
                        except Exception as _e:
                            self._sync_result = json.dumps({"error": repr(_e)}, ensure_ascii=False)
                            self._sync_event.set()

                    elif cmd_type in ("__sway__", "swayState"):
                        # 自检入口：读渲染层的身体/衣物摆动状态（通道表、当前偏移、
                        # 本次手势峰值）。重置峰值传 {"type":"__sway__","reset":true}。
                        js_code = "setPetCommand(" + json.dumps(
                            {"type": "swayState", "reset": bool(cmd.get("reset"))},
                            ensure_ascii=False) + ")"

                        def _sway_cb(res):
                            try:
                                data = json.loads(res) if isinstance(res, str) else (res or {})
                            except Exception:
                                data = {"error": "bad js result"}
                            self._sync_result = json.dumps(data, ensure_ascii=False)
                            self._sync_event.set()

                        try:
                            page.runJavaScript(js_code, _sway_cb)
                        except Exception as _e:
                            self._sync_result = json.dumps({"error": repr(_e)}, ensure_ascii=False)
                            self._sync_event.set()

                    elif cmd_type == "__bubble__":
                        # 自检入口：显示一次「加载/切换模型」气泡，并回报气泡矩形
                        # 有没有落在覆盖层的 setMask 命中区里。local 模式的 mask
                        # 是模型 alpha 剪影，气泡贴在头顶之外——没并进命中区就会被
                        # 裁成半截（用户看到的"消息提醒显示不完全"）。
                        try:
                            txt = str(cmd.get("text") or "切换模型中")
                            pet.show_loading(txt)
                            bb = pet._bubble
                            r = bb.geometry().adjusted(-4, -4, 4, 4)
                            m = pet.mask()
                            res = {
                                "text": txt,
                                "bubble_visible": bool(bb.isVisible()),
                                "bubble_rect": list(r.getRect()),
                                "overlay": [pet.width(), pet.height()],
                                "mask_empty": bool(m.isEmpty()),
                                "mask_bounds": list(m.boundingRect().getRect()),
                                "mask_rects": len(m.rects()) if hasattr(m, "rects") else -1,
                                "bubble_in_mask": bool(m.contains(r)),
                                "display_mode": state.get("display_mode"),
                                "has_alpha": bool((state.get("alpha_mask") or {}).get("bits")),
                                "model_rect_norm": state.get("model_rect_norm"),
                            }
                            _log_line("INFO", "bubble check: %s" % json.dumps(res, ensure_ascii=False))
                        except Exception as _e:
                            res = {"error": repr(_e)}
                            _log_line("ERROR", "bubble check failed: %r" % (_e,))
                        self._sync_result = json.dumps(res, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__geom__":
                        # 自检入口：核对"模型到底画在屏幕哪块"与"seed 用哪套坐标"。
                        # 两者不一致时，热区圆点就会停在固定区域、而不是贴在部件上。
                        _js_geom = (
                            "(function(){try{"
                            "var s=app.renderer.screen,b=model.getBounds();"
                            "var st=_layoutStageLocal(),cl=_layoutClipLocal();"
                            "return JSON.stringify({"
                            "screen:[s.width,s.height],"
                            "win:[window.innerWidth,window.innerHeight],"
                            "winOrigin:_winOrigin(),mode:_layoutMode,"
                            "stageLocal:[st.x,st.y,st.w,st.h],"
                            "clipLocal:[cl.x,cl.y,cl.w,cl.h],"
                            "bounds:[b.x,b.y,b.width,b.height],"
                            "boundsNorm:[b.x/s.width,b.y/s.height,b.width/s.width,b.height/s.height],"
                            "canvas:(_canvasSize()||[]),"
                            "pix:[_pixW,_pixH],"
                            "crop:(_crop&&_crop.valid)?[1,_crop.x,_crop.y,_crop.w,_crop.h]:[0]"
                            "});}catch(e){return JSON.stringify({error:String(e)});}})()"
                        )

                        def _geom_cb(res=None):
                            try:
                                data = json.loads(res) if isinstance(res, str) else dict(res or {})
                            except Exception:
                                data = {"error": "bad js result"}
                            try:
                                data["stage_rect"] = list(pet.stage_rect())
                                data["overlay_win"] = [pet.width(), pet.height()]
                                data["display_mode"] = state.get("display_mode")
                                data["model"] = (state.get("assets") or {}).get("model")
                                data["hit_ids"] = _hit_areas_of(data["model"])
                                _cm = state.get("click_map") or {}
                                data["cm_model"] = _cm.get("model")
                                data["cm_cells"] = _cm.get("cells")
                                data["cm_cells_raw"] = _cm.get("cells_raw")
                                data["cm_grid"] = [_cm.get("cols"), _cm.get("rows"),
                                                   _cm.get("cell_px")]
                                data["cm_regions"] = _cm.get("regions")
                                data["cm_grid_ms"] = _cm.get("grid_ms")
                                data["seeds"] = [
                                    {"n": s.get("n"), "nx": s.get("nx"), "ny": s.get("ny"),
                                     "zone": s.get("zone"), "reg": s.get("reg"),
                                     "label": s.get("label")}
                                    for s in (_cm.get("seeds") or [])]
                                # 字幕条几何 + 相对模型中心的归一化偏移（回归"字幕跟模型走"用）
                                try:
                                    _sr = pet._sub_rect
                                    data["sub_rect"] = ([_sr.x(), _sr.y(),
                                                         _sr.width(), _sr.height()]
                                                        if not _sr.isEmpty() else [])
                                    data["sub_rel"] = (list(pet._sub_rel)
                                                       if getattr(pet, "_sub_rel", None) else None)
                                    data["sub_text"] = str(getattr(pet, "_sub_text", ""))[:40]
                                    data["sub_left"] = round(
                                        float(getattr(pet, "_sub_until", 0.0)) - time.monotonic(), 2)
                                except Exception:
                                    pass
                            except Exception:
                                pass
                            self._sync_result = json.dumps(data, ensure_ascii=False)
                            self._sync_event.set()

                        try:
                            page.runJavaScript(_js_geom, _geom_cb)
                        except Exception as _e:
                            self._sync_result = json.dumps({"error": repr(_e)}, ensure_ascii=False)
                            self._sync_event.set()

                    elif cmd_type == "getFrameStats" and _direct_mode:
                        # 直出模式没有 readPixels 帧缓冲：立即返回，避免 tool 侧等 20s 超时。
                        self._sync_result = json.dumps(
                            {"ok": False, "mode": _render_mode,
                             "error": "direct/electron 模式没有帧缓冲；帧率请用页面 requestAnimationFrame 统计"},
                            ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "cycleOutfit":
                        # 与中键同一条路径；nx/ny 可选（不给就是全局轮转）。
                        # 诊断用途：屏幕外测试时真实鼠标到不了，用它验证部位判定。
                        _nrm = None
                        if cmd.get("nx") is not None and cmd.get("ny") is not None:
                            _nrm = (float(cmd.get("nx")), float(cmd.get("ny")))
                        _zone = _zone_at_norm(_nrm[0], _nrm[1]) if _nrm else ""
                        _ok = _cycle_outfit(_nrm[0] if _nrm else None,
                                            _nrm[1] if _nrm else None)
                        _cur = {}
                        for _ch in (state.get("outfits") or []):
                            _cur[_ch["param"]] = int(
                                (state.get("outfit_sel") or {}).get(_ch["param"], 0) or 0)
                        self._sync_result = json.dumps(
                            {"ok": bool(_ok), "zone": _zone, "sel": _cur},
                            ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "showSubtitle":
                        # 诊断/回归入口：不经过 TTS 也能验证字幕覆盖层显示与自动消失。
                        pet.show_subtitle(str(cmd.get("text") or ""),
                                          float(cmd.get("secs") or 3.6))
                        self._sync_result = json.dumps({"ok": True}, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "subKaraoke":
                        # 诊断/回归入口：走 TTS 真正用的那条逐字高亮路径。
                        pet.sub_karaoke(str(cmd.get("text") or ""),
                                        int(cmd.get("spoken") or 0),
                                        cmd.get("secs"))
                        self._sync_result = json.dumps({"ok": True}, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "__modelready__":
                        # 诊断/回归入口：模拟"模型(重新)打开/切换"的那一下，
                        # 用来验证换模型时旧字幕必须整条撤掉（不能只剩半条挂在剪影上）。
                        _on_model_ready()
                        self._sync_result = json.dumps({"ok": True}, ensure_ascii=False)
                        self._sync_event.set()

                    elif cmd_type == "getParams" or cmd.get("sync"):
                        # 同步获取 JS 返回值（getParams / getAssets / playMotion / setExpression…）
                        js_code = f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"

                        def _cb(result):
                            self._sync_result = result
                            self._sync_event.set()

                        page.runJavaScript(js_code, _cb)

                    elif cmd_type == "__reload_model__":
                        # 切换模型 = 换一个全新的 WebEngine 页面（新 WebContents）。
                        # 不能在同一 WebContents 里 page.load()：实测第 2 次起 Chromium
                        # 合成器 shared image 失效，画面碎块条纹（现场 last.png 同款）。
                        mu = cmd.get("modelUrl") or ""
                        if mu:
                            _show_loading("切换模型中")
                            self._reload_pending_push = True
                            try:
                                _swap_render_view(mu, cmd.get("scale") or state["scale"])
                            except Exception as e:
                                self._reload_pending_push = False
                                self._model_error = f"重建渲染页失败: {e}"
                                self._model_ready = False
                                self._model_ready_event.set()

                    else:
                        # 其他命令直接转发给 JS
                        js_code = f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
                        page.runJavaScript(js_code)

            except queue.Empty:
                pass
            if _PERF["on"]:
                _tn = time.monotonic()
                _PERF["seg_cmd"] += (_tn - _tk) * 1000.0
                _tk = _tn
            # 命令处理完再补一次掩膜：新字幕、新位置都是在这一轮命令里产生的，
            # 只在轮次开头应用掩膜的话，这一帧画出来的字幕还没被掩膜罩住，
            # 落到模型剪影外的部分会被 setMask 裁掉（"字幕被剪影"）。
            try:
                pet._sub_housekeeping()
                pet._apply_interaction_mask()
            except Exception:
                pass
            if _PERF["on"]:
                _tn = time.monotonic()
                _PERF["seg_mask"] += (_tn - _tk) * 1000.0
                _pd = (_tn - _p0) * 1000.0
                _PERF["poll_ms"] += _pd
                if _pd > _PERF["poll_max"]:
                    _PERF["poll_max"] = _pd

        timer = QTimer()
        timer.timeout.connect(_poll_cmds)
        timer.start(10)

        # Electron 的 HWND_TOPMOST 会被其它程序/全屏切换清掉；定时补一次。
        # 注意：补置顶现在**不会**把自己抬到 topmost 组最上层（见 _set_hwnd_topmost），
        # 所以多个模型同时打开时不会互相抢占；间隔再抖一下，避免各 worker 同频
        # 触发同一帧的 DWM 排位。
        def _repin_windows():
            # 菜单打开时优先保住菜单：不要再把 Electron/Qt 抬到菜单上面。
            if state.get("menu_open"):
                _pin_menu_topmost()
            else:
                _pin_electron_topmost()
                _pin_qt_topmost()
            try:
                _topmost_timer.start(random.randint(2400, 3600))
            except Exception:
                pass

        _topmost_timer = QTimer()
        _topmost_timer.timeout.connect(_repin_windows)
        if _electron_mode:
            _topmost_timer.start(random.randint(2400, 3600))

        self._app = app
        # 对外句柄：读当前模型的双击热区分配（selftest / tool.py 诊断用）
        self._hotspots_fn = lambda: list((state.get("click_map") or {}).get("seeds") or [])
        # 对外句柄：读当前字幕状态（selftest / tool.py 诊断用）
        self._subinfo_fn = lambda: {
            "text": str(getattr(pet, "_sub_text", "") or ""),
            "left": round(float(getattr(pet, "_sub_until", 0.0)) - time.monotonic(), 2),
            "on": bool(state.get("subtitles")),
        }
        # 对外句柄：配音诊断文本（“配音诊断”菜单和自检共用同一份）
        self._diag_fn = lambda: _sound_diag_text()
        # ==== TTS 配音服务：必须在本线程（Qt 主线程）创建 ====
        # QAudioOutput 与字幕 tick 都绑在这个线程的事件循环上；从 HTTP 线程 new 会直接废掉。
        try:
            from tts.service import TTSService as _TTSSvc
            _tts = _TTSSvc(pet=pet, parent=pet)
            _tts.open()
            globals()["_TTS_SERVICE"] = _tts
            self._tts_fn = lambda: _tts.status()
            _log_line("INFO", "tts service ready")
            # 后台预热本地模型（约 11~16s，不阻塞界面）。预热完成后第一次 speak
            # 不再吃冷加载，用户按下播放就能在几秒内听到声音。
            try:
                if _tts._c("prewarm", True):
                    _tts.warmup()
                    _log_line("INFO", "tts warmup started")
            except Exception:
                pass
        except Exception as _e:      # 配音坏掉不能连累桌宠本身
            globals()["_TTS_SERVICE"] = None
            self._tts_fn = lambda: {"ok": False, "error": "TTS 服务未启动: %r" % (_e,)}
            _log_line("WARN", "tts init failed: %r" % (_e,))
        app.exec_()
        self._running = False
        # 正常情况下事件循环常驻（"关闭"只隐藏，不退出）。仅当异常导致循环退出时
        # 走到这里：只复位状态，绝不销毁 QApplication —— QtWebEngine 销毁后
        # 无法在同进程内二次创建，销毁即让下次 show 崩掉整个后端。
        self._app = None
        self._shutdown_event.set()

    # ===== 对外线程安全 API =====
    def switch_model(self, model_url: str, scale: Optional[float] = None):
        """切换模型：销毁旧渲染页、新建一个 WebEngine 页面加载模型。

        为什么不能 page.load()：同一个 WebContents 里重新加载页面时，Chromium 会复用
        已被回收的 compositor shared image（日志：SharedImageManager::ProduceGLTexture
        non-existent mailbox / texture bound to texture unit 0 is not renderable），
        第 2 次起画面就是碎块条纹（现场 last.png 同款，而 CPU 侧 drawable/UV 全正常）。
        换一个全新 QWebEngineView（新 WebContents）连续切换 5 个模型全部渲染正确。
        """
        try:
            scheme = (os.environ.get("PET_MODEL_SCHEME") or "file").strip().lower()
            if scheme != "http" and str(model_url or "").lower().startswith("http"):
                lp = local_model_json_of_url(model_url)
                if lp:
                    from PyQt5.QtCore import QUrl as _QUrl
                    model_url = _QUrl.fromLocalFile(lp).toString()
        except Exception:
            pass
        self.send_command({
            "type": "__reload_model__",
            "modelUrl": model_url,
            "scale": scale,
        })

    def send_command(self, cmd: dict):
        """从任意线程发送命令（非阻塞）。"""
        self._cmd_queue.put(cmd, timeout=0.1)

    def send_command_sync(self, cmd: dict, timeout: float = 5.0) -> dict:
        """发送命令并等待 JS 同步返回结果（用于 getParams）。"""
        self._sync_event.clear()
        self._sync_result = None
        self._cmd_queue.put(cmd, timeout=0.1)
        if self._sync_event.wait(timeout=timeout):
            result = self._sync_result
            if isinstance(result, str):
                try:
                    return json.loads(result)
                except json.JSONDecodeError:
                    return {"raw": result}
            return result or {"error": "empty result"}
        return {"error": "timeout waiting for JS response"}

    def hotspots(self) -> list:
        """当前模型的双击热区分配（每个动画一个点），诊断/自检用。

        每项：{n, kind, label, zone, part, nx, ny, group, index, id}
        """
        fn = getattr(self, "_hotspots_fn", None)
        try:
            return fn() if fn else []
        except Exception:
            return []

    def subtitle(self) -> dict:
        """当前配音字幕状态（诊断/自检用）：{text, left(剩余秒), on(开关)}。"""
        fn = getattr(self, "_subinfo_fn", None)
        try:
            return fn() if fn else {}
        except Exception:
            return {}

    def sound_diag(self) -> str:
        """配音诊断文本（与右键「配音诊断」弹窗同一份），自检用。"""
        fn = getattr(self, "_diag_fn", None)
        try:
            return str(fn()) if fn else ""
        except Exception:
            return ""

    def reset_model_ready(self):
        """加载/切换前复位就绪状态（供 tool.py 等待模型加载完成）。"""
        self._model_error = None
        self._model_ready = False
        self._model_ready_event.clear()

    def wait_model_ready(self, timeout: float = 12.0) -> bool:
        """等待 JS 侧模型加载/切换结束：成功 True，失败/超时 False。"""
        finished = self._model_ready_event.wait(max(0.1, float(timeout)))
        return bool(finished and self._model_ready and not self._model_error)

    @property
    def model_error(self) -> Optional[str]:
        """最近一次模型加载/切换失败原因（成功时为 None）。"""
        return self._model_error

    def close_window(self):
        self.send_command({"type": "__close__"})

    def shutdown(self, timeout: float = 5.0) -> bool:
        """隐藏桌宠窗口（不销毁线程 / QApplication / QWebEngine）。

        旧实现"等待 PyQt 线程退出"是崩溃根源：QtWebEngine 不支持在同一进程内
        销毁后二次初始化，重建 QApplication + QWebEngineView 必然 access violation。
        现在"关闭"只发 __close__ 隐藏窗口，PyQt 线程常驻，下次 show 复用。
        """
        if self._running or self._thread and self._thread.is_alive():
            try:
                self.send_command({"type": "__close__"})
            except Exception:
                pass
        return True

    def is_running(self) -> bool:
        return self._running

    def is_visible(self) -> bool:
        """桌宠窗口当前是否真正可见（show 后 True；hide / 关闭 / 隐藏后 False）。

        供 tool.py 判断「需要模型显示出来」的操作（播放动画、表情、流式动画）能否
        被看到：窗口不存在时也是 False。
        """
        if not self._running and not (self._thread and self._thread.is_alive()):
            return False
        return bool(self._visible)


# ===== 进程级单例 =====
# QApplication / QWebEngine 只能在一个进程里创建一次（QtWebEngine 二次初始化会
# 触发 access violation，直接带走整个后端进程）。所有桌宠窗口实例共用同一个
# PyQt 线程与窗口，"关闭"仅隐藏、保留渲染上下文供再次 show 复用。
_PET_SINGLETON: "DesktopPetWindow | None" = None
_PET_SINGLETON_LOCK = threading.Lock()


def get_pet_window() -> "DesktopPetWindow":
    """返回进程内唯一的桌宠窗口实例（不存在则创建）。

    用模块级工厂而非让调用方直接 DesktopPetWindow()，保证即使 tool.py 被插件
    热重载（模块级 _window 被重置为 None），也能拿到同一个仍在运行的窗口，
    不会在已存活的 QApplication 上再起一个线程/事件循环。
    """
    global _PET_SINGLETON
    with _PET_SINGLETON_LOCK:
        if _PET_SINGLETON is None:
            # 环境准备（建 profile 目录 / 清残留缓存 / 追加 Chromium flags）推到
            # 这里：import 本模块不产生任何副作用，避免污染宿主的 QtWebEngine。
            _ensure_runtime_env()
            _PET_SINGLETON = DesktopPetWindow()
        return _PET_SINGLETON


# ============================================================================
# ===== 多实例 worker 模式（每个模型一个子进程）=====
# ----------------------------------------------------------------------------
# 为什么必须分进程：QApplication / QtWebEngine 在一个进程内只能初始化一次，
# 且 QApplication 只能有一个 GUI 线程——所有 QWidget 都必须建在这个线程里，
# 而本模块的渲染链路是把整个窗口生命周期包在一段闭包里（末尾 app.exec_() 常驻）。
# 所以「同屏多个模型」在单进程内要么重建 QApplication（实测 access violation），
# 要么把 2600 行闭包重写成可重入的实例工厂（风险远大于收益）。
#
# 分进程方案：每个模型一个 worker 子进程，内部跑的仍是这条已经验证过的单模型
# 链路（一个 QApplication + 一个透明窗口 + 一个离屏 WebEngine + 一个帧服务器）。
# 父进程（tool.py）通过 localhost JSON 控制通道按 model_path 路由命令；
# close 直接结束该 worker → Chromium 渲染进程 / WebGL 上下文 / 大贴图全部释放，
# 这是单进程内销毁视图做不到的（QtWebEngine 的 GPU 内存不保证回收）。
#
# 进程间隔离的额外好处：每个 worker 独占自己的 Chromium profile 目录
# （user-data-dir 带进程级锁，两个进程共用会互踢、写坏缓存）。
# ============================================================================

_WSTATE = {
    "model": "",            # model3.json 绝对路径
    "model_name": "",       # 短名（符玄）
    "model_dir": "",
    "x": 0.5,               # 归一化位置（父进程命令驱动 / 用户拖拽回调都会更新）
    "y": 0.8,
    "scale": 1.0,
    "track": True,          # 是否跟随鼠标
    "ready": False,         # 模型是否真正加载完成
    "error": None,          # 最近一次加载失败原因
    "http_url": "",         # 该模型目录的 HTTP 服务地址
    "pid": os.getpid(),
    "started": round(time.time(), 3),
    "shutdown": False,
    # 窗口可见性快照：全局快捷键的钩子跑在后台线程，不能碰 Qt 对象，
    # 只能读这个纯 Python 快照决定"这次按键算不算我这份桌宠"。
    "visible": False,
    "parent_port": 0,       # 父进程回连端口（0=独立运行，右键「关闭」退回只隐藏）
}

# 父进程回连端口：worker 右键「关闭」时用它通知父进程「把我从注册表摘掉并结束我」。
_PARENT_PORT = 0


def _state_file_path() -> str:
    return os.path.join(_WORKERS_DIR, "state_%d.json" % os.getpid())


def _write_state_file() -> None:
    """把自己的身份与控制端口落盘，供父进程（含重启后的新父进程）找回。

    父进程的实例注册表只在内存里：插件进程一重启，屏幕上还在跑的 worker 就没人认领，
    list_open_models 也就漏报。落盘 + 存活校验是唯一可靠的补救方式。
    """
    try:
        os.makedirs(_WORKERS_DIR, exist_ok=True)
        path = _state_file_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_WSTATE, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _remove_state_file() -> None:
    try:
        os.remove(_state_file_path())
    except Exception:
        pass


def _port_reachable(port: int, timeout: float = 0.6) -> bool:
    """父进程回连服务是否还在监听（只看 TCP 能否握手，不真的发请求）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        s.close()


def _json_reply(handler, obj, code=200):
    """控制通道统一 JSON 返回（带 Content-Length，HTTP/1.1 keep-alive 才不挂）。"""
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass


def _start_control_server(win: "DesktopPetWindow", port: int):
    """父进程 → worker 的本地控制通道（独立线程，不占 Qt 线程）。

    端点：
      GET  /ping        存活探测
      GET  /state       状态快照（可见性/就绪/位置/缩放/帧数/错误）
      POST /cmd         {"type": ...} 投递到窗口命令队列（非阻塞）
      POST /cmd_sync    {"cmd": {...}, "timeout": 秒} 等 JS 返回
      POST /reset_ready 复位模型就绪状态
      POST /shutdown    置退出标志（主线程收尾后退出）
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"   # 父进程高频轮询 /state，keep-alive 省握手

        def log_message(self, *args):
            pass

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                raw = self.rfile.read(n).decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
            except Exception:
                return {}

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/ping":
                return _json_reply(self, {"ok": True, "pid": os.getpid()})
            if path == "/state":
                st = dict(_WSTATE)
                # 状态以当前实际模型路径为准：即使某次切换漏了回调，
                # 也不要把启动时的旧 model_dir 报给平台。
                if st.get("model"):
                    st["model_dir"] = str(Path(str(st["model"])).parent)
                try:
                    st["visible"] = bool(win.is_visible())
                    _WSTATE["visible"] = st["visible"]    # 主动权交给真实窗口状态
                    st["hotkey_last"] = dict(_HK_STAT)    # 快捷键最近一次观测
                    st["alive"] = bool(win.is_running())
                    st["stage_rect"] = list(_WSTATE.get("stage_rect") or [])
                    st["display_mode"] = _WSTATE.get("display_mode")
                    st["model_rect_norm"] = _WSTATE.get("model_rect_norm")
                    # 包围盒是"抓帧那一刻的 Stage 原点"量出来的；把原点一起报出去，
                    # 外部（诊断脚本/工具）才能算出模型**此刻**在屏幕上的位置：
                    #   model_live = norm × 屏幕 + (当前 stage 原点 - 抓帧原点)
                    st["model_rect_stage"] = _WSTATE.get("model_rect_stage")
                    st["frames"] = int(getattr(win, "frame_count", 0) or 0)
                    st["fps"] = round(float(getattr(win, "fps", 0.0) or 0.0), 1)
                    fs = getattr(win, "_frame_server", None)
                    if fs is not None:
                        st.update(fs.stats())
                    # 就绪状态按「此刻」的真实值反馈：worker 内部的同步等待可能早于
                    # 模型真正加载完成（大模型冷启动慢），只报快照会让父进程一直看到 false。
                    if bool(getattr(win, "_model_ready", False)) and not getattr(win, "model_error", None):
                        st["ready"] = True
                        _WSTATE["ready"] = True
                        _WSTATE["error"] = None
                    err = getattr(win, "model_error", None)
                    if err:
                        st["error"] = err
                except Exception as e:      # 窗口线程异常时不要让父进程拿到假状态
                    st["alive"] = False
                    st["error"] = st.get("error") or "状态读取失败: %r" % (e,)
                return _json_reply(self, st)
            if path == "/tts":
                svc = globals().get("_TTS_SERVICE")
                if svc is None:
                    return _json_reply(self, {"ok": False, "error": "TTS 服务未就绪"}, 503)
                # 默认精简状态；排查时用 /tts?detail=1 取全量（音频/口型/微动作/清单）
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                detail = str((qs.get("detail") or ["0"])[0]).lower() in ("1", "true", "yes")
                return _json_reply(self, svc.status(detail=detail))
            return _json_reply(self, {"error": "unknown endpoint: %s" % path}, 404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            data = self._read_json()

            if path == "/cmd":
                cmd = data.get("cmd") if isinstance(data.get("cmd"), dict) else data
                cmd = cmd if isinstance(cmd, dict) else {}
                t = cmd.get("type")
                # 命令式移动/缩放：顺手更新状态快照（拖拽/避让走 on_pos_change 回调）
                try:
                    if t in ("show", "setPosition") and "x" in cmd and "y" in cmd:
                        _WSTATE["x"] = round(float(cmd["x"]), 4)
                        _WSTATE["y"] = round(float(cmd["y"]), 4)
                    if t in ("show", "setScale") and "scale" in cmd:
                        _WSTATE["scale"] = round(float(cmd["scale"]), 4)
                except (TypeError, ValueError):
                    pass
                win.send_command(cmd)
                return _json_reply(self, {"ok": True})

            if path == "/cmd_sync":
                cmd = data.get("cmd") or {}
                try:
                    timeout = float(data.get("timeout") or 5.0)
                except (TypeError, ValueError):
                    timeout = 5.0
                res = win.send_command_sync(cmd if isinstance(cmd, dict) else {}, timeout=timeout)
                return _json_reply(self, {"ok": True, "result": res})

            if path == "/reset_ready":
                win.reset_model_ready()
                _WSTATE["ready"] = False
                _WSTATE["error"] = None
                return _json_reply(self, {"ok": True})

            if path == "/tts":
                # 配音入口：合成/停止/状态/卸载都在这里收口，tool.py 只负责转发
                svc = globals().get("_TTS_SERVICE")
                if svc is None:
                    return _json_reply(self, {"ok": False, "error": "TTS 服务未就绪"}, 503)
                op = str(data.get("op") or "speak")
                try:
                    if op == "speak":
                        res = svc.speak(
                            text=data.get("text") or "",
                            voice=data.get("voice") or data.get("speaker") or "Serena",
                            backend=str(data.get("backend") or "local"),
                            # 不传 = None，由 service 取该后端默认（本地 0.90 / 在线 1.0）；
                            # 这里写 `or 1.0` 会把"没传"变成"1.0"，默认语速永远改不动。
                            speed=(None if data.get("speed") is None
                                   else float(data.get("speed"))),
                            bgm=str(data.get("bgm") or ""),
                            # 不传音量 = None，由 service 按 config（0.75 / duck 0.55）走
                            bgm_volume=(None if data.get("bgm_volume") is None
                                        else float(data.get("bgm_volume"))),
                            bgm_duck=(None if data.get("bgm_duck") is None
                                      else float(data.get("bgm_duck"))),
                            subtitle=bool(data.get("subtitle", True)),
                            animation=str(data.get("animation") or ""),
                            motion_group=str(data.get("motion_group") or ""),
                            lip_sync=bool(data.get("lip_sync", True)),
                            mode=str(data.get("mode") or "replace"),
                            wait=float(data.get("wait") or 0),
                            blocking=bool(data.get("blocking", False)),
                            # 在线 key 由工具参数临时带下来：只传给本次合成就结束，不写配置
                            api_key=str(data.get("api_key") or ""),
                            # 唱歌：MiMo 独占能力，service 会拒绝非 mimo 后端
                            sing=bool(data.get("sing", False)),
                            # MiMo 自然语言风格指令与方言标签：按次生效，不写盘
                            style=str(data.get("style") or ""),
                            dialect=str(data.get("dialect") or ""),
                        )
                    elif op == "prepare":
                        # 跨模型轮流说话：只预合成、不播放，轮到本条时 speak 直接命中缓存
                        res = svc.prepare(
                            text=data.get("text") or "",
                            voice=data.get("voice") or data.get("speaker") or "",
                            backend=str(data.get("backend") or "mimo"),
                            speed=(None if data.get("speed") is None
                                   else float(data.get("speed"))),
                            api_key=str(data.get("api_key") or ""),
                            style=str(data.get("style") or ""),
                            dialect=str(data.get("dialect") or ""),
                        )
                    elif op == "stop":
                        res = svc.stop()
                    elif op == "status":
                        res = svc.status(detail=bool(data.get("detail", False)))
                    elif op == "unload":
                        res = svc.unload()
                    elif op == "config":
                        res = {"ok": True, "config": dict(svc.cfg)}
                    else:
                        res = {"ok": False, "error": "未知 tts op: %s" % op}
                except Exception as e:
                    res = {"ok": False, "op": op, "error": "%r" % (e,)}
                return _json_reply(self, res)

            if path == "/shutdown":
                _WSTATE["shutdown"] = True
                _json_reply(self, {"ok": True})
                # 响应已写出，稍等 0.3s 让父进程读完再硬退出（不给 Qt 收尾机会：
                # 结束进程就是释放内存的手段，正常 exec_ 退出反而可能卡在 GPU 侧）。
                def _bye():
                    _remove_state_file()   # 先撤自述文件，父进程扫描时不会误认存活
                    # 硬退出前先把 Electron 显示窗收走：它不随 worker 退出，
                    # 留着就是"清单里没有、屏幕上还在、还点不动"的残影。
                    _stop_electron_display()
                    time.sleep(0.3)
                    os._exit(0)
                threading.Thread(target=_bye, daemon=True).start()
                return

            if path == "/set_parent_port":
                # 被新父进程收养后，把自己的回连目标改成新端口（右键关闭才会继续有效）
                try:
                    _WSTATE["parent_port"] = int(data.get("port") or 0)
                    _write_state_file()
                except (TypeError, ValueError):
                    pass
                return _json_reply(self, {"ok": True})

            return _json_reply(self, {"error": "unknown endpoint: %s" % path}, 404)

    class _Srv(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = False     # Windows 上允许复用会让两个 worker 抢同一端口

    httpd = _Srv(("127.0.0.1", int(port)), Handler)
    # 端口写进状态：父进程重启后靠自述文件里的这个端口把窗口连回来
    _WSTATE["control_port"] = int(httpd.server_address[1])
    threading.Thread(target=httpd.serve_forever,
                     kwargs={"poll_interval": 0.1}, daemon=True).start()
    return httpd


def _worker_main(argv=None) -> int:
    """worker 入口：一个进程只服务一个模型，常驻直到收到 /shutdown。"""
    import argparse

    parser = argparse.ArgumentParser(prog="pet_window.py --worker")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--model-dir", required=True, help="模型目录（内含 *.model3.json）")
    parser.add_argument("--control-port", type=int, required=True, help="本地控制通道端口")
    parser.add_argument("--profile-tag", default="", help="独立 Chromium profile / 日志标签")
    parser.add_argument("--parent-port", type=int, default=0,
                        help="父进程回连端口（右键「关闭」时通知它摘掉并结束本 worker）")
    parser.add_argument("--track", type=int, default=1, help="1=跟随鼠标，0=不跟随")
    parser.add_argument("--x", type=float, default=0.5)
    parser.add_argument("--y", type=float, default=0.8)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args(list(argv if argv is not None else sys.argv[1:]))

    # 必须在 get_pet_window()（= _ensure_runtime_env）之前设好：profile 目录与日志标签
    # 都是按进程隔离的，晚一步就会跟别的 worker 抢同一个 Chromium user-data-dir。
    tag = (args.profile_tag or "").strip()
    if tag:
        os.environ["PET_PROFILE_DIR"] = os.path.join(_PROFILE_DIR, tag)
        os.environ["PET_LOG_TAG"] = tag
    os.environ["PET_TRACK"] = "1" if args.track else "0"

    model_dir = os.path.abspath(args.model_dir)
    jsons = sorted(Path(model_dir).glob("*.model3.json"))
    if not jsons:
        print("[worker] 找不到 *.model3.json：%s" % model_dir, file=sys.stderr)
        return 2
    model_file = str(jsons[0])
    global _PARENT_PORT
    _PARENT_PORT = int(getattr(args, "parent_port", 0) or 0)
    _WSTATE.update({
        "model": model_file,
        "model_name": Path(model_file).name[:-len(".model3.json")]
                      if Path(model_file).name.endswith(".model3.json") else Path(model_file).stem,
        "model_dir": model_dir,
        "x": args.x, "y": args.y, "scale": args.scale,
        "track": bool(args.track),
        "ready": False, "error": None,
        "parent_port": _PARENT_PORT,
    })

    win = get_pet_window()

    def _on_pos(x, y):
        try:
            _WSTATE["x"] = round(float(x), 4)
            _WSTATE["y"] = round(float(y), 4)
        except (TypeError, ValueError):
            pass

    def _on_scale(s):
        try:
            _WSTATE["scale"] = round(float(s), 4)
            # 记住桌面高度：下次启动沿用同一个 scale，不再忽大忽小。
            save_ui_cfg(scale=round(float(s), 4))
        except (TypeError, ValueError):
            pass

    def _on_model_switched(p, http_url=None):
        # 用户右键就地切模型：真实加载的模型变了，状态里如实反映（并同步落盘）。
        # model_dir / http_url 也必须一起换：否则平台路由已经是新模型，
        # /state 却还报启动时旧目录，排查时会误以为 worker 加载了另一个模型。
        try:
            p = str(p or "")
            _WSTATE["model"] = p
            _WSTATE["model_name"] = Path(p).stem
            _WSTATE["model_dir"] = str(Path(p).parent)
            if http_url:
                _WSTATE["http_url"] = str(http_url)
            _WSTATE["ready"] = False
            _WSTATE["error"] = None
            _write_state_file()
        except Exception:
            pass

    win.on_pos_change = _on_pos
    win.on_scale_change = _on_scale
    win.on_model_switch = _on_model_switched

    # 先把控制通道拉起来：父进程即使等模型加载也能立刻拿到 /state（含失败原因）
    _start_control_server(win, args.control_port)
    # 控制端口已经能应答了才写自述文件：父进程扫描后可直接连上来，不会连到空壳
    _write_state_file()

    try:
        http_url = serve_model(model_dir)
    except Exception as e:
        _WSTATE["error"] = "模型目录 HTTP 服务启动失败: %s" % (e,)
        print("[worker] serve_model 失败: %r" % (e,), file=sys.stderr)
        time.sleep(1.0)
        return 3
    _WSTATE["http_url"] = http_url
    _write_state_file()

    win.reset_model_ready()
    # 模型资源默认走 file:// 直读，不走本地 HTTP。
    #
    # 现场故障（2026-11-15）：同一台机器上 show 时不时卡死 —— 页面里 Live2DModel.from
    # 对 model3.json 的**异步 XHR 被 Chromium 直接取消**（xhr.status=0，pixi 抛
    # "Aborted."），模型永远加载不出来，而同一页面里**同步** XHR 请求同一 URL 却
    # 稳定 200。测到的事实：失败会话里所有异步 XHR（text/json/arraybuffer，含别的
    # 端口）都在 1–80ms 内 abort，页面 visibility/focus 均正常、无 webglcontextlost、
    # 库没调用 abort、补 CORS/私有网络预检头都无效 —— 即该会话的「异步网络加载」
    # 整条链路坏了，与 URL、CORS、模型无关。
    # file:// 的 XHR 不经过那条链路（由浏览器进程的 file loader 直接读盘），实测
    # 10 次连续启动 9 次正常（对照：同一时段 http:// 方案 4 次 0 成功）。
    # 需要回到 HTTP 方案时：环境变量 PET_MODEL_SCHEME=http。
    scheme = (os.environ.get("PET_MODEL_SCHEME") or "file").strip().lower()
    model_arg = model_arg_for_serve(model_file) or (http_url + Path(model_file).name)
    _log_line("INFO", "model scheme=%s arg=%s" % (scheme, model_arg))
    win.start(model_arg, args.x, args.y, args.scale)

    # 内部同步等待只是「尽快进入可服务状态」，超时不算失败：大模型冷启动可能超过
    # 30s，真正的就绪与否由 /state 实时反映（见 /state 里的 _model_ready 刷新）。
    ready = win.wait_model_ready(30.0)
    _WSTATE["ready"] = bool(ready)
    _WSTATE["error"] = win.model_error or None
    _write_state_file()
    _log_line("INFO", "worker ready=%s model=%s" % (ready, model_file))

    # 主线程常驻（Qt 事件循环在守护线程里跑，主线程退出会带走整个进程）+ 父进程看门狗。
    # 宿主进程一死，它的回连端口就没人监听；连续几次探测不通就自行结束，不在屏幕上留孤儿。
    # 只有「曾经连通过」才启用看门狗：独立运行 / 父进程端口本来就不可达的场景不受影响。
    watch = {"seen": False, "fails": 0, "next": time.time() + 2.0}
    while not _WSTATE.get("shutdown"):
        time.sleep(0.2)
        if time.time() < watch["next"]:
            continue
        watch["next"] = time.time() + 4.0
        port = int(_WSTATE.get("parent_port") or 0)
        if not port:
            continue
        if _port_reachable(port):
            watch["seen"] = True
            watch["fails"] = 0
        elif watch["seen"]:
            watch["fails"] += 1
            if watch["fails"] >= 3:
                _log_line("WARN", "父进程回连端口 %d 连续 %d 次不可达，自行结束"
                          % (port, watch["fails"]))
                _remove_state_file()
                os._exit(0)
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv[1:]:
        sys.exit(_worker_main(sys.argv[1:]))
    print("pet_window.py 是桌宠渲染库；多实例请用 --worker 由 tool.py 拉起", file=sys.stderr)
    sys.exit(1)
