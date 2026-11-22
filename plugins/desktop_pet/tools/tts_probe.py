# coding=utf-8
"""P0 测试脚本：生成配音 + 字幕，并给出延迟/显存报告。

用法（宿主 python，带 PyQt5）：
  python tools\\tts_probe.py --backend local --out <目录> --frames 3
  python tools\\tts_probe.py --backend local --preview
  python tools\\tts_probe.py --backend mimo  --text "你好" --out <目录>   # 需先配 MiMo key

产物：
  voice_dry.wav   纯配音（48k 单声道）
  voice_mix.wav   配音 + BGM（带 ducking）
  voice.srt       段级字幕
  marks.json      段 + 逐字时间轴（插件后续直接复用）
  report.json     首包/RTF/显存/同步误差
  frame_*.png     字幕排版快照（--frames）
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from tts.config import DEFAULT_MODEL_DIR, VENV_PY, load_config  # noqa: E402
from tts.marks import build_segments, to_srt  # noqa: E402
from tts.resample import resample_int16  # noqa: E402
from tts.segmenter import split_segments  # noqa: E402

DEFAULT_TEXT = (
    "今天天气不错，我们一起出去玩吧。"
    "不过要先把作业写完，不然又要被说了。"
    "你等我一下，我换件外套就来。"
)

OUT_SR = 48000


# ---------------------------------------------------------------- 工具
def log(msg: str):
    print(msg, flush=True)


class Conn:
    """带缓冲的 socket 读取器：行头与 PCM 会粘在同一个 recv 里，必须保留余量。"""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""

    def line(self) -> bytes:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self.buf += chunk
        idx = self.buf.index(b"\n")
        head, self.buf = self.buf[:idx], self.buf[idx + 1:]
        return head

    def exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("PCM 读取中断（需要 %d，已读 %d）" % (n, n - len(self.buf)))
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data


class Daemon:
    """启动并连接 tts_daemon（venv 里的 python）。"""

    def __init__(self, venv_py: str, model_dir: str, extra: List[str], log_path: Path):
        self.venv_py = venv_py
        self.model_dir = model_dir
        self.extra = extra
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._stderr_lines: List[str] = []

    def start(self, timeout: float = 40.0) -> int:
        if not os.path.isfile(self.venv_py):
            raise RuntimeError("TTS venv 不存在：%s（先跑 tools\\tts_setup.py --install）" % self.venv_py)
        cmd = [
            self.venv_py, str(ROOT / "tools" / "tts_daemon.py"),
            "--model-dir", self.model_dir,
        ] + self.extra
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError("daemon 退出：%s" % "\n".join(self._stderr_lines[-8:]))
                continue
            log("[daemon] " + line.strip())
            if line.startswith("READY "):
                info = json.loads(line[6:])
                self.port = int(info["port"])
                return self.port
            if line.startswith("ERR "):
                raise RuntimeError(json.loads(line[4:]).get("code", "DAEMON_ERR"))
        raise RuntimeError("daemon 启动超时（%.0fs）" % timeout)

    def _pump_stdout(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._lines.put(line)

    def _pump_stderr(self):
        assert self.proc and self.proc.stderr
        with open(self.log_path, "a", encoding="utf-8") as f:
            for line in self.proc.stderr:
                self._stderr_lines.append(line.rstrip())
                f.write(line)
                f.flush()

    def stop(self):
        if not self.proc:
            return
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as c:
                c.sendall(b'{"op":"shutdown"}\n')
                try:
                    c.recv(1024)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.proc.wait(timeout=4)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------- 读取帧
class Frames:
    """把 daemon 的帧读出来，按顺序交给消费者。"""

    def __init__(self):
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.ready: Optional[Dict[str, Any]] = None
        self.end: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, Any]] = None
        self.ttfa_ms: Optional[float] = None
        self.first_pcm_ms: Optional[float] = None
        self._t_req = 0.0

    def run(self, conn: Conn, payload: Dict[str, Any]):
        self._t_req = time.monotonic()
        try:
            conn.sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            while True:
                line = conn.line()
                msg = json.loads(line.decode("utf-8"))
                ev = msg.get("ev")
                if ev == "ready":
                    self.ready = msg
                    self.q.put({"ev": "ready", "data": msg})
                elif ev == "seg":
                    n = int(msg.get("samples", 0)) * 2
                    pcm = np.frombuffer(conn.exact(n), dtype=np.int16)
                    if self.ttfa_ms is None:
                        self.ttfa_ms = (time.monotonic() - self._t_req) * 1000.0
                    self.q.put({"ev": "seg", "meta": msg, "pcm": pcm})
                elif ev == "end":
                    self.end = msg
                    self.q.put({"ev": "end", "data": msg})
                    return
                elif ev == "error":
                    self.error = msg
                    self.q.put({"ev": "error", "data": msg})
                    return
        except Exception as e:
            self.error = {"code": type(e).__name__, "message": str(e)}
            self.q.put({"ev": "error", "data": self.error})


# ---------------------------------------------------------------- 参数
def parse_args():
    ap = argparse.ArgumentParser(description="桌宠 TTS P0 试听脚本")
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--text-file", default="")
    ap.add_argument("--backend", choices=["local", "edge", "mimo"], default="local")
    ap.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    ap.add_argument("--venv", default=str(VENV_PY))
    ap.add_argument("--voice", default="Serena")
    ap.add_argument("--language", default="Chinese")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--attn", default="sdpa")
    ap.add_argument("--max-new-tokens", type=int, default=1024)
    ap.add_argument("--non-streaming", action="store_true", help="关闭 qwen-tts 的流式文本模拟")
    ap.add_argument("--out", default="")
    ap.add_argument("--bgm", default="pad", help="'pad'=内置垫乐，'none'=无，或文件路径")
    ap.add_argument("--bgm-volume", type=float, default=0.35)
    ap.add_argument("--bgm-duck", type=float, default=0.25)
    ap.add_argument("--bgm-fade", type=int, default=180)
    ap.add_argument("--prebuffer-ms", type=int, default=600)
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--frames", type=int, default=0, help="生成 N 张字幕快照 PNG")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--edge-rate", default="+0%")
    ap.add_argument("--speed", type=float, default=1.0, help="mimo 专用：1.0=正常，折成指令近似控制")
    ap.add_argument("--api-key", default="", help="mimo 专用：临时携带的 key（不写盘，优先于配置/环境变量）")
    ap.add_argument("--sing", action="store_true", help="mimo 专用：唱歌模式（整曲一次请求，不做分句）")
    ap.add_argument("--list-speakers", action="store_true")
    return ap.parse_args()


# ---------------------------------------------------------------- 主流程
def main() -> int:
    args = parse_args()

    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    text = " ".join(text.split())
    if not text:
        log("文本为空")
        return 2

    cfg = load_config()
    if args.backend == "local" and args.model_dir == DEFAULT_MODEL_DIR:
        args.model_dir = str((cfg.get("local") or {}).get("model_dir") or DEFAULT_MODEL_DIR)

    out_dir = Path(args.out) if args.out else (ROOT / "_tts" / "probe" / datetime.now().strftime("%Y%m%d_%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "daemon.log"
    log("输出目录: %s" % out_dir)

    # 唱歌：整曲一次请求，不切句（切了会变成一段段各自起唱的碎片）
    segments_text = [text] if getattr(args, "sing", False) else split_segments(text)
    log("分段 %d 句：" % len(segments_text))
    for i, s in enumerate(segments_text):
        log("  [%d] %s" % (i, s))

    # BGM
    bgm = None
    if args.bgm and args.bgm.lower() != "none":
        from tts.bgm import load_bgm, synth_soft_pad
        if args.bgm.lower() == "pad":
            bgm = synth_soft_pad(OUT_SR, 8.0)
            log("BGM: 内置 8s 循环垫乐")
        else:
            bgm = load_bgm(args.bgm, OUT_SR)
            log("BGM: %s (%.2fs)" % (args.bgm, bgm.size / OUT_SR))

    # ---------- 合成 ----------
    collected: List[Dict[str, Any]] = []
    t_start = time.monotonic()
    ready_info: Dict[str, Any] = {}

    frames = Frames()
    daemon: Optional[Daemon] = None

    if args.backend == "local":
        daemon = Daemon(args.venv, args.model_dir, [
            "--dtype", args.dtype, "--attn", args.attn,
            "--max-new-tokens", str(args.max_new_tokens),
            "--parent-pid", str(os.getpid()),
        ], log_path)
        try:
            port = daemon.start()
        except Exception as e:
            log("启动失败: %s" % e)
            return 2
        import atexit
        atexit.register(daemon.stop)   # 异常/中断也必须回收，否则孤儿进程占显存
        conn = Conn(socket.create_connection(("127.0.0.1", port), timeout=args.timeout))
        conn.sock.settimeout(args.timeout)
        payload = {
            "op": "speak", "text": text,
            "speaker": args.voice, "language": args.language,
            "max_new_tokens": args.max_new_tokens,
            "non_streaming_mode": bool(args.non_streaming),
        }
        reader = threading.Thread(target=frames.run, args=(conn, payload), daemon=True)
        reader.start()
    else:
        # 在线：逐段合成，线程里生成，主线程消费（便于边合成边播）
        worker = _mimo_worker if args.backend == "mimo" else _edge_worker
        reader = threading.Thread(target=worker, args=(args, frames, text, segments_text), daemon=True)
        reader.start()

    def drain_blocking(until_end: bool = True) -> bool:
        """同步消费帧（非播放模式）。"""
        while True:
            try:
                item = frames.q.get(timeout=0.2)
            except queue.Empty:
                if frames.error:
                    return False
                if reader is not None and not reader.is_alive() and frames.q.empty():
                    return frames.end is not None
                continue
            if item["ev"] == "ready":
                ready_info.update(item["data"])
            elif item["ev"] == "seg":
                collected.append(item)
            elif item["ev"] == "end":
                ready_info.update(item["data"])
                return True
            elif item["ev"] == "error":
                return False

    play_mode = bool(args.play or args.preview)
    if args.preview and not args.play:
        args.play = True

    if not play_mode:
        ok = drain_blocking()
    else:
        ok = _run_qt(args, frames, reader, collected, ready_info, bgm, out_dir)

    if frames.error:
        log("合成失败: %s" % json.dumps(frames.error, ensure_ascii=False))
        if daemon:
            daemon.stop()
        return 3
    if not collected:
        log("没有拿到任何音频")
        if daemon:
            daemon.stop()
        return 3

    # ---------- 后处理 ----------
    out = _write_outputs(args, collected, ready_info, frames, bgm, out_dir, segments_text, text,
                         time.monotonic() - t_start)

    if args.frames > 0:
        _write_frames(args, out["segments"], out_dir)

    if daemon:
        # 显存峰值要在卸载前取
        try:
            with socket.create_connection(("127.0.0.1", daemon.port), timeout=5) as c:
                c.sendall(b'{"op":"status"}\n')
                st = json.loads(Conn(c).line().decode("utf-8"))
                out["report"]["vram_after"] = st.get("engine", {})
        except Exception:
            pass
        daemon.stop()

    _finalize_report(out_dir, out, args, text, segments_text)

    log("")
    log("完成：%s" % out_dir)
    log("  首包 %.0f ms · 音频 %.2f s · RTF %.2f · 峰值显存 %s MB" % (
        out["report"].get("ttfa_ms") or 0,
        out["report"].get("audio_ms", 0) / 1000.0,
        out["report"].get("rtf_overall", 0),
        out["report"].get("vram_peak_mb", 0),
    ))
    return 0


# ---------------------------------------------------------------- MiMo 生成
def _mimo_worker(args, frames: Frames, text: str, segments: List[str]):
    from tts.engine_mimo import MimoTTSError, load_settings, resolve_voice, synth_segment
    from tts.marks import char_marks

    st = load_settings(load_config())
    # --voice 的默认值是本地的 Serena，对 MiMo 没意义：没显式指定就用配置里的 MiMo 音色
    want = str(args.voice or "")
    voice = resolve_voice(want if want not in ("", "Serena") else st.get("voice"),
                          str(st.get("voice") or ""))

    frames._t_req = time.monotonic()
    try:
        frames.q.put({"ev": "ready", "data": {"backend": "mimo", "model": st.get("model"),
                                              "voice": voice, "segments": len(segments)}})
        cursor = 0.0
        for i, seg in enumerate(segments):
            pcm, sr, _words, ttfa = synth_segment(seg, voice=voice, speed=args.speed,
                                                  sr=OUT_SR, settings=st,
                                                  api_key=getattr(args, "api_key", ""),
                                                  sing=bool(getattr(args, "sing", False)))
            dur = len(pcm) * 1000.0 / sr
            if frames.ttfa_ms is None:
                frames.ttfa_ms = (time.monotonic() - frames._t_req) * 1000.0
            meta = {
                "ev": "seg", "i": i, "text": seg, "sr": sr, "samples": len(pcm),
                "start_ms": int(round(cursor)), "dur_ms": int(round(dur)),
                # MiMo 不返回逐字时间戳，字幕按字数均分
                "aligned": "estimated",
                "chars": char_marks(seg, int(round(dur))), "synth_ms": round(ttfa, 1),
                "rtf": round(ttfa / max(1.0, dur), 3),
            }
            frames.q.put({"ev": "seg", "meta": meta, "pcm": pcm})
            cursor += dur
        frames.end = {"total_ms": int(round(cursor))}
        frames.q.put({"ev": "end", "data": frames.end})
    except MimoTTSError as e:
        frames.error = {"code": "MIMO_ERROR", "message": str(e)}
        frames.q.put({"ev": "error", "data": frames.error})
    except Exception as e:
        frames.error = {"code": type(e).__name__, "message": str(e)}
        frames.q.put({"ev": "error", "data": frames.error})


# ---------------------------------------------------------------- Edge 生成
def _edge_worker(args, frames: Frames, text: str, segments: List[str]):
    from tts.engine_edge import EdgeTTSError, map_words_to_chars, synth_segment

    frames._t_req = time.monotonic()
    try:
        frames.q.put({"ev": "ready", "data": {"backend": "edge", "voice": args.voice, "segments": len(segments)}})
        cursor = 0.0
        for i, seg in enumerate(segments):
            pcm, sr, words, ttfa = synth_segment(seg, args.voice, rate=args.edge_rate, sr=OUT_SR, timeout=30)
            dur = len(pcm) * 1000.0 / sr
            if frames.ttfa_ms is None:
                frames.ttfa_ms = (time.monotonic() - frames._t_req) * 1000.0
            chars = map_words_to_chars(seg, words or [], int(round(dur)))
            meta = {
                "ev": "seg", "i": i, "text": seg, "sr": sr, "samples": len(pcm),
                "start_ms": int(round(cursor)), "dur_ms": int(round(dur)),
                "aligned": "native" if words else "estimated",
                "chars": chars, "synth_ms": round(ttfa, 1),
                "rtf": round(ttfa / max(1.0, dur), 3),
            }
            frames.q.put({"ev": "seg", "meta": meta, "pcm": pcm})
            cursor += dur
        frames.end = {"total_ms": int(round(cursor))}
        frames.q.put({"ev": "end", "data": frames.end})
    except EdgeTTSError as e:
        frames.error = {"code": "EDGE_ERROR", "message": str(e)}
        frames.q.put({"ev": "error", "data": frames.error})
    except Exception as e:
        frames.error = {"code": type(e).__name__, "message": str(e)}
        frames.q.put({"ev": "error", "data": frames.error})


# ---------------------------------------------------------------- Qt 播放
def _run_qt(args, frames: Frames, reader, collected, ready_info, bgm, out_dir) -> bool:
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication

    # QtMultimedia 的 Windows 音频后端依赖 QGuiApplication，QCoreApplication 会崩
    app = QApplication.instance() or QApplication(sys.argv[:1])

    from tts.audio_engine import AudioEngine
    engine = AudioEngine(sample_rate=OUT_SR, prebuffer_ms=args.prebuffer_ms, buffer_ms=60)
    dev = engine.open()
    log("音频设备: %s @ %dHz (format_ok=%s)" % (dev["device"], dev["sample_rate"], dev["format_ok"]))
    if bgm is not None:
        engine.set_bgm(bgm, args.bgm_volume, args.bgm_duck, "speech")

    preview = None
    state = {"done": False, "err": False}

    def consume():
        while True:
            try:
                item = frames.q.get_nowait()
            except queue.Empty:
                return
            ev = item["ev"]
            if ev == "ready":
                ready_info.update(item["data"])
            elif ev == "seg":
                collected.append(item)
                meta = dict(item["meta"])
                pcm = item["pcm"]
                if meta.get("sr") != OUT_SR:
                    pcm = resample_int16(pcm, int(meta.get("sr")), OUT_SR)
                meta["sr"] = OUT_SR
                seg = engine.enqueue(pcm, meta, OUT_SR)
                if ready_info.get("first_play_ms") is None:
                    ready_info["first_play_ms"] = seg["start_ms"]
            elif ev == "end":
                ready_info.update(item["data"])
                engine.mark_eof()

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(consume)
    timer.start()

    def on_finished():
        if state["done"]:
            return
        state["done"] = True
        log("播放结束（%.2fs）" % (engine.duration_ms() / 1000.0))
        ready_info["audio_stats"] = engine.stats()
        if preview is not None:
            QTimer.singleShot(1400, preview.close)
        QTimer.singleShot(1200, app.quit)

    engine.finished.connect(on_finished)

    if args.preview:
        from tts.preview import SubtitlePreview
        preview = SubtitlePreview(engine.segments, engine.duration_ms(), engine=engine)
        preview.show()

    # 超时保护
    limit = int(args.timeout * 1000)
    QTimer.singleShot(limit, app.quit)

    app.exec_()

    try:
        engine.close()
    except Exception:
        pass
    if not state["done"] and not frames.error:
        log("注意：播放未正常结束（可能被超时中断）")
    return not state["err"]


# ---------------------------------------------------------------- 输出
def _write_outputs(args, collected, ready_info, frames, bgm, out_dir, segments_text, text, wall_s):
    import soundfile as sf

    seg_rows = []
    voice48 = []
    cursor = 0
    boundary_err = 0

    for item in collected:
        meta = dict(item["meta"])
        pcm = item["pcm"]
        src_sr = int(meta.get("sr", 24000))
        if src_sr != OUT_SR:
            pcm = resample_int16(pcm, src_sr, OUT_SR)
        start_ms_local = int(round(cursor * 1000.0 / OUT_SR))
        boundary_err = max(boundary_err, abs(start_ms_local - int(meta.get("start_ms", 0))))
        dur_ms_local = int(round(len(pcm) * 1000.0 / OUT_SR))

        row = {
            "i": int(meta.get("i", 0)),
            "text": meta.get("text", ""),
            "start_ms": start_ms_local,
            "dur_ms": dur_ms_local,
            "end_ms": start_ms_local + dur_ms_local,
            "aligned": meta.get("aligned", "estimated"),
            "chars": meta.get("chars", []),
            "synth_ms": float(meta.get("synth_ms", 0)),
            "rtf": float(meta.get("rtf", 0)),
        }
        seg_rows.append(row)
        voice48.append(pcm)
        cursor += len(pcm)

    voice = np.concatenate(voice48) if voice48 else np.zeros(0, dtype=np.int16)
    total_ms = int(round(voice.size * 1000.0 / OUT_SR))

    dry_path = out_dir / "voice_dry.wav"
    sf.write(str(dry_path), voice, OUT_SR, subtype="PCM_16")

    mix_path = out_dir / "voice.wav"
    if bgm is not None:
        from tts.bgm import mix_voice_bgm
        vf = voice.astype(np.float32) / 32768.0
        mixed = mix_voice_bgm(vf, OUT_SR, bgm, args.bgm_volume, args.bgm_duck, args.bgm_fade)
        sf.write(str(mix_path), (mixed * 32767.0).astype(np.int16), OUT_SR, subtype="PCM_16")
    else:
        sf.write(str(mix_path), voice, OUT_SR, subtype="PCM_16")

    # 字幕
    segments = build_segments(
        [r["text"] for r in seg_rows],
        [r["dur_ms"] for r in seg_rows],
        [r["start_ms"] for r in seg_rows],
    )
    for seg, row in zip(segments, seg_rows):
        if row.get("aligned") == "native":
            seg["aligned"] = "native"
            seg["chars"] = row["chars"]

    (out_dir / "voice.srt").write_text(to_srt(segments), encoding="utf-8")
    (out_dir / "marks.json").write_text(
        json.dumps({"segments": segments, "total_ms": total_ms,
                    "sample_rate": OUT_SR, "text": text}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    synth_total = sum(r["synth_ms"] for r in seg_rows)
    report = {
        "backend": args.backend,
        "voice": args.voice,
        "text_chars": len(text),
        "segments": len(seg_rows),
        "load_ms": float(ready_info.get("load_ms", 0) or 0),
        "load_cached": bool(ready_info.get("cached", False)),
        "ttfa_ms": float(frames.ttfa_ms or 0),
        "audio_ms": total_ms,
        "wall_ms": int(wall_s * 1000),
        "rtf_overall": round(synth_total / max(1.0, total_ms), 3),
        "segment_boundary_error_ms": boundary_err,
        "vram_peak_mb": int(ready_info.get("vram_peak_mb", 0) or 0),
        "vram_free_mb": int(ready_info.get("vram_free_mb", 0) or 0),
        "bgm": bool(bgm is not None),
        "bgm_volume": args.bgm_volume,
        "bgm_duck": args.bgm_duck,
        "sample_rate": OUT_SR,
        "play_start_ms": ready_info.get("first_play_ms"),
        "audio_stats": ready_info.get("audio_stats"),
        "seg_detail": seg_rows,
    }
    return {"segments": segments, "voice": voice, "report": report, "paths": {
        "dry": str(dry_path), "mix": str(mix_path),
        "srt": str(out_dir / "voice.srt"), "marks": str(out_dir / "marks.json"),
    }}


def _finalize_report(out_dir: Path, out, args, text, segments_text):
    rep = out["report"]
    rep["run_at"] = datetime.now().isoformat(timespec="seconds")
    rep["model_dir"] = args.model_dir if args.backend == "local" else ""
    rep["prebuffer_ms"] = args.prebuffer_ms
    (out_dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_frames(args, segments, out_dir: Path):
    from PyQt5.QtGui import QGuiApplication
    from tts.subtitle import render_frame

    if QGuiApplication.instance() is None:
        _app = QGuiApplication(sys.argv[:1])  # noqa: F841  QPainter/QFont 需要 GUI app

    total = max(1, int(segments[-1]["end_ms"])) if segments else 1
    for n in range(args.frames):
        frac = (n + 1) / (args.frames + 1)
        t = int(total * frac)
        seg = next((s for s in segments if s["start_ms"] <= t < s["end_ms"]), segments[-1])
        local = t - seg["start_ms"]
        spoken = sum(1 for c in seg.get("chars", []) if c["t1"] <= local)
        img = render_frame(
            640, 220, seg["text"], spoken,
            title="TTS 字幕预览 · %d/%d 句" % (seg["i"] + 1, len(segments)),
            footer="t=%.2fs  %s" % (t / 1000.0, "逐字高亮" if spoken else "起始"),
        )
        path = out_dir / ("frame_%02d_%05dms.png" % (n + 1, t))
        img.save(str(path))
        log("字幕快照: %s" % path.name)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
