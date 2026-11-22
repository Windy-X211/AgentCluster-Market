# coding=utf-8
"""TTS 服务：worker 进程内的配音调度器。

职责边界：
  * 合成在 daemon 里（独立 venv + Qwen3-TTS，绝不污染宿主 python）；
  * 播放、字幕时钟、BGM 混音都在本进程的 Qt 主线程（与桌宠同窗口）；
  * 字幕逐字进度用真实播放样本数推算，不用定时器累加。

线程模型：
  speak() 从 HTTP 线程进来 → 起一个读 socket 的线程 → 每收到一段 PCM 就 emit 信号，
  由 Qt 队列投递回主线程入队播放。这样音频缓冲与 QAudioOutput 永远只在主线程被碰。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt5.QtCore import QObject, QTimer, Qt, pyqtSignal

from .audio_engine import AudioEngine
from .bgm import load_bgm, synth_soft_pad, resolve_bgm
from .segmenter_marks import split_sentences

# daemon 是全进程唯一的：桌宠可能开出多个窗口/TTSService 实例，
# 每个实例各起一个 daemon 会抢显存、并且让 speak 连到没加载模型的那个（表现为"一直没声音"）。
_DAEMON_LOCK = threading.Lock()
_DAEMON_STATE = {"proc": None, "port": 0, "ready": False, "pid": 0}


def _pid_alive(pid: int) -> bool:
    """只判断 pid 是否还活着（不做任何进程树遍历，够快）。"""
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
                             capture_output=True, text=True, timeout=4,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return str(int(pid)) in (out.stdout or "")
    except Exception:
        return True    # 判断失败就当作还活着，避免误杀/重启
from .config import PLUGIN_DIR, load_config, model_dir_from_config, venv_python
from .marks import char_marks, build_segments
from .markers import bad_marks, char_at_content, content_index, parse_anim_markers
from .speech_motion import SpeechMotion

DAEMON = str(PLUGIN_DIR / "tools" / "tts_daemon.py")

# 两套音色互不通用，随 backend 分开提供；名字由模型目录里的 speaker 决定。
# 本地 sherpa 引擎的音色来自「一个模型 + 多个 sid」，注册表见 tts/voices.py：
# 换音色只是换 sid，零加载开销，也不会多占一份内存。
try:
    from .voices import voice_list as _voice_list
    LOCAL_VOICES = _voice_list()
except Exception:      # 引擎降级时也不能让整个插件起不来
    LOCAL_VOICES = [
        {"id": "huayan", "gender": "女", "trait": "Piper 轻量流式（兜底）"},
        {"id": "Serena", "gender": "女", "trait": "Qwen3-TTS 温柔知性（需 local.engine=qwen）"},
        {"id": "vivian", "gender": "女", "trait": "清亮，偏年轻"},
        {"id": "ono_anna", "gender": "女", "trait": "轻快，日系"},
        {"id": "sohee", "gender": "女", "trait": "柔和，韩系"},
        {"id": "ryan", "gender": "男", "trait": "沉稳，叙事感"},
        {"id": "aiden", "gender": "男", "trait": "年轻，偏明亮"},
        {"id": "dylan", "gender": "男", "trait": "松弛，口语感"},
        {"id": "eric", "gender": "男", "trait": "磁性，偏成熟"},
        {"id": "uncle_fu", "gender": "男", "trait": "厚重，中年"},
    ]

ONLINE_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "gender": "女", "trait": "自然亲和，默认"},
    {"id": "zh-CN-XiaoyiNeural", "gender": "女", "trait": "年轻活泼"},
    {"id": "zh-CN-YunxiNeural", "gender": "男", "trait": "清朗，少年感"},
    {"id": "zh-CN-YunjianNeural", "gender": "男", "trait": "浑厚，解说感"},
    {"id": "zh-CN-YunyangNeural", "gender": "男", "trait": "端正，新闻播报"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "gender": "女", "trait": "东北口音"},
]

# 在线第二家：小米 MiMo-V2.5-TTS（预置音色见 tts/engine_mimo.py）。
# 名字体系与 Edge 完全不同（冰糖 / 茉莉 / 苏打 / 白桦 + 四个英文音色），
# 所以 backend=mimo 时必须用这套名字，跨后端照抄会被归一成各自默认音色。
try:
    from .engine_mimo import MIMO_DEFAULT_VOICE, MIMO_VOICES
except Exception:       # 缺依赖时也不能让插件起不来
    MIMO_VOICES = [{"id": "冰糖", "gender": "女", "trait": "中文女声，清晰自然（默认）"}]
    MIMO_DEFAULT_VOICE = "冰糖"




class _Conn:
    """带缓冲的 socket 读取器：行与二进制 PCM 混在同一个流里，必须保留余量。"""

    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def recv_more(self, timeout=None):
        if timeout is not None:
            self.s.settimeout(timeout)
        data = self.s.recv(65536)
        if not data:
            raise ConnectionError("daemon 已断开")
        self.buf += data

    def line(self) -> dict:
        while b"\n" not in self.buf:
            self.recv_more()
        head, self.buf = self.buf.split(b"\n", 1)
        return json.loads(head.decode("utf-8", "replace"))

    def exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.recv_more()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class TTSService(QObject):

    # speak() 在 HTTP 线程里跑，不能直接 QTimer.start()（Qt 定时器只能在所属线程启停）。
    # 用信号投递到主线程，保证 tick 一定重新起搏。
    wake = pyqtSignal()
    motionWake = pyqtSignal()

    def __init__(self, pet=None, parent=None):
        super().__init__(parent)
        self.pet = pet
        self.cfg = load_config()

        self._engine: Optional[AudioEngine] = None
        self._daemon_host = "127.0.0.1"
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()
        self._warm_th: Optional[threading.Thread] = None

        # 时间轴：[{text, start_ms, end_ms, marks(list[ms] 相对段首), chars}]
        self._tl: List[Dict[str, object]] = []
        # 流式投递中的段：key(gen-i) → {entry, speech_ms, gap_ms}
        self._stream_seg: Dict[str, Dict[str, object]] = {}
        # 整段推理（唱歌等）时的合成进度：桌宠头顶气泡显示百分比
        self._prog: Optional[Dict[str, object]] = None
        self._prog_timer = QTimer(self)
        self._prog_timer.setInterval(300)
        self._prog_timer.timeout.connect(self._prog_tick)
        # 唱歌等待期垫场：是否正在放"等待背景音"（歌一开口就淡出）
        self._singing = False
        self._wait_bgm_on = False
        self._wait_sub: Optional[Dict[str, object]] = None   # 说明句字幕
        self._pending: List[Dict[str, object]] = []          # mode=queue 排队待唱/待说
        self._queue_timer = QTimer(self)                      # wait 秒：上一条播完后延迟再起下一条
        self._queue_timer.setSingleShot(True)
        self._queue_timer.timeout.connect(self._start_delayed_pending)
        self._queue_pending: Optional[Dict[str, object]] = None
        self._song_cache: Dict[Tuple, Dict[str, object]] = {}  # 排队唱歌预取：第一条播时先把下一条合成好
        self._song_cache_lock = threading.Lock()
        self._song_prefetching: set = set()
        # 排队说话预取（depth=1，仅 mode=queue 说话；结构同 _song_cache，但按段存 chunk 列表，
        # 命中后能完整复现流式投递 sc/sed/end，字幕/口型/BGM 时间轴不提前）。
        # key = (backend, voice, speed, style, dialect, text)；value = {"segs","chunks","sr","ts"}。
        self._speech_cache: Dict[Tuple, Dict[str, object]] = {}
        self._speech_cache_lock = threading.Lock()
        self._speech_prefetching: set = set()
        # 预取代际：用户 stop/replace 时递增，旧线程即使刚好越过 stop_flag 检查，
        # 也不能把打断前的音频写回缓存（否则 replace 后同文本会命中旧缓存）。
        self._speech_epoch = 0
        self._user_bgm = False           # 调用方自己传了 bgm 时不动它的音乐
        self._state = {
            "speaking": False, "backend": "", "voice": "",
            "ttfa_ms": None, "segments": 0, "error": None,
            "text": "", "started_at": None, "finish_ms": None,
        }
        self._t0 = 0.0
        self._last_clock = -1.0
        self._hold_token = None          # 字幕停留令牌：新句子会作废旧的回调
        self._gen = 0                    # 会话代号：丢弃上一句残留线程投递的段
        # 台词里的内置动画标记：每句重置，段到达时用锚点定位到具体字
        self._anim_marks: List[Dict[str, object]] = []

        # 说话时的程序化微动作：只在"智能体没指定动作"时生效（指定了就让位）
        self._motion = SpeechMotion(gain=float(self._c("motion_gain", 1.0)),
                                    nod_every_ms=int(self._c("motion_nod_every_ms", 2200)))
        self._motion_ok = True
        self._motion_last = 0.0
        self._motion_out: Dict[str, float] = {}
        self._motion_timer: Optional[QTimer] = None

        # 三个 daemon 状态不在这里初始化：它们代理到模块级 _DAEMON_STATE，
        # 由第一个实例/第一次拉起重置，避免新实例把已在跑的 daemon 忘掉。
        self._init_qt_parts()

    # daemon 状态对本进程内所有实例共享（见模块级 _DAEMON_STATE）
    def _kill_daemon(self, graceful: bool = False, wait_s: float = 6.0) -> None:
        """按 READY 上报的真实 pid 收掉 daemon（launcher 句柄杀不干净）。"""
        port, dpid = self._daemon_port, self._daemon_pid
        if port and graceful:
            try:
                s = socket.create_connection((self._daemon_host, port), timeout=2.0)
                s.sendall(b'{"op":"shutdown"}\n')
                s.close()
            except Exception:
                pass
            t_end = time.monotonic() + wait_s
            while time.monotonic() < t_end:
                if not dpid or not _pid_alive(dpid):
                    break
                time.sleep(0.2)
        if dpid and _pid_alive(dpid):
            try:
                subprocess.run(["taskkill", "/PID", str(dpid), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        self._proc = None
        self._daemon_port = 0
        self._daemon_pid = 0
        self._daemon_ready = False
    @property
    def _proc(self):
        return _DAEMON_STATE["proc"]

    @_proc.setter
    def _proc(self, v):
        _DAEMON_STATE["proc"] = v

    @property
    def _daemon_port(self):
        return _DAEMON_STATE["port"]

    @_daemon_port.setter
    def _daemon_port(self, v):
        _DAEMON_STATE["port"] = int(v or 0)

    @property
    def _daemon_ready(self):
        return _DAEMON_STATE["ready"]

    @_daemon_ready.setter
    def _daemon_ready(self, v):
        _DAEMON_STATE["ready"] = bool(v)

    @property
    def _daemon_pid(self):
        return _DAEMON_STATE["pid"]

    @_daemon_pid.setter
    def _daemon_pid(self, v):
        _DAEMON_STATE["pid"] = int(v or 0)

    def _init_qt_parts(self):
        # 合成线程 → 主线程只走这个队列：跨线程 emit 带 numpy 的对象会让 PyQt
        # 在 sip 层 0xC0000409 直接带走进程（实测），队列最稳。
        self._inbox: "queue.Queue" = queue.Queue()

        self._tick = QTimer(self)
        self._tick.setInterval(30)
        self._tick.timeout.connect(self._on_tick)
        # 跨线程唤醒 tick（排到主线程事件循环里执行）
        self.wake.connect(self._tick.start, Qt.QueuedConnection)

        # 微动作心跳必须在这里建（TTSService 在主线程构造）：
        # speak() 是从 HTTP 线程进来的，在那边 new QTimer(self) 会得到
        # "Timers can only be used with threads started with QThread"，
        # 定时器永远不响，动作完全不生效。
        self._motion_timer = QTimer(self)
        self._motion_timer.setInterval(50)
        self._motion_timer.timeout.connect(self._motion_body)
        self.motionWake.connect(self._motion_start, Qt.QueuedConnection)

    # ------------------------------------------------------------ 线程间投递
    def _c(self, key: str, default=None):
        """读配置：顶层与 local/audio/online/subtitle/bgm 各层都找一遍。

        DEFAULTS 是分层的（local.dtype / local.prebuffer_ms / bgm.volume...），
        而播放侧只关心一个名字，这里做统一归一化，避免两边各写一套取值路径。
        """
        for scope in (None, "audio", "local", "online", "subtitle", "bgm"):
            node = self.cfg if scope is None else (self.cfg.get(scope) or {})
            if isinstance(node, dict) and key in node:
                val = node[key]
                if key == "voice" and isinstance(val, dict):
                    return val.get("local") or default
                return val
        return default

    def _post(self, kind: str, *payload) -> None:
        self._inbox.put((kind,) + payload)

    def _drain(self) -> None:
        while True:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            try:
                if kind == "seg":
                    self._on_seg(item[1], item[2])
                elif kind == "sc":
                    self._on_sc(item[1], item[2])
                elif kind == "sed":
                    self._on_sed(item[1])
                elif kind == "prg":
                    self._on_prog(item[1])
                elif kind == "waitp":
                    self._on_waitp(item[1])
                elif kind == "end":
                    self._on_end(item[1])
                elif kind == "err":
                    self._on_err(item[1], item[2] if len(item) > 2 else -1)
            except Exception as e:
                self._state["error"] = "处理 %s 失败: %s" % (kind, e)

    # ------------------------------------------------------------ 生命周期
    def open(self) -> Dict[str, object]:
        if self._engine is not None:
            return self._engine.info if hasattr(self._engine, "info") else {"ok": True}
        # audio_backend: qt（默认，走 QtMultimedia，零额外依赖）
        #                sd（PortAudio/sounddevice，回调式，延迟更低）
        eng_cls = AudioEngine
        if str(self._c("audio_backend", "qt")).lower() in ("sd", "portaudio"):
            try:
                from .sd_engine import SdAudioEngine as eng_cls   # noqa: F811
            except Exception as e:
                self._state["error"] = "sd 引擎不可用，回退 Qt: %s" % e
                eng_cls = AudioEngine
        kw = dict(sample_rate=int(self._c("sample_rate", 48000)),
                  prebuffer_ms=int(self._c("prebuffer_ms", 320)))
        try:
            eng = eng_cls(buffer_ms=int(self._c("buffer_ms", 100)), parent=self, **kw)
        except TypeError:
            eng = eng_cls(parent=self, **kw)
        info = eng.open()
        eng.finished.connect(self._on_audio_finished)
        self._engine = eng
        try:
            self.wake.emit()     # 同样走主线程，不在这里直接 start
        except Exception:
            pass
        return dict(info, ok=True)

    def warmup(self) -> Dict[str, object]:
        """后台预热本地模型：spawn daemon + 加载权重 + 一次极短合成。

        放在桌宠启动后立刻调用，用户第一次 speak 就不必再等十几秒的冷加载。
        没有私有 venv 或模型时静默跳过，不影响桌宠启动。
        """
        if os.environ.get("DESKTOP_PET_TTS_NOWARM"):
            return {"ok": False, "reason": "disabled"}
        if self._warm_th is not None and self._warm_th.is_alive():
            return {"ok": True, "warming": True}

        def _run():
            try:
                if not self._ensure_proc():
                    return
                reply = self._request({"op": "preload",
                                       "voice": str(self._c("voice", "Serena"))}, 600.0)
                self._state["warm"] = {"ok": True,
                                       "load_ms": reply.get("load_ms"),
                                       "warm_ms": reply.get("warm_ms"),
                                       "vram_mb": reply.get("vram_mb")}
            except Exception as e:
                self._state["warm"] = {"ok": False, "error": str(e)}

        self._state["warm"] = {"ok": None}
        self._warm_th = threading.Thread(target=_run, daemon=True)
        self._warm_th.start()
        return {"ok": True, "warming": True}

    def close(self) -> None:
        self.stop()
        self.unload()
        try:
            self._tick.stop()
        except Exception:
            pass
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:
                pass
            self._engine = None

    # ------------------------------------------------------------ 合成/播放
    def speak(self, text: str, voice: str = "Serena", backend: str = "local",
              speed: Optional[float] = None, bgm: str = "", bgm_volume: Optional[float] = None,
              bgm_duck: Optional[float] = None, subtitle: bool = True,
              animation: str = "", motion_group: str = "", lip_sync: bool = True,
              mode: str = "replace", wait: float = 0.0, blocking: bool = False,
              api_key: str = "",
              sing: bool = False, style: str = "", dialect: str = "",
              _from_queue: bool = False) -> Dict[str, object]:
        # 换行不能被抹成空格：唱歌按行拆句/拆段都靠它（行是最自然的音乐短语边界）。
        raw = str(text or "").replace("\r", "\n")
        txt = "\n".join(" ".join(ln.split()) for ln in raw.split("\n") if ln.strip())
        if not txt:
            return {"ok": False, "error": "text 为空"}

        # backend 归一：auto/online 按 online.provider 落地，mimo/edge 直接指定服务商。
        # 必须放在语速折算与音色解析之前，否则 mimo 会被当成"在线 Edge"错配音色。
        backend = self._normalize_backend(backend)

        # 唱歌是 MiMo 的独占能力（(唱歌) 标签 + mimo-v2.5-tts 模型），别家做不了：
        # 早拒绝，免得 local/edge 把歌词当普通句子念出来还以为在唱。
        if sing and backend != "mimo":
            return {"ok": False, "action": "speak",
                    "error": "唱歌只有 backend=mimo（MiMo-V2.5-TTS）支持，当前 backend=%s" % backend}

        # MiMo 的 key 是"按次携带"的（参数预设每次注入），所以缺 key 在这里同步报错，
        # 而不是丢进后台线程、只在状态里留一行错 —— 调用方能立刻拿到失败原因。
        if backend == "mimo":
            from .engine_mimo import find_api_key, load_settings as _mimo_settings
            _mimo = _mimo_settings(self.cfg)
            if not find_api_key(_mimo, override=api_key):
                return {"ok": False, "action": "speak", "backend": backend,
                        "error": "MiMo 需要 API Key：调用时传 api_key（放进参数预设即可自动带上），"
                                 "或设置环境变量 %s，或在配置里写 online.mimo.api_key"
                                 % (_mimo.get("api_key_env") or "MIMO_API_KEY")}

        # 台词里的内置动画标记（[[nod]] / [[点头]]）：先剥掉再合成，标记不能念出来
        txt, anim_marks = parse_anim_markers(txt)
        bad = bad_marks(anim_marks)
        if not txt:
            return {"ok": False, "error": "text 去掉动画标记后为空"}
        self._anim_marks = anim_marks
        # 段落定位用的"已消化内容位数"：按段累加，标记的 pos 落在哪一段就挂到哪一段。
        # 不走锚点子串，避免"锚点被分段切断"或"单字锚点撞到别的段"。
        self._anim_base = 0

        # 语速语义：speed=1.0 = "正常语速"（对本地就是 Piper 原速 × base_speed(0.90)）。
        # 智能体只需要记 1 是正常、<1 更慢、>1 更快，不需要知道底层基准是多少。
        if speed is None:
            speed = 1.0
        try:
            speed = max(0.5, min(2.0, float(speed)))
        except Exception:
            speed = 1.0
        if backend == "local":
            # spd_internal 是底层引擎的"原速倍数"。两套引擎的天然语速不同：
            # Piper 偏慢（0.80 才等于用户认可的正常语速），sherpa 的实测语速
            # 与 Piper@0.80 基本一致，所以 sherpa 的基准是 1.0，不要乘 0.80。
            eng = str(self._c("engine", "sherpa")).lower()
            base = (float(self._c("base_speed", 0.80)) if eng == "piper"
                    else float(self._c("sherpa_speed_scale", 1.0)))
            # WSOLA 只在 0.6~1.6 真实生效；对外 0.5~2.0 夹到内部可用区间
            spd_internal = max(0.6, min(1.6, speed * base))
        else:
            spd_internal = speed

        # 音色按后端归一化：本地是预设名（Serena…），edge 是 Azure 短名（zh-CN-XxxNeural），
        # mimo 是 MiMo 的短名（冰糖/茉莉/苏打/白桦/Mia…），三套互不通用
        voice = self._resolve_voice(backend, voice)

        # mode 必须由调用方明确指定：replace 打断当前，queue 排队。
        # 不再做 auto 推断——智能体必须自己判断上下文，避免"上一句还没说完就被替换"。
        mode = str(mode or "replace").strip().lower()
        if mode not in ("replace", "queue"):
            return {"ok": False, "action": "speak",
                    "error": "mode 只支持 replace / queue（auto 已移除）"}
        if mode == "queue" and (bool(self._state.get("speaking"))
                                or bool(self._queue_pending)
                                or (self._queue_timer is not None and self._queue_timer.isActive())):
            # 排队：当前这首/这句完整播完再播新的，绝不打断；返回排位供智能体判断
            with self._lock:
                self._pending.append(dict(
                    text=text, voice=voice, backend=backend, speed=speed, bgm=bgm,
                    bgm_volume=bgm_volume, bgm_duck=bgm_duck, subtitle=subtitle,
                    animation=animation, motion_group=motion_group, lip_sync=lip_sync,
                    mode="replace", wait=self._wait_seconds(wait), blocking=False, api_key=api_key,
                    sing=sing, style=style, dialect=dialect))
                pos = len(self._pending)
            # 排队唱歌：第一条还在唱时就把下一条送去合成，播到时直接出声音，
            # 不再出现"第一条唱完 → 第二条才开始算 → 中间十几秒空白"。
            if bool(sing):
                try:
                    self._start_song_prefetch(txt, voice, speed, api_key, style, dialect)
                except Exception as e:
                    self._state["prefetch_error"] = str(e)
            # 排队说话 depth=1 预取：只对 mimo 说话（edge/local 不做适配），
            # 只在这是队列第一条（pos==1）时触发——已经有一条在预取就不重复起线程。
            elif (backend == "mimo" and pos == 1
                  and bool(self._mimo_cfg().get("speech_prefetch_enabled", True))):
                try:
                    self._start_speech_prefetch(txt, backend, voice, speed, api_key,
                                                style, dialect)
                except Exception as e:
                    self._state["prefetch_error"] = str(e)
            return {"ok": True, "action": "speak", "backend": backend, "voice": voice,
                    "chars": len(txt), "queued": True, "position": pos}
        if mode == "replace":
            # 排队里起下一条时不能把剩下的队列清掉，否则一次排三首只会播前两首
            # 跨模型 prepare 生成的缓存正是马上要投递的这一句：开播时保留它，
            # 否则预合成完全白做（stop() 默认会清 _speech_cache）。
            keep_speech_cache = False
            if backend == "mimo" and self._speech_prefetch_on():
                key = self._speech_cache_key(backend, voice, speed, style, dialect, txt)
                with self._speech_cache_lock:
                    keep_speech_cache = key in self._speech_cache
            self.stop(notify_pet=False, keep_pending=bool(_from_queue),
                      keep_speech_cache=keep_speech_cache)
            self._gen += 1          # 上一句的合成线程可能还在跑，换代后它的投递会被丢掉
        wait_s = self._wait_seconds(wait)
        if wait_s > 0 and not _from_queue and blocking:
            # blocking=true 是显式“等整段念完才返回”的旧语义，保留同步等待；
            # 普通 speak 默认 blocking=false，走下面的定时器，不阻塞工具调用。
            self._stop_flag.clear()
            end = time.monotonic() + wait_s
            while time.monotonic() < end:
                if self._stop_flag.is_set():
                    return {"ok": False, "error": "等待被 stop 取消"}
                time.sleep(min(0.2, max(0.01, end - time.monotonic())))
        if wait_s > 0 and not _from_queue and not blocking:
            # 不阻塞工具调用：当前没有可排队的上一句时，不能在这里 sleep 等待。
            # 把本次请求挂到 Qt 单次定时器上，N 秒后再真正起合成线程；期间主线程
            # 仍能处理智能体后续的 speak / stop / 状态查询。replace 模式已在上面 stop。
            item = dict(
                text=text, voice=voice, backend=backend, speed=speed, bgm=bgm,
                bgm_volume=bgm_volume, bgm_duck=bgm_duck, subtitle=subtitle,
                animation=animation, motion_group=motion_group, lip_sync=lip_sync,
                mode="replace", wait=0.0, blocking=False, api_key=api_key,
                sing=sing, style=style, dialect=dialect)
            with self._lock:
                self._queue_pending = item
            self._queue_timer.start(int(wait_s * 1000))
            return {"ok": True, "action": "speak", "backend": backend, "voice": voice,
                    "chars": len(txt), "delayed": True, "wait": wait_s}
        self.open()

        # BGM：早进晚退（人声前垫 preroll，人声后再延 tail），首尾都做淡化
        self._wait_bgm_on = False
        self._singing = bool(sing)
        try:
            arr = resolve_bgm(bgm, sr=int(self._c("sample_rate", 48000)))
            # 音量不传就用配置里的（默认 0.75 / duck 0.55）：以前这里硬编码 0.35×0.25，
            # 说话时 BGM 只剩 9%，几乎听不见。
            _bgm_cfg = self.cfg.get("bgm") or {}
            vol = _bgm_cfg.get("volume", 0.75) if bgm_volume is None else bgm_volume
            dck = _bgm_cfg.get("duck", 0.55) if bgm_duck is None else bgm_duck
            if arr is not None:
                if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                    print("[tts] bgm vol=%.2f duck=%.2f preroll=%sms tail=%sms sing=%s" % (
                        float(vol), float(dck), 0 if sing else self._c("preroll_ms", 1200),
                        self._c("tail_ms", 1600), bool(sing)), flush=True)
                # 唱歌：整曲要十几秒才出声，BGM 必须是 "always" 才能扛过这段没人声的等待
                # （"speech" 模式下人声一空就判欠载、设备 suspend，音乐跟着停）；
                # 也不用 preroll——等待期本来就没语音，淡入交给 fade_ms，不能让歌往后挪 1.2s。
                self._engine.set_bgm(
                    arr, volume=float(vol), duck=float(dck),
                    mode=("always" if sing else "speech"),
                    preroll_ms=(0 if sing else int(self._c("preroll_ms", 1200))),
                    tail_ms=int(self._c("tail_ms", 1600)),
                    fade_ms=int(self._bgm_fade_ms()),
                )
            else:
                self._engine.set_bgm(None)
            self._user_bgm = arr is not None     # 调用方传了音乐：唱歌等待期别覆盖它
        except Exception as e:
            self._state["error"] = "BGM 加载失败: %s" % e

        self._stop_flag.clear()
        self._hold_token = None          # 新句子开始，作废上一句的"停留后清除"
        try:
            if not self._tick.isActive() and self._engine is not None:
                self.wake.emit()         # 上一句播完时停过，这里必须重新起搏（走主线程）
        except Exception:
            pass
        with self._lock:
            self._tl = []
            self._stream_seg = {}
            self._state.update({"speaking": True, "backend": backend, "voice": voice,
                                "ttfa_ms": None, "segments": 0, "error": None,
                                "text": txt, "started_at": time.monotonic(),
                                "finish_ms": None, "subtitle": bool(subtitle),
                                "animation": animation, "motion_group": motion_group,
                                "lip_sync": bool(lip_sync), "speed": round(float(speed), 3),
                                # 本次的自然语言表演指令（MiMo 用，排查时看它有没有送到）
                                "style": str(style or ""),
                                "dialect": str(dialect or ""),
                                # 内部实际用的倍数（本地 = speed × base_speed），排查语速时用它
                                "speed_internal": round(float(spd_internal), 3),
                                # 台词里的动画标记（已剥掉，仅用于回读与排查）
                                "anim_marks": [{"name": m.get("name"),
                                                "anchor": m.get("anchor")} for m in anim_marks
                                               if m.get("name")],
                                "anim_mark_error": bad,
                                "anim_fired": []})
        self._t0 = time.monotonic()

        # 出字前先把动画推起来：让"动画与配音同播"，而不是等音频
        # 智能体指定了动作就让它独占（返回 True 才算真播了）；
        # 没指定（或模型没动作文件播不了）→ 走程序化微动作，否则站着像木头。
        self._motion_ok = True
        if animation and self.pet is not None:
            try:
                if self.pet.tts_play_motion(motion_group or animation):
                    self._motion_ok = False
            except Exception:
                pass
        self._motion.start(time.monotonic())
        self._motion_ensure()

        if backend == "local":
            target = self._run_local
        elif backend == "mimo":
            target = self._run_mimo
        else:
            target = self._run_edge
        kw = dict(text=txt, voice=voice, speed=spd_internal, gen=self._gen)
        if backend == "mimo":
            # 在线 key 只在这条合成线程里活一次：不写配置、不进状态快照
            kw["api_key"] = str(api_key or "").strip()
            kw["sing"] = bool(sing)
            # 自然语言表演指令：只对本次生效，同样不写盘
            kw["style"] = str(style or "").strip()
            kw["dialect"] = str(dialect or "").strip()
        th = threading.Thread(target=target, kwargs=kw, daemon=True)
        self._thread = th
        th.start()

        if blocking:
            while th.is_alive() and not self._stop_flag.is_set():
                time.sleep(0.02)
            return self.status()
        return {"ok": True, "action": "speak", "backend": backend, "voice": voice,
                "chars": len(txt), "mode": mode}

    def prepare(self, text: str, voice: str = "", backend: str = "mimo",
                speed=None, api_key: str = "", style: str = "",
                dialect: str = "") -> Dict[str, object]:
        """只把这条 MiMo 说话合成到缓存，不播放、不碰字幕/口型/BGM。

        工具层在跨模型 wait_others 排队时调用它：目标模型先算好下一句，
        轮到它时 speak() 直接命中缓存，换人空白从首包延迟降到只剩余缓冲。
        edge/local 不做预合成（没有同构缓存消费路径），返回 prepared=False 由调用方忽略。
        """
        backend = self._normalize_backend(backend)
        if backend != "mimo":
            return {"ok": True, "prepared": False, "reason": "only mimo speech"}
        if not self._speech_prefetch_on():
            # 总开关与队列内预取共用：关掉 speech_prefetch_enabled 时，
            # 跨模型 prepare 也必须一起失效，保证能一键回退。
            return {"ok": True, "prepared": False, "reason": "prefetch disabled"}
        txt = self._normalize_speak_text(text)
        if not txt:
            return {"ok": False, "error": "text 为空"}
        try:
            speed = max(0.5, min(2.0, float(speed))) if speed is not None else 1.0
        except Exception:
            speed = 1.0
        voice = self._resolve_voice(backend, voice)
        style = str(style or "").strip()
        dialect = str(dialect or "").strip()
        if dialect == "普通话":
            dialect = ""
        # 调用方（deferred prepare 线程）已经确认目标模型 speaking=false 且 queued=0，
        # 这里直接清 stop_flag：否则上一条 stop() 留下的置位会让预取线程第一块就退出，
        # 缓存永远填不上（第一轮换人因此回落到 1 秒级冷启动）。
        try:
            self._stop_flag.clear()
        except Exception:
            pass
        key = self._speech_cache_key(backend, voice, speed, style, dialect, txt)
        with self._speech_cache_lock:
            if key in self._speech_cache:
                return {"ok": True, "prepared": True, "cached": True,
                        "backend": backend, "voice": voice,
                        "chars": len(txt)}
            if key in self._speech_prefetching:
                return {"ok": True, "prepared": True, "pending": True,
                        "backend": backend, "voice": voice,
                        "chars": len(txt)}
            self._speech_prefetching.add(key)
            epoch = int(self._speech_epoch)
        try:
            self._state["prepare_count"] = int(self._state.get("prepare_count") or 0) + 1
            self._state["prepare_last_chars"] = len(txt)
            self._state["prepare_t0"] = time.time()
        except Exception:
            pass
        # 同步完成再返回：调用方是后台 prepare 线程，不会阻塞工具调用；
        # 但能保证“prepare 返回时缓存已经就绪”，避免异步线程没跑完就被 speak 抢走。
        self._prefetch_speech(key, txt, voice, speed,
                              str(api_key or "").strip(), style, dialect, epoch)
        with self._speech_cache_lock:
            cached = key in self._speech_cache
        try:
            self._state["prepare_ms"] = int(
                (time.time() - float(self._state.get("prepare_t0") or time.time())) * 1000)
            self._state["prepare_cached"] = bool(cached)
        except Exception:
            pass
        if cached:
            return {"ok": True, "prepared": True, "cached": True,
                    "backend": backend, "voice": voice, "chars": len(txt)}
        return {"ok": False, "prepared": False, "backend": backend,
                "voice": voice, "chars": len(txt),
                "error": self._state.get("prefetch_error") or "预合成未写入缓存"}

    def _request(self, cmd: Dict[str, object], timeout: float = 30.0) -> Dict[str, object]:
        """一问一答（preload/status/unload 这类只要有首个 JSON 回包的操作）。"""
        s = socket.create_connection((self._daemon_host, self._daemon_port), timeout=timeout)
        try:
            s.settimeout(timeout)
            s.sendall((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
            conn = _Conn(s)
            return conn.line()
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _bgm_fade_ms(self) -> int:
        """BGM 淡入/淡出时长：先看 bgm.fade_ms，没有就按淡入淡出各取一半。"""
        v = self._c("fade_ms", None)
        if v:
            return int(v)
        return 500

    def _voice_for(self, backend: str) -> str:
        vmap = self.cfg.get("voice") or {}
        if isinstance(vmap, dict):
            if backend == "local":
                return str(vmap.get("local") or "")
            if backend == "mimo":
                return str(vmap.get("mimo") or self._mimo_cfg().get("voice") or MIMO_DEFAULT_VOICE)
            return str(vmap.get("online") or "")
        return str(vmap or "")

    def _mimo_cfg(self) -> Dict[str, object]:
        """online.mimo 这一段（不存在时给空字典，不抛异常）。"""
        node = self.cfg.get("online") or {}
        mimo = node.get("mimo") if isinstance(node, dict) else None
        return dict(mimo) if isinstance(mimo, dict) else {}

    def _online_provider(self) -> str:
        """backend=auto/online 时走哪家在线服务：配置 online.provider，默认 edge。"""
        node = self.cfg.get("online") or {}
        prov = str((node.get("provider") if isinstance(node, dict) else "") or "edge").lower()
        return "mimo" if prov in ("mimo", "mimo-tts", "xiaomi", "mi") else "edge"

    def _normalize_backend(self, backend) -> str:
        """把外部传进来的 backend 名字归一成 local / edge / mimo。

        auto 与 online 都按配置的 online.provider 落地 —— 老调用方写 edge 仍然
        明确指 Edge，不会被配置改道，避免"我只想换个在线服务商，结果老代码全变了"。
        """
        b = str(backend or "local").strip().lower()
        if b in ("auto", "online"):
            return self._online_provider()
        if b in ("mimo", "mimo-tts", "xiaomi", "mi"):
            return "mimo"
        if b in ("edge", "edge-tts", "azure"):
            return "edge"
        return "local"

    def _resolve_voice(self, backend: str, voice) -> str:
        """音色名跨后端会失效（Serena 不是 Edge 的合法音色，冰糖也不属于 Edge）。

        规则：先按 backend 各自的命名体系归一；不匹配时回落到该后端的默认音色，
        而不是把错误音色名丢给服务端。
        """
        name = str(voice or "").strip()
        # MiMo 的音色是"冰糖/茉莉…"这种短名，跟 Edge 的 zh-CN-xxxNeural 互不通用：
        # 先交给 engine_mimo 归一，认不出来就回落该后端默认，绝不把错名字丢给服务端。
        if backend == "mimo":
            default = self._voice_for("mimo") or MIMO_DEFAULT_VOICE
            try:
                from .engine_mimo import resolve_voice
                return resolve_voice(name, default=default)
            except Exception:
                return default
        want_online = backend == "edge"
        eng = str(self._c("engine", "sherpa")).lower()
        # Piper 是单音色引擎：任何名字都归一成它唯一的音色，避免把预设名丢进去报错
        if not want_online and eng == "piper":
            return "huayan"
        # sherpa 是「一个模型 + 多个 sid 音色」：不认识的名字统一回落到默认音色，
        # 绝不把错名字传给 daemon（否则会念成别的角色，甚至出空音频）
        if not want_online and eng == "sherpa":
            try:
                from .voices import resolve
                return resolve(name, "sherpa")["id"]
            except Exception:
                return self._voice_for("local") or "eula_girl"
        looks_online = "Neural" in name
        if not name or (looks_online != want_online and name not in ("default", "auto")):
            return self._voice_for(backend) or ("zh-CN-XiaoxiaoNeural" if want_online else "Serena")
        return name

    def _run_local(self, text: str, voice: str, speed: float = 1.0, gen: int = 0) -> None:
        try:
            if not self._ensure_proc():
                return
            # daemon 是「一个连接一个请求」，处理完就关，所以每次 speak 都重连
            try:
                s = socket.create_connection((self._daemon_host, self._daemon_port), timeout=5.0)
            except OSError:
                # 端口没人接：daemon 可能被 idle 卸载/被杀，重建一次再连（只重试一次）
                with _DAEMON_LOCK:
                    self._daemon_ready = False
                    self._daemon_port = 0
                    self._kill_daemon()
                    ok = self._ensure_proc_inner()
                if not ok:
                    return
                s = socket.create_connection((self._daemon_host, self._daemon_port), timeout=8.0)
            # 用「事件间隔」而不是「总时长」做上限：本地一条长句可能要合几十秒，
            # 但只要还在出段就正常；半天不出一段就是生成卡死（显存被桌宠渲染挤爆时会有）。
            s.settimeout(float(self._c("local_stall_s", 45)))
            conn = _Conn(s)
            cmd = {"op": "speak", "text": text, "speaker": voice,
                   "language": str(self._c("language", "Chinese")),
                   # 语速：Piper 走 length_scale（1/speed）；Qwen 引擎收到但不生效
                   "speed": float(speed or 1.0),
                   # 首句切短一点：本地 RTF 很高，先出 4~8 个字就能出声，
                   # 不必等整句 20 秒才听到第一个字。
                   "min_chars": int(self._c("min_chars", 6)),
                   "max_chars": int(self._c("max_chars", 24)),
                   # 文本停顿符号（~ | || |||）解析 + 段间静音长度
                   "pause_marks": bool(self._c("pause_marks", True)),
                   "pause_short_ms": int(self._c("pause_short_ms", 150)),
                   "pause_mid_ms": int(self._c("pause_mid_ms", 300)),
                   "pause_long_ms": int(self._c("pause_long_ms", 500))}
            s.sendall((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
            try:
                while not self._stop_flag.is_set():
                    ev = conn.line()
                    t = ev.get("ev")
                    if t in ("error", "err"):
                        self._post("err", str(ev.get("message") or ev.get("error") or "合成失败"), gen)
                        return
                    if t == "seg":
                        n = int(ev.get("samples") or 0)
                        raw = conn.exact(n * 2) if n > 0 else b""
                        pcm = np.frombuffer(raw, dtype=np.int16).copy() if n > 0 else np.zeros(0, np.int16)
                        if self._state["ttfa_ms"] is None:
                            self._state["ttfa_ms"] = int((time.monotonic() - self._t0) * 1000)
                        self._post("seg", dict(ev, _gen=gen), pcm)
                        continue
                    if t == "end":
                        self._post("end", dict(ev, _gen=gen))
                        return
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        except socket.timeout:
            stall = int(self._c("local_stall_s", 45))
            # daemon 卡在 generate 里时连接关掉它也不退，会把显存一直占着 —— 必须杀掉重建
            self._kill_daemon()
            self._post("err", "本地合成停顿超过 %ds（生成未收敛或显存不足），已重启本地引擎" % stall, gen)
        except Exception as e:
            self._post("err", "daemon 通信失败: %s" % e, gen)

    def _run_edge(self, text: str, voice: str, speed: float = 1.0, gen: int = 0) -> None:
        try:
            from .segmenter_marks import split_segments_marked, strip_pause_marks
            from .engine_edge import synth_segment
            rate = "%+d%%" % int(round((float(speed) - 1.0) * 100))
            # 在线后端也先剥掉停顿符号：它自己会按标点处理停顿，符号不能念给用户听
            segs = [{"text": str(x["text"]), "gap_after_ms": int(x.get("gap_after_ms") or 0)}
                    for x in split_segments_marked(text, use_marks=True)]
            # 状态/字幕里也别留符号：这份 text 会被 status() 与字幕直接回显
            self._state["text"] = strip_pause_marks(text)
            for i, item in enumerate(segs):
                if self._stop_flag.is_set():
                    return
                seg = item["text"]
                pcm, sr, words, ttfa = synth_segment(seg, voice=voice, rate=rate)
                if self._state["ttfa_ms"] is None and ttfa is not None:
                    self._state["ttfa_ms"] = int(ttfa)
                self._post("seg", {"ev": "seg", "i": i, "text": seg, "sr": sr,
                                    "samples": int(pcm.size),
                                    "gap_after_ms": item["gap_after_ms"], "_gen": gen}, pcm)
            self._post("end", {"ev": "end", "_gen": gen})
        except Exception as e:
            self._post("err", "在线合成失败: %s" % e, gen)

    def _sing_segments(self, text: str, st: dict, lines: Optional[List[str]] = None) -> List[dict]:
        """把歌词拆成"能尽快起唱"的若干段：剥符号后的干净歌词 + 每段后面的呼吸时长。

        MiMo 唱歌是整段推理（实测 0.29 秒/字），整曲一次请求时 100 字要等 29 秒
        才出第一声，而且流式对唱歌只回一整块、完全不提速。所以按标点/停顿符号
        拆句、并行预取，让第一句先唱起来，后面的边唱边算。
        代价：每段都会自己"起唱"，旋律不保证跨段连续——用 sing_split_chars=0 可退回整曲一次。

        lines：原始歌词的行（调用方从换行切好传进来）。**行是最自然的音乐短语边界**，
        必须当硬边界用：否则 max_chars 打包会把"而我在等你"和"炊烟袅袅升起"接成一句，
        或者切在"炊烟袅袅｜升起"这种半句中间 —— 每多切一刀，模型就多一次"重新起调"，
        跑调就是这么来的。
        """
        from .segmenter_marks import punct_gap, split_head_chars, split_segments, split_segments_marked
        chunk = int(st.get("sing_split_chars") or 0)
        first = int(st.get("sing_first_chars") or 0)
        src_lines = [str(x).strip() for x in (lines or []) if str(x).strip()]
        if chunk <= 0:
            # 整曲一次也要把"行"留住：字幕在 _on_seg 里按行/标点拆句，
            # 换成空格会让整首歌词被当成一句（一次性全摊到屏幕上）。
            return [{"text": "\n".join(src_lines) if src_lines else text, "gap_after_ms": 0}]
        if not src_lines:
            src_lines = [text]
        segs: List[dict] = []
        for li, line in enumerate(src_lines):
            line_segs = split_segments_marked(
                line, use_marks=True, min_chars=4, max_chars=chunk,
                short_ms=int(self._c("pause_short_ms", 150)),
                mid_ms=int(self._c("pause_mid_ms", 300)),
                long_ms=int(self._c("pause_long_ms", 500)))
            if not line_segs:
                line_segs = [{"text": line, "gap_after_ms": 0}]
            for si, x in enumerate(line_segs):
                g = int(x.get("gap_after_ms") or 0)
                if si == len(line_segs) - 1 and li < len(src_lines) - 1:
                    # 换行 = 换句：至少留一个中停，别让两行黏成一句
                    g = max(g, int(self._c("pause_mid_ms", 300)))
                segs.append({"text": str(x["text"]), "gap_after_ms": g})
        if not segs:
            return [{"text": text, "gap_after_ms": 0}]
        # 首段再压短：首声延迟 ≈ 字数 × 0.35 秒，20 字 ≈ 7 秒；不压的话长歌词要十几秒才出声
        if first > 0 and len(str(segs[0]["text"])) > first:
            parts = split_segments(str(segs[0]["text"]), 2, first)
            if len(parts) <= 1:
                # 无标点的长句 split_segments 不会硬切（怕劈开词），这里直接按字数切
                parts = split_head_chars(str(segs[0]["text"]), first)
            if len(parts) > 1:
                g0 = int(segs[0].get("gap_after_ms") or 0)
                head = [{"text": p, "gap_after_ms": (punct_gap(p) if i < len(parts) - 1 else g0)}
                        for i, p in enumerate(parts)]
                rest = segs[1:]
                # 尾巴只有几个字时别让它单独起唱，接到下一句前面（歌词本来就是连着的）
                if len(head) >= 2 and len(head[-1]["text"]) < 6 and rest:
                    rest[0]["text"] = str(head[-1]["text"]) + str(rest[0]["text"])
                    head.pop()
                segs = head + rest
        segs[-1]["gap_after_ms"] = 0
        # 碎片合并：单独去唱"解渴。"这种两三个字的尾巴只会听着突兀，并回上一段
        merged: List[dict] = []
        for x in segs:
            t = str(x["text"])
            if merged and len(t) < 4 and len(merged[-1]["text"]) + len(t) <= chunk * 2:
                merged[-1]["text"] = merged[-1]["text"] + t
                merged[-1]["gap_after_ms"] = int(x.get("gap_after_ms") or 0)
            else:
                merged.append({"text": t, "gap_after_ms": int(x.get("gap_after_ms") or 0)})
        if merged:
            merged[-1]["gap_after_ms"] = 0
        return merged

    def _run_mimo(self, text: str, voice: str, speed: float = 1.0, gen: int = 0,
                  api_key: str = "", sing: bool = False, style: str = "",
                  dialect: str = "") -> None:
        """MiMo 在线合成：逐句请求、按序投递，最多 parallel 句并发预取。

        顺序必须严格按 segs 下标消费 —— 字幕时间轴是按投递顺序串起来的，
        "谁先合成完谁先播"会让字幕和声音错位。并发只用来提前把后面的句子算好。
        api_key 由 speak() 从工具参数带下来，空字符串表示走配置/环境变量。
        唱歌（sing 或文本已带 `(唱歌)`）：先剥掉插件自定义的停顿符号再拆句，
        按"歌词行 → 行内标点/字数"两级边界拆分（默认 28 字/段、首段 20 字）。
        """
        try:
            from concurrent.futures import ThreadPoolExecutor
            from .engine_mimo import is_singing, load_settings, strip_sing_tag, synth_segment
            from .segmenter_marks import split_segments_marked, strip_pause_marks

            st = load_settings(self.cfg)
            sr = int(st.get("sample_rate") or 48000)
            singing = bool(sing) or is_singing(text)
            self._singing = singing          # 供 _on_seg 判断"歌开口要收等待背景音"
            if singing:
                # 符号（~ | || |||）和 (唱歌) 标签只对合成/分段有意义，绝不能发给 TTS，
                # 更不能进字幕：在线模型自己按标点停顿，符号只会被当作怪字符
                clean = strip_pause_marks(strip_sing_tag(text))
                self._state["text"] = clean
                if not clean:
                    self._post("err", "歌词为空：剥掉停顿符号后没有可唱的内容", gen)
                    return
                # 排队预取命中：整首已经合成好，直接投递，不再请求一次 MiMo
                cached = self._take_song_cache(text, voice, speed, style, dialect)
                if cached is not None:
                    pcm = cached["pcm"]
                    psr = int(cached.get("sr") or sr)
                    self._state["text"] = str(cached.get("clean") or clean)
                    if self._state["ttfa_ms"] is None:
                        self._state["ttfa_ms"] = 0
                    self._post("seg", {"ev": "seg", "i": 0, "text": clean,
                                       "sr": psr, "samples": int(pcm.size),
                                       "gap_after_ms": 0,
                                       "synth_ms": round(float(cached.get("synth_ms") or 0), 1),
                                       "aligned": "estimated", "_gen": gen}, pcm)
                    self._post("end", {"ev": "end", "_gen": gen})
                    return
                # 先把"行"切出来（strip_pause_marks 会把换行抹成空格，所以要在原文上切），
                # 再交给分段器当硬边界用：行是最自然的音乐短语边界
                raw_lines = [ln for ln in str(text).replace("\r", "\n").split("\n") if ln.strip()]
                clean_lines = [strip_pause_marks(strip_sing_tag(ln)) for ln in raw_lines]
                clean_lines = [ln for ln in clean_lines if ln]
                segs = self._sing_segments(clean, st, lines=clean_lines)
            else:
                # 停顿符号先剥掉：MiMo 自己按标点停顿，符号不能念给用户听
                rows = split_segments_marked(
                    text, use_marks=True,
                    min_chars=int(self._c("min_chars", 6)),
                    max_chars=int(self._c("max_chars", 24)),
                    short_ms=int(self._c("pause_short_ms", 150)),
                    mid_ms=int(self._c("pause_mid_ms", 300)),
                    long_ms=int(self._c("pause_long_ms", 500)))
                segs = [{"text": str(x["text"]), "gap_after_ms": int(x.get("gap_after_ms") or 0)}
                        for x in rows]
            if not segs:
                self._post("err", "MiMo 分段为空", gen)
                return
            sr = int(st.get("sample_rate") or 48000)
            # 排队说话预取命中：段已在后台合成好，按 chunks 原样投递（字幕/口型/BGM 时间轴
            # 与真实流式逐字节同构），只是不再发网络请求——这是段间 gap 下降的来源。
            if (not singing) and bool(st.get("stream", True)):
                cached = self._take_speech_cache("mimo", voice, speed, style, dialect, text)
                if cached is not None:
                    c_segs = cached.get("segs") or []
                    # 段切分参数若被改过，缓存里的段数/文本与当前 segs 对不上就丢弃，
                    # 绝不能拿旧切分去投新文本（会串字幕）。
                    ok = (len(c_segs) == len(segs)
                          and all(str(a.get("text")) == str(b.get("text"))
                                  for a, b in zip(c_segs, segs)))
                    if ok:
                        # 不硬写 ttfa=0：_deliver_cached_speech 里首块入缓冲时按 (now-_t0) 填真实值
                        self._deliver_cached_speech(cached, gen)
                        return
                    # 对不上：落到正常流式重新合成（缓存已 pop，不会重复错配）
            # 说话走流式：边合成边出声（实测 40 字首块 1.32s，整包要等全部算完）。
            # 唱歌不能走流式：实测唱歌只回 1 块、首块时间等于总耗时，等于没流式。
            if (not singing) and bool(st.get("stream", True)):
                self._run_mimo_stream(segs, voice, speed, gen, api_key, st, style, dialect)
                return
            # 唱歌拆句后同样可以并行预取：首段先唱，后面几段在后台算，播到时就已就绪
            if singing:
                # 等待期垫场：先背景音 + 一句"清嗓子"配音（缓存 wav，不发额外请求）
                self._post("waitp", {"ev": "waitp", "_gen": gen,
                                     "bgm_on": bool(st.get("sing_wait_bgm", True)),
                                     "scene": str(st.get("sing_wait_scene") or "ambient"),
                                     "volume": float(st.get("sing_wait_volume") or 0.55),
                                     "voice_on": bool(st.get("sing_wait_voice", True)),
                                     "line": str(st.get("sing_wait_line") or ""),
                                     "voice": voice})
                # 唱歌没有任何中间进度可拿（流式也只回 1 块），只能在桌宠头顶按
                # "已过时间 × 每字耗时"折算百分比，让十几秒的等待有反馈。
                # 总字数取第一段：进度表达的是"第一声什么时候出来"。
                first = " ".join(str(segs[0]["text"]).split())
                self._post("prg", {"ev": "prg", "i": 0, "chars": max(1, len(first)),
                                   "done_chars": 0, "total_chars": max(1, len(first)),
                                   "ms_per_char": float(st.get("sing_ms_per_char") or 300.0),
                                   "t0": time.monotonic(), "text": "清嗓酝酿中", "_gen": gen})
            workers = max(1, min(4, int(st.get("parallel") or 2), len(segs)))
            pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
            try:
                futs = None
                if pool is not None:
                    futs = [pool.submit(synth_segment, s["text"], voice=voice, speed=speed,
                                        sr=sr, settings=st, api_key=api_key,
                                        sing=singing, style=style, dialect=dialect) for s in segs]
                for i, item in enumerate(segs):
                    if self._stop_flag.is_set():
                        return
                    seg = item["text"]
                    if futs is not None:
                        pcm, psr, _words, synth_ms = futs[i].result()
                    else:
                        pcm, psr, _words, synth_ms = synth_segment(
                            seg, voice=voice, speed=speed, sr=sr, settings=st,
                            api_key=api_key, sing=singing, style=style, dialect=dialect)
                    if self._state["ttfa_ms"] is None:
                        # 首句 TTFA 用真实挂钟（含排队），不用单次请求耗时糊弄
                        self._state["ttfa_ms"] = int((time.monotonic() - self._t0) * 1000)
                    self._post("seg", {"ev": "seg", "i": i, "text": seg, "sr": int(psr),
                                       "samples": int(pcm.size),
                                       "gap_after_ms": int(item.get("gap_after_ms") or 0),
                                       "synth_ms": round(float(synth_ms), 1),
                                       # MiMo 不返回逐字时间戳，字幕按字数均分
                                       "aligned": "estimated", "_gen": gen}, pcm)
                self._post("end", {"ev": "end", "_gen": gen})
            finally:
                if pool is not None:
                    # 被打断时取消还没发的句子，别让后台继续烧额度
                    try:
                        pool.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        pool.shutdown(wait=False)
        except Exception as e:
            self._post("err", "MiMo 合成失败: %s" % e, gen)

    def _run_mimo_stream(self, segs, voice, speed, gen, api_key, st, style="", dialect="") -> None:
        """说话走流式：MiMo 下发的 24kHz pcm16 块按顺序立刻入队，边算边播。

        为什么按块入队（而不是整句收完再入队）：实测 40 字首块 1.32s、音频 10.72s，
        整包非流式必须等全部算完才有声音。

        为什么要预取下一段（in_flight=parallel）：实测首块延迟在 0.73~7.66 秒之间抖，
        并非限速，而是服务端排队。串行逐段请求时，若后段正好碰上 7 秒的排队，
        上一段音频（约 4.8 秒）已经播完，就会断腔/欠载。所以当前段一开始就把它后面
        `parallel` 段也并发发出去，块先收进内存、按顺序投递 —— 后段的排队时间被藏在
        当前段的播放里。字幕时间轴仍严格按段序串，不会因为"谁先算完"错位。

        三个不能省的细节：
          * declick=False：每块都做 4ms 淡入淡出，会在 320ms 一块的接缝挖出空隙，
            听感就是"一段一段喘气"。淡入由每段首块自己做，收尾由流结束后补 4ms 降音。
          * lip 只在每段首块更新：否则每块都改一次口型响度基准，嘴型会跟着抖。
          * 块按段序投递：由本线程唯一负责，worker 线程只往队列里塞块。
        """
        from .engine_mimo import synth_stream
        sr = 24000          # 流式固定 pcm16 24kHz（文档要求），引擎按设备采样率自己重采样
        fade = max(1, int(sr * 0.004))
        in_flight = max(1, min(3, int(st.get("parallel") or 2)))
        qs: Dict[int, "queue.Queue"] = {}
        started = 0

        def _producer(k: int) -> None:
            q = qs[k]

            def _on(pcm, _q=q):
                x = np.asarray(pcm, dtype=np.int16)
                if x.size:
                    _q.put(("c", x))

            try:
                r = synth_stream(segs[k]["text"], voice=voice, speed=speed, sr=sr, settings=st,
                                 api_key=api_key, style=style, dialect=dialect, on_chunk=_on,
                                 should_stop=self._stop_flag.is_set)
                q.put(("e", r))
            except Exception as e:                  # 错误要能传回本线程，不能吞
                q.put(("x", str(e)))

        def _start(k: int) -> None:
            nonlocal started
            if k >= len(segs) or k in qs:
                return
            qs[k] = queue.Queue()
            th = threading.Thread(target=_producer, args=(k,), daemon=True)
            th.start()
            started = max(started, k + 1)

        try:
            for i, item in enumerate(segs):
                while started < min(len(segs), i + in_flight):
                    _start(started)
                if not qs.get(i):
                    _start(i)
                if self._stop_flag.is_set():
                    return
                text = item["text"]
                meta = {"key": "%d-%d" % (gen, i), "i": i, "text": text, "sr": sr,
                        "gap_ms": int(item.get("gap_after_ms") or 0), "_gen": gen}
                q = qs[i]
                state = {"first": True, "last": 0, "got": False}
                stats: Dict[str, object] = {}
                while True:
                    if self._stop_flag.is_set():
                        return
                    try:
                        kind, payload = q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if kind == "c":
                        x = payload
                        if state["first"]:
                            # 每段首块自己做 4ms 淡入（引擎的 declick 关掉了，见上面注释）
                            k = min(fade, x.size)
                            x = x.copy()
                            x[:k] = (x[:k].astype(np.float32)
                                     * np.linspace(0.0, 1.0, k, dtype=np.float32)).astype(np.int16)
                            state["first"] = False
                            if self._state.get("ttfa_ms") is None:
                                self._state["ttfa_ms"] = int((time.monotonic() - self._t0) * 1000)
                        state["last"] = int(x[-1])
                        state["got"] = True
                        self._post("sc", meta, x)
                    elif kind == "e":
                        stats = payload
                        break
                    else:
                        # 某一段合成失败：已经播出去的照播，这里明确报错并结束整句
                        self._post("err", "MiMo 流式合成失败: %s" % payload, gen)
                        return
                # 流结束：补一段 4ms 降音。否则最后一个非零样本直接切到静音/下一段，
                # 接缝处会"啪"一下（这是之前段接缝爆音的来源之一）。
                if state["got"] and abs(state["last"]) > 40:
                    ramp = np.linspace(float(state["last"]), 0.0, fade, dtype=np.float32)
                    self._post("sc", meta, np.clip(ramp, -32768, 32767).astype(np.int16))
                self._post("sed", dict(meta, samples=int(stats.get("samples") or 0),
                                       chunks=int(stats.get("chunks") or 0),
                                       first_chunk_ms=float(stats.get("first_chunk_ms") or 0.0)))
                if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                    print("[tts] stream seg%d %r chunks=%s first=%.0fms audio=%.2fs prefetch=%d" % (
                        i, text[:14], stats.get("chunks"),
                        float(stats.get("first_chunk_ms") or 0.0),
                        float(stats.get("samples") or 0) / float(sr), started - i - 1), flush=True)
            self._post("end", {"ev": "end", "_gen": gen})
        except Exception as e:
            self._post("err", "MiMo 流式合成失败: %s" % e, gen)

    # ------------------------------------------------------------ daemon
    def _ensure_proc(self) -> bool:
        """确保 daemon 进程活着（加锁，避免 warmup 与 speak 抢着各起一个）。"""
        with _DAEMON_LOCK:
            return self._ensure_proc_inner()

    def _ensure_proc_inner(self) -> bool:
        # 有端口且 ready 就直接复用。注意：绝不能靠 proc.poll() 判断存活——
        # _tts_env\Scripts\python.exe 是 launcher 型入口，它会再 exec 一个真解释器
        # 然后自己立刻退出，句柄一 poll 就是"已退出"；以前按它判断导致每次说话都
        # 重新拉一个 daemon（各背一份模型、各冷加载 70s），于是永远等不到首段声音。
        if self._daemon_port and self._daemon_ready:
            return True
        self._kill_daemon()
        py = venv_python()
        if not os.path.exists(py):
            self._post("err", "私有环境不存在：%s（先跑 tools\\tts_setup.py --install）" % py)
            return False
        model_dir = model_dir_from_config(self.cfg)
        args = [py, DAEMON, "--port", "0", "--parent-pid", str(os.getpid()),
                "--backend", str(self._c("engine", "piper")),
                "--piper-model", str(self._c("piper_model", "")),
                "--model-dir", model_dir, "--dtype", str(self._c("dtype", "bfloat16")),
                "--idle-unload", str(int(self._c("idle_unload_s", 120))),
                "--daemon-exit", str(int(self._c("daemon_exit_s", 600))),
                "--noise-scale", str(float(self._c("noise_scale", 0.10))),
                "--noise-w", str(float(self._c("noise_w", 0.10)))]
        try:
            log_dir = PLUGIN_DIR / "_workers"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            errf = open(str(log_dir / "tts_daemon.log"), "ab", buffering=0)
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errf,
                                    cwd=str(PLUGIN_DIR),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self._post("err", "daemon 启动失败: %s" % e)
            return False
        self._proc = proc
        deadline = time.monotonic() + float(self._c("daemon_start_timeout_s", 60))
        port = 0
        dpid = 0
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("READY "):
                info = json.loads(s[6:])
                port = int(info.get("port") or 0)
                dpid = int(info.get("pid") or 0)   # 真正解释器的 pid（不是 launcher）
                break
            if s.startswith("ERR "):
                self._post("err", "daemon 启动失败: %s" % s[4:])
                return False
        if not port:
            self._post("err", "daemon 未就绪（超时）")
            return False
        self._daemon_host = "127.0.0.1"
        self._daemon_port = port
        self._daemon_pid = dpid
        self._daemon_ready = True
        return True

    def unload(self) -> Dict[str, object]:
        """关掉 daemon（释放 2.2GB 显存），下次 speak 会自动再拉起来。"""
        self.stop(notify_pet=False)
        self._kill_daemon(graceful=True)
        if self._engine is not None:
            try:
                self._engine.reset()
            except Exception:
                pass
        return {"ok": True, "action": "tts_unload", "daemon": False}

    # ------------------------------------------------------------ 停/结束
    def stop(self, notify_pet: bool = True, fade_ms: int = 0,
             keep_pending: bool = False, keep_speech_cache: bool = False) -> Dict[str, object]:
        self._stop_flag.set()
        with self._lock:
            self._tl = []
            self._state["speaking"] = False
        if self._engine is not None:
            try:
                self._engine.reset()
            except Exception:
                pass
        # 打断/切句：微动作也跟着缓出，别让身体停在半弯腰的位置
        try:
            self._motion.stop(time.monotonic())
            self._motion_ensure()
        except Exception:
            pass
        if notify_pet and self.pet is not None:
            try:
                self.pet.sub_karaoke("", 0, secs=0)
            except Exception:
                pass
        self._prog = None          # 进度气泡由主线程 tick 在 300ms 内收掉（定时器不能跨线程停）
        self._stop_wait_bgm(200)   # 打断时把等待期背景音一起收掉
        self._wait_sub = None      # 说明句字幕也撤掉
        if not keep_pending:
            try:
                self._queue_timer.stop()
                self._queue_pending = None
            except Exception:
                pass
            with self._lock:
                self._pending = []     # 用户打断就是全停：排队的也一起清掉
            # 说话预取缓存也要作废：否则 stop 后用户再说同一句话，
            # 可能命中打断前遗留的旧音频（唱歌缓存另有保留原因，见下方注释）。
            # epoch 递增同时挡住“刚好越过 stop_flag 检查”的旧预取线程回写。
            # keep_speech_cache=True 用在“跨模型预合成后真正开播”：此时缓存
            # 对应的是马上要播的新句子，不能把它当成打断遗留清掉。
            if not keep_speech_cache:
                with self._speech_cache_lock:
                    self._speech_epoch += 1
                    self._speech_cache.clear()
        # 注意：不能清 _song_cache。_start_next_pending 起下一条时会走 speak(mode=replace)
        # → stop()，如果这里把预取音频清掉，排队唱歌就永远命不中缓存，只能重新请求，
        # 表现为"第二条要等十几秒才出声"。缓存键包含歌词/音色/风格，复用时不会串歌。
        try:
            if self._engine is not None:
                self._engine.stop_wait_voice(0)     # 等待配音立即清掉（打断不该还在念）
        except Exception:
            pass
        return {"ok": True, "action": "stop_speak"}

    def _tl_append_song(self, text: str, start_ms: int, speech_ms: int) -> dict:
        """唱歌：一段音频 → 多句字幕（每句只显示自己那句，逐字高亮按句内进度）。

        MiMo 不返回逐字/逐句时间戳，按"各句字数占比 × 本段真实时长"切分，误差只在
        句界上（曲中换句的呼吸处），不会出现"整首歌词一起显示、颜色跑到还没唱到的地方"。
        """
        parts = split_sentences(text) or [text]
        total = sum(max(1, len(p)) for p in parts)
        base = int(start_ms)
        left = int(speech_ms)
        last = None
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                ms = max(1, left)
            else:
                ms = max(1, int(round(speech_ms * (max(1, len(part)) / float(total)))))
                ms = min(ms, max(1, left - (len(parts) - 1 - i)))
                left -= ms
            last = self._tl_append(part, base, ms, 0)
            base += ms
        return last

    def _tl_append(self, text: str, start_ms: int, speech_ms: int, gap_ms: int = 0) -> dict:
        """登记一段字幕时间轴，返回条目本身（流式路径收完后要回来改时长）。

        start_ms：这段语音在整句里的绝对起点；speech_ms：真实语音长度（不含段尾静音）；
        gap_ms：段尾补的静音。逐字高亮只铺在真实语音上，静音不算进语速。
        """
        text = " ".join(str(text or "").split())
        marks = char_marks(text, int(speech_ms)) if text else []
        # 台词动画标记定位：按"内容位"挂段（pos 在 [base, base+本段内容位] 内），
        # 段内再把内容位换算成字符下标，所以分段切在哪、锚点是否被切断都不影响。
        anim_for_seg = []
        base = int(getattr(self, "_anim_base", 0) or 0)
        seg_content = content_index(text)
        for m in self._anim_marks:
            if not m.get("name") or m.get("at") is not None:
                continue
            pos = m.get("pos")
            if pos is None:                       # 旧数据兜底：按锚点子串找
                anchor = str(m.get("anchor") or "")
                at = -1
                for L in (len(anchor), 3, 2):
                    if L <= 1:
                        break
                    at = text.find(anchor[:L])
                    if at >= 0:
                        break
            else:
                at = -1
                if base <= int(pos) <= base + seg_content:
                    at = char_at_content(text, int(pos) - base)
            if at >= 0 and text:
                m["at"] = int(at)
                with self._lock:
                    m["_seg"] = len(self._tl)
                anim_for_seg.append({"name": str(m["name"]),
                                     "at": int(at), "done": False})
        self._anim_base = base + seg_content
        entry = {
            "text": text,
            "start_ms": int(start_ms),
            "end_ms": int(start_ms) + int(speech_ms) + int(gap_ms),
            "marks": marks,
            "chars": len(text),
            "anim": anim_for_seg,
        }
        with self._lock:
            self._tl.append(entry)
            self._state["segments"] = len(self._tl)
        return entry

    def _tl_refresh(self, entry: dict, speech_ms: int, gap_ms: int = 0) -> None:
        """流式段落收完后按真实长度修正时长与逐字时间。"""
        if not entry:
            return
        entry["end_ms"] = int(entry["start_ms"]) + int(speech_ms) + int(gap_ms)
        entry["marks"] = char_marks(str(entry.get("text") or ""), int(speech_ms))

    def _on_prog(self, meta: dict) -> None:
        """worker 开始"整段推理"前投递的进度起点：唱歌要十几~几十秒才出第一声。

        MiMo 唱歌不返回任何中间进度，只能按"已过时间 × 每字耗时"折算百分比；
        每字耗时取实测 0.29~0.37 秒的中位 300ms（`online.mimo.sing_ms_per_char`）。
        """
        try:
            if int(meta.get("_gen", self._gen)) != self._gen:
                return
            self._prog = dict(meta)
            self._prog_tick()              # 立刻显示，不等 300ms 后第一次 tick
            if not self._prog_timer.isActive():
                self._prog_timer.start()
        except Exception as e:
            self._state["error"] = "进度提示失败: %s" % e

    def _prog_tick(self) -> None:
        """刷新气泡：百分比 =（已完成字数 + 本段已折算字数）/ 总字数。"""
        p = self._prog
        if not p:
            try:
                if self._prog_timer.isActive():
                    self._prog_timer.stop()
                if self.pet is not None:
                    self.pet.tts_progress("", None)
            except Exception:
                pass
            return
        try:
            total = int(p.get("total_chars") or 0)
            done0 = float(p.get("done_chars") or 0)
            chars = int(p.get("chars") or 0)
            ms_per = max(50.0, float(p.get("ms_per_char") or 300.0))
            t0 = float(p.get("t0") or time.monotonic())
            elapsed_ms = max(0.0, (time.monotonic() - t0) * 1000.0)
            done = done0 + min(float(chars), elapsed_ms / ms_per)
            ratio = min(0.99, done / total) if total > 0 else None
            if self.pet is not None:
                self.pet.tts_progress(str(p.get("text") or "合成中"), ratio)
        except Exception as e:
            self._state["error"] = "进度刷新失败: %s" % e

    def _prog_hide(self) -> None:
        """收起进度气泡：第一段音频到达 / 播完 / 出错 / 被 stop 都走这里。

        只从主线程碰 QTimer 与桌宠（Qt 定时器不能跨线程启停）；不在这条线程时
        只清掉 `_prog`，下一次 tick（≤300ms）会自己收尾。
        """
        self._prog = None
        try:
            if threading.current_thread() is threading.main_thread():
                if self._prog_timer.isActive():
                    self._prog_timer.stop()
                if self.pet is not None:
                    self.pet.tts_progress("", None)
        except Exception:
            pass

    # ------------------------------------------------------------ 唱歌等待垫场
    def _wait_line(self, line: str = "") -> str:
        """等待说明句的文本（字幕用）：优先请求里给的，其次配置默认。"""
        text = " ".join(str(line or "").split())
        if not text:
            try:
                text = " ".join(str((self._mimo_cfg() or {}).get("sing_wait_line") or "").split())
            except Exception:
                text = ""
        return text

    def _wait_line_path(self, voice: str) -> Optional[str]:
        """等待配音缓存路径：_tts/sing_wait/<音色>.wav（tools/bake_sing_wait.py 生成）。"""
        try:
            from pathlib import Path
            root = Path(__file__).resolve().parents[1]
            name = "".join(c for c in str(voice or "default") if c not in '\\/:*?"<>|').strip() or "default"
            p = root / "_tts" / "sing_wait" / (name + ".wav")
            return str(p) if p.is_file() else None
        except Exception:
            return None

    def _load_wait_pcm(self, voice: str):
        """读缓存 wav → (int16 pcm, sr)；没有缓存就返回 (None, 0)（只放背景音）。"""
        p = self._wait_line_path(voice)
        if not p:
            return None, 0
        try:
            import soundfile as sf
            data, sr = sf.read(p, dtype="int16", always_2d=False)
            if getattr(data, "ndim", 1) > 1:
                data = data[:, 0]
            return np.ascontiguousarray(data), int(sr)
        except Exception as e:
            self._state["error"] = "等待配音读取失败: %s" % e
            return None, 0

    def _on_waitp(self, meta: dict) -> None:
        """唱歌等待期：先垫背景音，再播一句"清嗓子"配音。

        整曲一次要等 15~30 秒才出第一声，这段时间如果没有声音，用户会以为卡死。
        - 背景音用 `mode="always"`：人声没到时设备不 suspend，音乐才不会被"欠载"掐掉；
        - 等待配音读缓存 wav（不额外发请求、不拖慢唱歌请求），没有缓存就只放音乐；
        - 歌一开口（第一段 seg）就 `fade_out_bgm` 收掉，不在旋律上叠音乐。
        """
        try:
            if int(meta.get("_gen", self._gen)) != self._gen or self._engine is None:
                return
            dbg = bool(os.environ.get("DESKTOP_PET_TTS_DEBUG"))
            # 调用方自己传了 bgm：尊重它，不再叠等待音乐
            if bool(meta.get("bgm_on", True)) and not self._user_bgm:
                arr = resolve_bgm(str(meta.get("scene") or "ambient"),
                                  sr=int(self._c("sample_rate", 48000)))
                if arr is not None:
                    self._engine.set_bgm(arr, volume=float(meta.get("volume") or 0.55),
                                         duck=float(self._c("duck", 0.60)), mode="always",
                                         preroll_ms=0, tail_ms=0, fade_ms=500)
                    self._wait_bgm_on = True
                    if dbg:
                        print("[tts] wait bgm scene=%s vol=%.2f" % (
                            meta.get("scene"), float(meta.get("volume") or 0.55)), flush=True)
            if bool(meta.get("voice_on", True)):
                voice = str(meta.get("voice") or "")
                pcm, wsr = self._load_wait_pcm(voice)
                if pcm is not None and getattr(pcm, "size", 0):
                    # 独立声道循环播放：不进时间轴（它不属于歌词），也不会把歌顶后
                    unit_ms = self._engine.set_wait_voice(pcm, src_sr=wsr, gap_ms=0, loop=False)
                    line = self._wait_line(str(meta.get("line") or ""))
                    if line:
                        # 说明句也有字幕：跟它自己的播放头对时，念完就撤
                        self._wait_sub = {
                            "text": line,
                            "marks": char_marks(line, max(1, int(unit_ms))),
                            "end_ms": int(unit_ms),
                            "line": line,
                        }
                    if self._state.get("lip_sync") and self.pet is not None:
                        try:
                            self.pet.tts_lip_start(pcm, wsr)
                        except Exception:
                            pass
                    if dbg:
                        print("[tts] wait voice %s %.2fs（只念一遍 %.2fs，念完背景音继续）" % (
                            voice, pcm.size / max(1, wsr), unit_ms / 1000.0), flush=True)
                elif dbg:
                    print("[tts] wait voice 无缓存（%s），只放背景音" % voice, flush=True)
        except Exception as e:
            self._state["error"] = "等待垫场失败: %s" % e

    def _stop_wait_bgm(self, ms: int = 600) -> None:
        """歌开口 / 出错 / 播完：把等待期背景音淡出收掉（用户自己传的 bgm 不动）。"""
        if not self._wait_bgm_on:
            return
        self._wait_bgm_on = False
        try:
            if self._engine is not None:
                self._engine.fade_out_bgm(int(ms))
        except Exception:
            pass

    def _on_seg(self, meta: dict, pcm) -> None:
        if self._stop_flag.is_set() or self._engine is None:
            if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                print("[tts] seg REJECT stop=%s eng=%s" % (
                    self._stop_flag.is_set(), self._engine is not None), flush=True)
            return
        # 上一句被 replace 掉后，它的线程可能还在吐段；只接受当前会话的段
        if int(meta.get("_gen", self._gen)) != self._gen:
            if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                print("[tts] seg REJECT gen meta=%s self=%s" % (
                    meta.get("_gen"), self._gen), flush=True)
            return
        dbg = bool(os.environ.get("DESKTOP_PET_TTS_DEBUG"))
        self._prog_hide()            # 第一段音频到了：合成的等待结束
        self._wait_sub = None        # 说明句字幕让位给歌词
        if self._singing:
            self._stop_wait_bgm()    # 歌开口：等待期背景音淡出，别在旋律上叠音乐
            try:
                self._engine.stop_wait_voice(300)   # 循环的"清嗓子"也一起淡出
            except Exception:
                pass
        try:
            sr = int(meta.get("sr") or 24000)
            if dbg:
                print("[tts] seg in text=%r samples=%s sr=%s gap=%s" % (
                    meta.get("text"), getattr(pcm, "size", -1), sr, meta.get("gap_after_ms")), flush=True)
            # 段尾补静音：Piper 段尾自带静音会被 trim 掉，段与段原本是 0 静音硬接，
            # 于是「句尾降调」直接黏到下一段起音 —— 听感就是一句话被砍成几句。
            # 这里按标点/符号解析出的 gap_after_ms 精确补零，停顿变可控。
            speech_ms = int(round(pcm.size * 1000.0 / max(1, sr))) if pcm is not None else 0
            gap_ms = int(meta.get("gap_after_ms") or 0)
            pcm_out = pcm
            if gap_ms > 0 and pcm is not None and pcm.size:
                pad = np.zeros(int(sr * gap_ms / 1000.0), dtype=np.int16)
                pcm_out = np.concatenate([pcm, pad])
            seg = self._engine.enqueue(pcm_out, {"text": meta.get("text", ""), "sr": sr}, src_sr=sr,
                                       lead_ms=int(self._c("preroll_ms", 1200)))
            if dbg:
                print("[tts] enqueued start=%s end=%s" % (seg.get("start_ms"), seg.get("end_ms")), flush=True)
            if self._singing:
                # 唱歌是整曲一次（一个 seg），但字幕要"一句一句"：按标点把这一段的字幕
                # 时间轴拆成多句，时长按字数比例分配。音频不拆，旋律才连贯。
                entry = self._tl_append_song(str(meta.get("text") or ""),
                                             int(seg["start_ms"]), speech_ms)
            else:
                entry = self._tl_append(str(meta.get("text") or ""), int(seg["start_ms"]),
                                        speech_ms, gap_ms)
            if dbg:
                print("[tts] dur=%d speech=%d gap=%d chars=%d marks=%d" % (
                    int(seg["dur_ms"]), speech_ms, gap_ms, entry["chars"],
                    len(entry["marks"])), flush=True)
                print("[tts] tl=%d lip=%s" % (len(self._tl), self._state.get("lip_sync")), flush=True)
            # 每段开头点名：段落起点点头，句子边界换重心（幅度标准档）
            if self._motion_ok:
                try:
                    self._motion.on_segment(max(0, len(self._tl) - 1), gap_ms, time.monotonic())
                except Exception:
                    pass
            if self._state.get("lip_sync") and self.pet is not None:
                try:
                    self.pet.tts_lip_start(pcm, sr)
                except Exception:
                    pass
            if dbg:
                print("[tts] seg done", flush=True)
        except Exception as e:
            self._state["error"] = "入队失败: %s" % e

    def _on_sc(self, meta: dict, pcm) -> None:
        """流式音频块入队。

        首块：建字幕条目（时长先按字数估，收完再修正），并定口型基准、做段落动作；
        后续块：只累计真实长度，把条目 end_ms 往上顶（否则 remain 会算成负数，
        字幕"剩余时间"会抖）。
        """
        if self._stop_flag.is_set() or self._engine is None:
            return
        if int(meta.get("_gen", self._gen)) != self._gen:
            return
        dbg = bool(os.environ.get("DESKTOP_PET_TTS_DEBUG"))
        try:
            sr = int(meta.get("sr") or 24000)
            key = str(meta.get("key") or "")
            x = np.asarray(pcm, dtype=np.int16)
            first = key not in self._stream_seg
            seg = self._engine.enqueue(x if x.size else np.zeros(0, np.int16),
                                       {"text": meta.get("text", "")}, src_sr=sr,
                                       declick=False, lip=first)
            if first and self._state.get("ttfa_ms") is None:
                self._state["ttfa_ms"] = int((time.monotonic() - self._t0) * 1000)
            st = self._stream_seg.get(key)
            if st is None:
                text = str(meta.get("text") or "")
                est_per = float(self._mimo_cfg().get("stream_est_ms_per_char") or 260)
                est = max(400, int(len(text) * est_per))
                entry = self._tl_append(text, int(seg["start_ms"]), est, 0)
                st = {"entry": entry, "speech_ms": 0, "gap_ms": int(meta.get("gap_ms") or 0)}
                self._stream_seg[key] = st
                if dbg:
                    print("[tts] sc first key=%s est=%dms start=%d" % (
                        key, est, int(seg["start_ms"])), flush=True)
                if self._motion_ok:
                    try:
                        self._motion.on_segment(max(0, len(self._tl) - 1),
                                                int(meta.get("gap_ms") or 0), time.monotonic())
                    except Exception:
                        pass
                if self._state.get("lip_sync") and self.pet is not None:
                    try:
                        self.pet.tts_lip_start(x, sr)
                    except Exception:
                        pass
            st["speech_ms"] = int(st.get("speech_ms") or 0) + int(round(x.size * 1000.0 / max(1, sr)))
            entry = st["entry"]
            entry["end_ms"] = max(int(entry["end_ms"]),
                                  int(entry["start_ms"]) + int(st["speech_ms"]) + int(st["gap_ms"]))
        except Exception as e:
            self._state["error"] = "流式入队失败: %s" % e

    def _on_sed(self, meta: dict) -> None:
        """一段流式语音结束：按真实长度修正字幕，并补段尾静音。"""
        key = str(meta.get("key") or "")
        st = self._stream_seg.pop(key, None)
        if not st:
            return
        sr = int(meta.get("sr") or 24000)
        gap = int(st.get("gap_ms") or 0)
        try:
            if gap > 0 and self._engine is not None:
                self._engine.enqueue(np.zeros(int(sr * gap / 1000.0), dtype=np.int16),
                                     {"text": ""}, src_sr=sr, declick=False, lip=False)
            self._tl_refresh(st["entry"], int(st.get("speech_ms") or 0), gap)
            if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                print("[tts] sed key=%s speech=%sms gap=%sms" % (
                    key, st.get("speech_ms"), gap), flush=True)
        except Exception as e:
            self._state["error"] = "流式收尾失败: %s" % e


    def _on_end(self, summary: dict) -> None:
        self._prog_hide()
        self._stop_wait_bgm(300)
        try:
            if self._engine is not None:
                self._engine.stop_wait_voice(0)
        except Exception:
            pass
        if int((summary or {}).get("_gen", self._gen)) != self._gen:
            if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                print("[tts] end REJECT gen meta=%s self=%s" % (
                    summary.get("_gen"), self._gen), flush=True)
            return
        if self._engine is not None and not self._stop_flag.is_set():
            try:
                self._engine.mark_eof()
            except Exception:
                pass

    def _song_cache_key(self, text: str, voice: str, speed: float, style: str, dialect: str) -> Tuple:
        """排队唱歌预取的键：同一段歌词/音色/指令才会复用，避免拿错音频。"""
        try:
            from .engine_mimo import strip_sing_tag
            from .segmenter_marks import strip_pause_marks
            clean = strip_pause_marks(strip_sing_tag(str(text or "")))
        except Exception:
            clean = str(text or "")
        return (clean, str(voice or ""), round(float(speed or 1.0), 3),
                str(style or ""), str(dialect or ""))

    def _start_song_prefetch(self, text: str, voice: str, speed: float,
                             api_key: str, style: str, dialect: str) -> None:
        """排队唱歌时立刻在后台合成整首，等第一条播完可直接投递。"""
        key = self._song_cache_key(text, voice, speed, style, dialect)
        with self._song_cache_lock:
            if key in self._song_cache or key in self._song_prefetching:
                return
            self._song_prefetching.add(key)
        th = threading.Thread(
            target=self._prefetch_song,
            args=(key, str(text or ""), str(voice or ""), float(speed or 1.0),
                  str(api_key or ""), str(style or ""), str(dialect or "")),
            daemon=True)
        th.start()

    def _prefetch_song(self, key: Tuple, text: str, voice: str, speed: float,
                       api_key: str, style: str, dialect: str) -> None:
        try:
            from .engine_mimo import load_settings, strip_sing_tag, synth_segment
            from .segmenter_marks import strip_pause_marks
            st = load_settings(self.cfg)
            clean = strip_pause_marks(strip_sing_tag(text))
            pcm, psr, _words, synth_ms = synth_segment(
                clean, voice=voice, speed=float(speed or 1.0),
                sr=int(st.get("sample_rate") or 48000), settings=st,
                api_key=api_key, sing=True, style=style, dialect=dialect)
            with self._song_cache_lock:
                self._song_cache[key] = {
                    "pcm": pcm, "sr": int(psr), "synth_ms": float(synth_ms),
                    "clean": clean, "text": text, "ts": time.time(),
                }
        except Exception as e:
            with self._song_cache_lock:
                self._song_cache[key] = {"error": str(e)}
        finally:
            with self._song_cache_lock:
                self._song_prefetching.discard(key)

    def _take_song_cache(self, text: str, voice: str, speed: float,
                         style: str, dialect: str) -> Optional[Dict[str, object]]:
        key = self._song_cache_key(text, voice, speed, style, dialect)
        with self._song_cache_lock:
            item = self._song_cache.pop(key, None)
        if not item or "pcm" not in item:
            return None
        return item

    # ------------------------------------------------------------ 排队说话预取（depth=1）
    def _normalize_speak_text(self, raw: str) -> str:
        """把原始台词清洗成 _run_mimo 实际收到的 txt（必须与 speak() 开头一致）。"""
        r = str(raw or "").replace("\r", "\n")
        t = "\n".join(" ".join(ln.split()) for ln in r.split("\n") if ln.strip())
        try:
            t, _marks = parse_anim_markers(t)
        except Exception:
            pass
        return t

    def _speech_cache_key(self, backend: str, voice: str, speed: float,
                          style: str, dialect: str, text: str) -> Tuple:
        """说话预取键：backend/voice/speed/style/dialect/text 缺一维就会串音色或风格。"""
        return (str(backend or ""), str(voice or ""), round(float(speed or 1.0), 3),
                str(style or ""), str(dialect or ""), str(text or ""))

    def _speech_prefetch_on(self) -> bool:
        try:
            return bool(self._mimo_cfg().get("speech_prefetch_enabled", True))
        except Exception:
            return True

    def _start_speech_prefetch(self, txt: str, backend: str, voice: str,
                               speed: float, api_key: str, style: str, dialect: str) -> None:
        """后台把"下一条要说的 mimo 说话"完整合成好（按段切分 + 流式 chunk 原样收集）。

        绝不调用 _post / engine / pet 回调——只在自己线程里跑 synth_stream，结果只入字典。
        命中时由 _deliver_cached_speech 走正常 sc/sed/end 链路投递，字幕/口型/BGM 时间轴不变。
        """
        if not txt or backend != "mimo" or not self._speech_prefetch_on():
            return
        key = self._speech_cache_key(backend, voice, speed, style, dialect, txt)
        with self._speech_cache_lock:
            if key in self._speech_cache or key in self._speech_prefetching:
                return
            self._speech_prefetching.add(key)
            epoch = int(self._speech_epoch)
        th = threading.Thread(
            target=self._prefetch_speech,
            args=(key, txt, voice, float(speed or 1.0), str(api_key or ""),
                  str(style or ""), str(dialect or ""), epoch),
            daemon=True)
        th.start()

    def _prefetch_speech(self, key: Tuple, txt: str, voice: str, speed: float,
                         api_key: str, style: str, dialect: str, epoch: int) -> None:
        try:
            from .engine_mimo import load_settings, synth_stream
            from .segmenter_marks import split_segments_marked
            st = load_settings(self.cfg)
            # 段切分必须与 _run_mimo 里完全一致（min/max_chars、pause_ms 都来自同一份配置），
            # 否则命中后投递的段数/文本与字幕时间轴对不上。
            rows = split_segments_marked(
                txt, use_marks=True,
                min_chars=int(self._c("min_chars", 6)),
                max_chars=int(self._c("max_chars", 24)),
                short_ms=int(self._c("pause_short_ms", 150)),
                mid_ms=int(self._c("pause_mid_ms", 300)),
                long_ms=int(self._c("pause_long_ms", 500)))
            segs = [{"text": str(x["text"]), "gap_after_ms": int(x.get("gap_after_ms") or 0)}
                    for x in rows]
            if not segs:
                return
            sr = 24000
            # 与 _run_mimo_stream 同样的 in_flight 并发（不调高，只是不串行）：
            # 一段在收 chunks 时后段已经在首块延迟里排队，预取总时长 ≈ 最长一段而非累加。
            inflight = max(1, min(3, int(st.get("parallel") or 2)))
            qs: Dict[int, "queue.Queue"] = {}
            def _start_p(k: int):
                if k in qs:
                    return
                q: "queue.Queue" = queue.Queue()
                qs[k] = q
                seg = segs[k]
                def _on(pcm, _q=q):
                    x = np.asarray(pcm, dtype=np.int16)
                    if x.size:
                        _q.put(("c", x))
                def _produce():
                    try:
                        r = synth_stream(seg["text"], voice=voice, speed=speed, sr=sr,
                                         settings=st, api_key=api_key, style=style,
                                         dialect=dialect, on_chunk=_on,
                                         should_stop=self._stop_flag.is_set)
                        q.put(("e", r))
                    except Exception as e:
                        q.put(("x", str(e)))
                threading.Thread(target=_produce, daemon=True).start()
            for k in range(min(len(segs), inflight)):
                _start_p(k)
            all_chunks: List[List[np.ndarray]] = []
            for i in range(len(segs)):
                if i + inflight < len(segs):
                    _start_p(i + inflight)
                if self._stop_flag.is_set():
                    return
                q = qs[i]
                chunks: List[np.ndarray] = []
                while True:
                    if self._stop_flag.is_set():
                        return
                    try:
                        kind, payload = q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if kind == "c":
                        chunks.append(payload)
                    elif kind == "e":
                        break
                    else:
                        self._state["prefetch_error"] = str(payload)[:160]
                        return                  # 某段合成失败：整条丢弃
                all_chunks.append(chunks)
            if self._stop_flag.is_set():
                return                          # 写完前又被打断：丢弃
            # TTL + 上限淘汰
            now = time.time()
            ttl = float(self._mimo_cfg().get("speech_prefetch_ttl_s", 300) or 300)
            max_n = int(self._mimo_cfg().get("speech_prefetch_max_entries", 4) or 4)
            with self._speech_cache_lock:
                # stop/replace 后旧线程不能回写；epoch 比 stop_flag 更可靠，
                # 因为下一条 speak 会重新 clear() stop_flag。
                if epoch != self._speech_epoch or self._stop_flag.is_set():
                    return
                expired = [k for k, v in self._speech_cache.items()
                           if now - float(v.get("ts") or 0) > ttl]
                for k in expired:
                    self._speech_cache.pop(k, None)
                while len(self._speech_cache) >= max_n:
                    oldest = min(self._speech_cache,
                                 key=lambda k: float(self._speech_cache[k].get("ts") or 0))
                    self._speech_cache.pop(oldest, None)
                self._speech_cache[key] = {
                    "segs": segs, "chunks": all_chunks, "sr": sr,
                    "ts": now, "text": txt, "epoch": epoch,
                }
        except Exception as e:
            try:
                self._state["prefetch_error"] = str(e)
            except Exception:
                pass
        finally:
            with self._speech_cache_lock:
                self._speech_prefetching.discard(key)

    def _take_speech_cache(self, backend: str, voice: str, speed: float,
                           style: str, dialect: str, txt: str
                           ) -> Optional[Dict[str, object]]:
        key = self._speech_cache_key(backend, voice, speed, style, dialect, txt)
        with self._speech_cache_lock:
            item = self._speech_cache.pop(key, None)
        if not item or "chunks" not in item:
            return None
        # 读取时也校验 TTL：排队项可能等了很久，不能让过期音频被重放。
        try:
            ttl = float(self._mimo_cfg().get("speech_prefetch_ttl_s", 300) or 300)
            if ttl > 0 and time.time() - float(item.get("ts") or 0) > ttl:
                return None
        except Exception:
            pass
        return item

    def _deliver_cached_speech(self, cached: Dict[str, object], gen: int) -> None:
        """命中预取：按与 _run_mimo_stream 完全一致的事件序列投递（sc 首块→后续块→sed→…→end）。

        这样 _on_sc 会建字幕条目/定口型基准/入队，_on_sed 按真实长度修正字幕并补 gap 静音，
        _on_end mark_eof——跟真实流式逐字节同构，只是不再发网络请求。
        """
        sr = int(cached.get("sr") or 24000)
        segs = cached.get("segs") or []
        chunks = cached.get("chunks") or []
        if not segs or len(segs) != len(chunks):
            return
        fade = max(1, int(sr * 0.004))
        for i, seg in enumerate(segs):
            if self._stop_flag.is_set():
                return
            text = str(seg.get("text") or "")
            gap_ms = int(seg.get("gap_after_ms") or 0)
            meta = {"key": "%d-%d" % (gen, i), "i": i, "text": text, "sr": sr,
                    "gap_ms": gap_ms, "_gen": gen}
            state = {"first": True, "last": 0, "got": False}
            n_samples = 0
            for x in chunks[i]:
                if self._stop_flag.is_set():
                    return
                x = np.asarray(x, dtype=np.int16)
                if x.size == 0:
                    continue
                if state["first"]:
                    k = min(fade, x.size)
                    x = x.copy()
                    x[:k] = (x[:k].astype(np.float32)
                             * np.linspace(0.0, 1.0, k, dtype=np.float32)).astype(np.int16)
                    state["first"] = False
                state["last"] = int(x[-1])
                state["got"] = True
                n_samples += int(x.size)
                self._post("sc", meta, x)
            if state["got"] and abs(state["last"]) > 40:
                ramp = np.linspace(float(state["last"]), 0.0, fade, dtype=np.float32)
                self._post("sc", meta, np.clip(ramp, -32768, 32767).astype(np.int16))
            self._post("sed", dict(meta, samples=n_samples, chunks=len(chunks[i]),
                                   first_chunk_ms=0.0))
        self._post("end", {"ev": "end", "_gen": gen})

    def _maybe_prefetch_next(self) -> None:
        """队列里新的第一条（刚被弹走后剩下的）若还是 mimo 说话，立即后台预取。"""
        if not self._speech_prefetch_on():
            return
        with self._lock:
            if not self._pending:
                return
            nxt = self._pending[0]
        if bool(nxt.get("sing")):
            return                                  # 唱歌预取走 _start_song_prefetch
        backend = self._normalize_backend(nxt.get("backend") or "")
        if backend != "mimo":
            return                                  # edge/local 不做预取适配
        raw = nxt.get("text") or ""
        txt = self._normalize_speak_text(raw)
        if not txt:
            return
        voice = self._resolve_voice(backend, nxt.get("voice") or "")
        try:
            self._start_speech_prefetch(
                txt, backend, voice, float(nxt.get("speed") or 1.0),
                str(nxt.get("api_key") or ""),
                str(nxt.get("style") or ""), str(nxt.get("dialect") or ""))
        except Exception:
            pass

    def _wait_seconds(self, value) -> float:
        """wait 参数：秒，0=不等待，上限 30；非法值按 0。"""
        try:
            sec = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(30.0, sec))

    def _start_next_pending(self) -> bool:
        """当前这句/这首播完：把排队里的下一条起来（没有就返回 False）。"""
        with self._lock:
            if not self._pending:
                return False
            item = self._pending.pop(0)
        # 刚弹出一条开始播：队列里若还有新的第一条，立即后台预取它（depth=1）。
        # 刚弹出的这条可能已命中缓存（_take_speech_cache 在 speak 里 pop），这里不影响。
        self._maybe_prefetch_next()
        wait_s = self._wait_seconds(item.get("wait"))
        if wait_s > 0:
            # 上一条已经播完，再等 N 秒才起下一条；用 Qt 定时器，不阻塞主线程
            self._queue_pending = item
            self._queue_timer.start(int(wait_s * 1000))
            return True
        return self._start_pending_item(item)

    def _start_pending_item(self, item: Dict[str, object]) -> bool:
        try:
            self.speak(_from_queue=True, **item)
            return True
        except Exception as e:
            self._state["error"] = "排队内容启动失败: %s" % e
            return False

    def _start_delayed_pending(self) -> None:
        item = self._queue_pending
        self._queue_pending = None
        if item:
            self._start_pending_item(item)

    def _on_err(self, msg: str, gen: int = -1) -> None:
        if gen >= 0 and gen != self._gen:
            return
        self._prog_hide()          # 合成失败：把"清嗓酝酿中 xx%"收掉，改显示错误
        self._stop_wait_bgm(300)
        try:
            if self._engine is not None:
                self._engine.stop_wait_voice(0)
        except Exception:
            pass
        self._state["error"] = msg
        self._state["speaking"] = False
        # 合成失败时也别让嘴停在张开状态（在线 403/超时后最容易看到）
        if self.pet is not None:
            try:
                self.pet.tts_lip_stop()
            except Exception:
                pass
        try:
            self._motion.stop(time.monotonic())
            self._motion_ensure()
        except Exception:
            pass
        if self.pet is not None:
            try:
                self.pet.sub_karaoke("配音失败：%s" % msg, 0, secs=4.0)
            except Exception:
                pass
        self._start_next_pending()

    def _on_audio_finished(self) -> None:
        with self._lock:
            self._state["speaking"] = False
            self._state["finish_ms"] = int(self._engine.duration_ms()) if self._engine else 0
        # 口型立刻停；字幕说完后再停留 hold_ms 才消失（读屏留白，别一闪就没）
        if self.pet is not None:
            try:
                self.pet.tts_lip_stop()
            except Exception:
                pass
        try:
            self._tick.stop()            # 停掉刷新，否则 30ms 一次会把停留中的字幕重新画上去
        except Exception:
            pass
        # 微动作不立刻断：600ms 缓出回基准，突然复位会像"定格"
        try:
            self._motion.stop(time.monotonic())
            self._motion_ensure()
        except Exception:
            pass
        next_started = self._start_next_pending()
        if next_started and self._state.get("speaking"):
            # 下一条已经开口：旧字幕不能继续挂在屏幕上（新字幕由新 speak 接管）
            if self.pet is not None:
                try:
                    self.pet.sub_karaoke("", 0, secs=0)
                except Exception:
                    pass
            self._state["sub"] = None
            return
        token = object()
        self._hold_token = token

        def _clear():
            if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
                print("[tts] hold clear token_ok=%s" % (getattr(self, "_hold_token", None) is token), flush=True)
            if getattr(self, "_hold_token", None) is not token:
                return                      # 期间又开了一句，别把新字幕擦掉
            if self.pet is not None:
                try:
                    self.pet.sub_karaoke("", 0, secs=0)
                except Exception:
                    pass
            self._state["sub"] = None       # 状态里也不再挂着上一句

        try:
            hold = int(self._c("hold_ms", 3000))
        except Exception:
            hold = 3000
        # 尾奏还在放音乐时字幕本来就该留着，所以要把尾奏时长从 3 秒里扣掉，
        # 保证「人声结束 → 3 秒后字幕消失」这个体验不被 tail 拉长。
        try:
            hold -= int(self._engine.bgm_tail_ms()) if self._engine else 0
        except Exception:
            pass
        hold = max(300, hold)
        # UI 层兜底：把字幕到期时间收紧到 hold，避免音频结束了、字幕时间轴还在往后跑
        if self.pet is not None:
            try:
                self.pet.subtitle_hold(hold)
            except Exception:
                pass
        if os.environ.get("DESKTOP_PET_TTS_DEBUG"):
            print("[tts] finished, hold=%dms speaking=%s" % (hold, self._state.get("speaking")), flush=True)
        QTimer.singleShot(max(0, hold), _clear)

    # ------------------------------------------------------------ 说话微动作
    def _motion_ensure(self) -> None:
        """请求主线程把 50ms 心跳跑起来（本方法可能被 HTTP 线程调用）。

        跨线程只发信号，绝不在这里碰 QTimer —— 在非主线程 start 的 Qt 定时器会静默失效。
        """
        try:
            self.motionWake.emit()
        except Exception:
            pass

    def _motion_start(self) -> None:
        """主线程槽：真正启动心跳。"""
        try:
            if self._motion_timer is not None and not self._motion_timer.isActive():
                self._motion_timer.start()
        except Exception:
            pass

    def _motion_body(self) -> None:
        try:
            if not self._motion_ok or self.pet is None:
                self._motion_timer.stop()
                return
            if self._motion.is_idle():
                self.pet.tts_gesture({})        # 收尾：清空偏移，回到基准姿态
                self._motion_timer.stop()
                return
            self._motion_last = time.monotonic()
            offs = self._motion.tick(self._motion_last)
            self._motion_out = dict(offs or {})       # 对外可回读，便于排查"看不见动作"
            self.pet.tts_gesture(offs)
        except Exception as e:
            self._state["error"] = "微动作失败: %s" % e
            try:
                self._motion_timer.stop()
            except Exception:
                pass

    # ------------------------------------------------------------ 台词动画
    def _fire_anim(self, name: str) -> None:
        """念到标记所在的字 → 播一次内置动画，并让自动微动作让位约 0.95s。"""
        if not name or self.pet is None:
            return
        try:
            ok = bool(self.pet.tts_play_motion(name))
        except Exception:
            ok = False
        if ok:
            try:
                self._motion.pause(time.monotonic(), 950)
            except Exception:
                pass
        try:
            fired = self._state.setdefault("anim_fired", [])
            fired.append({"name": name, "ok": ok, "clock": round(float(self._last_clock), 0)})
        except Exception:
            pass

    # ------------------------------------------------------------ 字幕时钟
    def _on_tick(self) -> None:
        # PyQt 槽里逃出的异常会被解释成 abort（进程 0xC0000409 直接消失），
        # 这里必须全包住，任何异常只落到 _state["error"]。
        try:
            self._tick_body()
        except Exception as e:
            self._state["error"] = "tick 失败: %s" % e

    def _tick_body(self) -> None:
        self._drain()
        if self._engine is None:
            return
        # 防卡死：MiMo 流式偶发收不到收尾事件，设备时钟会一直往前跑，
        # 但 speaking 还挂着 true —— 下一条会被堵在队列里，字幕也永远不消失。
        # 播放头超过「音频总时长 + BGM 尾奏 + 3 秒」就强制收尾，按正常结束处理。
        #
        # 两处必须的限定，否则唱歌必被误杀（现象："动画在播、一声没出/只唱一句、
        # 然后突然停"）：
        #  1) 唱歌等待期（歌还没出第一段）不做判定：整曲一次要 10~30s 才出首声，
        #     这期间 dur 还是 0、而设备时钟已经被垫场（清嗓子/BGM 早进）推了十几秒。
        #  2) 时钟基准换成人声播放头（voice_clock_ms），不能用设备时钟 clock_ms：
        #     后者从垫场起播那一刻起跑，等歌的十几~二十几秒会被算成"已播放"，
        #     于是歌一到货 raw 就已经越过 dur+尾奏+3s，整首歌当场被判死。
        if self._state.get("speaking"):
            try:
                # 人声播放头；负数只代表"预垫静音还没过完"（voice_origin 含 lead），
                # 钳到 0 即可 —— 绝不能回退到设备时钟，否则长等待时旧 bug 又回来了。
                raw = float(self._engine.voice_clock_ms())
                if raw < 0:
                    raw = 0.0
                dur = int(self._engine.duration_ms())
                tail = int(self._engine.bgm_tail_ms())
                with self._lock:
                    sing_waiting = bool(self._singing) and not self._tl
                if (raw > 0 and dur > 0 and raw > dur + tail + 3000
                        and not sing_waiting):
                    self._state["error"] = "音频结束信号缺失，已自动收尾"
                    self.stop(notify_pet=True, keep_pending=True)
                    self._start_next_pending()
                    return
            except Exception:
                pass
        # 唱歌等待期：说明句自己的字幕（电视幕打歌前先打这句），念完就撤
        wait = self._wait_sub
        if wait:
            if self._engine.wait_voice_active():
                pos = self._engine.wait_voice_ms()
                text = str(wait.get("text") or "")
                spoken = 0
                for m in wait.get("marks") or []:
                    t0 = m.get("t0", 0) if isinstance(m, dict) else m
                    if t0 <= pos:
                        spoken += 1
                    else:
                        break
                remain = (float(wait.get("end_ms") or 0) - pos) / 1000.0
                if self.pet is not None and text:
                    try:
                        self.pet.sub_karaoke(text, spoken, secs=max(0.5, remain))
                    except Exception:
                        pass
                self._state["sub"] = {"text": text, "spoken": spoken,
                                      "clock": round(pos, 1), "wait": True}
                return
            # 念完了：字幕撤掉，背景音继续垫到歌开口
            self._wait_sub = None
            if self.pet is not None:
                try:
                    self.pet.sub_karaoke("", 0, secs=0)
                except Exception:
                    pass
            if (self._state.get("sub") or {}).get("wait"):
                self._state["sub"] = None
        # 字幕时钟用"人声起点"而不是设备起点：BGM 提前进场的那段不算进度
        clock = self._engine.voice_clock_ms()
        self._last_clock = clock
        # 口型：跟随当前正在播的人声振幅（含垫场静音期=闭嘴，播完自动归零）
        if self.pet is not None and self._state.get("lip_sync"):
            try:
                self.pet.tts_lip_level(self._engine.lip_level())
            except Exception:
                pass
        if clock < 0:
            return
        # 只显示"正在说的这一句"：按 clock 找当前段，不给全文。
        # 段与段之间不留空档——上一段说完、下一段还没开始时仍显示上一段。
        with self._lock:
            tl = list(self._tl)
        if not tl or self.pet is None:
            return
        if not self._state.get("speaking") and clock >= int(tl[-1]["end_ms"]):
            return                       # 已播完：别再刷最后一句，交给 hold 到期清除
        cur = None
        for s in tl:
            if clock < int(s["start_ms"]):
                break
            cur = s
        if cur is None:
            cur = tl[0]
        text = str(cur.get("text") or "")
        if not text:
            return
        st = int(cur["start_ms"])
        local = max(0.0, clock - st)
        spoken = 0
        for m in cur.get("marks") or []:
            t0 = m.get("t0", 0) if isinstance(m, dict) else m
            if t0 <= local:
                spoken += 1
            else:
                break
        remain = (int(cur["end_ms"]) - clock) / 1000.0
        # 台词里点名的内置动画：念到那个字再触发（念完之前不动，避免提前剧透）
        for a in cur.get("anim") or []:
            if a.get("done") or spoken < int(a.get("at") or 0):
                continue
            a["done"] = True
            self._fire_anim(str(a.get("name") or ""))
        try:
            self.pet.sub_karaoke(text, spoken, secs=max(0.5, remain))
        except Exception:
            pass
        self._state["sub"] = {"text": text, "spoken": spoken, "clock": round(clock, 1)}

    # ------------------------------------------------------------ 状态
    def status(self, detail: bool = False) -> Dict[str, object]:
        """配音状态。默认精简，detail=True 才给全量。

        精简态只留"智能体做决策要用的字段"：是否在播 / 后端 / 音色 / 当前字幕文本 /
        已播段数 / 报错 / 可用后端 / 各后端默认音色 / BGM 场景。约 200~300 字符。

        清单类（三套音色全表 1722 字符、mimo 自述 1058、BGM 预设 506）与诊断类
        （audio / lip / motion / 时间轴）占原文九成以上，而且音色与 BGM 名在工具描述里
        已经写全了，默认不必再回一遍；排查用 detail=true 取全量。
        """
        st = dict(self._state)
        st["ok"] = True
        st["action"] = "tts_status"
        st["backends"] = ["local", "edge", "mimo"]
        try:
            st["defaults"] = {"local": self._voice_for("local"),
                              "edge": self._voice_for("edge"),
                              "mimo": self._voice_for("mimo")}
        except Exception:
            pass
        try:
            st["bgm_scenes"] = {
                "piano": "日常/默认，温柔钢琴",
                "ambient": "安静/谈心，空灵氛围",
                "cafe": "慵懒闲聊，lo-fi",
                "night": "深夜陪伴，低沉",
                "nature": "自然场景，风雨森林",
                "bright": "开心/节日，轻快明亮",
            }
        except Exception:
            pass

        if not detail:
            lean = {
                "ok": True,
                "speaking": bool(st.get("speaking")),
                "segments": st.get("segments"),
            }
            try:
                lean["queued"] = len(getattr(self, "_pending", []) or []) + (
                    1 if getattr(self, "_queue_pending", None) else 0)
            except Exception:
                pass
            if st.get("error"):
                lean["error"] = st["error"]
            return lean

        st["clock_ms"] = round(self._last_clock, 1)
        st["daemon"] = bool(self._daemon_ready)
        try:
            with self._speech_cache_lock:
                st["speech_cache"] = {
                    "entries": len(self._speech_cache),
                    "prefetching": len(self._speech_prefetching),
                    "prepare_count": int(self._state.get("prepare_count") or 0),
                    "prepare_last_chars": int(self._state.get("prepare_last_chars") or 0),
                    "prepare_ms": int(self._state.get("prepare_ms") or 0),
                    "prepare_cached": bool(self._state.get("prepare_cached")),
                }
        except Exception:
            pass
        st["segments_timeline"] = [
            {"text": s["text"], "start_ms": s["start_ms"], "end_ms": s["end_ms"],
             "chars": s["chars"]} for s in self._tl
        ]
        if self._engine is not None:
            st["audio"] = self._engine.stats()
            # 口型可观测：驱动开关 + 本次采样幅度 + 当前段的响度基准
            try:
                st["lip"] = {"on": bool(self._state.get("lip_sync")),
                             "level": round(float(self._engine.lip_level()), 2),
                             "peak": round(float(getattr(self._engine, "_lip_peak", 0.0)), 3)}
            except Exception:
                st["lip"] = {"on": bool(self._state.get("lip_sync")), "level": 0.0, "peak": 0.0}
        # 微动作可观测：是否让位给智能体指定的动作 / 当前段 / 本段转头目标
        try:
            st["motion"] = dict(self._motion.describe(),
                                on=bool(self._motion_ok),
                                timer=bool(self._motion_timer and self._motion_timer.isActive()),
                                out=dict(getattr(self, "_motion_out", {}) or {}))
        except Exception:
            st["motion"] = {"on": False}
        # 让调用方（智能体）能就近查到三套音色清单，不必猜名字
        st["voices"] = {
            "local": LOCAL_VOICES,
            "online": ONLINE_VOICES,        # edge（微软）
            "mimo": MIMO_VOICES,            # 小米 MiMo-V2.5-TTS
            "default": {"local": self._voice_for("local"),
                        "online": self._voice_for("edge"),
                        "mimo": self._voice_for("mimo")},
        }
        # 在线服务商现状：auto 会走哪家、MiMo 当前模型与音色（不报 key 状态）
        try:
            st["online"] = {"auto": self._online_provider(),
                            "edge": {"provider": "edge",
                                     "voice": self._voice_for("edge")}}
            from .engine_mimo import describe
            st["online"]["mimo"] = describe(self.cfg)
        except Exception as e:
            st["online"] = {"auto": self._online_provider(), "error": str(e)}
        # 同理把可用的 BGM 预设列出来，让智能体按场景自己挑，不用记文件名
        try:
            from .bgm import available_presets
            st["bgm"] = {
                "presets": available_presets(),
                "scenes": st.get("bgm_scenes") or {},
                "timing": {"preroll_ms": int(self._c("preroll_ms", 1200)),
                           "tail_ms": int(self._c("tail_ms", 1600)),
                           "fade_ms": self._bgm_fade_ms()},
            }
        except Exception:
            pass
        return st
