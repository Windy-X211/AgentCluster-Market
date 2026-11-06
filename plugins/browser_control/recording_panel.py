"""操作模板录制面板 —— Python tkinter GUI。

⚠️ Windows 上 tkinter 的 Tk() + mainloop() 必须在同一个线程里，
   而且不能是 daemon 子线程。所以我们在 _BrowserWorker.__init__ 里就
   启动一个专用 UI 线程，让它持有全局 Tk root 并跑 mainloop。
   后面要弹面板时用 root.after() 调度到 UI 线程安全创建窗口。
"""
from __future__ import annotations

import json
import queue as _queue
import threading
import time as _time
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    _TK_OK = True
except Exception:
    _TK_OK = False


# ============================================================
# 全局 UI 线程（在 BrowserWorker.__init__ 里启动）
# ============================================================
_ui_root: "tk.Tk | None" = None
_ui_thread_started = False
_ui_thread_lock = threading.Lock()
_active_panel = None


def ensure_ui_thread() -> "tk.Tk | None":
    """确保全局 UI 线程已启动，返回全局 Tk root。worker.__init__ 里调用。"""
    global _ui_root, _ui_thread_started
    if not _TK_OK:
        return None
    with _ui_thread_lock:
        if _ui_thread_started:
            return _ui_root
        _ui_thread_started = True

        def _ui_loop():
            global _ui_root
            try:
                _ui_root = tk.Tk()
                _ui_root.withdraw()  # 初始隐藏，不显示空白根窗口
                _ui_root.title("BrowserControl UI")
                _ui_root.protocol("WM_DELETE_WINDOW", lambda: None)  # 防止关闭
                _ui_root.mainloop()
            except Exception as e:
                print(f"[recording_panel] UI 线程异常: {e}")
                _ui_root = None

        t = threading.Thread(target=_ui_loop, name="tk-ui", daemon=True)
        t.start()
        # 等 Tk() 真正创建好
        for _ in range(50):
            if _ui_root is not None:
                break
            _time.sleep(0.02)
        return _ui_root


def ui_root() -> "tk.Tk | None":
    return _ui_root


# ============================================================
# open_panel —— 从 worker 线程调用，调度到 UI 线程安全创建窗口
# ============================================================
def open_panel(worker, meta: dict) -> "RecordingPanel | None":
    global _active_panel, _ui_root
    if not _TK_OK:
        return None
    root = ensure_ui_thread()
    if root is None:
        return None

    with _ui_thread_lock:
        # 复用已有面板（如果还在）
        if (_active_panel is not None
                and _active_panel.panel is not None
                and _active_panel.panel.winfo_exists()):
            try:
                _active_panel.panel.deiconify()
                _active_panel.panel.lift()
                return _active_panel
            except Exception:
                _active_panel = None

        # 创建面板实例（不在 UI 线程里也可以，只要 UI 操作通过 after 调度）
        panel = RecordingPanel(worker, meta)
        _active_panel = panel

    # 把 UI 创建调度到 UI 线程
    root.after(0, panel._real_show)
    return panel


