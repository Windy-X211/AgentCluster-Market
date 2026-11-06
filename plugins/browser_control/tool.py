"""browser_control 插件 —— Playwright 驱动的统一浏览器控制

支持两种模式（通过 mode 参数选择）：
  1. system  — 系统已安装的 Edge/Chrome（channel 方式启动，有独立 context，cookie 持久化到磁盘）
  2. cdp     — 桥接到正在运行的 Edge/Chrome（connect_over_cdp，共享用户的登录态、cookies、扩展）
  3. chromium — Playwright 内置 Chromium（最干净的沙箱，无头/有头都可）
  4. auto    — 自动选择（优先级 cdp > system > chromium）

CDP 模式用法：
  用户先在命令行启动带远程调试端口的浏览器：
    msedge --remote-debugging-port=9222
    chrome --remote-debugging-port=9222
  然后 agent 调用 browser_control(action="navigate", url="...", mode="cdp")

调用签名 (由 tool.yaml 的 parameters 决定)：
    run(action="navigate", url="...", selector="...", text="...",
        js="...", value=1, timeout=15, visible=True,
        mode="auto", cdp_endpoint="http://localhost:9222")

设计要点：
  - 所有 Playwright 操作固定在 **同一个 worker 线程** 里执行。
  - 主入口 run() 把请求塞进队列，等 worker 线程回结果。
  - 这样即使 run() 从 FastAPI/agent_runtime 的 async 事件循环里被调用，
    Playwright sync API 也不会因为检测到 loop 而抛 "Please use Async API"。
  - 切换 mode 时会自动关掉旧浏览器、用新模式重启。
"""
from __future__ import annotations

import base64
import io
import json as _json
import os
import re
import sys
import threading
import queue as _queue
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许独立 import（不依赖 backend 作为包）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
# 把插件目录加入 sys.path：本插件经 spec_from_file_location 加载（无包上下文），
# `from . import recording_panel` 必然失败；加了 sys.path 后
# `from recording_panel import ...` 可命中 sys.modules 缓存的同一模块实例，
# 全局只启一个 Tk UI 线程（否则每次录制都会新建独立模块 + 独立 UI 线程）。
_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

try:
    from playwright.sync_api import sync_playwright, Page, TimeoutError as _PWTimeout
    _PW_OK = True
except ImportError:
    _PW_OK = False


