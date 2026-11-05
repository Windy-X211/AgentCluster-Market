"""pet_window.py — PyQt5 透明窗口 + 离屏 WebEngine 渲染 + QPainter 逐帧合成

渲染链路：离屏 QWebEngineView(WebGL) → toDataURL → QWebChannel
→ Python Bridge → QPixmap → QPainter 绘制到透明 QWidget。

不能直接用透明 QWebEngineView 显示（Windows GPU 合成不输出 alpha，透明变黑）。
"""
import json
import math
import os
import functools
import threading
import queue
import sys
import time
import http.server
import socketserver
from pathlib import Path
from typing import Optional

# === QTWEBENGINE_CHROMIUM_FLAGS（必须在任何 from PyQt5 之前设置）===
_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_webengine_profile")
os.makedirs(_PROFILE_DIR, exist_ok=True)
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--disable-backgrounding-occluded-windows "
                      "--disable-renderer-backgrounding "
                      "--disable-background-timer-throttling "
                      f'--user-data-dir="{_PROFILE_DIR.replace(chr(92), "/")}"')

# === 临时 HTTP server 提供模型文件 ===
_MODELS = {}  # dir → (httpd, url)


def serve_model(model_dir: str) -> str:
    """启动临时 HTTP server 提供模型目录，返回访问 URL。

    每个 server 通过 `directory` 参数固定自己的服务目录，
    不用 os.chdir（进程级全局）——切换模型不会破坏其他
    server 的服务目录，因此初始模型可以随时切回。
    """
    if model_dir in _MODELS:
        return _MODELS[model_dir][1]
    abs_dir = os.path.abspath(model_dir)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=abs_dir
    )
    for port in range(18765, 18780):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{port}/"
            _MODELS[model_dir] = (httpd, url)
            return url
        except OSError:
            continue
    raise RuntimeError("无法绑定端口（18765-18780 均被占用）")


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
        self.on_scale_change = None   # 回调(new_scale) — 滚轮缩放后通知 tool.py
        self.on_model_switch = None   # 回调(model_path) — 右键切换模型后通知 tool.py
        self.on_pos_change = None     # 回调(x, y) — 鼠标避让/拖拽后通知 tool.py

    def start(self, model_http_url: str, x: float, y: float, scale: float):
        """在守护线程启动 PyQt5 + 离屏渲染 + 透明宠物窗口。"""
        self._thread = threading.Thread(
            target=self._qapp_main,
            args=(model_http_url, x, y, scale),
            daemon=True
        )
        self._thread.start()
        self._running = True

    def _qapp_main(self, model_url, x, y, scale):
        from PyQt5.QtCore import Qt, QUrl, QObject, pyqtSlot, pyqtSignal, QByteArray, QTimer, QRect, QRectF
        from PyQt5.QtWidgets import QApplication, QWidget, QMenu, QAction, QFileDialog
        from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineSettings
        from PyQt5.QtWebChannel import QWebChannel
        from PyQt5.QtGui import QColor, QImage, QPixmap, QPainter, QCursor, QPainterPath, QFont

        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        app = QApplication.instance() or QApplication(sys.argv)

        BASE_W, BASE_H = 700, 800   # 基础窗口尺寸（验证原型同款，避免原始尺寸过小）
        RENDER_QUALITY = 1.5          # 超采样倍率：画布 = 窗口 × 1.5（更清晰）
        MAX_RENDER_PIX = 4_000_000    # 画布最大像素数上限（~2000×2000，避免 FPS 暴跌）
        PET_W = int(BASE_W * scale)
        PET_H = int(BASE_H * scale)
        screen = app.primaryScreen().availableGeometry()

        # 共享可变状态（滚轮缩放 + 眼睛/头部跟随 + 鼠标避让）
        state = {
            "scale": scale,
            "track": False,
            "was_near": False,
            "avoid": False,
            "avoid_orig_win_pos": None,   # (x, y) — 开启避让瞬间的窗口左上角，永不丢失
            "avoid_zone": None,           # ZoneMarker QWidget — 可见区域的标记
            "avoid_pushing": False,
            "avoid_push_dir": None,
            "avoid_visible_rect": None,   # (lx, ly, w, h) — 模型可见 rect（相对窗口左上角）
        }

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

            def hide_loading(self):
                if not self.isVisible():
                    return
                self._fade = "out"
                self._fade_start = time.monotonic()
                if not self._timer.isActive():
                    self._timer.start(50)

            def reposition(self):
                """水平居中于窗口顶部（贴近模型头部上方）。"""
                p = self.parent()
                pw = p.width() if p else 700
                # 轻微上下浮动（±2px）由 _tick 写入 _bob
                x = (pw - self.width()) // 2
                self.move(x, 6 + int(getattr(self, "_bob", 0)))

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
                        return
                if self._fade == "steady":
                    t = now - self._start
                    # 呼吸：不透明度 0.72~1.0 正弦脉动
                    self._opacity = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(t * 3.0))
                    # 省略号：每 330ms 进一位（0→3 循环）
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
                qp.drawText(body, Qt.AlignCenter, self._text + "." * self._dot_count)
                qp.end()

        # === 透明宠物窗口（QPainter 逐帧绘制，真正可见）===
        class PetWidget(QWidget):
            def __init__(self):
                super().__init__()
                self.setWindowFlags(
                    Qt.FramelessWindowHint |
                    Qt.WindowStaysOnTopHint |
                    Qt.Tool
                )
                self.setAttribute(Qt.WA_TranslucentBackground, True)
                self.setAttribute(Qt.WA_NoSystemBackground, True)
                self._img = QImage()
                self._pix = QPixmap()
                self._dragging = False
                self._offset = None
                self._bubble = LoadingBubble(self)   # 加载提示消息泡泡
                self.resize(PET_W, PET_H)
                self.move(int(x * (screen.width() - PET_W)),
                          int(y * (screen.height() - PET_H)))

            def setFrame(self, dataUrl: str):
                """接收 JS 推来的 dataURL，解码 → 同时保留 QImage（用于像素级鼠标检测）+ QPixmap（用于绘制）"""
                try:
                    b64 = dataUrl.split(',', 1)[1]
                    img = QImage.fromData(QByteArray.fromBase64(b64.encode()))
                    if not img.isNull():
                        self._img = img          # 新增：保留 QImage，pixelColor() 查 alpha
                        self._pix = QPixmap.fromImage(img)
                        self.update()
                except Exception:
                    pass

            def resizeEvent(self, e):
                """缩放后立即用当前 pixmap 重绘，避免留白/闪动。"""
                super().resizeEvent(e)
                self._bubble.reposition()
                self.update()

            def show_loading(self, text="加载中"):
                self._bubble.show_loading(text)

            def hide_loading(self):
                self._bubble.hide_loading()

            def paintEvent(self, e):
                if not self._pix.isNull():
                    qp = QPainter(self)
                    pw, ph = self.width(), self.height()
                    iw, ih = self._pix.width(), self._pix.height()
                    # 画布与窗口始终同宽高比（BASE_W:BASE_H），直接铺满拉伸——
                    # 避免 KeepAspectRatio 居中取整产生的 ±1px 抖动；比例异常时才等比留白
                    if iw > 0 and ih > 0 and abs(iw / ih - pw / ph) < 0.02:
                        qp.setRenderHint(QPainter.SmoothPixmapTransform, True)
                        qp.drawPixmap(0, 0, pw, ph, self._pix, 0, 0, iw, ih)
                    else:
                        scaled = self._pix.scaled(
                            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        qp.drawPixmap((pw - scaled.width()) // 2,
                                      (ph - scaled.height()) // 2, scaled)
                    qp.end()

            def mousePressEvent(self, e):
                if e.button() == Qt.LeftButton:
                    self._dragging = True
                    self._offset = e.globalPos() - self.pos()

            def mouseMoveEvent(self, e):
                if self._dragging:
                    self.move(e.globalPos() - self._offset)

            def mouseReleaseEvent(self, e):
                self._dragging = False

            def wheelEvent(self, e):
                """滚轮缩放：上滚放大，下滚缩小，以鼠标位置为基点。
                即时到位（无过渡动画），避免逐帧窗口变化引起的取整抖动/闪烁。"""
                delta = e.angleDelta().y()
                if delta == 0:
                    return
                step = 0.1 if delta > 0 else -0.1
                new_scale = max(0.3, min(3.0, state["scale"] + step))
                if new_scale != state["scale"]:
                    _scale_immediate(new_scale, e.globalPos())

            def contextMenuEvent(self, e):
                """右键菜单：鼠标跟随 / 鼠标避让 / 隐藏 / 关闭 / 切换模型。"""
                menu = QMenu(self)
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
                menu.addSeparator()
                menu.addAction("隐藏", pet.hide)
                menu.addAction("关闭", _shutdown_pet)
                menu.addAction("切换模型…", _pick_model)
                menu.exec_(e.globalPos())

        pet = PetWidget()
        pet.show()

        # === 离屏渲染 QWebEngineView（屏幕外，不可见但保持 WebGL 渲染）===
        # 画布初始尺寸 = 初始缩放的理想渲染尺寸（窗口 × 超采样倍率，受上限约束）
        # 动画期间画布保持不动（避免异步 resize 导致反向抖动），结束后一次性对齐。
        _w0, _h0 = int(BASE_W * scale), int(BASE_H * scale)
        _rw, _rh = int(_w0 * RENDER_QUALITY), int(_h0 * RENDER_QUALITY)
        _pix = _rw * _rh
        if _pix > MAX_RENDER_PIX:
            cap = (MAX_RENDER_PIX / _pix) ** 0.5
            _rw, _rh = int(_rw * cap), int(_rh * cap)
        _rw, _rh = max(100, _rw), max(100, _rh)
        render_view = QWebEngineView()
        render_view.resize(_rw, _rh)
        render_view.move(-_rw - 2000, 0)
        render_view.show()

        class SilentPage(QWebEnginePage):
            def javaScriptConsoleMessage(self, level, msg, line, source):
                pass

        page = SilentPage(render_view)
        render_view.setPage(page)
        st = page.settings()
        st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        st.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        st.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        st.setAttribute(QWebEngineSettings.WebGLEnabled, True)
        st.setAttribute(QWebEngineSettings.ShowScrollBars, False)
        page.setBackgroundColor(QColor(0, 0, 0, 0))

        # === QWebChannel 桥：JS 推帧 → Python 解码 → 宠物窗口重绘 ===
        class Bridge(QObject):
            frameReady = pyqtSignal(str)
            modelReady = pyqtSignal()

            @pyqtSlot(str)
            def pushFrame(self, dataUrl: str):
                self.frameReady.emit(dataUrl)

            @pyqtSlot()
            def notifyModelReady(self):
                """JS 侧模型加载/切换完成时回调，隐藏加载泡泡。"""
                self.modelReady.emit()

        bridge = Bridge()
        bridge.frameReady.connect(pet.setFrame)

        # === 加载提示泡泡的控制 ===
        # 模型加载/切换完成 → 隐藏泡泡；15s 兜底超时（防止 JS 回调意外丢失）
        _loading_timeout = QTimer()
        _loading_timeout.setSingleShot(True)

        def _on_model_ready():
            _loading_timeout.stop()
            pet.hide_loading()

        bridge.modelReady.connect(_on_model_ready)
        _loading_timeout.timeout.connect(pet.hide_loading)

        def _show_loading(text="加载中"):
            pet.show_loading(text)
            _loading_timeout.start(15000)

        _show_loading("加载中")   # 初始模型加载提示

        channel = QWebChannel(page)
        channel.registerObject("bridge", bridge)
        page.setWebChannel(channel)

        # 加载 renderer.html，把 model URL 通过 query 传入
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderer.html")
        url = QUrl.fromLocalFile(html_path)
        url.setQuery(f"model={model_url}&scale={scale}")
        render_view.load(url)

        # === 缩放几何：动画期间只动窗口；画布在动画结束后一次性对齐 ===
        # 关键：动画期间不 resize 画布。逐帧 resize 时 JS 异步处理（延迟/乱序/合并）
        # 会短暂按过期画布尺寸 applyFit，导致缩放反向抖动。
        # 动画期间画布固定，模型显示尺寸由 paintEvent 等比缩放精确补偿（无抖动）。
        # anchor=None → 保持窗口中心点固定（智能体控制缩放用）
        # anchor=QPoint → 保持该屏幕点固定（滚轮缩放以鼠标为基点）
        def _apply_scale_geom(s, anchor=None, notify=True):
            s = max(0.3, min(3.0, s))
            state["scale"] = s
            old_w, old_h = pet.width(), pet.height()
            w, h = int(BASE_W * s), int(BASE_H * s)
            if anchor is None:
                ax = pet.x() + old_w // 2
                ay = pet.y() + old_h // 2
                fx, fy = 0.5, 0.5
            else:
                ax, ay = anchor.x(), anchor.y()
                fx = (ax - pet.x()) / old_w if old_w else 0.5
                fy = (ay - pet.y()) / old_h if old_h else 0.5
            px = int(round(ax - fx * w))
            py = int(round(ay - fy * h))
            if anchor is None:
                # 智能体控制：窗口保持在屏幕内
                px = max(0, min(px, screen.width() - w))
                py = max(0, min(py, screen.height() - h))
            else:
                # 滚轮：锚点精确固定，仅防止窗口完全移出屏幕（保留至少 80px 可见）
                m = 80
                px = max(-w + m, min(px, screen.width() - m))
                py = max(-h + m, min(py, screen.height() - m))
            pet.setGeometry(px, py, w, h)  # 位置+尺寸一步设置，避免两步跳动
            # 仅动画结束时通知 tool.py 同步最终缩放
            if notify and self.on_scale_change:
                self.on_scale_change(s)

        def _render_size_for(s):
            """目标缩放的理想渲染画布尺寸（窗口 × 超采样倍率，受上限约束）。"""
            w, h = int(BASE_W * s), int(BASE_H * s)
            rw, rh = int(w * RENDER_QUALITY), int(h * RENDER_QUALITY)
            _pix = rw * rh
            if _pix > MAX_RENDER_PIX:
                cap = (MAX_RENDER_PIX / _pix) ** 0.5
                rw, rh = int(rw * cap), int(rh * cap)
            return max(100, rw), max(100, rh)

        def _sync_render_size(s):
            """动画结束后把画布一次性对齐到当前缩放。
            只调 JS syncCanvas（改渲染缓冲，不动页面视口）——
            render_view 永远不 resize，杜绝 Qt→Chromium 异步中间态 → 无瞬时变大小。"""
            _rw, _rh = _render_size_for(s)
            page.runJavaScript(
                f"setPetCommand({json.dumps({'type': 'syncCanvas', 'w': _rw, 'h': _rh})})"
            )

        # === 平滑缩放动画（200ms easeInOutCubic 插值，避免突变）===
        anim = {"active": False, "from": state["scale"], "to": state["scale"],
                "anchor": None, "start": 0.0}

        scale_timer = QTimer()

        def _animate_scale_to(to, anchor=None):
            """从当前缩放平滑动画到目标缩放（滚轮/命令共用）。"""
            to = max(0.3, min(3.0, to))
            if abs(to - state["scale"]) < 0.005:
                return
            anim["from"] = state["scale"]
            anim["to"] = to
            anim["anchor"] = anchor
            anim["start"] = time.monotonic()
            anim["active"] = True
            if not scale_timer.isActive():
                scale_timer.start(16)  # ~60fps 步进

        def _scale_tick():
            if not anim["active"]:
                return
            dur = 0.20  # 200ms 动画时长
            k = min(1.0, (time.monotonic() - anim["start"]) / dur)
            k2 = k * k
            ease = 3 * k2 - 2 * k * k2  # easeInOutCubic：平滑起步+平滑收尾
            s = anim["from"] + (anim["to"] - anim["from"]) * ease
            final = k >= 1.0
            _apply_scale_geom(s, anim["anchor"], notify=final)
            if final:
                anim["active"] = False
                scale_timer.stop()
                _sync_render_size(s)  # 动画结束：画布一次性对齐目标缩放

        scale_timer.timeout.connect(_scale_tick)

        # === 滚轮即时缩放：一次到位（无过渡动画）+ 画布分辨率防抖对齐 ===
        # 逐帧动画会持续改变窗口尺寸/位置，叠加取整与重绘，产生可见晃动；
        # 即时缩放只改一次几何，滚动停手后再一次性对齐渲染画布分辨率（保证清晰）。
        _scale_sync_timer = QTimer()
        _scale_sync_timer.setSingleShot(True)
        _scale_sync_timer.timeout.connect(lambda: _sync_render_size(state["scale"]))

        def _scale_immediate(to, anchor):
            """立即缩放到位：停掉命令动画，设置几何，停手 120ms 后对齐画布。"""
            anim["active"] = False
            scale_timer.stop()
            to = max(0.3, min(3.0, to))
            if abs(to - state["scale"]) < 0.0005:
                return
            _apply_scale_geom(to, anchor, notify=True)
            _scale_sync_timer.start(120)   # 防抖：滚动间隙不再触发，仅停手后对齐

        # === 鼠标跟随（眼睛/头部追踪鼠标，类似桌宠软件）===
        def _toggle_track(checked):
            state["track"] = bool(checked)
            state["was_near"] = False
            # 关闭跟随时发送零目标，JS 会平滑复位
            if not checked:
                _send_track_params(0, 0)

        def _send_track_params(dx, dy):
            """把鼠标偏移换算成眼睛/头部/身体参数，发送目标值给 JS。
            JS 用每帧指数平滑逼近目标（不重启 tween），因此平滑度与 FPS 无关。
            """
            params = {
                "ParamEyeBallX": round(dx * 1.0, 4),       # 眼球左右
                "ParamEyeBallY": round(-dy * 1.0, 4),      # 眼球上下
                "ParamAngleX": round(dx * 15, 2),          # 头部左右转
                "ParamAngleY": round(-dy * 12, 2),         # 头部上下点
                "ParamBodyAngleX": round(dx * 8, 2),       # 身体左右扭转
                "ParamBodyAngleZ": round(dx * 4, 2),        # 身体侧倾
            }
            cmd = {"type": "setTrackTarget", "params": params}
            page.runJavaScript(
                f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
            )

        def _follow_tick():
            if not state["track"]:
                return
            if not pet.isVisible():
                return
            t = QCursor.pos()
            # mapFromGlobal 处理高 DPI 坐标转换
            local = pet.mapFromGlobal(t)
            # 靠近判定：自适应范围（至少 300px 或半窗口宽高）
            nr_x = max(300, pet.width() // 2)
            nr_y = max(300, pet.height() // 2)
            near = (-nr_x <= local.x() <= pet.width() + nr_x and
                    -nr_y <= local.y() <= pet.height() + nr_y)
            if near:
                state["was_near"] = True
                # 归一化偏移（-1~1），以窗口半宽/半高为分母
                hw = max(1, pet.width() // 2)
                hh = max(1, pet.height() // 2)
                dx = max(-1.0, min(1.0, (local.x() - hw) / hw))
                dy = max(-1.0, min(1.0, (local.y() - hh) / hh))
                _send_track_params(dx, dy)
            elif state["was_near"]:
                # 鼠标离开：JS 平滑复位到正视前方
                state["was_near"] = False
                _send_track_params(0, 0)

        def _combined_tick():
            _follow_tick()
            _avoid_tick()

        follow_timer = QTimer()
        follow_timer.timeout.connect(_combined_tick)
        follow_timer.start(20)  # ~50fps（跟随目标推送 + 避让位置动画）

        # === 鼠标避让（标记原始占据区域，鼠标进入则让开，离开后回位）===
        AVOID_PUSH_MARGIN = 15   # 推开后距区域边缘的安全间距（仅侧移，不遮挡即可）
        MOVE_ANIM_DUR = 0.18     # 移动动画时长（秒）

        move_anim = {"active": False, "from": (0, 0), "to": (0, 0),
                     "start": 0.0, "on_done": None}

        def _animate_move_to(tx, ty, on_done=None):
            """平滑移动宠物窗口到目标屏幕坐标。"""
            if abs(tx - pet.x()) < 2 and abs(ty - pet.y()) < 2:
                return
            move_anim["from"] = (pet.x(), pet.y())
            move_anim["to"] = (tx, ty)
            move_anim["start"] = time.monotonic()
            move_anim["active"] = True
            move_anim["on_done"] = on_done

        def _move_tick():
            if not move_anim["active"]:
                return
            dur = MOVE_ANIM_DUR
            k = min(1.0, (time.monotonic() - move_anim["start"]) / dur)
            k2 = k * k
            ease = 3 * k2 - 2 * k * k2  # easeInOutCubic
            fx, fy = move_anim["from"]
            tx, ty = move_anim["to"]
            cx = int(round(fx + (tx - fx) * ease))
            cy = int(round(fy + (ty - fy) * ease))
            pet.move(cx, cy)
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
            """把窗口位置限制在屏幕内，默认保证整个窗口可见；
            提供 vr=(lx,ly,w,h) 则保证**模型可见部分**完整在屏幕内。
            """
            w, h = pet.width(), pet.height()
            if vr:
                # 模型可见部分的屏幕 rect = (x+lx, y+ly, w, h)
                lx, ly, vw, vh = vr
                x = max(-lx, min(x, screen.width() - lx - vw))
                y = max(-ly, min(y, screen.height() - ly - vh))
            else:
                x = max(0, min(x, screen.width() - w))
                y = max(0, min(y, screen.height() - h))
            return x, y

        def _notify_pos():
            """通知 tool.py 当前宠物位置（归一化坐标）。"""
            if self.on_pos_change:
                w, h = pet.width(), pet.height()
                sx = screen.width() - w
                sy = screen.height() - h
                nx = pet.x() / sx if sx else 0.5
                ny = pet.y() / sy if sy else 0.5
                nx = max(0.0, min(1.0, nx))
                ny = max(0.0, min(1.0, ny))
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
            """用可见 rect（相对窗口）创建 ZoneMarker，位置始终基于**开启瞬间**的窗口原始位置。"""
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

        def _query_visible_then_start_avoid():
            """向 JS 查模型可见 rect，拿到后创建 ZoneMarker。
            先用窗口中心 70% 做合理 fallback，JS 回调到达后再替换。
            """
            pw, ph = pet.width(), pet.height()
            fw = max(int(pw * 0.7), 50)
            fh = max(int(ph * 0.7), 50)
            flx = (pw - fw) // 2
            fly = (ph - fh) // 2
            _create_avoid_zone_from_visible_rect(flx, fly, fw, fh)

            js = f"setPetCommand({json.dumps({'type': 'getModelRect'}, ensure_ascii=False)})"

            def _cb(result):
                try:
                    data = json.loads(result) if isinstance(result, str) else {}
                except (json.JSONDecodeError, TypeError):
                    data = {}
                if not data.get("fallback") and "w" in data and "h" in data:
                    lx = data.get("x", 0)
                    ly = data.get("y", 0)
                    w = data["w"]
                    h = data["h"]
                    # JS 返回的是画布坐标（1.5× 超采样），需转成窗口坐标
                    img = getattr(pet, '_img', None)
                    if (img and not img.isNull()
                            and pet.width() and pet.height()):
                        sx = pet.width() / img.width()
                        sy = pet.height() / img.height()
                        lx, ly = lx * sx, ly * sy
                        w, h = w * sx, h * sy
                    if state["avoid"] and state.get("avoid_orig_win_pos"):
                        _create_avoid_zone_from_visible_rect(
                            int(lx), int(ly), int(w), int(h))

            page.runJavaScript(js, _cb)

        def _toggle_avoid(checked):
            """切换鼠标避让开关。开启时**立即**锁定窗口原始位置，然后异步查可见 rect。"""
            state["avoid"] = bool(checked)
            if checked:
                # 立即锁定原始位置（在任何 move 发生之前）
                state["avoid_orig_win_pos"] = (pet.x(), pet.y())
                _query_visible_then_start_avoid()
            else:
                # 关闭回位：始终回到 avoid_orig_win_pos
                zone = state.get("avoid_zone")
                orig_x, orig_y = state.get("avoid_orig_win_pos", (pet.x(), pet.y()))
                if zone:
                    zone.close()
                state["avoid_zone"] = None
                state["avoid_visible_rect"] = None
                state["avoid_orig_win_pos"] = None
                state["avoid_pushing"] = False
                state["avoid_push_dir"] = None

                if pet.x() != orig_x or pet.y() != orig_y:
                    if not move_timer.isActive():
                        move_timer.start(16)
                    _animate_move_to(orig_x, orig_y, on_done=_notify_pos)

        _pos_notify_counter = 0
        _pixel_hit_logged = False

        def _check_pixel_alpha(cursor_pos):
            """检测鼠标位置对应像素是否有不透明内容（alpha > 20）。
            返回 True 表示"鼠标碰到了桌宠可见像素"，忽略周围透明 padding。

            坐标映射关键：setFrame 收到的 PNG 是完整渲染画布（窗口的 1.5×
            超采样，画布与窗口同宽高比），paintEvent 等比缩放到窗口居中绘制。
            因此窗口局部坐标 → 图像坐标必须按**实时比例**换算
            （img.w / pet.w），否则鼠标在桌宠上时查的是错位像素 → 永远 False。
            """
            img = getattr(pet, '_img', None)
            if img is None or img.isNull():
                return False
            iw, ih = img.width(), img.height()
            pw, ph = pet.width(), pet.height()
            if iw <= 0 or ih <= 0 or pw <= 0 or ph <= 0:
                return False
            rel_x = int((cursor_pos.x() - pet.x()) * iw / pw)
            rel_y = int((cursor_pos.y() - pet.y()) * ih / ph)
            if 0 <= rel_x < iw and 0 <= rel_y < ih:
                return img.pixelColor(rel_x, rel_y).alpha() > 20
            return False

        def _avoid_tick():
            """像素级检测：鼠标碰到桌宠可见像素则避让；同时离开桌宠和原位置 zone 才回位。"""
            nonlocal _pos_notify_counter, _pixel_hit_logged
            if not state["avoid"]:
                return
            if not pet.isVisible():
                return
            if getattr(pet, '_dragging', False):
                return

            cursor = QCursor.pos()

            # ① 像素级精确检测：鼠标是否真的在桌宠可见像素上
            on_pet = _check_pixel_alpha(cursor)
            if on_pet and not _pixel_hit_logged:
                _pixel_hit_logged = True
                img = getattr(pet, '_img', None)
                iw, ih = (img.width(), img.height()) if img and not img.isNull() else (0, 0)
                print(f"[avoid] pixel hit: img={iw}x{ih} win={pet.width()}x{pet.height()} "
                      f"cursor=({cursor.x()},{cursor.y()}) pet=({pet.x()},{pet.y()})")

            # ② 固定 zone（原位置区域），仅用于回位滞回区
            zone = state.get("avoid_zone")
            zone_rect = zone.geometry() if zone else pet.geometry()
            in_zone = zone_rect.contains(cursor)

            if on_pet:
                # === 推开阶段 ===
                # 每帧都重新获取 vr（可见 rect），用于位移和 clamp 计算
                vr = state.get("avoid_visible_rect") or (0, 0, pet.width(), pet.height())
                v_lx, v_ly, v_w, v_h = vr

                if not state["avoid_pushing"]:
                    state["avoid_pushing"] = True
                    move_anim["active"] = False
                    move_timer.stop()

                    # 锁定推开方向：以**当前可见像素所在位置**为基准
                    # 简化：从 pet 中心指向鼠标的反方向
                    pc_x = pet.x() + pet.width() // 2
                    pc_y = pet.y() + pet.height() // 2
                    dx = cursor.x() - pc_x
                    dy = cursor.y() - pc_y
                    if abs(dx) < 1 and abs(dy) < 1:
                        state["avoid_push_dir"] = (0.0, -1.0)
                    else:
                        state["avoid_push_dir"] = (
                            -dx / max(abs(dx), 1),
                            -dy / max(abs(dy), 1)
                        )

                dir_x, dir_y = state["avoid_push_dir"]
                orig_x, orig_y = state["avoid_orig_win_pos"]

                # 目标：让可见部分沿锁定方向从原位置推出去 MARGIN
                orig_vis_x = orig_x + v_lx
                orig_vis_y = orig_y + v_ly

                if dir_x > 0.01:
                    target_x = orig_vis_x + v_w + AVOID_PUSH_MARGIN - v_lx
                elif dir_x < -0.01:
                    target_x = orig_vis_x - AVOID_PUSH_MARGIN - v_lx - v_w
                else:
                    target_x = pet.x()

                if dir_y > 0.01:
                    target_y = orig_vis_y + v_h + AVOID_PUSH_MARGIN - v_ly
                elif dir_y < -0.01:
                    target_y = orig_vis_y - AVOID_PUSH_MARGIN - v_ly - v_h
                else:
                    target_y = pet.y()

                tx, ty = _clamp_pos(target_x, target_y, vr=vr)
                pet.move(tx, ty)

                _pos_notify_counter += 1
                if _pos_notify_counter >= 10:
                    _pos_notify_counter = 0
                    _notify_pos()

            else:
                # === 回位条件：鼠标同时离开桌宠和 zone ===
                if state["avoid_pushing"] and not in_zone:
                    state["avoid_pushing"] = False
                    state["avoid_push_dir"] = None
                    orig_x, orig_y = state.get("avoid_orig_win_pos", (pet.x(), pet.y()))
                    if pet.x() != orig_x or pet.y() != orig_y:
                        if not move_timer.isActive():
                            move_timer.start(16)
                        _animate_move_to(orig_x, orig_y, on_done=_notify_pos)

        # === 右键菜单动作 ===
        def _shutdown_pet():
            self.close_window()

        def _pick_model():
            """选择模型目录 → 找到 model3.json → 启动 HTTP server → 重载渲染器。"""
            dir_path = QFileDialog.getExistingDirectory(
                pet, "选择 Live2D 模型目录", os.path.expanduser("~")
            )
            if not dir_path:
                return
            m3 = list(Path(dir_path).glob("*.model3.json")) or \
                 list(Path(dir_path).rglob("*.model3.json"))
            if not m3:
                return
            model_file = str(m3[0])
            model_dir = str(Path(model_file).parent)
            try:
                http_url = serve_model(model_dir)
            except Exception:
                return
            new_model_url = http_url + Path(model_file).name
            # 切换模型 → 显示加载提示泡泡（JS 加载完成后回调 notifyModelReady 隐藏）
            _show_loading("切换模型中")
            # JS 就地切换模型（不重载页面，避免 WebGL/QWebChannel 重建卡住）
            cmd = {"type": "switchModel", "modelUrl": new_model_url}
            page.runJavaScript(
                f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
            )
            # 通知 tool.py 更新当前模型路径
            if self.on_model_switch:
                self.on_model_switch(model_file)

        # === 命令轮询（10ms，从 queue 取命令 → runJavaScript）===
        def _poll_cmds():
            try:
                while True:
                    cmd = self._cmd_queue.get_nowait()
                    cmd_type = cmd.get("type")

                    if cmd_type == "__close__":
                        app.quit()
                        return

                    elif cmd_type == "show":
                        if "scale" in cmd:
                            _animate_scale_to(cmd["scale"])
                        if "x" in cmd and "y" in cmd:
                            pet.move(int(cmd["x"] * (screen.width() - pet.width())),
                                     int(cmd["y"] * (screen.height() - pet.height())))
                        pet.show()
                        page.runJavaScript(
                            f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
                        )

                    elif cmd_type == "hide":
                        pet.hide()

                    elif cmd_type == "toggle":
                        if pet.isVisible():
                            pet.hide()
                        else:
                            pet.show()

                    elif cmd_type == "setPosition":
                        if "x" in cmd and "y" in cmd:
                            pet.move(int(cmd["x"] * (screen.width() - pet.width())),
                                     int(cmd["y"] * (screen.height() - pet.height())))

                    elif cmd_type == "setScale":
                        if "scale" in cmd:
                            _animate_scale_to(cmd["scale"])
                        page.runJavaScript(
                            f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
                        )

                    elif cmd_type == "getParams":
                        # 同步获取 JS 返回值
                        js_code = f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"

                        def _cb(result):
                            self._sync_result = result
                            self._sync_event.set()

                        page.runJavaScript(js_code, _cb)

                    else:
                        # 其他命令直接转发给 JS
                        js_code = f"setPetCommand({json.dumps(cmd, ensure_ascii=False)})"
                        page.runJavaScript(js_code)

            except queue.Empty:
                pass

        timer = QTimer()
        timer.timeout.connect(_poll_cmds)
        timer.start(10)

        self._app = app
        app.exec_()
        self._running = False

    # ===== 对外线程安全 API =====
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

    def close_window(self):
        self.send_command({"type": "__close__"})

    def is_running(self) -> bool:
        return self._running
