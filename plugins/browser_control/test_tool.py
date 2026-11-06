# -*- coding: utf-8 -*-
"""browser_control 插件单元测试：滚轮录制识别 + 回放选择器失配坐标回退。

不启动真实浏览器：
  - JS 监听器：node vm 沙箱模拟 window，派发 wheel/click 事件，验证 emit 的
    __AC_REC__ JSON（真正验证「滚轮能被识别」的浏览器侧逻辑；无 node 则跳过）
  - _parse_rec_console：scroll / click(带坐标) payload → step dict
  - _append_step：scroll 连续合并 / 正负抵消 / 不同容器不合并
  - _do_scroll：mock page 验证分批 wheel 调用序列与鼠标定位
  - _template_run：click/type 选择器失败 → 坐标回退；scroll 步骤分发
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_PY = os.path.join(HERE, "tool.py")

_spec = importlib.util.spec_from_file_location("bc_tool_under_test", TOOL_PY)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# ============================================================
# 测试替身
# ============================================================
class MockMouse:
    def __init__(self):
        self.calls = []

    def move(self, x, y, **kw):
        self.calls.append(("move", x, y))

    def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))

    def click(self, x, y, **kw):
        self.calls.append(("click", x, y))


class MockKeyboard:
    def __init__(self):
        self.calls = []

    def press(self, key, **kw):
        self.calls.append(("press", key))

    def type(self, text, delay=None, **kw):
        self.calls.append(("type", text))


class MockPage:
    def __init__(self, vw=1000, vh=800):
        self.viewport_size = {"width": vw, "height": vh}
        self.mouse = MockMouse()
        self.keyboard = MockKeyboard()
        self.url = "https://example.com/"

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state, timeout=None):
        pass

    def evaluate(self, js):
        return {"w": self.viewport_size["width"], "h": self.viewport_size["height"]}

    def is_closed(self):
        return False


def make_worker():
    """绕过 __init__（不启动 Playwright worker / Tk UI 线程），手动补录制态属性。"""
    w = object.__new__(mod._BrowserWorker)
    w._page = None
    w._recording = False
    w._recording_paused = False
    w._recording_steps = []
    w._recording_lock = threading.Lock()
    w._recording_meta = {}
    w._last_step_time = 0.0
    w._recording_handlers = []
    w._mode = "auto"
    w._cdp_endpoint = ""
    w._context = None
    w._visible = False
    return w


def rec_text(payload: dict) -> str:
    return mod._BrowserWorker._AC_REC_PREFIX + json.dumps(payload)


# ============================================================
# _parse_rec_console
# ============================================================
def test_parse_scroll_step():
    w = make_worker()
    step = w._parse_rec_console(
        rec_text({"kind": "scroll", "selector": "", "delta_y": 320, "nx": 0.405, "ny": 0.3875}), None)
    assert step is not None
    assert step["action"] == "scroll"
    assert step["delta_y"] == 320
    assert step["coordinate"] == {"x": 0.405, "y": 0.3875}
    assert step["selector"] == ""


def test_parse_scroll_negative_and_bad_delta():
    w = make_worker()
    step = w._parse_rec_console(rec_text({"kind": "scroll", "delta_y": -240, "nx": 0.5, "ny": 0.5}), None)
    assert step["delta_y"] == -240
    step2 = w._parse_rec_console(rec_text({"kind": "scroll", "delta_y": "abc"}), None)
    assert step2["delta_y"] == 0
    assert step2["coordinate"] is None


def test_parse_click_with_coord():
    w = make_worker()
    step = w._parse_rec_console(
        rec_text({"kind": "click", "selector": "#btn", "text": "提交", "tag": "button",
                  "nx": 0.25, "ny": 0.5}), None)
    assert step["action"] == "click"
    assert step["coordinate"] == {"x": 0.25, "y": 0.5}


def test_parse_click_without_coord_backward_compat():
    w = make_worker()
    step = w._parse_rec_console(
        rec_text({"kind": "click", "selector": "#btn", "text": "提交", "tag": "button"}), None)
    assert step["action"] == "click"
    assert step["coordinate"] is None


def test_parse_click_no_selector_dropped():
    w = make_worker()
    assert w._parse_rec_console(rec_text({"kind": "click", "selector": "", "nx": 0.1, "ny": 0.1}), None) is None


def test_parse_type_with_coord():
    w = make_worker()
    step = w._parse_rec_console(
        rec_text({"kind": "type", "selector": "#q", "text": "hello", "nx": 0.3, "ny": 0.2}), None)
    assert step["action"] == "type"
    assert step["coordinate"] == {"x": 0.3, "y": 0.2}


def test_parse_unknown_kind_dropped():
    w = make_worker()
    assert w._parse_rec_console(rec_text({"kind": "hover", "selector": "#x"}), None) is None


# ============================================================
# _append_step：scroll 连续合并
# ============================================================
def _start_recording(w):
    w._recording = True
    w._recording_paused = False
    w._recording_steps = []
    w._last_step_time = 0.0


def test_append_scroll_merges_consecutive():
    w = make_worker()
    _start_recording(w)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 320, "nx": 0.4, "ny": 0.3}), None))
    time.sleep(0.02)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 100, "nx": 0.41, "ny": 0.31}), None))
    assert len(w._recording_steps) == 1
    assert w._recording_steps[0]["delta_y"] == 420
    assert w._recording_steps[0]["coordinate"] == {"x": 0.41, "y": 0.31}


def test_append_scroll_cancel_out():
    w = make_worker()
    _start_recording(w)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 320, "nx": 0.4, "ny": 0.3}), None))
    time.sleep(0.02)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": -320, "nx": 0.4, "ny": 0.3}), None))
    assert len(w._recording_steps) == 0


def test_append_scroll_different_container_no_merge():
    w = make_worker()
    _start_recording(w)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "#feed", "delta_y": 320, "nx": 0.4, "ny": 0.3}), None))
    time.sleep(0.02)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 100, "nx": 0.4, "ny": 0.3}), None))
    assert len(w._recording_steps) == 2


def test_append_scroll_far_apart_no_merge():
    w = make_worker()
    _start_recording(w)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 320, "nx": 0.4, "ny": 0.3}), None))
    # 模拟间隔 > 3s
    w._last_step_time -= 4.0
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 100, "nx": 0.4, "ny": 0.3}), None))
    assert len(w._recording_steps) == 2


def test_append_scroll_click_between_no_merge():
    w = make_worker()
    _start_recording(w)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 320, "nx": 0.4, "ny": 0.3}), None))
    time.sleep(0.02)
    w._append_step(w._parse_rec_console(rec_text({"kind": "click", "selector": "#btn", "nx": 0.4, "ny": 0.3}), None))
    time.sleep(0.02)
    w._append_step(w._parse_rec_console(rec_text({"kind": "scroll", "selector": "", "delta_y": 100, "nx": 0.4, "ny": 0.3}), None))
    assert len(w._recording_steps) == 3
    assert [s["action"] for s in w._recording_steps] == ["scroll", "click", "scroll"]


# ============================================================
# _do_scroll：分批 wheel + 鼠标定位
# ============================================================
def test_do_scroll_batches_positive():
    w = make_worker()
    p = MockPage()
    res = w._do_scroll(p, 300)
    assert res["ok"] is True
    wheels = [c for c in p.mouse.calls if c[0] == "wheel"]
    assert wheels == [("wheel", 0, 120), ("wheel", 0, 120), ("wheel", 0, 60)]


def test_do_scroll_batches_negative():
    w = make_worker()
    p = MockPage()
    res = w._do_scroll(p, -250)
    assert res["ok"] is True
    wheels = [c for c in p.mouse.calls if c[0] == "wheel"]
    assert wheels == [("wheel", 0, -120), ("wheel", 0, -120), ("wheel", 0, -10)]


def test_do_scroll_moves_mouse_to_norm_coord():
    w = make_worker()
    p = MockPage(vw=1000, vh=800)
    w._do_scroll(p, 120, 0.25, 0.5)
    moves = [c for c in p.mouse.calls if c[0] == "move"]
    assert moves == [("move", 250, 400)]


def test_do_scroll_without_coord_no_move():
    w = make_worker()
    p = MockPage()
    w._do_scroll(p, 120)
    assert not [c for c in p.mouse.calls if c[0] == "move"]


def test_do_scroll_invalid_coord_ignored():
    w = make_worker()
    p = MockPage()
    w._do_scroll(p, 120, -1, -1)
    w._do_scroll(p, 120, 1.5, 0.5)
    w._do_scroll(p, 120, None, None)
    assert not [c for c in p.mouse.calls if c[0] == "move"]


# ============================================================
# _template_run：scroll 步骤分发 + click/type 坐标回退
# ============================================================
def make_run_worker(steps, handle_impl, page=None):
    w = make_worker()
    template = {"name": "t", "mode": "auto", "steps": steps}
    w._template_load = lambda req: {"ok": True, "template": template}
    w._resolve_mode = lambda m, e: (m or "auto", e)
    w._ensure_page = lambda **kw: (page or MockPage())
    w._handle = handle_impl
    w._page = page or MockPage()
    return w


def _failing_selector_handle(calls):
    """click/type 模拟选择器超时失败；coordinate_click/scroll/navigate 成功。"""
    def h(req):
        calls.append(dict(req))
        if req["action"] in ("click", "type"):
            return {"ok": False, "error": "Timeout 15000ms exceeded", "current_url": ""}
        return {"ok": True, "data": "ok", "current_url": ""}
    return h


def test_template_run_click_fallback_to_coordinate():
    calls = []
    w = make_run_worker(
        [{"index": 0, "action": "click", "selector": "div:nth-of-type(1) > span",
          "stable_selector": "div:nth-of-type(1) > span",
          "coordinate": {"x": 0.25, "y": 0.5}, "wait_after": 0}],
        _failing_selector_handle(calls))
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is True
    fb = [c for c in calls if c["action"] == "coordinate_click"]
    assert len(fb) == 1
    assert abs(fb[0]["x"] - 0.25) < 1e-9 and abs(fb[0]["y"] - 0.5) < 1e-9
    r0 = res["results"][0]
    assert r0.get("fallback") is True
    assert "回退鼠标坐标点击" in r0.get("fallback_note", "")


def test_template_run_click_fail_without_coord_stops():
    calls = []
    w = make_run_worker(
        [{"index": 0, "action": "click", "selector": "div > span",
          "stable_selector": "div > span", "coordinate": None, "wait_after": 0}],
        _failing_selector_handle(calls))
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is False
    assert res.get("failed_at_step") == 0
    assert not [c for c in calls if c["action"] == "coordinate_click"]


def test_template_run_type_fallback_types_via_keyboard():
    calls = []
    p = MockPage()
    w = make_run_worker(
        [{"index": 0, "action": "type", "selector": "#q", "stable_selector": "#q",
          "text": "hello world", "coordinate": {"x": 0.3, "y": 0.2}, "wait_after": 0}],
        _failing_selector_handle(calls), page=p)
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is True
    r0 = res["results"][0]
    assert r0.get("fallback") is True
    # 坐标点击聚焦 → 全选清空 → 键盘输入
    assert p.keyboard.calls == [("press", "Control+a"), ("press", "Delete"), ("type", "hello world")]


def test_template_run_fallback_fail_stops():
    calls = []

    def h(req):
        calls.append(dict(req))
        return {"ok": False, "error": "boom", "current_url": ""}

    w = make_run_worker(
        [{"index": 0, "action": "click", "selector": "div > span",
          "coordinate": {"x": 0.25, "y": 0.5}, "wait_after": 0}], h)
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is False
    assert res.get("failed_at_step") == 0


def test_template_run_scroll_step_dispatch():
    calls = []

    def h(req):
        calls.append(dict(req))
        return {"ok": True, "data": "ok", "current_url": ""}

    w = make_run_worker(
        [{"index": 0, "action": "scroll", "delta_y": -300,
          "coordinate": {"x": 0.5, "y": 0.5}, "wait_after": 0}], h)
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is True
    scr = [c for c in calls if c["action"] == "scroll"]
    assert scr == [{"action": "scroll", "delta_y": -300, "x": 0.5, "y": 0.5,
                    "timeout": 15, "visible": True}]


def test_template_run_scroll_zero_skipped():
    calls = []

    def h(req):
        calls.append(dict(req))
        return {"ok": True, "data": "ok", "current_url": ""}

    w = make_run_worker([{"index": 0, "action": "scroll", "delta_y": 0, "wait_after": 0}], h)
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is True
    assert res["results"][0].get("skipped") is True
    assert not [c for c in calls if c["action"] == "scroll"]


def test_template_run_scroll_without_coord_uses_minus_one():
    calls = []

    def h(req):
        calls.append(dict(req))
        return {"ok": True, "data": "ok", "current_url": ""}

    w = make_run_worker([{"index": 0, "action": "scroll", "delta_y": 240, "coordinate": None,
                          "wait_after": 0}], h)
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is True
    scr = [c for c in calls if c["action"] == "scroll"][0]
    assert scr["x"] == -1 and scr["y"] == -1


def test_template_run_click_success_no_fallback():
    calls = []

    def h(req):
        calls.append(dict(req))
        return {"ok": True, "data": "已点击", "current_url": ""}

    w = make_run_worker(
        [{"index": 0, "action": "click", "selector": "#btn", "stable_selector": "#btn",
          "coordinate": {"x": 0.25, "y": 0.5}, "wait_after": 0}], h)
    res = w._template_run({"template_name": "t"})
    assert res["ok"] is True
    assert not [c for c in calls if c["action"] == "coordinate_click"]
    assert "fallback" not in res["results"][0]


# ============================================================
# JS 监听器：node vm 沙箱模拟 wheel/click 事件
# ============================================================
_NODE_RUNNER = r"""
const fs = require('fs');
const vm = require('vm');
const listenerJs = fs.readFileSync(process.argv[2], 'utf8');
let CFG = {};
if (process.argv[3]) CFG = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const emitted = [];
const winListeners = {};
const timers = [];
const win = {
  innerWidth: 1000, innerHeight: 800,
  frameElement: null,
  addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
  getComputedStyle: () => ({ overflowY: CFG.overflowY || 'visible' }),
};
win.top = win;
const sandbox = {
  console: { log: (s) => emitted.push(String(s)) },
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  window: win,
  document: { getElementById: () => null },
  location: { href: 'https://example.com/' },
  history: { pushState() {}, replaceState() {} },
};
vm.createContext(sandbox);
vm.runInContext(listenerJs, sandbox);