# ==============================================================
# 专用 worker 线程 —— 所有 Playwright 对象都在这里创建/使用
# ==============================================================
class _BrowserWorker:
    """单例 worker：浏览器、page 全在这个线程里。外面只通过 submit() 调它。"""

    _INST: "_BrowserWorker | None" = None
    _INST_LOCK = threading.Lock()

    def __init__(self) -> None:
        self._q: _queue.Queue[tuple[dict, _queue.Queue]] = _queue.Queue()
        self._thread = threading.Thread(target=self._run, name="browser-worker", daemon=True)
        self._thread.start()
        # —— 录制状态（线程安全）——
        self._recording = False
        self._recording_paused = False  # 暂停录制期间 append_step 会忽略新步骤
        self._recording_steps: list[dict] = []
        self._recording_meta: dict = {}   # {name, description, start_url, start_time, mode}
        self._recording_lock = threading.Lock()
        self._recording_started_at: float = 0   # Unix 时间戳，过滤录制开始前的 console 消息
        self._last_step_time: float = 0         # 上一步的时间戳，用于计算 wait_after
        self._recording_handlers: list = []      # (page/context, event, fn) 三元组，teardown 精确移除
        # —— 模板目录 ——
        self._templates_dir = Path(__file__).resolve().parent / "templates"
        self._templates_dir.mkdir(parents=True, exist_ok=True)
        # —— 提前启动 UI 线程（tkinter Tk root + mainloop 必须在同一线程）——
        try:
            from . import recording_panel as _rp  # type: ignore
            _rp.ensure_ui_thread()
        except Exception:
            try:
                from recording_panel import ensure_ui_thread as _eu  # type: ignore
                _eu()
            except Exception:
                pass  # 没装 tkinter 或环境无显示 —— 录制面板功能不可用，但浏览器功能不受影响

    @classmethod
    def instance(cls) -> "_BrowserWorker":
        if cls._INST is None:
            with cls._INST_LOCK:
                if cls._INST is None:
                    cls._INST = cls()
        return cls._INST

    # —— 给外部用：提交一个 request dict，拿到结果 dict ——
    def submit(self, req: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        if not _PW_OK:
            return {"ok": False, "data": "", "error": "playwright 未安装，请 pip install playwright && playwright install chromium",
                    "current_url": ""}
        out: _queue.Queue = _queue.Queue(maxsize=1)
        self._q.put((req, out))
        try:
            return out.get(timeout=timeout)
        except _queue.Empty:
            return {"ok": False, "data": "", "error": f"browser worker 超时 ({timeout}s)", "current_url": ""}

    # —— worker 主循环 ——
    def _run(self) -> None:
        try:
            self._pw = sync_playwright().start()
        except Exception as e:
            self._startup_error = f"Playwright start failed: {e}"
            self._loop_fail()
            return
        self._startup_error = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._visible: bool = False
        self._mode: str = ""
        self._cdp_endpoint: str = ""
        self._page_ws_url: str | None = None  # CDP 模式下保存 page 的 websocket 地址
        # 处理请求
        while True:
            try:
                req, out = self._q.get(timeout=0.25)
                res = self._handle(req)
                out.put(res)
            except _queue.Empty:
                # 录制期间必须泵事件：Playwright sync API 的 console/framenavigated
                # 事件只在线程处于某个 Playwright 调用内部时才分发。record_start
                # 返回后线程若阻塞在普通队列上，用户操作产生的 __AC_REC__ console
                # 事件会永远滞留、步骤数恒为 0。所以空闲时用 wait_for_timeout 泵。
                if self._recording:
                    self._pump_recording_events()
            except Exception as e:
                try:
                    out.put({"ok": False, "data": "", "error": f"{type(e).__name__}: {e}", "current_url": self.current_url()})
                except Exception:
                    pass

    def _pump_recording_events(self) -> None:
        """录制期间空闲泵：挑一个存活 page 做一次轻量 Playwright 调用，
        让积压的 console / framenavigated 事件得到分发。"""
        try:
            page = self._page
            if page is None or page.is_closed():
                page = None
                ctx = self._context
                if ctx is not None:
                    for p in ctx.pages:
                        if not p.is_closed():
                            page = p
                            break
            if page is not None:
                page.wait_for_timeout(150)
        except Exception:
            pass

    def _loop_fail(self) -> None:
        while True:
            req, out = self._q.get()
            out.put({"ok": False, "data": "", "error": self._startup_error or "browser worker 未启动", "current_url": ""})

    def current_url(self) -> str:
        try:
            return self._page.url if (self._page is not None and not self._page.is_closed()) else ""
        except Exception:
            return ""

    def _list_pages(self) -> list[dict[str, Any]]:
        """枚举当前 context 下所有标签页：[{index, url, title, is_active}, ...]。

        is_active 表示该页是否为 self._page（即后续 navigate/click/type 等操作的默认目标）。
        """
        out: list[dict[str, Any]] = []
        if self._context is None:
            return out
        try:
            pages = list(self._context.pages)
        except Exception:
            return out
        for i, p in enumerate(pages):
            try:
                url = p.url or ""
            except Exception:
                url = ""
            try:
                title = p.title() or ""
            except Exception:
                title = ""
            out.append({
                "index": i,
                "url": url,
                "title": title,
                "is_active": (p is self._page),
            })
        return out

    # ==============================================================
    # 模式检测与选择
    # ==============================================================
    @staticmethod
    def _detect_system_browser() -> str | None:
        """检测系统已安装的 Edge/Chrome，返回 Playwright channel 名。"""
        candidates: list[tuple[str, Path]] = [
            ("msedge", Path(r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")),
            ("msedge", Path(r"C:/Program Files/Microsoft/Edge/Application/msedge.exe")),
            ("chrome", Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe")),
            ("chrome", Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")),
        ]
        for channel, exe in candidates:
            if exe.is_file():
                return channel
        return None

    @staticmethod
    def _probe_cdp(endpoint: str) -> bool:
        """探测 CDP 端点是否可达（访问 /json/version 或 /json 任一成功即可）。"""
        import urllib.request
        for path in ("/json/version", "/json"):
            try:
                url = endpoint.rstrip("/") + path
                req = urllib.request.Request(url)
                # timeout 必须传给 urlopen，不是 Request！(Windows + IPv6 坑)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                continue
        return False

    def _resolve_mode(self, requested: str, cdp_endpoint: str) -> tuple[str, str]:
        """解析最终使用的 mode 和 cdp_endpoint。auto 会根据可用性自动选。"""
        req = (requested or "auto").strip().lower()
        ep = self._normalize_cdp_endpoint(cdp_endpoint)

        if req == "auto":
            # 先试 CDP（如果有端点）
            if ep and self._probe_cdp(ep):
                return "cdp", ep
            # 再试系统浏览器
            if self._detect_system_browser():
                return "system", ep
            # 最后用内置 Chromium
            return "chromium", ep

        if req == "cdp":
            return "cdp", ep
        if req == "system":
            return "system", ep
        if req == "chromium":
            return "chromium", ep
        # 兜底
        return "chromium", ep

    @staticmethod
    def _normalize_cdp_endpoint(endpoint: str) -> str:
        """把 localhost 规范化为 127.0.0.1，规避 Windows IPv6(::1) 解析坑。"""
        ep = (endpoint or "").strip()
        if not ep:
            return "http://127.0.0.1:9222"
        # localhost → 127.0.0.1
        ep = ep.replace("localhost", "127.0.0.1")
        # 补全 scheme
        if not ep.startswith(("http://", "https://")):
            ep = "http://" + ep
        return ep.rstrip("/")

    # ==============================================================
    # storage_state 持久化（system/chromium 模式用）
    # ==============================================================
    @staticmethod
    def _state_file() -> Path:
        return Path.home() / "AppData" / "Local" / "Temp" / "agentcluster_browser_state.json"

    def _save_state(self) -> None:
        if self._context is not None and self._mode != "cdp":
            try:
                self._context.storage_state(path=str(self._state_file()))
            except Exception:
                pass

    # ==============================================================
    # CDP 原生 screenshot —— 用 Playwright 内部 CDP session 绕过 fonts 等待
    # ==============================================================
    def _cdp_screenshot_direct(self) -> bytes | None:
        """在 CDP 模式下，通过 Playwright 内置的 CDP session 调 Page.captureScreenshot。
        不受浏览器的 --remote-allow-origins 限制（Playwright 内部已处理）。
        返回 PNG bytes；失败返回 None。"""
        if self._browser is None:
            return None
        try:
            # Playwright 内置 CDP session，不受 origin 限制
            session = self._browser.new_browser_cdp_session()
            # 需要用当前活跃 page 的 target，用 Page.captureScreenshot 的 pageId
            # 但 Playwright 的 cdp session 已经绑定到 browser，发 Page 命令会作用到默认 page
            result = session.send("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": False,
            })
            session.detach()
            if result and "data" in result:
                return base64.b64decode(result["data"])
        except Exception:
            pass
        return None

    # ==============================================================
    # 生命周期
    # ==============================================================
    def _ensure_page(self, visible: bool, mode: str, cdp_endpoint: str) -> Page:
        need_restart = False

        # 模式变了 → 必须重启
        if mode != self._mode or cdp_endpoint != self._cdp_endpoint:
            need_restart = True
        # visible 变了 → system/chromium 需要重启（cdp 模式忽略 visible）
        elif self._mode != "cdp" and self._page is not None and not self._page.is_closed():
            if self._visible != bool(visible):
                need_restart = True

        if need_restart and self._page is not None:
            self._close()

        if self._page is None or self._page.is_closed():
            self._start(visible=visible, mode=mode, cdp_endpoint=cdp_endpoint)

        assert self._page is not None
        return self._page

    def _start(self, visible: bool, mode: str, cdp_endpoint: str) -> None:
        self._visible = bool(visible)
        self._mode = mode
        self._cdp_endpoint = cdp_endpoint

        if mode == "cdp":
            self._start_cdp(cdp_endpoint)
        elif mode == "system":
            self._start_system(self._visible)
        else:  # chromium
            self._start_chromium(self._visible)

    def _start_cdp(self, endpoint: str) -> None:
        """通过 CDP 连接到用户正在运行的浏览器。"""
        import socket, urllib.request as ur
        norm_ep = self._normalize_cdp_endpoint(endpoint)
        parsed = ur.urlparse(norm_ep)
        host, port = parsed.hostname, parsed.port or 9222

        # Step 1: TCP 端口检测
        tcp_ok = False
        try:
            with socket.create_connection((host, port), timeout=2):
                tcp_ok = True
        except OSError:
            tcp_ok = False

        # Step 2: HTTP /json 端点有效性
        http_ok = False
        try:
            resp = ur.urlopen(norm_ep.rstrip("/") + "/json", timeout=3)
            http_ok = resp.status == 200
        except Exception:
            http_ok = False

        if not tcp_ok:
            raise RuntimeError(
                f"CDP 端口 {host}:{port} 未监听。请先启动浏览器并加 --remote-debugging-port 参数：\n"
                f'  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" --remote-debugging-port=9222 --remote-allow-origins=*\n'
                f'  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=*\n'
                f"注意：必须关掉所有已有浏览器实例再启动，否则新参数不生效。"
            )
        if tcp_ok and not http_ok:
            raise RuntimeError(
                f"CDP 端口 {host}:{port} 有响应但不是有效的 CDP 端点。\n"
                f"可能被其他程序占用，或浏览器启动时未加 --remote-debugging-port。"
            )

        # Step 3: 保存 page 的 websocket URL（供 CDP 底层命令如 screenshot 使用）
        try:
            targets = json.loads(ur.urlopen(norm_ep.rstrip("/") + "/json/list", timeout=3).read())
            page_target = next((t for t in targets if t.get("type") == "page"), None)
            if page_target:
                self._page_ws_url = page_target["webSocketDebuggerUrl"]
            else:
                self._page_ws_url = None
        except Exception:
            self._page_ws_url = None

        # Step 4: Playwright 连接（它内部会正确处理 origin 检查）
        try:
            self._browser = self._pw.chromium.connect_over_cdp(norm_ep)
        except Exception as e:
            raise RuntimeError(f"connect_over_cdp 失败: {e}")

        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def _start_system(self, visible: bool) -> None:
        """用系统 Edge/Chrome 的 channel 模式启动（有独立 context + storage_state）。"""
        channel = self._detect_system_browser()
        launch_kwargs: dict[str, Any] = {"headless": not visible}
        if channel:
            launch_kwargs["channel"] = channel
        try:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception as e:
            if channel:
                try:
                    self._browser = self._pw.chromium.launch(headless=not visible)
                except Exception:
                    raise RuntimeError(f"Playwright 无法启动浏览器 (channel={channel})。\n{e}")
            else:
                raise RuntimeError(f"Playwright 无法启动浏览器。请先跑: playwright install chromium\n{e}")

        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
        state_file = self._state_file()
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
        self._context = self._browser.new_context(**ctx_kwargs)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def _start_chromium(self, visible: bool) -> None:
        """用 Playwright 内置 Chromium 启动。"""
        try:
            self._browser = self._pw.chromium.launch(headless=not visible)
        except Exception as e:
            raise RuntimeError(f"Playwright 无法启动 Chromium。请先跑: playwright install chromium\n{e}")

        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 800}}
        state_file = self._state_file()
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
        self._context = self._browser.new_context(**ctx_kwargs)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def _close(self) -> None:
        self._save_state()
        # cdp 模式下不能关闭浏览器（那是用户正在用的），只断开 connection
        is_cdp = self._mode == "cdp"
        try:
            if self._page is not None and not self._page.is_closed():
                if not is_cdp:
                    self._page.close()
        except Exception:
            pass
        self._page = None
        try:
            if self._context is not None and not is_cdp:
                self._context.close()
        except Exception:
            pass
        self._context = None
        try:
            if hasattr(self, "_browser") and self._browser is not None:
                # cdp: disconnect；其他: close
                if is_cdp:
                    self._browser.close()  # disconnect_over_cdp 没有单独方法，close 等价于断开
                else:
                    self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._mode = ""
        self._cdp_endpoint = ""

    # ==============================================================
    # 元素快照（@eN 编号）—— get_html(format=snapshot) 的核心
    # 设计：默认发结构化快照（"目录+页码"），按需回填 outerHTML（"翻到某页"）。
    # ==============================================================
    EID_ATTR = "data-ac-eid"

    @staticmethod
    def _parse_eid(selector: str) -> tuple[int, str] | None:
        """解析 '@e5' / '@f1e3' / 'e5' / 'f1e3' → (frame_idx, eid)。默认 frame_idx=0（主文档）。"""
        s = (selector or "").strip()
        if s.startswith("@"):
            s = s[1:]
        # 带帧前缀：f1e3 / @f1e3
        if s.startswith("f") and "e" in s:
            try:
                f_part, e_part = s.split("e", 1)
                f_num = f_part[1:] if f_part.startswith("f") else f_part
                if f_num.isdigit() and e_part.isdigit():
                    return int(f_num), "e" + e_part
            except ValueError:
                pass
        # 不带帧前缀：e5 → 默认主文档 f0
        if s.startswith("e") and s[1:].isdigit():
            return 0, s
        return None

    @classmethod
    def _translate_selector(cls, selector: str) -> tuple[int | None, str]:
        """翻译选择器，返回 (frame_idx_or_None, css_selector)。

        - '@f1e3' → (1, '[data-ac-eid="e3"]')  — 子帧
        - '@e5'   → (0, '[data-ac-eid="e5"]')  — 主文档
        - '.btn'  → (None, '.btn')             — 普通 CSS（在当前 page.frames[0] 上执行）
        """
        parsed = cls._parse_eid(selector)
        if parsed:
            frame_idx, eid = parsed
            return frame_idx, f'[{cls.EID_ATTR}="{eid}"]'
        return None, selector

    @staticmethod
    def _resolve_frame(page, frame_idx: int | None):
        """根据 frame_idx 从 page 上拿到正确的 frame 对象。None / 0 = main_frame。"""
        if frame_idx is None or frame_idx == 0:
            return page.main_frame
        frames = page.frames
        if 0 <= frame_idx < len(frames):
            return frames[frame_idx]
        return page.main_frame

    def _snapshot_js(self) -> str:
        """生成页面结构化快照：DOM 瘦身 + 关键元素编号 + 紧凑文本。

        多通道采集：A. 语义标签/role（button、input、[role=button]…）
        B. 有 onclick 回调  C. 可聚焦 tabindex  D. cursor:pointer 且有 onclick/role（严准入，排除装饰元素）
        去重后按文档位置排序，确保 @eN 顺序 ≈ 视觉从上到下。
        """
        return r"""(frameIdx) => {
  const EID = "data-ac-eid";
  document.querySelectorAll("[" + EID + "]").forEach(e => e.removeAttribute(EID));
  const NOISE_TAGS = new Set(["script","style","noscript","svg","canvas","iframe","template","link","meta","head","path"]);
  const NOISE_SEL = "script,style,noscript,svg,canvas,iframe,template,link,meta,head";
  function isNoise(el){ if(!el||el.nodeType!==1) return true; const tag=el.tagName.toLowerCase(); if(NOISE_TAGS.has(tag)) return true; try{ const cs=window.getComputedStyle(el); if(cs.display==="none"||cs.visibility==="hidden"||cs.opacity==="0") return true; }catch(_){} const r=el.getBoundingClientRect(); if(r.width===0&&r.height===0) return true; return false; }
  function hasNoiseAncestor(el){ let p=el.parentElement; while(p&&p!==document.body){ if(isNoise(p)) return true; p=p.parentElement; } return false; }
  const KEY="a[href],button,input,select,textarea,option,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],[role=combobox],[role=switch],[role=searchbox],[role=textbox],[contenteditable],h1,h2,h3,h4,h5,h6,table,tr,th,td,img[alt],figure,label,summary";
  const vw=window.innerWidth||document.documentElement.clientWidth||0;
  const vh=window.innerHeight||document.documentElement.clientHeight||0;
  const seen=new Set(); const all=[];
  function add(el){ if(!el||el.nodeType!==1) return; if(seen.has(el)) return; seen.add(el); all.push(el); }
  if(document.body){
    // 通道 A：语义关键元素
    document.body.querySelectorAll(KEY).forEach(add);
    // 通道 B/C：onclick 或可聚焦（排除 tabindex=-1 这种禁用焦点）
    document.body.querySelectorAll("[onclick],[tabindex]").forEach(el=>{
      if(el.getAttribute("tabindex")==="-1") return;
      add(el);
    });
    // 通道 D：cursor:pointer + (有 onclick 或有 role) — 严准入，排除装饰性 cursor:pointer
    const candidates=document.body.querySelectorAll("*");
    for(const el of candidates){
      if(el.tagName==="BODY"||el.tagName==="HTML") continue;
      if(NOISE_SEL.split(",").includes(el.tagName.toLowerCase())) continue;
      if(el.firstElementChild&&el.firstElementChild.tagName==="SVG"&&el.children.length===1) continue; // 跳过纯 svg 包装器
      if(seen.has(el)) continue;
      let cs; try{ cs=window.getComputedStyle(el); }catch(_){ continue; }
      if(cs.cursor!=="pointer") continue;
      if(!el.hasAttribute("onclick")&&!el.getAttribute("role")) continue; // 必须有语义信号，排除纯装饰 cursor
      add(el);
    }
  }
  // 按文档位置排序，让 @eN 编号 ≈ 视觉从上到下
  all.sort((a,b)=>{ const p=a.compareDocumentPosition(b); if(p&Node.DOCUMENT_POSITION_FOLLOWING) return -1; if(p&Node.DOCUMENT_POSITION_PRECEDING) return 1; return 0; });
  let counter=0; const out=[]; const MAX=400;
  for(const el of all){
    if(counter>=MAX) break;
    if(isNoise(el)||hasNoiseAncestor(el)) continue;
    counter++;
    const eid="e"+counter; el.setAttribute(EID,eid);
    const r=el.getBoundingClientRect();
    const inVP=r.width>0&&r.height>0&&r.bottom>=0&&r.right>=0&&r.top<=vh&&r.left<=vw;
    const tag=el.tagName.toLowerCase();
    const role=el.getAttribute("role")||"";
    const type=(el.getAttribute("type")||"").toLowerCase();
    const name=el.getAttribute("name")||"";
    const id=el.id||"";
    const href=el.getAttribute("href")||"";
    const disabled=el.hasAttribute("disabled")||el.getAttribute("aria-disabled")==="true";
    const placeholder=el.getAttribute("placeholder")||"";
    const value=el.getAttribute("value")||"";
    const ariaLabel=el.getAttribute("aria-label")||"";
    const alt=el.getAttribute("alt")||"";
    const hasOnclick=el.hasAttribute("onclick");
    let text="";
    if(tag==="input"||tag==="textarea"||tag==="select"){ text=placeholder||value||ariaLabel||""; }
    else{ text=(el.innerText||el.textContent||"").trim().replace(/\s+/g," "); if(text.length>80) text=text.slice(0,77)+"..."; }
    let parentId=""; let p=el.parentElement;
    while(p){ const pid=p.getAttribute&&p.getAttribute(EID); if(pid){parentId=pid;break;} p=p.parentElement; }
    out.push({eid,tag,role,type,name,id,href,disabled:!!disabled,inVP:!!inVP,placeholder,value,ariaLabel,alt,text,parentId,hasOnclick});
  }
  const byEid=new Map(out.map(o=>[o.eid,o]));
  function depth(o){ let d=0,pid=o.parentId,seen=new Set(); while(pid&&!seen.has(pid)){seen.add(pid);d++;const p=byEid.get(pid);pid=p?p.parentId:"";} return d; }
  const lines=out.map(o=>{
    const ind="  ".repeat(Math.min(depth(o),10));
    const a=[];
    if(o.role) a.push("role="+o.role);
    if(o.type) a.push("type="+o.type);
    if(o.name) a.push("name="+o.name);
    if(o.id) a.push("#"+o.id);
    if(o.href) a.push("href="+o.href);
    if(o.placeholder) a.push("ph="+JSON.stringify(o.placeholder));
    if(o.value&&o.tag==="input") a.push("val="+JSON.stringify(o.value));
    if(o.ariaLabel) a.push("al="+JSON.stringify(o.ariaLabel));
    if(o.alt) a.push("alt="+JSON.stringify(o.alt));
    if(o.disabled) a.push("disabled");
    if(o.hasOnclick&&!o.role&&!["a","button","input","select","textarea"].includes(o.tag)) a.push("clickable");
    if(!o.inVP) a.push("offscreen");
    const as=a.length?" ["+a.join(" ")+"]":"";
    const ts=o.text?" "+JSON.stringify(o.text):"";
    const eidOut = (frameIdx||0) > 0 ? "@f" + frameIdx + o.eid.slice(1) : "@" + o.eid;
    return ind + eidOut + " <" + o.tag + ">" + as + ts;
  });
  return { count:out.length, viewport:{w:vw,h:vh}, snapshot:lines.join("\n"), truncated:counter>=MAX };
}"""

    def _element_extract_js(self) -> str:
        """按 @eN 取单个元素 outerHTML（已剔除内嵌 script/style 噪声）。"""
        return r"""(eid) => {
  const EID = "data-ac-eid";
  const el = document.querySelector("[" + EID + "='" + eid + "']");
  if (!el) return { found:false, error:"元素 @" + eid + " 未找到（页面可能已刷新或跳转，请重新 get_html(format=snapshot) 生成编号）" };
  let html = el.outerHTML || "";
  html = html.replace(/<script[\s\S]*?<\/script>/gi, "").replace(/<style[\s\S]*?<\/style>/gi, "");
  return { found:true, outerHTML: html.slice(0, 8000) };
}"""

    # ==============================================================
    # 稳定 CSS 选择器生成（用于模板回放，避免 @eN 的瞬态性）
    # ==============================================================
    def _stable_selector_js(self) -> str:
        """给定 data-ac-eid 编号，生成稳定的 CSS 选择器。
        优先级：#id → [name=] → .class → nth-child 路径。
        失败返回空字符串。"""
        return r"""(eid) => {
  const EID = "data-ac-eid";
  const el = document.querySelector("[" + EID + "='" + eid + "']");
  if (!el) return "";
  function escape(s){ return s.replace(/[^\w-]/g, m => "\\" + m); }
  // 1) id
  if (el.id) return "#" + escape(el.id);
  // 2) name 属性（表单元素）
  if (el.name && ["input","textarea","select","button"].includes(el.tagName.toLowerCase())) {
    return el.tagName.toLowerCase() + "[name=\"" + el.name.replace(/"/g,'\\"') + "\"]";
  }
  // 3) 有意义的 class（排除框架噪声如 col-*, row, container 等）
  const NOISE = /^(col-|row$|container|container-fluid|btn-group|form-control|form-group|input-group|navbar|modal|dropdown|tooltip|popover|fade|show|active|hidden|visible|collapse|expand|justify|align|text-|bg-|border-|rounded|shadow|p-|m-|d-|flex|grid|table-|thead|tbody|thead|list-group|breadcrumb|pagination)/;
  const meaningful = Array.from(el.classList).filter(c => !NOISE.test(c) && c.length < 30);
  if (meaningful.length > 0) {
    return el.tagName.toLowerCase() + "." + meaningful.map(escape).join(".");
  }
  // 4) nth-child 路径（限制深度）
  let path = [];
  let cur = el;
  let depth = 0;
  while (cur && cur.nodeType === 1 && cur.tagName !== "BODY" && cur.tagName !== "HTML" && depth < 8) {
    const tag = cur.tagName.toLowerCase();
    let nth = 1;
    let sib = cur;
    while ((sib = sib.previousElementSibling) !== null) {
      if (sib.tagName === cur.tagName) nth++;
    }
    path.unshift(tag + ":nth-of-type(" + nth + ")");
    cur = cur.parentElement;
    depth++;
  }
  return path.join(" > ");
}"""

    def _get_stable_selector(self, page, frame_idx: int | None, eid: str) -> str:
        """通过 JS 在页面中查询并生成稳定 CSS 选择器。"""
        try:
            frame = self._resolve_frame(page, frame_idx)
            return str(frame.evaluate(self._stable_selector_js(), eid) or "")
        except Exception:
            return ""

    # ==============================================================
    # 用户操作录制 —— 核心方案：注入 JS 监听器到浏览器页面
    #   1) add_init_script 注册全局监听器（跨导航存活）
    #   2) Playwright page.on('console') 捕获 __AC_REC__: 前缀的 JSON
    #   3) page.on('framenavigated') 捕获导航事件
    #   4) 同时注入一个浏览器内悬浮提示条
    # ==============================================================

    # 注入到每个 frame 的监听器（跨导航通过 add_init_script 重新注册）
    _AC_REC_PREFIX = "__AC_REC__:"

    def _user_recording_listeners_js(self) -> str:
        # 注意：add_init_script / frame.evaluate 接受的是"直接执行的 JS 代码"，
        # 所以这里用 IIFE 立即执行，而不是 () => { ... } 函数表达式。
        # 关键设计：
        #   1) 监听器挂在 window 捕获阶段（最先触发，防止页面脚本 stopPropagation 拦截）
        #   2) SPA 路由跳转（pushState/replaceState/popstate/hashchange）→ emit navigate
        #   3) 注入后立即发心跳，便于 Python 侧确认 console 链路通
        return (r"""(function(){
  if (window.__ac_rec_installed__) return;
  window.__ac_rec_installed__ = true;

  var PREFIX = "__AC_REC__:";
  function emit(obj){
    try { console.log(PREFIX + JSON.stringify(obj)); } catch(e){}
  }
  // 心跳：注入成功标记（Python 侧只确认链路，不记 step）
  try { console.log("[AC_REC] installed on " + location.href); } catch(e){}

  function stableSel(el) {
    if (!el || el.nodeType !== 1) return "";
    function esc(s){ return s.replace(/[^\w-]/g, function(m){ return "\\" + m; }); }
    if (el.id) return "#" + esc(el.id);
    if (el.name && ["input","textarea","select","button"].indexOf(el.tagName.toLowerCase()) >= 0) {
      return el.tagName.toLowerCase() + "[name=\"" + el.name.replace(/"/g,'\\"') + "\"]";
    }
    var NOISE = /^(col-|row$|container|form-control|navbar|modal|btn-|text-|bg-|p-|m-|d-|active|show|fade|collapse|dropdown|tooltip|popover)/;
    var meaningful = Array.from(el.classList||[]).filter(function(c){ return !NOISE.test(c) && c.length < 30; });
    if (meaningful.length > 0) {
      return el.tagName.toLowerCase() + "." + meaningful.map(esc).join(".");
    }
    var path=[]; var cur=el; var depth=0;
    while(cur && cur.nodeType===1 && cur.tagName!=="BODY" && cur.tagName!=="HTML" && depth<8){
      var tag=cur.tagName.toLowerCase(); var nth=1; var sib=cur;
      while((sib=sib.previousElementSibling)!==null){ if(sib.tagName===cur.tagName) nth++; }
      path.unshift(tag+":nth-of-type("+nth+")"); cur=cur.parentElement; depth++;
    }
    return path.join(" > ");
  }

  function isOverlayNode(el) {
    var cur = el;
    while (cur && cur.nodeType === 1) {
      if (cur.id === '__ac_rec_overlay__') return true;
      cur = cur.parentElement;
    }
    return false;
  }

  // 归一化坐标 [0,1]（相对顶层视口；子帧坐标换算到页面级，跨域取不到则退化为帧内）
  function normXY(clientX, clientY) {
    var x = clientX, y = clientY;
    try {
      var fe = window.frameElement;
      while (fe) {
        var r = fe.getBoundingClientRect();
        x += r.left; y += r.top;
        fe = (fe.ownerDocument && fe.ownerDocument.defaultView) ? fe.ownerDocument.defaultView.frameElement : null;
      }
    } catch(e) {}
    var w = window.innerWidth || 1, h = window.innerHeight || 1;
    try {
      if (window.top !== window) { w = window.top.innerWidth || w; h = window.top.innerHeight || h; }
    } catch(e) {}
    return [Math.min(1, Math.max(0, x / Math.max(1, w))), Math.min(1, Math.max(0, y / Math.max(1, h)))];
  }

  // 找真正滚动的容器（向上找 overflow 可滚祖先；到 BODY = 页面级滚动，返回 ''）
  function scrollSel(el) {
    try {
      var cur = el;
      while (cur && cur.nodeType === 1 && cur.tagName !== 'BODY' && cur.tagName !== 'HTML') {
        var st = window.getComputedStyle(cur);
        var ov = st.overflowY || st.overflow || '';
        if (/auto|scroll/.test(ov) && cur.scrollHeight > cur.clientHeight + 1) {
          return stableSel(cur);
        }
        cur = cur.parentElement;
      }
    } catch(e) {}
    return '';
  }

  // —— click（window 捕获阶段，最先触发，防页面脚本拦截）——
  // nx/ny 归一化坐标：回放时选择器失配的坐标点击兜底用
  window.addEventListener('click', function(e) {
    var tgt = e.target; if (!tgt || isOverlayNode(tgt)) return;
    var tn = (tgt.tagName || '').toUpperCase();
    // 点 BODY/HTML = 点页面空白处，不是有效步骤；且 body.innerText 会把
    // 悬浮条文字「正在录制操作模板」带进来，回放时形成一个没有选择器的
    // click 直接报错中断 —— 必须过滤。
    if (tn === 'BODY' || tn === 'HTML') return;
    var pos = normXY(e.clientX, e.clientY);
    emit({ kind:'click', selector: stableSel(tgt), text:(tgt.innerText||tgt.textContent||'').trim().slice(0,100), tag: tgt.tagName.toLowerCase(), nx:+pos[0].toFixed(4), ny:+pos[1].toFixed(4) });
  }, true);

  // —— wheel 滚轮（防抖 800ms 聚合：一次滚动操作的几十个 wheel 事件 → 累计 deltaY 一步）——
  // ctrl+wheel 是页面缩放不是滚动，忽略；deltaMode 行/页模式换算为像素
  var wheelTimer = null;
  var wheelBuff = null;
  window.addEventListener('wheel', function(e) {
    var tgt = e.target; if (!tgt || isOverlayNode(tgt)) return;
    if (e.ctrlKey) return;
    var dy = e.deltaY || 0;
    if (e.deltaMode === 1) dy *= 40;        // DOM_DELTA_LINE → 像素
    else if (e.deltaMode === 2) dy *= 800;  // DOM_DELTA_PAGE → 像素
    if (!dy) return;
    var pos = normXY(e.clientX, e.clientY);
    if (wheelBuff) {
      wheelBuff.dy += dy;                   // 同一次滚动累计
      wheelBuff.nx = +pos[0].toFixed(4); wheelBuff.ny = +pos[1].toFixed(4);
      wheelBuff.sel = scrollSel(tgt);
    } else {
      wheelBuff = { dy: dy, nx: +pos[0].toFixed(4), ny: +pos[1].toFixed(4), sel: scrollSel(tgt) };
    }
    if (wheelTimer) clearTimeout(wheelTimer);
    wheelTimer = setTimeout(function() {
      if (wheelBuff && wheelBuff.dy !== 0) {
        emit({ kind:'scroll', selector: wheelBuff.sel, delta_y: Math.round(wheelBuff.dy), nx: wheelBuff.nx, ny: wheelBuff.ny });
      }
      wheelBuff = null;
    }, 800);
  }, { capture: true, passive: true });

  // —— input/textarea 输入（防抖 600ms）——
  var inputTimer = null;
  var inputBuff = null;
  window.addEventListener('input', function(e) {
    var tgt = e.target; if (!tgt || isOverlayNode(tgt)) return;
    if (["INPUT","TEXTAREA","SELECT"].indexOf(tgt.tagName) < 0) return;
    var pos = normXY(e.clientX, e.clientY);
    if (inputBuff && inputBuff.selector === stableSel(tgt)) {
      inputBuff.text = tgt.value || tgt.textContent || '';
      inputBuff.nx = +pos[0].toFixed(4); inputBuff.ny = +pos[1].toFixed(4);
    } else {
      inputBuff = { selector: stableSel(tgt), text: tgt.value || tgt.textContent || '', nx:+pos[0].toFixed(4), ny:+pos[1].toFixed(4) };
    }
    if (inputTimer) clearTimeout(inputTimer);
    inputTimer = setTimeout(function() {
      if (inputBuff) {
        emit({ kind:'type', selector: inputBuff.selector, text: inputBuff.text, nx: inputBuff.nx, ny: inputBuff.ny });
        inputBuff = null;
      }
    }, 600);
  }, true);

  // —— select change ——
  window.addEventListener('change', function(e) {
    var tgt = e.target; if (!tgt || isOverlayNode(tgt)) return;
    if (tgt.tagName === 'SELECT') {
      var r = tgt.getBoundingClientRect();
      var pos = normXY(r.left + r.width / 2, r.top + r.height / 2);
      emit({ kind:'type', selector: stableSel(tgt), text: (tgt.value||'').slice(0,200), nx:+pos[0].toFixed(4), ny:+pos[1].toFixed(4) });
    }
  }, true);

  // —— form submit ——
  window.addEventListener('submit', function(e) {
    emit({ kind:'form_submit', selector: stableSel(e.target) });
  }, true);

  // —— SPA 路由跳转（B 站等单页应用：pushState 不触发 framenavigated）——
  var lastUrl = location.href;
  function navEmit(){
    try {
      if (window.top !== window) return;  // 只记顶层窗口的 SPA 路由，子 iframe 导航是噪音
      if (location.href === lastUrl) return;
      lastUrl = location.href;
      emit({ kind:'navigate', url: location.href });
    } catch(e){}
  }
  try {
    var _origPush = history.pushState;
    history.pushState = function(){ _origPush.apply(this, arguments); setTimeout(navEmit, 50); };
    var _origReplace = history.replaceState;
    history.replaceState = function(){ _origReplace.apply(this, arguments); setTimeout(navEmit, 50); };
    window.addEventListener('popstate', navEmit);
    window.addEventListener('hashchange', navEmit);
  } catch(e){}
})()""")

    def _user_recording_overlay_js(self) -> str:
        """浏览器内悬浮提示条。

        注意：必须用 DOM API 逐节点构建，不能用 innerHTML ——
        启用 Trusted Types 的站点（如 B 站）会抛
        "Failed to set the 'innerHTML' property: This document requires 'TrustedHTML' assignment"。
        """
        return (r"""(function(){
  if (document.getElementById('__ac_rec_overlay__')) return;
  var mount = document.body || document.documentElement;
  if (!mount) return;
  var div = document.createElement('div');
  div.id = '__ac_rec_overlay__';
  var dot = document.createElement('span');
  dot.textContent = '●';
  dot.style.cssText = 'font-size:11px;animation:acRecPulse 1.2s infinite;';
  var txt = document.createElement('span');
  txt.textContent = '正在录制操作模板';
  txt.style.cssText = 'font-weight:500;';
  var st = div.style;
  st.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;'
               'background:rgba(231,76,60,0.92);color:#fff;padding:8px 14px;'
               'border-radius:8px;font-size:13px;font-family:system-ui,-apple-system,sans-serif;'
               'box-shadow:0 4px 16px rgba(0,0,0,0.25);cursor:default;user-select:none;'
               'display:flex;align-items:center;gap:8px;';
  div.appendChild(dot);
  div.appendChild(txt);
  var st2 = document.createElement('style');
  st2.textContent = '@keyframes acRecPulse{0%,100%{opacity:1}50%{opacity:0.3}}';
  (document.head || mount).appendChild(st2);
  mount.appendChild(div);
})()""")

    def _user_recording_teardown_js(self) -> str:
        return (r"""(function(){
  const el = document.getElementById('__ac_rec_overlay__');
  if (el) el.remove();
  window.__ac_rec_installed__ = false;
})()""")

    # —— 将一条 console 事件转成 step dict ——
    @staticmethod
    def _norm_coord(payload: dict) -> dict | None:
        """从录制 payload 提取归一化坐标 {x, y}∈[0,1]；无效/缺失返回 None。
        回放时选择器失配的坐标点击兜底用。"""
        try:
            nx = float(payload.get("nx", -1))
            ny = float(payload.get("ny", -1))
        except (TypeError, ValueError):
            return None
        if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
            return {"x": nx, "y": ny}
        return None

    def _parse_rec_console(self, text: str, page) -> dict | None:
        prefix = self._AC_REC_PREFIX
        if not text.startswith(prefix):
            return None
        try:
            payload = _json.loads(text[len(prefix):])
        except Exception:
            return None
        kind = payload.get("kind", "")
        step: dict = {
            "index": 0,  # 稍后重新编号
            "action": "",
            "url_before": "",
            "url_after": self.current_url(),
            "url": self.current_url(),
            "selector": payload.get("selector", ""),
            "stable_selector": payload.get("selector", ""),  # 注入的 JS 已经生成了稳定选择器
            "text": payload.get("text", ""),
            "coordinate": None,
            "annotation": "",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "_kind": kind,
        }
        if kind == "click":
            # 没有选择器的 click（点 BODY/HTML 空白处）无法回放，丢弃
            if not payload.get("selector"):
                return None
            step["action"] = "click"
            step["coordinate"] = self._norm_coord(payload)
        elif kind == "type":
            step["action"] = "type"
            step["coordinate"] = self._norm_coord(payload)
        elif kind == "scroll":
            # 滚轮滚动（JS 端 800ms 防抖聚合后的累计 deltaY）
            step["action"] = "scroll"
            try:
                step["delta_y"] = int(float(payload.get("delta_y", 0)))
            except (TypeError, ValueError):
                step["delta_y"] = 0
            step["coordinate"] = self._norm_coord(payload)
        elif kind == "form_submit":
            step["action"] = "form_submit"
        elif kind == "navigate":
            # SPA 路由跳转（pushState/replaceState/popstate/hashchange）
            step["action"] = "navigate"
            step["url"] = payload.get("url", "")
            step["url_before"] = ""
            step["url_after"] = payload.get("url", "")
        else:
            return None
        return step

    def _append_step(self, step: dict) -> None:
        with self._recording_lock:
            if not self._recording:
                return
            # 暂停期间不录制新步骤
            if self._recording_paused:
                return

            # —— navigate 去重保护 ——
            # 原因：JS 监听器的 pushState 和其他入口（如直接改 location）
            # 可能在短时间内重复 emit 同一个 URL。此外用户快速连续操作也可能
            # 产生连续 navigate。规则：
            #   1. 如果最新一步已经是 navigate 且 URL 完全相同 → 跳过
            #   2. 如果当前 step 是 navigate，而上一步是 click（间隔 < 3s）→ 跳过
            #      因为 click 本身已经会触发跳转，回放时会自然到达目标页
            now = _time.time()
            action = step.get("action", "")
            if action == "navigate":
                url = step.get("url") or step.get("url_after") or ""
                # 检查步骤列表尾部的已有步骤
                n = len(self._recording_steps)
                if n > 0:
                    last = self._recording_steps[-1]
                    # 规则 1：同 URL navigate 去重
                    if last.get("action") == "navigate":
                        last_url = last.get("url") or last.get("url_after") or ""
                        if url and url == last_url:
                            return  # 重复，跳过
                    # 规则 2：上一步是 click，且间隔 < 3s —— 说明是 click 触发的跳转
                    # 回放时 click 执行后页面会自动跳到这个 URL，不需要额外 navigate
                    if last.get("action") == "click":
                        # 通过 wait_after 判断间隔
                        gap_ms = int(last.get("wait_after") or 0)
                        if gap_ms < 3000:
                            return  # click 自带跳转，跳过这个 navigate

            # —— scroll 连续合并 ——
            # JS 端 800ms 防抖后仍可能把一次连续滚动拆成多步（滚动间歇偶超 800ms），
            # Python 端再合并一次：上一步也是 scroll、间隔 < 3s、滚动容器相同 → 累计 deltaY。
            if action == "scroll" and self._recording_steps:
                last = self._recording_steps[-1]
                gap_ms = int((now - self._last_step_time) * 1000) if self._last_step_time > 0 else 0
                if (last.get("action") == "scroll" and gap_ms < 3000
                        and (last.get("selector") or "") == (step.get("selector") or "")):
                    last["delta_y"] = int(last.get("delta_y") or 0) + int(step.get("delta_y") or 0)
                    if step.get("coordinate"):
                        last["coordinate"] = step.get("coordinate")
                    if last["delta_y"] == 0:
                        self._recording_steps.pop()  # 正负抵消，删除
                    else:
                        self._last_step_time = now  # 刷新时间戳，允许继续合并
                    return

            # 计算与上一步的间隔（毫秒）
            wait_after = 0
            if self._last_step_time > 0:
                wait_after = int((now - self._last_step_time) * 1000)
            self._last_step_time = now
            step["wait_after"] = wait_after
            step["index"] = len(self._recording_steps)
            self._recording_steps.append(step)

    # ==============================================================
    # 录制状态管理（public API：record_start / record_status / ...）
    # ==============================================================
    def _record_start(self, req: dict) -> dict:
        name = (req.get("record_name") or "").strip() or f"template_{int(_time.time())}"
        description = (req.get("record_description") or "").strip()
        # 确保 page 可用
        page = None
        try:
            requested_mode = (req.get("mode") or self._mode or "auto").strip().lower()
            cdp_endpoint = (req.get("cdp_endpoint") or self._cdp_endpoint or "http://localhost:9222").strip()
            final_mode, final_ep = self._resolve_mode(requested_mode, cdp_endpoint)
            page = self._ensure_page(visible=True, mode=final_mode, cdp_endpoint=final_ep)
        except Exception as e:
            return {"ok": False, "error": f"启动浏览器失败：{e}",
                    "_note": "录制需要一个可用的浏览器页面。"}

        start_url = self.current_url()
        mode = self._mode or "auto"

        with self._recording_lock:
            self._recording = True
            self._recording_paused = False
            self._recording_steps = []
            self._recording_meta = {
                "name": name, "description": description,
                "start_url": start_url,
                "start_time": datetime.now().isoformat(timespec="seconds"),
                "mode": mode,
            }
            self._recording_started_at = _time.time()
            self._last_step_time = 0

        # —— 给所有现有 page 挂 console 监听器（必须先于注入：注入的 JS 会立刻
        #     发心跳 console.log，先挂 handler 才能在日志里确认链路通、不丢事件）——
        # 重要：Windows 控制台是 gbk，B 站页面自身的中文 console 输出会让
        # print() 抛 UnicodeEncodeError。所以先判断 PREFIX 再打印，
        # 打印也必须包 try——否则一个中文噪音就把整条 __AC_REC__ 消息吞掉。
        def _on_console(msg):
            try:
                text = msg.text or ""
            except Exception:
                return
            if self._AC_REC_PREFIX not in text:
                # 心跳消息（注入成功标记），只打印确认链路，不记 step
                if text.startswith("[AC_REC]"):
                    try:
                        print(f"[browser_control] [HEARTBEAT] {text[:110]}")
                    except Exception:
                        pass
                return
            try:
                print(f"[browser_control] console: {text[:110]}")
            except Exception:
                pass
            step = self._parse_rec_console(text, page)
            if step:
                try:
                    print(f"[browser_control] [CAUGHT] {step.get('action')} sel={(step.get('selector') or '')[:50]}")
                except Exception:
                    pass
                self._append_step(step)
            else:
                try:
                    print(f"[browser_control] [PARSE-FAIL] {text[:100]}")
                except Exception:
                    pass

        def _on_nav(page_obj, frame):
            """framenavigated handler —— 只负责导航后重新注入 overlay，
            不再追加 navigate step（navigate 记录完全交给 JS 监听器的 pushState/
            location change 检测，避免同一 URL 被重复记录两次）。"""
            try:
                if frame is not page_obj.main_frame:
                    return
                url = frame.url or ""
            except Exception:
                return
            try:
                print(f"[browser_control] navigated (skip record): {url}")
            except Exception:
                pass
            if not self._recording:
                return
            # 导航后重新注入 overlay（页面重建了 DOM）
            try:
                page_obj.evaluate(self._user_recording_overlay_js())
            except Exception:
                pass

        def _attach_page_handlers(p) -> None:
            """给单个 page 挂 console/framenavigated handler，并按 (page, event, fn)
            三元组精确登记，保证 teardown 能一一对应移除（不泄漏、不重复）。"""
            try:
                p.on("console", _on_console)
                self._recording_handlers.append((p, "console", _on_console))
            except Exception:
                pass
            try:
                fn_nav = lambda u, _p=p: _on_nav(_p, u)  # noqa: E731
                p.on("framenavigated", fn_nav)
                self._recording_handlers.append((p, "framenavigated", fn_nav))
            except Exception:
                pass

        with self._recording_lock:
            # 精确清理旧 handler（避免多次 record_start 累积 / 错位）
            for target, event, fn in list(self._recording_handlers):
                try:
                    target.remove_listener(event, fn)
                except Exception:
                    pass
            self._recording_handlers.clear()
            if self._context is not None:
                # 用户录制中新开的标签页也要有监听器
                def _on_new_page(new_p):
                    if not self._recording:
                        return
                    _attach_page_handlers(new_p)
                    try:
                        init_script = self._user_recording_listeners_js()
                        for frame in new_p.frames:
                            try:
                                frame.evaluate(init_script)
                            except Exception:
                                pass
                        new_p.evaluate(self._user_recording_overlay_js())
                    except Exception:
                        pass
                try:
                    self._context.on("page", _on_new_page)
                    self._recording_handlers.append((self._context, "page", _on_new_page))
                except Exception:
                    pass
                for p in self._context.pages:
                    _attach_page_handlers(p)

        # —— 注入监听器（两重保险：add_init_script 跨导航 + 当前页面立即注入）——
        try:
            init_script = self._user_recording_listeners_js()
            _sl = lambda s: s.encode("ascii", "replace").decode("ascii") if s else s
            print(f"[browser_control] injecting recording listeners, context={self._context}, pages={len(self._context.pages) if self._context else 0}")
            if self._context is not None:
                self._context.add_init_script(init_script)
                print("[browser_control] [OK] add_init_script set")
            injected_count = 0
            for p in (self._context.pages if self._context else []):
                for frame in p.frames:
                    try:
                        frame.evaluate(init_script)
                        injected_count += 1
                    except Exception as e:
                        print(f"[browser_control] frame evaluate failed ({_sl((frame.url or '')[:60])}): {e}")
            print(f"[browser_control] [OK] listeners injected into {injected_count} frames")
            for p in (self._context.pages if self._context else []):
                try:
                    p.evaluate(self._user_recording_overlay_js())
                    print(f"[browser_control] [OK] overlay injected into {_sl((p.url or '')[:60])}")
                except Exception as e:
                    print(f"[browser_control] overlay inject failed: {e}")
        except Exception as e:
            return {"ok": False, "error": f"注入录制监听器失败：{e}"}

        # —— 弹出 Python 录制面板（独立于主应用和浏览器）——
        _panel_error = None
        try:
            from . import recording_panel as _rp  # type: ignore
            _rp.open_panel(self, {"name": name, "start_url": start_url})
            print("[browser_control] recording panel opened (import: from .)")
        except Exception as e1:
            print(f"[browser_control] from . import failed: {e1}")
            try:
                from recording_panel import open_panel as _open  # type: ignore
                _open(self, {"name": name, "start_url": start_url})
                print("[browser_control] recording panel opened (import: top-level)")
            except Exception as e2:
                print(f"[browser_control] import recording_panel failed: {e2}")
                try:
                    import importlib.util as _iu
                    _rp_path = Path(__file__).resolve().parent / "recording_panel.py"
                    _spec = _iu.spec_from_file_location("recording_panel", str(_rp_path))
                    _mod = _iu.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)
                    _mod.open_panel(self, {"name": name, "start_url": start_url})
                    print("[browser_control] recording panel opened (import: spec)")
                except Exception as e3:
                    print(f"[browser_control] spec_from_file_location also failed: {e3}")
                    _panel_error = f"{e1} / {e2} / {e3}"

        return {"ok": True, "recording": True, "name": name, "start_url": start_url,
                "_note": "⚠️ 现在必须立即停止运行！录制已开始，用户正在浏览器里操作。你不要调任何其他浏览器 action（navigate/click/type 等）——那些是智能体代录，不是用户录。"
                         "你现在应该告诉用户「录制已开始，请在浏览器里正常操作，完成后回到对话告诉我」，然后结束你的当前回复。"
                         "等用户操作完说「好了」/「保存吧」/「完成录制」，你再回来调 record_complete 或 record_status。",
                "_suggest_stop": True}   # 给智能体框架的显式信号：调完此 action 后应停止、等用户下一条消息

    def _teardown_user_recording(self) -> None:
        """停止录制：清理 handler + 浏览器内悬浮条 + add_init_script。"""
        if self._context is None:
            return
        # detach handlers（按 (target, event, fn) 三元组精确移除，含 context 级 page 监听）
        for target, event, fn in list(self._recording_handlers):
            try:
                target.remove_listener(event, fn)
            except Exception:
                pass
        self._recording_handlers.clear()
        # 移除浏览器内悬浮条
        try:
            for p in self._context.pages:
                p.evaluate(self._user_recording_teardown_js())
        except Exception:
            pass
        # 清理 add_init_script（Playwright 没有 remove_init_script，
        # 但它只对后续导航生效，且我们的 JS 有 __ac_rec_installed__ 防重复标记，
        # 所以简单做法是保留 init script 但 JS 内部检测 self._recording 状态）
        # 更干净的方案：在 teardown 时注册一个新的 init script 把 __ac_rec_installed__ 重置掉
        try:
            reset_js = "() => { window.__ac_rec_installed__ = false; const el = document.getElementById('__ac_rec_overlay__'); if (el) el.remove(); }"
            self._context.add_init_script(reset_js)
            for p in self._context.pages:
                for frame in p.frames:
                    try:
                        frame.evaluate(reset_js)
                    except Exception:
                        pass
        except Exception:
            pass

    def _record_status(self) -> dict:
        with self._recording_lock:
            if not self._recording:
                return {"recording": False, "paused": False, "steps": [], "meta": {}}
            steps_copy = [dict(s) for s in self._recording_steps]
            meta_copy = dict(self._recording_meta)
            paused = self._recording_paused
        return {"recording": True, "paused": paused, "steps": steps_copy, "meta": meta_copy}

    def _record_step_annotate(self, req: dict) -> dict:
        idx = int(req.get("step_index", -1))
        note = str(req.get("annotation") or "")
        with self._recording_lock:
            if not self._recording:
                return {"ok": False, "error": "当前没有正在进行的录制"}
            if 0 <= idx < len(self._recording_steps):
                self._recording_steps[idx]["annotation"] = note
                return {"ok": True, "step_index": idx, "annotation": note}
        return {"ok": False, "error": f"step_index {idx} 越界（共 {len(self._recording_steps)} 步）"}

    def _record_step_delete(self, req: dict) -> dict:
        idx = int(req.get("step_index", -1))
        with self._recording_lock:
            if not self._recording:
                return {"ok": False, "error": "当前没有正在进行的录制"}
            if 0 <= idx < len(self._recording_steps):
                removed = self._recording_steps.pop(idx)
                for i, s in enumerate(self._recording_steps):
                    s["index"] = i
                return {"ok": True, "removed": removed}
        return {"ok": False, "error": f"step_index {idx} 越界（共 {len(self._recording_steps)} 步）"}

    def _record_step_reorder(self, req: dict) -> dict:
        order = req.get("new_order") or req.get("order")
        if not order or not isinstance(order, list):
            return {"ok": False, "error": "需要 new_order 参数（整数数组）"}
        new_order = [int(x) for x in order]
        with self._recording_lock:
            if not self._recording:
                return {"ok": False, "error": "当前没有正在进行的录制"}
            n = len(self._recording_steps)
            if sorted(new_order) != list(range(n)):
                return {"ok": False, "error": f"new_order 必须是 0..{n-1} 的一个排列，收到 {new_order}"}
            self._recording_steps = [dict(self._recording_steps[i]) for i in new_order]
            for i, s in enumerate(self._recording_steps):
                s["index"] = i
        return {"ok": True, "new_order": new_order}

    def _record_step_set_wait(self, req: dict) -> dict:
        """设置某步的 wait_after（毫秒）。"""
        idx = int(req.get("step_index", -1))
        try:
            wait_ms = int(req.get("wait_after", 0))
        except (ValueError, TypeError):
            return {"ok": False, "error": "wait_after 必须是整数（毫秒）"}
        if wait_ms < 0:
            wait_ms = 0
        with self._recording_lock:
            if not self._recording:
                return {"ok": False, "error": "当前没有正在进行的录制"}
            if 0 <= idx < len(self._recording_steps):
                self._recording_steps[idx]["wait_after"] = wait_ms
                return {"ok": True, "step_index": idx, "wait_after": wait_ms}
        return {"ok": False, "error": f"step_index {idx} 越界（共 {len(self._recording_steps)} 步）"}

    def _record_pause(self) -> dict:
        """暂停录制（已录步骤保留，暂停期间新的用户操作不会被记录）。"""
        with self._recording_lock:
            if not self._recording:
                return {"ok": False, "error": "当前没有正在进行的录制"}
            if self._recording_paused:
                return {"ok": True, "_note": "已处于暂停状态"}
            self._recording_paused = True
        print("[browser_control] recording PAUSED")
        return {"ok": True, "paused": True}

    def _record_resume(self) -> dict:
        """恢复录制。"""
        with self._recording_lock:
            if not self._recording:
                return {"ok": False, "error": "当前没有正在进行的录制"}
            if not self._recording_paused:
                return {"ok": True, "_note": "已处于录制中"}
            self._recording_paused = False
            self._last_step_time = 0  # 恢复后第一步的间隔重新计算
        print("[browser_control] recording RESUMED")
        return {"ok": True, "paused": False}

    def _record_complete(self, req: dict) -> dict:
        template_name = (req.get("template_name") or req.get("record_name") or "").strip()
        description = (req.get("record_description") or "").strip()
        with self._recording_lock:
            if not self._recording:
                return {"ok": False, "error": "当前没有正在进行的录制"}
            steps_copy = [dict(s) for s in self._recording_steps]
            meta_copy = dict(self._recording_meta)
            final_url = self.current_url()
            self._recording = False
            self._recording_paused = False
            self._recording_steps = []
            self._recording_meta = {}
            self._last_step_time = 0
        self._teardown_user_recording()
        if template_name:
            meta_copy["name"] = template_name
        if description:
            meta_copy["description"] = description
        # 清理内部字段
        for s in steps_copy:
            s.pop("_kind", None)
        template = {
            "version": 1,
            "name": meta_copy.get("name", "template"),
            "description": meta_copy.get("description", ""),
            "start_url": meta_copy.get("start_url", ""),
            "mode": meta_copy.get("mode", "auto"),
            "created_at": meta_copy.get("start_time", datetime.now().isoformat(timespec="seconds")),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "steps": steps_copy,
            "final_url": final_url,
        }
        safe_name = re.sub(r'[^\w\-]+', '_', template["name"]).strip('_') or "template"
        save_path = self._templates_dir / f"{safe_name}.json"
        counter = 1
        while save_path.exists():
            save_path = self._templates_dir / f"{safe_name}_{counter}.json"
            counter += 1
        try:
            save_path.write_text(_json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": f"保存模板失败：{e}"}
        return {"ok": True, "template": template, "path": str(save_path),
                "steps_count": len(steps_copy)}

    def _record_cancel(self) -> dict:
        with self._recording_lock:
            if not self._recording:
                return {"ok": True, "_note": "当前没有正在进行的录制"}
            self._recording = False
            self._recording_paused = False
            self._recording_steps = []
            self._recording_meta = {}
            self._last_step_time = 0
        self._teardown_user_recording()
        return {"ok": True, "_note": "录制已取消，步骤已丢弃。"}

    # ==============================================================
    # 模板管理（worker 线程内执行，模板目录 I/O）
    # ==============================================================
    def _template_list(self) -> dict:
        items = []
        for fp in sorted(self._templates_dir.glob("*.json")):
            try:
                data = _json.loads(fp.read_text(encoding="utf-8"))
                items.append({
                    "name": data.get("name", fp.stem),
                    "filename": fp.name,
                    "path": str(fp),
                    "description": data.get("description", ""),
                    "start_url": data.get("start_url", ""),
                    "final_url": data.get("final_url", ""),
                    "mode": data.get("mode", "auto"),
                    "steps_count": len(data.get("steps", [])),
                    "created_at": data.get("created_at", ""),
                })
            except Exception:
                continue
        return {"ok": True, "templates": items}

    def _template_load(self, req: dict) -> dict:
        name = (req.get("template_name") or req.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "需要 template_name 参数"}
        # 尝试精确匹配 .json 或直接用 stem
        candidates = [
            self._templates_dir / name,
            self._templates_dir / (name + ".json"),
        ]
        found_path: Path | None = None
        for c in candidates:
            if c.exists():
                found_path = c
                break
        if found_path is None:
            # 模糊匹配（name 作为 stem）
            for fp in self._templates_dir.glob("*.json"):
                if fp.stem == name or fp.name == name:
                    found_path = fp
                    break
        if found_path is None:
            return {"ok": False, "error": f"未找到模板：{name}"}
        try:
            # utf-8-sig：兼容带 BOM 的文件（手编辑/外部工具改写模板时常见）
            data = _json.loads(found_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            return {"ok": False, "error": f"模板读取失败：{e}"}
        return {"ok": True, "template": data, "path": str(found_path)}

    def _template_delete(self, req: dict) -> dict:
        name = (req.get("template_name") or req.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "需要 template_name 参数"}
        for fp in list(self._templates_dir.glob("*.json")):
            if fp.stem == name or fp.name == name:
                try:
                    fp.unlink()
                    return {"ok": True, "deleted": fp.name}
                except Exception as e:
                    return {"ok": False, "error": f"删除失败：{e}"}
        return {"ok": False, "error": f"未找到模板：{name}"}

    def _do_scroll(self, page, dy: int, nx_raw=None, ny_raw=None) -> dict:
        """在 page 上执行滚轮滚动。

        可选 (nx, ny) 归一化坐标：先 mouse.move 到录制时的鼠标位置再滚，
        保证滚动的是录制时的同一个容器（滚动容器由鼠标位置决定）。
        分批派发（每批一格 120px、间隔 40ms）模拟真实滚轮，
        能触发抖音等页面的懒加载/无限滚动。
        """
        try:
            nx = float(nx_raw) if nx_raw is not None else -1.0
            ny = float(ny_raw) if ny_raw is not None else -1.0
        except (TypeError, ValueError):
            nx, ny = -1.0, -1.0
        if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
            vw = vh = 0
            try:
                vs = page.viewport_size or {}
                vw = int(vs.get("width") or 0)
                vh = int(vs.get("height") or 0)
            except Exception:
                pass
            if not vw or not vh:
                try:
                    dims = page.evaluate("() => ({w: window.innerWidth || 0, h: window.innerHeight || 0})")
                    vw = int(dims.get("w") or 0)
                    vh = int(dims.get("h") or 0)
                except Exception:
                    pass
            if vw and vh:
                try:
                    page.mouse.move(max(0, min(vw - 1, int(round(nx * vw)))),
                                    max(0, min(vh - 1, int(round(ny * vh)))))
                except Exception:
                    pass
        step_px = 120 if dy > 0 else -120
        remaining = dy
        while remaining != 0:
            chunk = step_px if abs(remaining) >= abs(step_px) else remaining
            page.mouse.wheel(0, chunk)
            remaining -= chunk
            if remaining != 0:
                page.wait_for_timeout(40)
        page.wait_for_timeout(300)
        direction = "向下" if dy > 0 else "向上"
        return {"ok": True, "data": f"已滚动{direction} {abs(dy)}px", "current_url": self.current_url()}

    def _template_run(self, req: dict) -> dict:
        """依次执行模板中的每一步（navigate → click → type → scroll → coordinate_click）。
        每一步调用 worker 内部的 _handle 方法。
        回放期间临时禁用录制捕获（避免把回放步骤录进当前录制会话）。
        使用步骤自带的 wait_after（毫秒）作为步骤间等待，默认 1000ms。

        click/type 步骤带录制坐标时：选择器定位失败自动回退鼠标坐标点击
        （type 回退为坐标点击聚焦 + 键盘输入），回退结果标注 fallback。

        回放前会预处理 steps：如果某个 navigate 紧邻 click（间隔 < 3s），
        说明是 click 触发的跳转，回放时跳过这个 navigate（click 本身会触发跳转）。"""
        load_res = self._template_load(req)
        if not load_res.get("ok"):
            return load_res
        template = load_res["template"]
        steps = template.get("steps", [])
        if not steps:
            return {"ok": False, "error": "模板没有步骤"}

        # —— 回放前预处理：标记可跳过的 navigate ——
        # 场景：navigate 被 framenavigated 记录了，同时 click 也被正确记录了，
        # 回放时 navigate 先跳到目标 URL，然后 click 在新页面上找不到元素就报错。
        # 规则：如果 navigate 的 wait_after < 3000ms（说明紧跟某个操作），且
        #   (a) 它前面是 click → skip（click 本身触发了跳转）
        #   (b) 它后面紧跟着 click → skip（click 在 navigate 之前就该执行）
        skip_indices: set[int] = set()
        for i in range(len(steps)):
            s = steps[i]
            if s.get("action") != "navigate":
                continue
            gap = int(s.get("wait_after") or 0)
            prev_action = steps[i - 1].get("action") if i > 0 else None
            next_action = steps[i + 1].get("action") if i + 1 < len(steps) else None
            if prev_action == "click" and gap < 3000:
                # click 触发的跳转 → 跳过 navigate
                skip_indices.add(i)
            elif next_action == "click" and gap == 0:
                # navigate 记录顺序不对（应该先 click 再 navigate）→ 跳过 navigate，
                # click 本身已经会处理跳转
                skip_indices.add(i)
        if skip_indices:
            print(f"[browser_control] template_run: skip {len(skip_indices)} redundant navigate steps: {sorted(skip_indices)}")

        results = []
        # 确保有 page
        requested_mode = (req.get("mode") or template.get("mode") or self._mode or "auto").strip().lower()
        cdp_endpoint = (req.get("cdp_endpoint") or self._cdp_endpoint or "").strip()
        final_mode, final_ep = self._resolve_mode(requested_mode, cdp_endpoint)
        try:
            page = self._ensure_page(visible=True, mode=final_mode, cdp_endpoint=final_ep)
        except Exception as e:
            return {"ok": False, "error": f"启动浏览器失败：{e}"}

        # 允许用户通过参数覆盖默认等待时间
        default_wait_ms = int(req.get("default_wait_ms", 1000))

        # —— 临时禁用录制捕获 ——
        with self._recording_lock:
            _prev_recording = self._recording
            self._recording = False
        try:
            for i, step in enumerate(steps):
                # 跳过预处理标记的冗余 navigate
                if i in skip_indices:
                    results.append({"step": i, "action": step.get("action", ""), "ok": True,
                                    "skipped": True, "note": "冗余 navigate（click 自带跳转）"})
                    continue

                action = step.get("action", "")
                step_req: dict = {}
                if action == "navigate":
                    step_req = {"action": "navigate", "url": step.get("url") or step.get("url_after") or ""}
                elif action == "click":
                    sel = step.get("stable_selector") or step.get("selector") or ""
                    if not sel.strip():
                        results.append({"step": i, "action": action, "ok": True, "skipped": True,
                                        "note": "无选择器（页面空白处误录），已跳过"})
                        continue
                    step_req = {"action": "click", "selector": sel}
                elif action == "type":
                    sel = step.get("stable_selector") or step.get("selector") or ""
                    if not sel.strip():
                        results.append({"step": i, "action": action, "ok": True, "skipped": True,
                                        "note": "无选择器（误录步骤），已跳过"})
                        continue
                    step_req = {"action": "type", "selector": sel, "text": step.get("text", "")}
                elif action == "scroll":
                    try:
                        dy = int(float(step.get("delta_y") or 0))
                    except (TypeError, ValueError):
                        dy = 0
                    if not dy:
                        results.append({"step": i, "action": action, "ok": True, "skipped": True,
                                        "note": "滚动量为 0，已跳过"})
                        continue
                    coord = step.get("coordinate") or {}
                    step_req = {"action": "scroll", "delta_y": dy,
                                "x": coord.get("x", -1), "y": coord.get("y", -1)}
                elif action == "coordinate_click":
                    coord = step.get("coordinate") or {}
                    step_req = {"action": "coordinate_click", "x": coord.get("x", -1), "y": coord.get("y", -1)}
                elif action == "wait":
                    step_req = {"action": "wait", "value": step.get("value", 1)}
                elif action == "screenshot":
                    step_req = {"action": "screenshot", "purpose": step.get("purpose", "inspect")}
                else:
                    results.append({"step": i, "action": action, "ok": False, "error": f"跳过未知 action: {action}"})
                    continue
                step_req.setdefault("timeout", req.get("timeout", 15))
                step_req.setdefault("visible", True)
                r = self._handle(step_req)

                # —— click/type 选择器定位失败 → 回退鼠标坐标点击 ——
                # 录制时记录了归一化坐标；nth-of-type 长链选择器在页面 DOM 变动后
                # 极易失配，此时用录制坐标直接鼠标点击兜底（type 再键盘输入）。
                if not r.get("ok") and action in ("click", "type"):
                    coord = step.get("coordinate") or {}
                    try:
                        fb_x = float(coord.get("x", -1))
                        fb_y = float(coord.get("y", -1))
                        fb_ok = 0.0 <= fb_x <= 1.0 and 0.0 <= fb_y <= 1.0
                    except (TypeError, ValueError):
                        fb_ok = False
                    if fb_ok:
                        note_sel = (step.get("stable_selector") or step.get("selector") or "")[:60]
                        print(f"[browser_control] template_run: step {i}({action}) 选择器定位失败，"
                              f"回退坐标点击 ({fb_x:.4f},{fb_y:.4f})")
                        fr = self._handle({"action": "coordinate_click", "x": fb_x, "y": fb_y,
                                           "timeout": req.get("timeout", 15)})
                        if fr.get("ok") and action == "type":
                            # 点击聚焦输入框后键盘输入（先全选清空，对齐 locator 版 fill("") 语义）
                            try:
                                kb = self._page.keyboard
                                kb.press("Control+a")
                                kb.press("Delete")
                                kb.type(step.get("text", ""), delay=30)
                                fr = {"ok": True, "current_url": self.current_url(),
                                      "data": f"选择器失配，已回退坐标输入 {len(step.get('text', ''))} 字符"}
                            except Exception as e:
                                fr = {"ok": False, "current_url": self.current_url(),
                                      "error": f"坐标回退输入失败: {e}"}
                        if fr.get("ok"):
                            fr["fallback"] = True
                            fr["fallback_note"] = f"选择器 [{note_sel}] 定位失败，已回退鼠标坐标点击"
                        r = fr

                results.append({"step": i, "action": action, **r})
                if not r.get("ok"):
                    return {"ok": False, "error": f"步骤 {i}({action}) 失败：{r.get('error','')}",
                            "results": results, "failed_at_step": i}
                page = self._page  # 刷新引用

                # —— 步骤间等待 ——
                wait_ms = int(step.get("wait_after") or 0)
                if wait_ms <= 0:
                    wait_ms = default_wait_ms

                # navigate 后额外等待页面加载完成
                if action == "navigate":
                    try:
                        # 优先等 networkidle（网络完全空闲），超时再用 domcontentloaded
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            pass
                # click 后如果页面可能导航，也等一下
                elif action == "click":
                    try:
                        # 短暂等待是否会发生导航（监听 load 事件）
                        page.wait_for_load_state("domcontentloaded", timeout=3000)
                    except Exception:
                        # 没导航（SPA 或 AJAX 更新），不报错
                        pass

                # 使用步骤自己的 wait_after 额外等待
                if wait_ms > 0:
                    try:
                        page.wait_for_timeout(wait_ms)
                    except Exception:
                        pass
        except Exception as e:
            return {"ok": False, "error": f"执行异常：{type(e).__name__}: {e}", "results": results}
        finally:
            # —— 恢复录制状态 ——
            with self._recording_lock:
                self._recording = _prev_recording

        return {"ok": True, "template": template.get("name"), "steps_executed": len(steps),
                "current_url": self.current_url(), "results": results}

    # ==============================================================
    # 请求分发（worker 线程内执行）
    # ==============================================================
    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        action = (req.get("action") or "").strip().lower()
        if not action:
            return {"ok": False, "data": "", "error": "缺少 action 参数", "current_url": self.current_url()}

        # —— 录制/模板管理 action（不需要浏览器 page，独立分发）——
        if action == "record_start":
            return self._record_start(req)

        if action == "record_status":
            return self._record_status()

        if action == "record_step_annotate":
            return self._record_step_annotate(req)

        if action == "record_step_delete":
            return self._record_step_delete(req)

        if action == "record_step_reorder":
            return self._record_step_reorder(req)

        if action == "record_complete":
            return self._record_complete(req)

        if action == "record_cancel":
            return self._record_cancel()

        if action == "record_pause":
            return self._record_pause()

        if action == "record_resume":
            return self._record_resume()

        if action == "record_step_set_wait":
            return self._record_step_set_wait(req)

        if action == "record_template_list":
            return self._template_list()

        if action == "record_template_load":
            return self._template_load(req)

        if action == "record_template_delete":
            return self._template_delete(req)

        if action == "record_template_run":
            # 模板回放走专门的执行器（会自行 _ensure_page + _handle 各步）
            return self._template_run(req)

        # resolve mode
        requested_mode = (req.get("mode") or self._mode or "auto").strip().lower()
        cdp_endpoint = (req.get("cdp_endpoint") or self._cdp_endpoint or "http://localhost:9222").strip()
        final_mode, final_ep = self._resolve_mode(requested_mode, cdp_endpoint)

        if action == "close":
            try:
                self._close()
                return {"ok": True, "data": "浏览器已关闭", "current_url": ""}
            except Exception as e:
                return {"ok": False, "data": "", "error": f"close 失败: {e}", "current_url": self.current_url()}

        # —— info：纯查询状态，不应无谓拉起新浏览器 ——
        # 但 cdp 模式例外：用户已经有浏览器在跑，只是我们还没 connect_over_cdp；
        # 做一次 ensure_page（cdp 模式只建立连接，不启动新浏览器），这样才能列出已有的标签页。
        if action == "info":
            if final_mode == "cdp" and self._context is None:
                try:
                    self._ensure_page(visible=False, mode="cdp", cdp_endpoint=final_ep)
                except Exception:
                    pass  # CDP 端点连不上就保持 context=None，下面会返回空列表
            page_list = self._list_pages() if self._context is not None else []
            n = len(page_list)
            active_idx = next((p["index"] for p in page_list if p["is_active"]), -1)
            cur_url = self.current_url()
            vw = vh = 0
            try:
                if self._page is not None and not self._page.is_closed():
                    vs = self._page.viewport_size or {}
                    vw = int(vs.get("width") or 0)
                    vh = int(vs.get("height") or 0)
            except Exception:
                pass
            lines = [f"mode={final_mode} visible={self._visible} pages={n} active=#{active_idx}"]
            for p in page_list:
                tag = "[active]" if p["is_active"] else "       "
                t = (p.get("title") or "").strip()
                url = p.get("url") or "(about:blank)"
                lines.append(f"[#{p['index']}]{tag} {url}" + (f"  — {t}" if t else ""))
            if vw and vh:
                lines.append(f"viewport={vw}x{vh}")
            if n == 0:
                lines.append("(当前没有打开的浏览器页面，navigate / new_tab 会自动启动)")
            summary = "\n".join(lines)
            return {
                "ok": True,
                "mode": final_mode,
                "cdp_endpoint": final_ep if final_mode == "cdp" else "",
                "visible": self._visible,
                "current_url": cur_url,
                "active_page_index": active_idx,
                "page_count": n,
                "pages": page_list,
                "viewport": {"w": vw, "h": vh} if vw and vh else {"w": 0, "h": 0},
                "data": summary,
            }

        # —— focus_tab：切标签页，需要已有连接但不应该自动启动浏览器 ——
        if action == "focus_tab":
            try:
                idx = int(req.get("tab_index")) if req.get("tab_index") is not None else -1
                if idx < 0:
                    idx = int(req.get("index")) if req.get("index") is not None else -1
            except Exception:
                idx = -1
            if idx < 0:
                return {"ok": False, "data": "", "error": "focus_tab 需要 index（≥0，可用 info 查看）",
                        "current_url": self.current_url()}
            # cdp 模式：如果浏览器在跑但还没 connect，先惰性连接（不启动新浏览器）
            if final_mode == "cdp" and self._context is None:
                try:
                    self._ensure_page(visible=False, mode="cdp", cdp_endpoint=final_ep)
                except Exception:
                    pass
            if self._context is None:
                return {"ok": False, "data": "", "error": "没有可用的浏览器（cdp 端点连不上 / 请先 navigate / new_tab）",
                        "current_url": self.current_url()}
            try:
                pages_now = list(self._context.pages)
            except Exception as e:
                return {"ok": False, "data": "", "error": f"获取标签页失败: {e}",
                        "current_url": self.current_url()}
            if idx >= len(pages_now):
                return {"ok": False, "data": "", "error": f"focus_tab index 越界：共 {len(pages_now)} 个，index={idx}",
                        "current_url": self.current_url()}
            target = pages_now[idx]
            try:
                target.bring_to_front()
            except Exception:
                pass
            self._page = target
            all_pages = self._list_pages()
            return {
                "ok": True,
                "mode": final_mode,
                "cdp_endpoint": final_ep if final_mode == "cdp" else "",
                "visible": self._visible,
                "current_url": self.current_url(),
                "active_page_index": idx,
                "page_count": len(all_pages),
                "pages": all_pages,
                "data": f"已切换到标签页 #{idx}: {self.current_url()}",
            }

        # —— 下面这些 action 需要浏览器页面，确保 page 可用 ——
        timeout_i = max(1, int(float(req.get("timeout") or 15)))
        visible = bool(req.get("visible"))
        try:
            page = self._ensure_page(visible=visible, mode=final_mode, cdp_endpoint=final_ep)
        except Exception as e:
            return {"ok": False, "data": "", "error": f"启动浏览器失败({final_mode}): {e}", "current_url": self.current_url()}

        try:
            # —— 可选：把本次操作重定向到 tab_index 指定的标签页 ——
            # switch_active=True：同时切为活动页（bring_to_front + 更新 self._page）；
            # switch_active=False（默认）：后台操作，不 bring_to_front、不改 self._page。
            redirected = False
            if action in ("navigate", "click", "coordinate_click", "type", "scroll",
                          "get_text", "get_html", "screenshot", "evaluate", "wait"):
                tab_index_req = req.get("tab_index")
                try:
                    tgt_idx = int(tab_index_req) if tab_index_req is not None else -1
                except Exception:
                    tgt_idx = -1
                if tgt_idx >= 0:
                    if self._context is None:
                        return {"ok": False, "data": "", "error": "没有可用的浏览器 context", "current_url": self.current_url()}
                    pages_now = list(self._context.pages)
                    if tgt_idx >= len(pages_now):
                        return {"ok": False, "data": "", "error": f"tab_index 越界：共有 {len(pages_now)} 个标签页，index={tgt_idx}", "current_url": self.current_url()}
                    target_page = pages_now[tgt_idx]
                    if bool(req.get("switch_active", False)):
                        try:
                            target_page.bring_to_front()
                        except Exception:
                            pass
                        self._page = target_page
                    page = target_page
                    redirected = True

            if action == "navigate":
                url = req.get("url") or ""
                if not url:
                    return {"ok": False, "data": "", "error": "navigate 需要 url", "current_url": self.current_url()}
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_i * 1000)
                page.wait_for_timeout(500)
                return {"ok": True, "data": f"已打开 {page.url}", "current_url": page.url}

            if action == "click":
                selector = req.get("selector") or ""
                if not selector:
                    return {"ok": False, "data": "", "error": "click 需要 selector", "current_url": self.current_url()}
                frame_idx, css = self._translate_selector(selector)  # @eN/@fNeN → (frame_idx, css)
                frame = self._resolve_frame(page, frame_idx)
                loc = frame.locator(css).first
                loc.wait_for(state="visible", timeout=timeout_i * 1000)
                loc.click(timeout=timeout_i * 1000)
                page.wait_for_timeout(300)
                return {"ok": True, "data": f"已点击 {selector}", "current_url": self.current_url()}

            if action == "coordinate_click":
                try:
                    nx = float(req.get("x") if req.get("x") is not None else -1.0)
                    ny = float(req.get("y") if req.get("y") is not None else -1.0)
                except Exception:
                    nx, ny = -1.0, -1.0
                if not (0.0 <= nx <= 1.0) or not (0.0 <= ny <= 1.0):
                    return {"ok": False, "data": "", "error": f"coordinate_click 需要归一化坐标 x,y ∈ [0,1]，收到 x={nx} y={ny}", "current_url": self.current_url()}
                vs = page.viewport_size or {}
                vw = int(vs.get("width") or 0)
                vh = int(vs.get("height") or 0)
                if not vw or not vh:
                    dims = page.evaluate("() => ({w: window.innerWidth || document.documentElement.clientWidth || 0, h: window.innerHeight || document.documentElement.clientHeight || 0})")
                    vw = int(dims.get("w") or 0)
                    vh = int(dims.get("h") or 0)
                if not vw or not vh:
                    return {"ok": False, "data": "", "error": "无法获取页面 viewport 尺寸，无法换算坐标", "current_url": self.current_url()}
                px = max(0, min(vw - 1, int(round(nx * vw))))
                py = max(0, min(vh - 1, int(round(ny * vh))))
                page.mouse.click(px, py)
                page.wait_for_timeout(300)
                return {"ok": True, "data": f"已点击 坐标 norm=({nx:.4f},{ny:.4f}) px=({px},{py}) viewport=({vw}x{vh})", "current_url": self.current_url(), "pixel": {"x": px, "y": py}, "viewport": {"width": vw, "height": vh}}

            if action == "scroll":
                try:
                    dy = int(float(req.get("delta_y") if req.get("delta_y") is not None
                                    else (req.get("dy") or 0)))
                except (TypeError, ValueError):
                    dy = 0
                if not dy:
                    return {"ok": False, "data": "", "error": "scroll 需要 delta_y（非 0 像素数；正=向下，负=向上）",
                            "current_url": self.current_url()}
                return self._do_scroll(page, dy, req.get("x"), req.get("y"))

            if action == "type":
                selector = req.get("selector") or ""
                text = req.get("text") or ""
                if not selector:
                    return {"ok": False, "data": "", "error": "type 需要 selector", "current_url": self.current_url()}
                frame_idx, css = self._translate_selector(selector)  # @eN/@fNeN → (frame_idx, css)
                frame = self._resolve_frame(page, frame_idx)
                loc = frame.locator(css).first
                loc.wait_for(state="visible", timeout=timeout_i * 1000)
                loc.fill("")
                loc.type(text, delay=30)
                return {"ok": True, "data": f"已在 {selector} 输入 {len(text)} 字符", "current_url": self.current_url()}

            if action == "get_text":
                selector = req.get("selector") or ""
                if selector:
                    frame_idx, css = self._translate_selector(selector)  # @eN/@fNeN → (frame_idx, css)
                    frame = self._resolve_frame(page, frame_idx)
                    loc = frame.locator(css).first
                    loc.wait_for(state="visible", timeout=timeout_i * 1000)
                    text = loc.inner_text(timeout=timeout_i * 1000)
                else:
                    text = page.inner_text("body")
                return {"ok": True, "data": (text or "")[:20000], "current_url": self.current_url()}

            if action == "get_html":
                fmt = (req.get("format") or "snapshot").strip().lower()
                selector = (req.get("selector") or "").strip()

                if fmt == "raw":
                    # 分层3：整页/整块原始 HTML（旧行为，仅在确需原始结构时用）
                    frame_idx, css = self._translate_selector(selector) if selector else (None, "")
                    if css:
                        frame = self._resolve_frame(page, frame_idx)
                        loc = frame.locator(css).first
                        loc.wait_for(state="visible", timeout=timeout_i * 1000)
                        html = loc.evaluate("(el) => el.outerHTML")
                    else:
                        html = page.content()
                    return {"ok": True, "data": (html or "")[:30000], "current_url": self.current_url(),
                            "format": "raw"}

                if fmt == "element":
                    # 分层2：按需回填单个元素的 outerHTML（@eN/@fNeN 或 CSS 选择器）
                    if not selector:
                        return {"ok": False, "data": "", "error": "format=element 需要 selector（@eN/@fNeN 或 CSS 选择器）",
                                "current_url": self.current_url()}
                    parsed = self._parse_eid(selector)
                    if parsed:
                        frame_idx, eid = parsed
                        frame = self._resolve_frame(page, frame_idx)
                        res_el = frame.evaluate(self._element_extract_js(), eid)
                        if not res_el or not res_el.get("found"):
                            return {"ok": False, "data": "", "error": (res_el or {}).get("error", "元素未找到"),
                                    "current_url": self.current_url()}
                        return {"ok": True, "data": res_el["outerHTML"], "current_url": self.current_url(),
                                "format": "element", "eid": f"f{frame_idx}{eid}"}
                    frame_idx, css = self._translate_selector(selector)
                    frame = self._resolve_frame(page, frame_idx)
                    loc = frame.locator(css).first
                    loc.wait_for(state="visible", timeout=timeout_i * 1000)
                    html = loc.evaluate("(el) => el.outerHTML")
                    return {"ok": True, "data": (html or "")[:8000], "current_url": self.current_url(),
                            "format": "element"}

                # 分层1（默认）：多帧结构化快照 —— DOM 瘦身 + 关键元素编号 + 帧前缀
                # 先等 SPA 渲染：轮询直到所有可见 iframe 里出现 input/contenteditable/[role=*]
                _iframe_waited = False
                for _ in range(6):
                    frames_ready = True
                    total_body_inputs = page.evaluate(
                        """() => document.querySelectorAll(
                            'input,textarea,[contenteditable],button,[role=button],[role=textbox],[role=searchbox]'
                        ).length"""
                    ) or 0
                    for f in page.frames:
                        if f == page.main_frame:
                            continue
                        try:
                            n = f.evaluate(
                                """() => document.querySelectorAll(
                                    'input,textarea,[contenteditable],button,[role=button],[role=textbox],[role=searchbox]'
                                ).length"""
                            ) or 0
                            if n > 0:
                                total_body_inputs += n
                                break
                        except Exception:
                            continue
                    if total_body_inputs > 0:
                        _iframe_waited = True
                        break
                    page.wait_for_timeout(800)

                # 遍历所有帧采集
                frame_lines: list[str] = []
                total_count = 0
                truncated = False
                frame_meta: list[dict] = []
                js_code = self._snapshot_js()
                for f_idx, frame in enumerate(page.frames):
                    try:
                        snap = frame.evaluate(js_code, f_idx)
                    except Exception:
                        continue
                    if not isinstance(snap, dict) or not snap.get("count"):
                        continue
                    # JS 端已经根据 frameIdx 输出了带/不带帧前缀的 @eN，这里直接取
                    frame_snap = snap.get("snapshot", "")
                    frame_title = ""
                    try:
                        frame_title = (frame.title() or "").strip()[:50]
                    except Exception:
                        pass
                    frame_url = ""
                    try:
                        frame_url = (frame.url or "")[:100]
                    except Exception:
                        pass
                    header = f"\n--- frame f{f_idx}: {frame_title or '(no title)'}  url={frame_url} ---\n"
                    frame_lines.append(header + frame_snap)
                    total_count += snap.get("count", 0)
                    if snap.get("truncated"):
                        truncated = True
                    frame_meta.append({
                        "idx": f_idx,
                        "title": frame_title,
                        "url": frame_url,
                        "count": snap.get("count", 0),
                        "main": f_idx == 0,
                    })

                note = (
                    "结构化快照（多帧采集）：已为关键元素编号 @fNeM（f=帧号，主帧省略 f0 前缀）。"
                    "后续 click/type/get_text/element 的 selector 直接传 @eN 或 @fNeM 即可操作。"
                    "找不到输入框？先 format=snapshot 看它在哪个 frame；跨域 iframe 里的元素编号会自动带帧前缀。"
                    "页面跳转/刷新后编号失效，需重新 snapshot。"
                )
                if truncated:
                    note += "（部分帧已达元素上限 400，可按需 format=element 补充）"
                if not _iframe_waited:
                    note += "（SPA 渲染等待超时，可能有元素未捕获，可稍后再 snapshot）"

                return {
                    "ok": True,
                    "data": "\n".join(frame_lines),
                    "format": "snapshot",
                    "element_count": total_count,
                    "frames": frame_meta,
                    "truncated": truncated,
                    "_note": note,
                    "current_url": self.current_url(),
                }

            if action == "screenshot":
                url = req.get("url") or ""
                if url:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_i * 1000)
                    page.wait_for_timeout(500)

                purpose = (req.get("purpose") or "inspect").strip().lower()

                # —— 截图策略 ——
                # CDP 模式下 Playwright screenshot 会卡住（内部等 fonts），
                # 优先用原生 websocket 调 CDP Page.captureScreenshot 绕过它。
                # 但 CDP direct 只能截浏览器默认页（_page_ws_url 绑定的是初始连接页），
                # 后台标签页操作时必须走 Playwright，否则截到的是别的页。
                png_bytes: bytes | None = None

                if self._mode == "cdp" and self._page_ws_url and not redirected:
                    png_bytes = self._cdp_screenshot_direct()

                if png_bytes is None:
                    # Playwright 兜底
                    try:
                        png_bytes = page.screenshot(
                            type="png", full_page=False,
                            animations="disabled", timeout=10000,
                        )
                    except Exception:
                        png_bytes = page.screenshot(type="png", full_page=False, timeout=30000)

                b64 = base64.b64encode(png_bytes).decode("ascii")
                data_url = f"data:image/png;base64,{b64}"

                res: dict[str, Any] = {
                    "ok": True,
                    "data": b64,
                    "content_type": "image/png",
                    "current_url": self.current_url(),
                }

                if purpose in ("share", "user", "save"):
                    # —— share 模式：不落盘，直接把 base64 data URL 塞进返回包 ——
                    # 数据会随 tool_result 一起流到前端对话区渲染成缩略图，
                    # 并保存在 AppData/conversations/*.json 的对话记录里，不再落独立截图文件夹。
                    res["purpose"] = "share"
                    res["image_data_url"] = data_url
                    res["_note"] = "截图已随本次工具结果返回给前端对话区展示，数据保存在 AppData 对话记录中。"
                else:
                    # —— inspect 模式（默认）：不落盘，只给智能体内部看图 ——
                    res["purpose"] = "inspect"
                    res["_note"] = "仅供智能体内部判断页面状况，未保存，也不推送到前端。"

                return res

            if action == "evaluate":
                code = ((req.get("js") or "") or (req.get("text") or "")).strip()
                if not code:
                    return {"ok": False, "data": "", "error": "evaluate 需要 js", "current_url": self.current_url()}
                wrapped = (
                    "(async () => {"
                    "  try {"
                    "    const __r = await (async function() {" + code + "})();"
                    "    return { __result: __r === undefined ? null : __r };"
                    "  } catch(e) { return { __error: String(e) }; }"
                    "})()"
                )
                res = page.evaluate(wrapped)
                if isinstance(res, dict) and "__error" in res:
                    return {"ok": False, "data": "", "error": f"JS 执行错误: {res['__error']}", "current_url": self.current_url()}
                try:
                    out_data = _json.dumps(res.get("__result"), ensure_ascii=False)[:20000]
                except Exception:
                    out_data = str(res)[:20000]
                return {"ok": True, "data": out_data, "current_url": self.current_url()}

            if action == "wait":
                ms = max(0, float(req.get("value") or 0)) * 1000
                page.wait_for_timeout(int(ms))
                return {"ok": True, "data": f"已等待 {ms:.0f}ms", "current_url": self.current_url()}

            if action == "new_tab":
                """新建标签页并设为当前活动页；url 可选，提供则在打开后跳转。"""
                try:
                    if self._context is None:
                        return {"ok": False, "data": "", "error": "没有可用的浏览器 context", "current_url": self.current_url()}
                    new_page = self._context.new_page()
                except Exception as e:
                    return {"ok": False, "data": "", "error": f"新建标签页失败: {type(e).__name__}: {e}", "current_url": self.current_url()}
                self._page = new_page
                url = (req.get("url") or "").strip()
                goto_err = ""
                if url:
                    try:
                        new_page.goto(url, wait_until="domcontentloaded", timeout=timeout_i * 1000)
                        new_page.wait_for_timeout(300)
                    except Exception as e:
                        goto_err = f"跳转失败: {e}"
                try:
                    new_page.bring_to_front()
                except Exception:
                    pass
                all_pages = self._list_pages()
                new_idx = next((p["index"] for p in all_pages if p["is_active"]), -1)
                if new_idx < 0 and all_pages:
                    new_idx = all_pages[-1]["index"]
                note = f"已新建标签页 #{new_idx}"
                try:
                    final_url = new_page.url
                except Exception:
                    final_url = ""
                if goto_err:
                    note += f"；{goto_err}"
                else:
                    note += f"：{final_url or '(空白)'}"
                return {
                    "ok": True,
                    "mode": final_mode,
                    "cdp_endpoint": final_ep if final_mode == "cdp" else "",
                    "visible": self._visible,
                    "current_url": self.current_url(),
                    "active_page_index": new_idx,
                    "page_count": len(all_pages),
                    "pages": all_pages,
                    "data": note,
                }

            return {"ok": False, "data": "", "error": f"未知 action: {action}", "current_url": self.current_url()}

        except _PWTimeout as e:
            return {"ok": False, "data": "", "error": f"操作超时: {e}", "current_url": self.current_url()}
        except Exception as e:
            return {"ok": False, "data": "", "error": f"{type(e).__name__}: {e}", "current_url": self.current_url()}


# ==============================================================
# run() —— 插件入口（线程安全、asyncio-loop 无关）
# ==============================================================
def run(action: str = "", url: str = "", selector: str = "",
        text: str = "", js: str = "", value: float | int = 1,
        timeout: float | int = 15, visible: bool = True,
        mode: str = "auto", cdp_endpoint: str = "",
        purpose: str = "",
        x: float = -1.0, y: float = -1.0,
        tab_index: int = -1, switch_active: bool = False,
        index: int = -1,  # 已弃用，请改用 tab_index；focus_tab 仍可识别
        format: str = "",
        **_extra: Any) -> dict[str, Any]:
    """供 LLM 调用的主入口。始终在线程中执行，不依赖当前是否有 asyncio loop。"""
    # cdp_endpoint 为空时由 _normalize_cdp_endpoint 兜底为 127.0.0.1:9222
    req = {
        "action": action, "url": url, "selector": selector,
        "text": text, "js": js, "value": value,
        "timeout": timeout, "visible": visible,
        "mode": mode, "cdp_endpoint": cdp_endpoint,
        "purpose": purpose,
        "x": x, "y": y,
        "tab_index": tab_index, "switch_active": switch_active,
        "index": index,  # 已弃用别名，仅供 focus_tab 兼容旧调用方
        "format": format,
    }
    # 合并额外参数（录制相关：record_name, record_description, step_index, annotation,
    # new_order, template_name, record_id 等）
    if _extra:
        req.update({k: v for k, v in _extra.items() if v is not None})
    # 模板回放 / 录制相关操作可能耗时较长 → 用更大的 submit 超时
    action = req.get("action", "")
    if action in ("record_template_run", "record_start"):
        submit_timeout = max(10, int(float(timeout or 15)) * 20 + 60)  # 例如 15*20+60=360s
    else:
        submit_timeout = max(5, int(float(timeout or 15)) + 30)
    return _BrowserWorker.instance().submit(req, timeout=submit_timeout)
