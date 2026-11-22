# coding=utf-8
"""TTSService 真机联调（不依赖桌宠窗口）：验证 daemon→播放→逐字字幕整条链路。

跑法：
  F:\\anaconda3\\python.exe tools\\tts_service_test.py --text "..." --bgm pad
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


class FakePet:
    """替代桌宠窗口：只记录字幕进度与回调，用来验证时钟对齐。"""

    def __init__(self):
        self.log = []
        self.last = None

    def sub_karaoke(self, text, spoken=0, secs=None):
        self.last = (text, int(spoken))
        self.log.append((round(time.monotonic() - T0, 3), text, int(spoken)))

    def tts_play_motion(self, name):
        self.log.append((round(time.monotonic() - T0, 3), "motion", name))
        return True

    def tts_lip_start(self, pcm, sr):
        return True

    def tts_lip_stop(self):
        return True


T0 = time.monotonic()

ap = argparse.ArgumentParser()
ap.add_argument("--text", default="今天天气不错，我们一起出去玩吧。不过要先把作业写完，不然又要被说了。")
ap.add_argument("--voice", default="Serena")
ap.add_argument("--backend", default="local")
ap.add_argument("--bgm", default="pad")
ap.add_argument("--timeout", type=float, default=180.0)
ap.add_argument("--unload", type=int, default=1)
args = ap.parse_args()

app = QApplication(sys.argv)
pet = FakePet()

from tts.service import TTSService  # noqa: E402

svc = TTSService(pet=pet)
info = svc.open()
print("[open]", info, flush=True)

res = svc.speak(args.text, voice=args.voice, backend=args.backend, bgm=args.bgm,
                subtitle=True, animation="", mode="replace", wait=False)
print("[speak]", res, flush=True)

state = {"first_clock": None, "done": False, "ticks": 0}


def poll():
    st = svc.status(detail=True)
    clock = st.get("clock_ms", -1)
    state["ticks"] += 1
    if state["first_clock"] is None and clock and clock > 0:
        state["first_clock"] = round(time.monotonic() - T0, 3)
        print("[first-sound] %.3fs clock=%.0fms ttfa=%sms segs=%s" % (
            state["first_clock"], clock, st.get("ttfa_ms"), st.get("segments")), flush=True)
    if state["ticks"] % 8 == 0:
        last = pet.last
        print("[t] clock=%7.0f ms segs=%s speaking=%s sub=%s err=%s" % (
            clock, st.get("segments"), st.get("speaking"),
            (last[1] if last else None), st.get("error")), flush=True)
    if not st.get("speaking") and state["first_clock"] is not None:
        state["done"] = True


timer = QTimer()
timer.timeout.connect(poll)
timer.start(250)

app.exec_() if False else None
# 手动跑事件循环，避免依赖 exec_ 的退出方式
deadline = time.monotonic() + args.timeout
while time.monotonic() < deadline and not state["done"]:
    app.processEvents()
    time.sleep(0.02)

st = svc.status(detail=True)
print("[final]", {k: st.get(k) for k in ("speaking", "ttfa_ms", "segments", "error", "finish_ms", "clock_ms")}, flush=True)
print("[audio]", st.get("audio"), flush=True)
print("[timeline]", st.get("segments_timeline"), flush=True)
print("[subtail]", pet.log[-6:], flush=True)

if args.unload:
    print("[unload]", svc.unload(), flush=True)
print("[done] total=%.1fs" % (time.monotonic() - T0), flush=True)
