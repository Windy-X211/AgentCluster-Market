"""overlay.py —— 录制浮层模块

使用独立子进程运行 tkinter 浮层窗口，避免子线程中 tkinter 的 Tcl 线程安全问题。
浮层不进入视频画面（通过 mss 采集时排除浮层区域）。
按钮事件通过 stdout 管道传回主进程。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable


_OVERLAY_SCRIPT = '''\
import sys, tkinter as tk, json

def send_cmd(cmd):
    print(cmd, flush=True)

def main():
    info = json.loads(sys.argv[1])
    sw, sh = info["screen_width"], info["screen_height"]
    audio = info.get("audio_source", "none")

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.88)

    w, h = 420, 36
    x = (sw - w) // 2
    y = 10
    root.geometry(f"{w}x{h}+{x}+{y}")

    elapsed = [0]
    state = ["recording"]

    audio_text = ""
    if audio == "system":
        audio_text = " [系统]"
    elif audio == "microphone":
        audio_text = " [麦克风]"
    elif audio == "both":
        audio_text = " [系统+麦克风]"

    frame = tk.Frame(root, bg="#1a1a1a")
    frame.pack(fill="both", expand=True)

    label = tk.Label(frame, text="", bg="#1a1a1a", fg="#ffffff",
                     font=("Microsoft YaHei", 9, "bold"), padx=8)
    label.pack(side="left", padx=(4, 6))

    btn_pause = tk.Button(frame, text="暂停", width=4,
                          bg="#333333", fg="#ffffff", relief="flat",
                          font=("Microsoft YaHei", 8),
                          activebackground="#555555", activeforeground="#ffffff")
    btn_pause.pack(side="left", padx=2)

    btn_stop = tk.Button(frame, text="结束", width=4,
                         bg="#c0392b", fg="#ffffff", relief="flat",
                         font=("Microsoft YaHei", 8),
                         activebackground="#e74c3c", activeforeground="#ffffff")
    btn_stop.pack(side="left", padx=2)

    btn_discard = tk.Button(frame, text="丢弃", width=4,
                            bg="#7f8c8d", fg="#ffffff", relief="flat",
                            font=("Microsoft YaHei", 8),
                            activebackground="#95a5a6", activeforeground="#ffffff")
    btn_discard.pack(side="left", padx=2)

    def on_pause():
        if state[0] == "recording":
            send_cmd("pause")
            state[0] = "paused"
            btn_pause.config(text="恢复")
        else:
            send_cmd("resume")
            state[0] = "recording"
            btn_pause.config(text="暂停")
        update_display()

    def on_stop():
        send_cmd("stop")
        root.destroy()

    def on_discard():
        send_cmd("discard")
        root.destroy()

    btn_pause.config(command=on_pause)
    btn_stop.config(command=on_stop)
    btn_discard.config(command=on_discard)

    def update_display():
        hours = int(elapsed[0] // 3600)
        minutes = int((elapsed[0] % 3600) // 60)
        seconds = int(elapsed[0] % 60)
        time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        if state[0] == "recording":
            dot = "●"
            status = "录制中"
        else:
            dot = "○"
            status = "已暂停"

        label.config(text=f"{dot} REC {status} {time_str}{audio_text}")

    def tick():
        if state[0] == "recording":
            elapsed[0] += 1
        update_display()
        root.after(1000, tick)

    update_display()
    root.after(1000, tick)
    root.mainloop()

main()
'''


class RecordingOverlay:
    """录制浮层

    通过子进程运行 tkinter 窗口，显示录制状态与控制按钮。
    按钮事件通过 stdout 管道传回主进程，通过回调函数执行对应操作。
    """

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._state = "recording"
        self._listener: threading.Thread | None = None
        self._callback: Callable[[str], None] | None = None

    def set_callback(self, callback: Callable[[str], None]):
        """设置按钮事件回调

        Args:
            callback: 接收命令字符串 ("pause"/"resume"/"stop"/"discard") 的回调函数
        """
        self._callback = callback

    def start(self, screen_width: int, screen_height: int,
              audio_source: str = "none") -> bool:
        """启动浮层子进程"""
        import json as _json

        script_path = Path(__file__).resolve().parent / "_overlay_window.py"
        script_path.write_text(_OVERLAY_SCRIPT, encoding="utf-8")

        info = _json.dumps({
            "screen_width": screen_width,
            "screen_height": screen_height,
            "audio_source": audio_source,
        })

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            self._process = subprocess.Popen(
                [sys.executable, str(script_path), info],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,
                text=True,
            )

            self._listener = threading.Thread(
                target=self._listen_commands,
                name="overlay-listener",
                daemon=True,
            )
            self._listener.start()
            return True
        except Exception:
            return False

    def _listen_commands(self):
        """监听子进程 stdout，接收按钮命令"""
        if self._process is None or self._process.stdout is None:
            return
        try:
            for line in self._process.stdout:
                cmd = line.strip()
                if cmd and self._callback:
                    self._callback(cmd)
        except Exception:
            pass

    def update_state(self, state: str, elapsed: float | None = None):
        """更新录制状态"""
        self._state = state

    def stop(self):
        """停止浮层子进程"""
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            finally:
                self._process = None

        try:
            script_path = Path(__file__).resolve().parent / "_overlay_window.py"
            if script_path.exists():
                script_path.unlink()
        except Exception:
            pass