function fire(type, ev) { (winListeners[type] || []).forEach(fn => fn(ev)); }
function flushTimers() { const t = timers.splice(0); t.forEach(fn => fn()); }
function el(tag, opts) {
  return Object.assign({ tagName: tag, nodeType: 1, id: '', name: '', classList: [],
                         parentElement: null, previousElementSibling: null,
                         innerText: '', textContent: '', value: '' }, opts || {});
}

// 1) 连续 wheel（同一次滚动）→ 聚合为一步
fire('wheel', { target: el('DIV', CFG.el), clientX: 400, clientY: 300, deltaY: 100, deltaMode: 0, ctrlKey: false });
fire('wheel', { target: el('DIV', CFG.el), clientX: 402, clientY: 305, deltaY: 100, deltaMode: 0, ctrlKey: false });
fire('wheel', { target: el('DIV', CFG.el), clientX: 405, clientY: 310, deltaY: 120, deltaMode: 0, ctrlKey: false });
// ctrl+wheel = 页面缩放，必须忽略
fire('wheel', { target: el('DIV', CFG.el), clientX: 400, clientY: 300, deltaY: 500, deltaMode: 0, ctrlKey: true });
flushTimers();

// 2) 行模式 deltaMode=1 → 换算像素（3 行 × 40）
fire('wheel', { target: el('DIV', CFG.el), clientX: 10, clientY: 10, deltaY: 3, deltaMode: 1, ctrlKey: false });
flushTimers();