# ============================================================
# 面板实现
# ============================================================
class RecordingPanel:

    ACTION_LABELS = {
        "navigate": "跳转", "click": "点击", "type": "输入",
        "coordinate_click": "坐标点击", "form_submit": "表单提交",
        "scroll": "滚动",
    }
    ACTION_COLORS = {
        "navigate": "#e8f4fd", "click": "#e8f5e9",
        "type": "#fff3e0", "coordinate_click": "#fce4ec",
        "form_submit": "#f3e5f5", "scroll": "#e0f7fa",
    }

    def __init__(self, worker, meta: dict):
        self.worker = worker
        self.meta = meta or {}
        self.panel: "tk.Toplevel | None" = None
        self._list_frame: "tk.Frame | None" = None
        self._canvas: "tk.Canvas | None" = None
        self._count_var: "tk.StringVar | None" = None
        self._status_var: "tk.StringVar | None" = None
        self._pause_btn: "tk.Button | None" = None
        self._closed = False
        self._paused = False
        # 增量渲染：记录已渲染的行数，避免全量销毁重建导致闪动
        self._rendered_count = 0

    # —— UI 线程里执行的方法 ——
    def _real_show(self):
        """在 UI 线程里创建 Toplevel 窗口。"""
        if self._closed or _ui_root is None:
            return
        try:
            self.panel = tk.Toplevel(_ui_root)
        except Exception:
            return
        name = self.meta.get("name", "未命名模板")
        self.panel.title(f"🎬 操作模板录制 — {name}")
        self.panel.geometry("700x560")
        self.panel.minsize(520, 420)
        self.panel.configure(bg="#f5f6f8")
        self.panel.protocol("WM_DELETE_WINDOW", self._on_user_close)

        self._build_content()
        self._tick()

    def _build_content(self):
        p = self.panel
        assert p is not None
        name = self.meta.get("name", "")
        start_url = self.meta.get("start_url", "")

        # 顶部状态
        top = tk.Frame(p, bg="#f5f6f8", height=60)
        top.pack(fill="x", padx=12, pady=(10, 4))
        self._status_var = tk.StringVar(value="● 正在录制")
        self._status_label = tk.Label(top, textvariable=self._status_var,
                                       fg="#e74c3c", bg="#f5f6f8",
                                       font=("Segoe UI", 11, "bold"))
        self._status_label.pack(side="left")
        mf = tk.Frame(top, bg="#f5f6f8")
        mf.pack(side="left", padx=14)
        tk.Label(mf, text=f"名称: {name}", bg="#f5f6f8", fg="#333",
                 font=("Segoe UI", 9)).pack(anchor="w")
        tk.Label(mf, text=f"起始: {start_url[:70]}", bg="#f5f6f8", fg="#666",
                 font=("Segoe UI", 8)).pack(anchor="w")
        self._count_var = tk.StringVar(value="0 步")
        tk.Label(top, textvariable=self._count_var, bg="#f5f6f8", fg="#e67e22",
                 font=("Segoe UI", 12, "bold")).pack(side="right")

        # 步骤列表（带滚动条）
        body = tk.Frame(p, bg="#f5f6f8")
        body.pack(fill="both", expand=True, padx=12, pady=6)
        cv = tk.Canvas(body, bg="#f5f6f8", highlightthickness=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=cv.yview)
        self._list_frame = tk.Frame(cv, bg="#f5f6f8")
        self._list_frame.bind("<Configure>",
                              lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=self._list_frame, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._canvas = cv

        # 底部按钮
        bottom = tk.Frame(p, bg="#f5f6f8")
        bottom.pack(fill="x", padx=12, pady=(6, 10))
        self._pause_btn = tk.Button(bottom, text="⏸ 暂停录制", bg="#e67e22", fg="white",
                                     relief="flat", font=("Segoe UI", 10), padx=10, pady=6,
                                     command=self._on_pause)
        self._pause_btn.pack(side="right")
        tk.Button(bottom, text="✕ 取消录制", bg="#e74c3c", fg="white", relief="flat",
                  font=("Segoe UI", 10), padx=10, pady=6,
                  command=self._on_cancel).pack(side="right", padx=(0, 8))
        tk.Button(bottom, text="✓ 完成录制并保存", bg="#2e7d32", fg="white", relief="flat",
                  font=("Segoe UI", 10), padx=14, pady=6,
                  command=self._on_complete).pack(side="right", padx=(0, 8))

        # 初始空提示
        self._show_empty()

    def _show_empty(self):
        if self._list_frame is None:
            return
        for w in self._list_frame.winfo_children():
            w.destroy()
        tk.Label(self._list_frame,
                 text="录制已开始，请在浏览器里正常操作\n"
                      "(点击 / 输入 / 滚动 / 跳转)...",
                 bg="#f5f6f8", fg="#aaa",
                 font=("Segoe UI", 10), pady=40).pack()

    # —— 定时刷新 ——
    def _tick(self):
        if self._closed or self.panel is None or not self.panel.winfo_exists():
            return
        try:
            status = self.worker._record_status()
            if not status.get("recording"):
                self._safe_destroy()
                return
            # 更新暂停状态显示
            self._paused = bool(status.get("paused", False))
            self._update_pause_ui()
            self._render_steps(status.get("steps", []))
        except Exception as e:
            print(f"[recording_panel] tick/render error: {e!r}")
        assert _ui_root is not None
        _ui_root.after(600, self._tick)

    def _update_pause_ui(self):
        """根据暂停状态更新顶部标签和暂停按钮。"""
        if self._status_var is None or self._pause_btn is None:
            return
        if self._paused:
            self._status_var.set("⏸ 已暂停")
            if hasattr(self, '_status_label') and self._status_label is not None:
                self._status_label.configure(fg="#e67e22")
            self._pause_btn.configure(text="▶ 继续录制", bg="#27ae60")
        else:
            self._status_var.set("● 正在录制")
            if hasattr(self, '_status_label') and self._status_label is not None:
                self._status_label.configure(fg="#e74c3c")
            self._pause_btn.configure(text="⏸ 暂停录制", bg="#e67e22")

    def _render_steps(self, steps: list[dict]):
        if self._list_frame is None:
            return
        total = len(steps)
        if self._count_var is not None:
            self._count_var.set(f"{total} 步")

        # 增量渲染策略：
        # 1. 首次（_rendered_count == 0）或步骤数 < 已渲染数（删除/重排后）→ 全量重建
        # 2. 否则只渲染新增的步骤（_rendered_count 到 total-1）
        # 这样避免每 600ms 全量销毁重建导致的闪动
        need_full_rebuild = (
            self._rendered_count == 0
            or total < self._rendered_count
            or total == 0
        )

        if need_full_rebuild:
            for w in self._list_frame.winfo_children():
                w.destroy()
            if total == 0:
                self._show_empty()
                self._rendered_count = 0
                return
            # 全量渲染所有行
            for i, step in enumerate(steps):
                self._make_row(i, step)
            self._rendered_count = total
        else:
            # 增量：只添加新步骤
            if self._rendered_count == 0:
                # 先删掉空提示
                for w in self._list_frame.winfo_children():
                    w.destroy()
            for i in range(self._rendered_count, total):
                self._make_row(i, steps[i])
            self._rendered_count = total

        # 滚到底（只在新增步骤时滚，避免用户手动滚上去又被拉回）
        if total > 0 and self._canvas is not None:
            try:
                self._canvas.update_idletasks()
                self._canvas.yview_moveto(1.0)
            except Exception:
                pass

    def _make_row(self, idx: int, step: dict):
        assert self._list_frame is not None
        action = step.get("action", "")
        bg = self.ACTION_COLORS.get(action, "#ffffff")
        row = tk.Frame(self._list_frame, bg=bg, padx=10, pady=6,
                       highlightbackground="#e0e0e0", highlightthickness=1)
        row.pack(fill="x", pady=3, padx=2)

        header = tk.Frame(row, bg=bg)
        header.pack(fill="x")
        tk.Label(header, text=f"#{idx + 1}", bg=bg, fg="#999",
                 font=("Consolas", 10)).pack(side="left")
        tk.Label(header, text=self.ACTION_LABELS.get(action, action),
                 bg=bg, fg="#333", font=("Segoe UI", 9, "bold"),
                 padx=6).pack(side="left", padx=(4, 0))
        summary = self._summary(step)
        tk.Label(header, text=summary, bg=bg, fg="#555",
                 font=("Consolas", 9), anchor="w", wraplength=280,
                 justify="left").pack(side="left", fill="x", expand=True, padx=4)

        btns = tk.Frame(header, bg=bg)
        btns.pack(side="right")
        tk.Button(btns, text="↑", width=2, relief="flat",
                  command=lambda i=idx: self._move_up(i),
                  state=("normal" if idx > 0 else "disabled")
                  ).pack(side="left", padx=1)
        tk.Button(btns, text="↓", width=2, relief="flat",
                  command=lambda i=idx: self._move_down(i),
                  state=("normal" if idx < 999 else "disabled")
                  ).pack(side="left", padx=1)
        tk.Button(btns, text="🗑", width=2, relief="flat", fg="#c0392b",
                  command=lambda i=idx: self._delete(i)
                  ).pack(side="left", padx=(4, 0))

        # 第二行：备注 + 等待时间
        edit_row = tk.Frame(row, bg=bg)
        edit_row.pack(fill="x", pady=(4, 0))

        note_var = tk.StringVar(value=step.get("annotation", ""))
        note_entry = tk.Entry(edit_row, textvariable=note_var,
                              bg="#fffbe6", relief="flat",
                              font=("Segoe UI", 9), fg="#555")
        note_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        note_entry.bind("<FocusOut>",
                        lambda e, i=idx, v=note_var: self._annotate(i, v.get()))
        note_entry.bind("<Return>",
                        lambda e, i=idx, v=note_var: (self._annotate(i, v.get()),
                                                       self.panel.focus_set() if self.panel else None))

        # 等待时间输入框（毫秒）
        wait_label = tk.Label(edit_row, text="等待(ms):", bg=bg, fg="#888",
                              font=("Segoe UI", 8))
        wait_label.pack(side="left")
        wait_var = tk.StringVar(value=str(step.get("wait_after", 0) or 0))
        wait_entry = tk.Entry(edit_row, textvariable=wait_var, width=6,
                              relief="flat", bg="#e8f5e9", fg="#2e7d32",
                              font=("Consolas", 9), justify="right")
        wait_entry.pack(side="left", padx=(2, 0))
        wait_entry.bind("<FocusOut>",
                        lambda e, i=idx, v=wait_var: self._set_wait(i, v.get()))
        wait_entry.bind("<Return>",
                        lambda e, i=idx, v=wait_var: (self._set_wait(i, v.get()),
                                                       self.panel.focus_set() if self.panel else None))

    @staticmethod
    def _summary(step: dict) -> str:
        action = step.get("action", "")
        parts: list[str] = []
        if action == "navigate":
            parts.append(str(step.get("url") or step.get("url_after") or ""))
        elif action == "scroll":
            try:
                dy = int(step.get("delta_y") or 0)
            except (TypeError, ValueError):
                dy = 0
            arrow = "↓" if dy > 0 else "↑"
            sel = str(step.get("selector") or "")
            parts.append(f"{arrow} {abs(dy)}px" + (f"  容器: {sel[:40]}" if sel else "  (整页)"))
        else:
            sel = str(step.get("stable_selector") or step.get("selector") or "")
            if sel:
                parts.append(sel[:70])
            text = str(step.get("text") or "")
            if text:
                parts.append(f'→ "{text[:50]}{"..." if len(text) > 50 else ""}"')
        return " ".join(parts)

    # —— 按钮回调 ——
    def _move_up(self, idx: int):
        if idx <= 0:
            return
        try:
            status = self.worker._record_status()
            steps = status.get("steps", [])
            if idx >= len(steps):
                return
            order = list(range(len(steps)))
            order[idx - 1], order[idx] = order[idx], order[idx - 1]
            self.worker._record_step_reorder({"new_order": order})
            self._rendered_count = 0  # 重排后需全量重建
        except Exception as e:
            self._err(f"重排失败: {e}")

    def _move_down(self, idx: int):
        try:
            status = self.worker._record_status()
            steps = status.get("steps", [])
            if idx >= len(steps) - 1:
                return
            order = list(range(len(steps)))
            order[idx], order[idx + 1] = order[idx + 1], order[idx]
            self.worker._record_step_reorder({"new_order": order})
            self._rendered_count = 0  # 重排后需全量重建
        except Exception as e:
            self._err(f"重排失败: {e}")

    def _delete(self, idx: int):
        try:
            status = self.worker._record_status()
            steps = status.get("steps", [])
            if idx >= len(steps):
                return
            label = self.ACTION_LABELS.get(steps[idx].get("action", ""), "")
            if not messagebox.askyesno("删除步骤", f"确定删除第 {idx + 1} 步「{label}」？",
                                       parent=self.panel):
                return
            self.worker._record_step_delete({"step_index": idx})
            self._rendered_count = 0  # 删除后需全量重建
        except Exception as e:
            self._err(f"删除失败: {e}")

    def _annotate(self, idx: int, text: str):
        self.worker._record_step_annotate({"step_index": idx, "annotation": text})

    def _set_wait(self, idx: int, value: str):
        try:
            ms = int(value)
            if ms < 0:
                ms = 0
        except (ValueError, TypeError):
            return  # 无效值，忽略
        self.worker._record_step_set_wait({"step_index": idx, "wait_after": ms})

    def _on_pause(self):
        """暂停/继续录制。"""
        try:
            if self._paused:
                self.worker._record_resume()
            else:
                self.worker._record_pause()
        except Exception as e:
            self._err(f"暂停/继续失败: {e}")

    def _on_complete(self):
        try:
            status = self.worker._record_status()
            steps = status.get("steps", [])
            if not steps:
                messagebox.showwarning("无法完成", "当前没有录制到任何步骤。", parent=self.panel)
                return
            name = simpledialog.askstring("完成录制", "模板名称:",
                                          initialvalue=self.meta.get("name", ""),
                                          parent=self.panel)
            if name is None:
                return
            desc = simpledialog.askstring("完成录制", "模板描述（可选）:",
                                           initialvalue=self.meta.get("description", ""),
                                           parent=self.panel) or ""
            # 走 worker 队列而不是直接调方法：_record_complete 里会执行
            # Playwright teardown（evaluate 等），必须在持有 Playwright 的
            # worker 线程里跑；从 Tk UI 线程直调会被 try/except 静默吞掉，
            # 导致浏览器内悬浮条/监听器清理不生效。
            res = self.worker.submit({"action": "record_complete",
                                      "template_name": name.strip(),
                                      "record_description": desc.strip()},
                                     timeout=30)
            if res.get("ok"):
                messagebox.showinfo("保存成功",
                                    f"模板已保存！\n名称: {res['template'].get('name')}\n"
                                    f"步骤数: {res['steps_count']}\n路径: {res['path']}",
                                    parent=self.panel)
                self._safe_destroy()
            else:
                self._err(f"保存失败: {res.get('error')}")
        except Exception as e:
            self._err(f"完成失败: {e}")

    def _on_cancel(self):
        if not messagebox.askyesno("取消录制", "确定取消录制？所有已录步骤将被丢弃。", parent=self.panel):
            return
        try:
            self.worker.submit({"action": "record_cancel"}, timeout=15)
        except Exception as e:
            print(f"[recording_panel] cancel error: {e!r}")
        self._safe_destroy()

    def _on_user_close(self):
        if not messagebox.askyesno("关闭面板",
                                   "关闭面板将视为取消录制，已录步骤不会保存。确定关闭吗？",
                                   parent=self.panel):
            return
        try:
            self.worker.submit({"action": "record_cancel"}, timeout=15)
        except Exception as e:
            print(f"[recording_panel] cancel error: {e!r}")
        self._safe_destroy()

    def _safe_destroy(self):
        self._closed = True
        global _active_panel
        if _active_panel is self:
            _active_panel = None
        if self.panel is not None:
            try:
                self.panel.destroy()
            except Exception:
                pass
            self.panel = None

    def _err(self, msg: str):
        print(f"[recording_panel] {msg}")
        if self.panel is not None:
            messagebox.showerror("错误", msg, parent=self.panel)
