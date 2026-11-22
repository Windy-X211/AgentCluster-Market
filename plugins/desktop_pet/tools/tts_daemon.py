# coding=utf-8
"""TTS 守护进程（在 _tts_env 里运行，只有它 import torch / qwen-tts）。

协议（本地 TCP，行头 JSON + 裸 PCM）：
  客户端 ->  {"op":"speak","text":...,"speaker":...,"language":...,...}\n
  服务端 ->  {"ev":"ready",...}\n
             {"ev":"seg","i":0,"text":...,"samples":N,"chars":[...]}\n
             <N*2 字节 int16 PCM>
             ...
             {"ev":"end","total_ms":...}\n

这样没有磁盘 IO、没有 HTTP 分块开销，首包一到就能立刻播。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在 import torch 之前设置，否则碎片化会导致 6GB 卡上接近 OOM
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True,max_split_size_mb:64")

from tts.marks import char_marks          # noqa: E402
from tts.segmenter import split_segments  # noqa: E402
from tts.segmenter_marks import split_segments_marked  # noqa: E402


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _process_alive(pid: int) -> bool:
    """Windows 下判断父进程是否还在（避免插件/脚本崩溃后留下吃显存的孤儿进程）。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    SYNCHRONIZE = 0x00100000
    h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


class Daemon:
    def __init__(self, args):
        self.args = args
        self.engine = None
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.sock: Optional[socket.socket] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def _get_engine(self):
        if self.engine is None:
            backend = str(getattr(self.args, "backend", "qwen") or "qwen").lower()
            if backend == "piper":
                # 轻量流式：ONNX CPU，63MB 权重，零显存
                from tts.engine_piper import PiperTTSEngine
                self.engine = PiperTTSEngine(
                    model_path=self.args.piper_model,
                    trim_silence=not self.args.no_trim,
                    noise_scale=float(getattr(self.args, "noise_scale", 0.10)),
                    noise_w=float(getattr(self.args, "noise_w", 0.10)),
                )
            elif backend == "sherpa":
                # 多角色：3 个模型常驻（约 545MB RAM、零显存），换音色零等待
                from tts.engine_sherpa import SherpaEnginePool
                self.engine = SherpaEnginePool(
                    trim_silence=not self.args.no_trim,
                    num_threads=int(getattr(self.args, "threads", 4) or 4),
                    warm=list(getattr(self.args, "sherpa_warm", []) or []),
                )
            else:
                from tts.engine_qwen import QwenTTSEngine  # 延迟到真正用时
                self.engine = QwenTTSEngine(
                    model_dir=self.args.model_dir,
                    dtype=self.args.dtype,
                    attn=self.args.attn,
                    device=self.args.device,
                    min_free_vram_mb=self.args.min_free_vram_mb,
                    trim_silence=not self.args.no_trim,
                )
        return self.engine

    def _send_json(self, conn, obj: Dict[str, Any]):
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        conn.sendall(line.encode("utf-8"))

    def _handle(self, conn: socket.socket):
        conn.settimeout(None)
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        req = json.loads(data.decode("utf-8"))
        op = str(req.get("op", "speak"))
        self.last_activity = time.monotonic()

        if op == "preload":
            # 预热：只加载模型（含 CUDA context / 权重搬运 / 一次极短合成），
            # 让用户的第一次 speak 不必再吃 load 的十几秒。
            t0 = time.monotonic()
            engine = self._get_engine()
            load_info = engine.load()
            if req.get("smoke", True):
                try:
                    engine.synthesize("你好。", speaker=req.get("voice") or "Serena")
                except Exception as e:
                    log("[preload] smoke 失败:", e)
            self._send_json(conn, {
                "ev": "preloaded",
                "load_ms": load_info.get("load_ms", 0.0),
                "cached": load_info.get("cached", False),
                "warm_ms": int(round((time.monotonic() - t0) * 1000)),
                "vram_mb": engine.status().get("vram_alloc_mb", 0),
            })
            return
        if op == "status":
            eng = self.engine
            self._send_json(conn, {"ev": "status", "engine": eng.status() if eng else {"loaded": False},
                                   "pid": os.getpid()})
            return
        if op == "unload":
            freed = -1
            if self.engine is not None:
                freed = self.engine.unload()
            self._send_json(conn, {"ev": "unloaded", "free_mb": freed})
            return
        if op == "shutdown":
            self._send_json(conn, {"ev": "bye"})
            self._stop.set()
            return

        text = str(req.get("text", "")).strip()
        if not text:
            self._send_json(conn, {"ev": "error", "code": "EMPTY_TEXT", "message": "text 为空"})
            return

        # 分段：先解析文本里的停顿/连读符号（~ | || |||），再按标点切。
        # 每段带 gap_after_ms —— service 会在该段 PCM 尾部补这段静音。
        segments = split_segments_marked(
            text,
            min_chars=int(req.get("min_chars", 6)),
            max_chars=int(req.get("max_chars", 24)),
            short_ms=int(req.get("pause_short_ms", 150)),
            mid_ms=int(req.get("pause_mid_ms", 300)),
            long_ms=int(req.get("pause_long_ms", 500)),
            use_marks=bool(req.get("pause_marks", True)),
        )
        if not segments:
            self._send_json(conn, {"ev": "error", "code": "EMPTY_TEXT", "message": "分段后为空"})
            return

        engine = self._get_engine()
        load_info = engine.load()
        st = engine.status()
        self._send_json(conn, {
            "ev": "ready",
            "backend": "local",
            "model": (os.path.basename(self.args.piper_model) if str(self.args.backend).lower() == "piper"
                      else (st.get("model") or os.path.basename(str(self.args.model_dir).rstrip("\\/")))),
            "load_ms": load_info.get("load_ms", 0.0),
            "cached": load_info.get("cached", False),
            "sample_rate": engine.sample_rate,
            "dtype": st.get("dtype"),
            "speakers": st.get("speakers", []),
            "vram_mb": st.get("vram_alloc_mb", 0),
            "vram_free_mb": st.get("vram_free_mb", 0),
            "segments": len(segments),
        })

        cursor_ms = 0.0
        total_samples = 0
        for i, seg in enumerate(segments):
            seg_text = str(seg["text"])
            gap_after = int(seg.get("gap_after_ms") or 0)
            t0 = time.monotonic()
            pcm, sr, synth_ms = engine.synthesize(
                text=seg_text,
                speaker=str(req.get("speaker") or "Serena"),
                language=str(req.get("language") or "Chinese"),
                max_new_tokens=int(req.get("max_new_tokens") or self.args.max_new_tokens),
                non_streaming_mode=bool(req.get("non_streaming_mode", False)),
                speed=float(req.get("speed") or 1.0),
            )
            dur_ms = len(pcm) * 1000.0 / float(sr)
            header = {
                "ev": "seg",
                "i": i,
                "text": seg_text,
                "sr": int(sr),
                "samples": int(len(pcm)),
                "start_ms": int(round(cursor_ms)),
                "dur_ms": int(round(dur_ms)),
                "aligned": "estimated",
                "chars": char_marks(seg_text, int(round(dur_ms))),
                "synth_ms": round(synth_ms, 1),
                "rtf": round(synth_ms / max(1.0, dur_ms), 3),
                "wall_ms": round((time.monotonic() - t0) * 1000.0, 1),
                "gap_after_ms": gap_after,
            }
            self._send_json(conn, header)
            conn.sendall(pcm.tobytes())
            cursor_ms += dur_ms + gap_after
            total_samples += len(pcm)
            self.last_activity = time.monotonic()

        self._send_json(conn, {
            "ev": "end",
            "total_ms": int(round(cursor_ms)),
            "total_samples": int(total_samples),
            "vram_peak_mb": engine.status().get("vram_peak_mb", 0),
        })
        self.last_activity = time.monotonic()

    # ------------------------------------------------------------------
    def _idle_loop(self):
        while not self._stop.is_set():
            time.sleep(5)
            if self.args.parent_pid and not _process_alive(self.args.parent_pid):
                log("[idle] parent gone, exit")
                if self.engine is not None and self.engine.stats.loaded:
                    try:
                        self.engine.unload()
                    except Exception:
                        pass
                self._stop.set()
                break
            idle = time.monotonic() - self.last_activity
            keep_warm = bool(getattr(self.engine, "keep_warm", False))
            if (self.engine is not None and self.engine.stats.loaded
                    and idle > self.args.idle_unload and not keep_warm):
                with self.lock:
                    try:
                        freed = self.engine.unload()
                        log("[idle] unloaded model, free=%sMB" % freed)
                    except Exception:
                        log(traceback.format_exc())
            if idle > self.args.daemon_exit:
                log("[idle] daemon exit")
                self._stop.set()
                break

    def _warm_load(self):
        """后台把常驻模型读进内存：sherpa 首句以前要 5~9s 冷加载，预热后第一句就出声。"""
        try:
            t0 = time.monotonic()
            engine = self._get_engine()
            info = engine.load()
            log("[warm] %s %s %.1fs" % (str(getattr(self.args, "backend", "")),
                                        json.dumps(info, ensure_ascii=False),
                                        time.monotonic() - t0))
        except Exception:
            log("[warm] failed:\n" + traceback.format_exc())

    def serve(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.args.host, int(self.args.port)))
        s.listen(4)
        s.settimeout(1.0)
        self.sock = s
        port = s.getsockname()[1]
        print("READY " + json.dumps({"port": port, "pid": os.getpid()}), flush=True)
        log("[daemon] listening on %s:%d" % (self.args.host, port))

        if str(getattr(self.args, "backend", "")).lower() in ("sherpa",):
            threading.Thread(target=self._warm_load, daemon=True).start()

        threading.Thread(target=self._idle_loop, daemon=True).start()

        while not self._stop.is_set():
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                try:
                    self._handle(conn)
                except Exception as e:
                    log(traceback.format_exc())
                    try:
                        self._send_json(conn, {"ev": "error", "code": type(e).__name__, "message": str(e)})
                    except Exception:
                        pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        try:
            s.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--model-dir", default="")
    ap.add_argument("--backend", default="qwen", choices=["qwen", "piper", "sherpa"])
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--piper-model", default="")
    ap.add_argument("--dtype", default="float16")
    ap.add_argument("--attn", default="sdpa")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--min-free-vram-mb", type=int, default=1600)
    ap.add_argument("--max-new-tokens", type=int, default=1024)
    ap.add_argument("--idle-unload", type=int, default=120)
    ap.add_argument("--daemon-exit", type=int, default=600)
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--no-trim", action="store_true")
    # Piper 生成随机性的两个旋钮：noise_w 决定字长抖动（停顿是否稳定），
    # noise_scale 决定音色/抑扬。0.10/0.10 是实测的稳定点。
    ap.add_argument("--noise-scale", type=float, default=0.10)
    ap.add_argument("--noise-w", type=float, default=0.10)
    args = ap.parse_args()

    if str(args.backend).lower() == "piper":
        if not args.piper_model:
            args.piper_model = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "assets", "piper", "zh_CN-huayan-medium.onnx")
        if not os.path.isfile(args.piper_model):
            print("ERR " + json.dumps({"code": "MODEL_MISSING", "piper_model": args.piper_model}), flush=True)
            return 2
        if not args.model_dir:
            args.model_dir = os.path.dirname(args.piper_model)
    elif str(args.backend).lower() == "sherpa":
        # 模型清单在 tts/voices.py 里，不用 --model-dir；这里只校验权重是否齐全
        try:
            from tts.voices import MODELS, WARM_MODELS
        except Exception as e:
            print("ERR " + json.dumps({"code": "VOICES_IMPORT", "message": str(e)}), flush=True)
            return 2
        missing = []
        for key in WARM_MODELS:
            m = MODELS.get(key) or {}
            d = m.get("dir", "")
            if not d or not os.path.isdir(d):
                missing.append({"model": key, "dir": d})
        if missing:
            print("ERR " + json.dumps({"code": "MODEL_MISSING", "missing": missing}), flush=True)
            return 2
    else:
        if not args.model_dir:
            from tts.config import model_dir_from_config
            args.model_dir = model_dir_from_config()

        if not os.path.isdir(args.model_dir):
            print("ERR " + json.dumps({"code": "MODEL_MISSING", "model_dir": args.model_dir}), flush=True)
            return 2

    Daemon(args).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