// 3) click 带归一化坐标
fire('click', { target: el('BUTTON', CFG.el), clientX: 250, clientY: 125 });

const out = emitted.filter(s => s.startsWith('__AC_REC__:')).map(s => JSON.parse(s.slice('__AC_REC__:'.length)));
console.log(JSON.stringify(out));
"""


def _run_node_sandbox(js: str, cfg: dict | None = None) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        js_path = os.path.join(td, "listeners.js")
        runner_path = os.path.join(td, "runner.js")
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js)
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(_NODE_RUNNER)
        cmd = ["node", runner_path, js_path]
        if cfg is not None:
            cfg_path = os.path.join(td, "cfg.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            cmd.append(cfg_path)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=td)
    assert r.returncode == 0, f"node 运行失败: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node，跳过 JS 沙箱验证")
def test_js_listeners_wheel_and_click():
    w = make_worker()
    js = w._user_recording_listeners_js()
    assert "wheel" in js
    events = _run_node_sandbox(js)
    scrolls = [e for e in events if e["kind"] == "scroll"]
    clicks = [e for e in events if e["kind"] == "click"]
    # 连续 3 次 wheel（100+100+120）聚合为一步 320；ctrl+wheel 被忽略
    assert len(scrolls) == 2
    assert scrolls[0]["delta_y"] == 320
    assert abs(scrolls[0]["nx"] - 0.405) < 1e-6 and abs(scrolls[0]["ny"] - 0.3875) < 1e-6
    # 行模式 3 行 × 40 = 120 像素
    assert scrolls[1]["delta_y"] == 120
    # click 携带归一化坐标（250/1000, 125/800；toFixed(4) 量化容差 1e-3）
    assert len(clicks) == 1
    assert abs(clicks[0]["nx"] - 0.25) < 1e-3 and abs(clicks[0]["ny"] - 0.15625) < 1e-3
    assert clicks[0]["selector"]


@pytest.mark.skipif(shutil.which("node") is None, reason="本机无 node，跳过 JS 沙箱验证")
def test_js_listeners_wheel_scroll_container_detection():
    """wheel 目标在可滚动容器内 → selector 记录该容器（overflowY:auto + 可滚）。"""
    w = make_worker()
    js = w._user_recording_listeners_js()
    events = _run_node_sandbox(js, cfg={
        "overflowY": "auto",
        "el": {"classList": ["feed"], "scrollHeight": 2000, "clientHeight": 500},
    })
    scrolls = [e for e in events if e["kind"] == "scroll"]
    assert scrolls and scrolls[0]["selector"] == "div.feed"