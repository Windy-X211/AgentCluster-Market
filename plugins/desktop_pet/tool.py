"""desktop_pet 插件 — Live2D 桌宠控制

调用签名 (由 tool.yaml 的 parameters 决定)：
    run(action="show", model_path="...", x=0.5, y=0.8, scale=1.0, ...)

多模型：一个模型 = 一个 pet_window.py --worker 子进程（各自独立 Chromium profile），
父进程用 _instances 注册表 + localhost 控制通道按 model_path 路由命令。
屏幕上只有 1 个模型时可以省略 model_path；≥2 个必须指定，否则直接报错并列出候选。

功能共 23 个 action：
  1. 窗口控制：show / hide / toggle / close / list_open_models / list_models
  2. 布局控制：switch / move / resize / update
  3. 捏脸参数：get_params / search_params / set_params / express
  4. 流式动画：start_stream / append_frames / stop_stream
  5. 动作与表情：list_motions / list_expressions / play_animation / stop_animation
  6. 配音：set_sound（模型动作自带 Sound 时播放/静音）
  7. 自检：preflight（不建窗口、不占端口）

返回体约定：所有 action 返回的空字段（None / "" / [] / {}）一律不出现，
统一在 run() 出口由 _prune_empty 裁剪，避免空占位浪费调用方 token。
"""
import contextlib
import atexit
import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# 插件目录（纯计算，不修改 sys.path —— 加载期必须零副作用）
_PLUGIN_DIR = str(Path(__file__).resolve().parent)

# 未指定 model_path 时的默认模型（插件自带）
_DEFAULT_MODEL_DIR = Path(_PLUGIN_DIR) / "assets" / "符玄"

# 缩放上下限（与 pet_window.py 的 MIN_SCALE / MAX_SCALE 保持一致）
MIN_SCALE, MAX_SCALE = 0.3, 5.0

# === 多实例状态 ===
# 每个模型一个 worker 子进程（pet_window.py --worker），这里维护 model_path → 实例。
# 为什么分进程：QApplication / QtWebEngine 在一个进程内只能初始化一次，且只有一个
# GUI 线程；多模型同屏唯一可靠的形态就是多进程，同时让 close 能真正把内存还回去。
_instances: dict = {}          # key(model3.json 规范化绝对路径) → _PetInst
_lock = threading.Lock()

# wait_others 的延迟执行令牌：命令立即返回，后台等到时机成熟后再下发。
# 每条 defer 命令独立 token + record；全局 FIFO 保证跨模型按入队顺序串行。
_deferred_lock = threading.Lock()
_deferred_seq = 0
# token → record: {model_key, action, invalidated, executed}
_deferred_tokens: dict = {}
# 全局 FIFO：token list，队首那个还没执行的 = 下一条可以下发的命令
_deferred_fifo: list = []

# 同屏上限：每个实例 ≈ 一个 Python+Qt 进程 + 一个 Chromium 渲染进程 + 一份贴图，
# 实测单实例常驻 300~450MB（模型大小相关），6 个是普通桌面机器还能接受的上限。
MAX_INSTANCES = 6

# show 时模型加载偶发失败（渲染进程 WebGL 上下文丢失 → Live2D 的资源请求被中断，
# 现场错误码就是 "Aborted."）：这种故障在同一进程里重建渲染器救不回来（context
# 已经废了），换一个新 worker 进程（新 Chromium + 新 GL 上下文）才行。所以这里
# 自动重开进程重试，最多 SHOW_RETRY_MAX 次；重试用尽才把真实错误返回给调用方。
SHOW_RETRY_MAX = 3
# 两次尝试之间的间隔：实测这类故障会成簇出现（10 秒内的几次加载全失败，
# 之后又正常），隔一会儿再开新进程，命中「坏窗口」的概率明显下降。
SHOW_RETRY_DELAY_S = 1.2
# 只有「临时性」错误才值得重开进程；模型文件不存在 / moc3 解析失败这类硬错误
# 重试只是白等一遍。
_SHOW_RETRY_HINTS = ("abort", "超时", "context", "lost", "gl ", "gpu", "network", "err_")

# 未指定位置时新实例的横向级联偏移（避免第 2 个模型正好盖住第 1 个）
_CASCADE_DX = (0.0, -0.14, 0.14, -0.28, 0.28, -0.42)

_WORKER_LOG_DIR = os.path.join(_PLUGIN_DIR, "_workers")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_WORKER_PY = {"exe": None}

# 以下为「当前调用绑定的实例」镜像，供既有 handler 逻辑无改动复用：
# run() 在持锁期间把目标实例的字段写进来，调用结束再写回实例（单线程串行，安全）。
_window = None
_current_model: str | None = None
_current_pos = {"x": 0.5, "y": 0.8}
_current_scale = 1.0
_stream_active = False

# 显式动画的「忙碌到期时间」：跨模型 wait_others 用。
# TTS 状态能从 worker 实时查到（speaking/queued），动画没有现成查询接口，
# 所以在工具层按动作文件 Meta.Duration 记一个到期时间；只影响等待判断，
# 不参与渲染。key 与实例路由键一致（规范化的 model3.json 路径）。
_MOTION_BUSY: dict = {}

# 循环动画没有真实结束点：只按「短暂占用」处理，绝不写 inf。
# 写 inf 会让其它模型的 speak/play_animation 永远等不到执行，而 stop_animation
# 之外的路径（换模型 / 关窗口 / 用户点模型播自带动作）都不会清这个标记，
# 等于把整条 waiting 队列永久毒化。
_LOOP_BUSY_SECONDS = 30.0

# 最近一次 deferred 命令等待超时的原因（key=模型路由键）。
# 命令在工具层早已返回 accepted，后台超时如果不留痕，调用方完全看不到失败。
_DEFER_ERRORS: dict = {}

# 已真正加载完成的模型（model3.json 绝对路径）。
# 与 _current_model 的区别：_current_model 是「最后一次请求的模型」，switch/show
# 失败时它也会被改写；_loaded_model 只在 wait_model_ready 成功后才更新，
# 因此它能准确回答「现在屏幕上到底是哪个模型」。
_loaded_model: str | None = None

# get_params / search_params 的单次返回字符硬上限：超过就按条数截断并给 next_offset，
# 绝不让框架层静默砍掉后半段（那会丢参数且没有续读游标）。
# 244 参数 ≈ 12K，取 20K 可一次给完；仍低于框架 ~26K 的输出上限，留有余量。
_PARAMS_MAX_CHARS = 20000

# 参数名/范围缓存：按模型（model3.json）分别缓存，多模型来回切也不用重取
_PARAM_META: dict = {}
# cdi3.json → {参数Id: {"alias": 中文名, "group": ParamGroupNN}} 缓存
_PARAM_ALIAS_CACHE: dict = {}

# Param + 纯数字（Param401）这类无语义编号：排序时统一沉底
_RE_VAGUE_PARAM = re.compile(r"^Param\d+$")
# search_params 的多词分隔符（中文逗号/顿号也算；不吃空格 —— 中文别名本身含空格）
_RE_MULTI_QUERY = re.compile(r"[,;、\n\r，]+")

# 标准 Live2D 参数的中文别名（cdi3.json 只给模型自定义参数起名，标准参数没有）
_STD_ALIAS = {
    "ParamAngleX": "头部旋转X(左右)",
    "ParamAngleY": "头部旋转Y(上下)",
    "ParamAngleZ": "头部旋转Z(倾斜)",
    "ParamEyeBallX": "眼球左右",
    "ParamEyeBallY": "眼球上下",
    "ParamEyeLOpen": "左眼开合",
    "ParamEyeROpen": "右眼开合",
    "ParamEyeLSmile": "左眼微笑",
    "ParamEyeRSmile": "右眼微笑",
    "ParamMouthOpenY": "嘴开合",
    "ParamMouthForm": "嘴形状",
    "ParamBrowLY": "左眉上下",
    "ParamBrowRY": "右眉上下",
    "ParamBrowLX": "左眉左右",
    "ParamBrowRX": "右眉左右",
    "ParamBrowLForm": "左眉形状",
    "ParamBrowRForm": "右眉形状",
    "ParamBrowLAngle": "左眉角度",
    "ParamBrowRAngle": "右眉角度",
    "ParamBodyAngleX": "身体左右",
    "ParamBodyAngleY": "身体上下",
    "ParamBodyAngleZ": "身体倾斜",
    "ParamBreath": "呼吸",
}

# 口语别名：调用方（智能体）最常用的说法 → 标准参数 id。
# 用于「一个词就能设置/搜到」的短输入路径；只在当前模型确实有这个参数时生效。
_POP_ALIAS = {
    "嘴": ["ParamMouthOpenY"],
    "张嘴": ["ParamMouthOpenY"],
    "说话": ["ParamMouthOpenY"],
    "闭嘴": ["ParamMouthOpenY"],
    "眨眼": ["ParamEyeLOpen", "ParamEyeROpen"],
    "眼睛": ["ParamEyeLOpen", "ParamEyeROpen"],
    "左眼": ["ParamEyeLOpen"],
    "右眼": ["ParamEyeROpen"],
    "闭眼": ["ParamEyeLOpen", "ParamEyeROpen"],
    "眯眼": ["ParamEyeLSmile", "ParamEyeRSmile"],
    "笑": ["ParamEyeLSmile", "ParamEyeRSmile"],
    "头": ["ParamAngleX", "ParamAngleY"],
    "转头": ["ParamAngleX"],
    "摇头": ["ParamAngleX"],
    "点头": ["ParamAngleY"],
    "抬头": ["ParamAngleY"],
    "歪头": ["ParamAngleZ"],
    "倾斜": ["ParamAngleZ"],
    "眉毛": ["ParamBrowLY", "ParamBrowRY"],
    "皱眉": ["ParamBrowLY", "ParamBrowRY"],
    "呼吸": ["ParamBreath"],
    "身体": ["ParamBodyAngleX", "ParamBodyAngleZ"],
    "晃动": ["ParamBodyAngleX", "ParamBodyAngleZ"],
    "表情": ["ParamMouthForm", "ParamEyeLSmile"],
}


# ===== 加载期自检（preflight）=====
# 设计原则：
#   1) 本模块 import 时零副作用（不 import PyQt5、不建目录、不改环境变量）；
#   2) 真正的重活（窗口、QtWebEngine、Chromium flags）全部推迟到第一次 run()；
#   3) 第一次 run() 之前先跑 preflight：OK 才继续，不 OK 就返回结构化错误并
#      拒绝加载任何窗口 —— 让宿主/调用方看到「插件不可用 + 原因」，而不是让
#      进程在创建 WebEngine 时直接崩掉。
_PREFLIGHT_CACHE = {"ok": None, "problems": [], "hint": "", "checked": 0.0}
_MIN_FILES = (("renderer.html", 1024), ("pet_window.py", 1024), ("tool.yaml", 256),
              ("manifest.yaml", 128))
# 不需要窗口的操作：插件坏了也能用来收尾 / 查看状态
_WINDOWLESS_ACTIONS = {"hide", "close", "close_all", "list_servers", "list_open_models",
                       "list_models", "preflight", "debug_info",
                       # 配音管理：不依赖窗口（装环境/读配置/查状态都能先跑）
                       "tts_setup", "tts_config"}
# 支持多模型排队的动作：wait_others=True 时先等其他模型当前配音/动画结束。
_WAIT_OTHERS_ACTIONS = {"speak", "play_animation"}


def preflight(force: bool = False) -> dict:
    """插件可用性自检：只读文件、查依赖是否存在，不创建窗口、不占端口、不改环境。

    返回 {"ok": bool, "error": str, "hint": str, "problems": [str]}。
    """
    if _PREFLIGHT_CACHE["ok"] is not None and not force:
        problems = list(_PREFLIGHT_CACHE["problems"])
        ok = bool(_PREFLIGHT_CACHE["ok"])
        return {"ok": ok, "problems": problems,
                "error": "" if ok else "; ".join(problems),
                "hint": _PREFLIGHT_CACHE["hint"]}
    problems: list[str] = []
    plugin_dir = Path(_PLUGIN_DIR)

    # 1) 关键文件齐全且非空
    for name, min_size in _MIN_FILES:
        p = plugin_dir / name
        if not p.is_file():
            problems.append(f"缺少文件：{name}")
        else:
            try:
                size = p.stat().st_size
            except OSError as e:
                problems.append(f"无法读取 {name}：{e}")
                continue
            if size < min_size:
                problems.append(f"{name} 不完整（{size} 字节 < {min_size}）")

    # 2) Python 源码可编译（compile 不落盘，避免写 __pycache__）
    for name in ("pet_window.py", "tool.py"):
        p = plugin_dir / name
        if p.is_file():
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as e:
                problems.append(f"{name} 语法错误：第 {e.lineno} 行 {e.msg}")
            except OSError as e:
                problems.append(f"无法读取 {name}：{e}")

    # 3) 默认模型
    default_json = _DEFAULT_MODEL_DIR / "符玄.model3.json"
    if not default_json.is_file():
        problems.append(f"默认模型缺失：{default_json}")

    # 4) 依赖是否存在（用 find_spec，只查不 import，零副作用）
    for mod, pip_name in (("PyQt5", "PyQt5>=5.15.9"),
                          ("PyQt5.QtWebEngineWidgets", "PyQtWebEngine>=5.15.7")):
        try:
            found = importlib.util.find_spec(mod) is not None
        except Exception:
            found = False
        if not found:
            problems.append(f"缺少依赖 {mod}（需要 pip install {pip_name}）")

    # 5) profile 目录可写（真正建目录仍在首次创建窗口时）
    profile_parent = plugin_dir
    if not os.access(str(profile_parent), os.W_OK):
        problems.append(f"插件目录不可写，WebEngine 无法建缓存目录：{profile_parent}")

    ok = not problems
    hint = ""
    if not ok:
        hint = ("插件未加载：自检未通过。请按 problems 逐项修复后重试；"
                "在修复前不会创建任何窗口，也不会占用端口。")
    _PREFLIGHT_CACHE.update(ok=ok, problems=problems, hint=hint, checked=time.time())
    return {"ok": ok, "problems": problems, "error": "" if ok else "; ".join(problems),
            "hint": hint}


def _cmd_preflight(kwargs):
    pf = preflight(force=True)
    pf.update({"ok": pf["ok"], "action": "preflight", "loaded": pf["ok"],
               "plugin": "desktop_pet", "plugin_dir": _PLUGIN_DIR})
    return pf


# 宿主若按常见命名去探测插件健康状态，这几个别名都能拿到同一个只读结论
health_check = preflight
validate_plugin = preflight
plugin_ready = preflight

# ===== 惰性加载（仅在调用时加载）=====
_PW_MODULE = None


def _ensure_plugin_path() -> None:
    """把插件目录加入 sys.path（幂等）。加载期不执行，第一次真正用到时才调用。"""
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)


def _pw():
    """惰性导入 pet_window（连带 PyQt5 / QtWebEngine）；失败抛出带原因的错误。"""
    global _PW_MODULE
    if _PW_MODULE is not None:
        return _PW_MODULE
    _ensure_plugin_path()
    try:
        import pet_window as _m
    except Exception as e:
        raise RuntimeError(
            f"无法加载 pet_window.py（{type(e).__name__}: {e}）；"
            "请确认已安装 PyQt5 / PyQtWebEngine，或先调用 action=preflight 查看原因"
        ) from e
    _PW_MODULE = _m
    return _m


def _pw_loaded():
    """已加载的 pet_window 模块；没加载过返回 None（诊断用，不为它触发加载）。"""
    return _PW_MODULE



def _on_ui_scale_changed(new_scale):
    """（遗留）滚轮缩放回调 — 多实例后由 worker 自己维护，父进程改读 /state。"""
    global _current_scale
    _current_scale = new_scale


def _on_ui_pos_changed(x, y):
    """（遗留）拖拽/避让位置回调 — 同上。"""
    _current_pos.update(x=x, y=y)


# ===== 多实例：实例对象 / worker 进程 / 控制通道 =====
class _PetInst:
    """一个模型窗口 = 一个 worker 进程（或一个被找回的遗留进程）+ 一条控制通道。

    注意 key / keys 的分工：`key` 只是注册表里的唯一主键（含 pid，永不重复），
    `keys` 是这个实例「对应过的所有 model 路径键」—— 用户在窗口里右键换模型后，
    新旧路径都要能定位到它，而且不能因为两个实例撞到同一个模型而互相覆盖。
    """

    def __init__(self, model_path: str, model_file: str, tag: str):
        self.key = ""                       # 注册表主键（_uniq_key，插入时确定）
        self.keys = {_key(model_file)}      # 所有可路由的 model 路径键
        self.model_path = model_path        # 调用方给的写法（原样回显，方便对齐）
        self.model_file = model_file        # model3.json 绝对路径
        self.model_dir = str(Path(model_file).parent)
        self.name = _short_model_name(model_file)
        self.tag = tag                      # 独立 Chromium profile / 日志标签
        self.pos = {"x": 0.5, "y": 0.8}
        self.scale = 1.0
        self.streaming = False
        self.ready = False
        self.track = True
        self.port = 0
        self.pid = 0
        self.proc = None                    # 不是本进程拉起的（收养来的）就是 None
        self.orphan = False                 # True=从磁盘自述文件找回的遗留窗口
        self.client = None
        self.log_path = ""


def _retryable_load_error(message: str) -> bool:
    """这个加载失败值不值得换一个新 worker 进程重试一次。

    现场只有「WebGL 上下文丢失 / 资源请求被中断（Aborted.）/ 加载卡住」这类临时
    故障能靠重开进程恢复；「模型文件不存在」「moc3 解析失败」这类硬错误重试只是
    白等，直接报错让调用方改参数更有用。
    """
    low = str(message or "").lower()
    return any(h in low for h in _SHOW_RETRY_HINTS)


def _uniq_key(model_file: str, pid: int) -> str:
    """注册表主键：模型路径 + pid。同一模型开多个窗口也不会互相顶掉。"""
    return "%s#%d" % (_key(model_file), int(pid or 0))


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（不依赖 proc 句柄：收养来的实例没有句柄）。"""
    if not pid:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000, False, int(pid))     # SYNCHRONIZE
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x00000102   # WAIT_TIMEOUT=还活着
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _kill_pid(pid: int) -> bool:
    """强杀一个不归本进程管的窗口进程（收养来的实例只能按 pid 收）。

    必须连子进程一起收：worker 会 Popen 一个 Electron 透明显示窗，只杀根进程
    的话它会变成孤儿继续把模型画在屏幕上（清单里已经没有、还点不动）。
    所以优先 taskkill /F /T（整棵进程树），TerminateProcess 只当兜底。
    """
    if not pid:
        return True
    try:
        r = subprocess.run(["taskkill", "/PID", str(int(pid)), "/F", "/T"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, creationflags=_NO_WINDOW)
        # 0=杀掉；128=进程本来就不在了 —— 两种都算成功，别白跑一次 TerminateProcess
        if r.returncode in (0, 128) and not _pid_alive(int(pid)):
            return True
    except Exception:
        pass
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0001, False, int(pid))        # PROCESS_TERMINATE
        if h:
            try:
                ok = bool(k32.TerminateProcess(h, 1))
            finally:
                k32.CloseHandle(h)
            if ok:
                return True
    except Exception:
        pass
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=20, creationflags=_NO_WINDOW)
        return True
    except Exception:
        return False


def _port_listening(port: int) -> bool:
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        s.close()


# ===== 宿主退出时收回所有 worker：三层保险 =====
# 1) atexit：宿主正常退出（含普通异常）→ 逐个 /shutdown + 兜底强杀
# 2) Windows Job Object：宿主「怎么死都算」—— 崩溃、os._exit、任务管理器强杀，
#    Job 最后一个句柄随进程消失时内核直接终止子树，这是唯一能覆盖「跳过 atexit」的机制
# 3) worker 侧看门狗（pet_window.py）：父进程回连端口连续不可达就自行退出
_JOB = {"handle": None, "tried": False}


def _ensure_job():
    """建一个「句柄关闭即杀子树」的 Job Object；不支持/失败返回 None。"""
    if os.name != "nt":
        return None
    if _JOB["handle"] is not None:
        return _JOB["handle"]
    if _JOB["tried"]:
        return None
    _JOB["tried"] = True
    try:
        import ctypes
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC),
                        ("IoInfo", _IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                wintypes.LPVOID, wintypes.DWORD]
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x00002000     # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(h)
            return None
        _JOB["handle"] = h
        return h
    except Exception:
        return None


def _assign_to_job(pid: int) -> bool:
    """把 worker 挂进 Job：宿主非正常退出时由内核负责连根收走。"""
    h = _ensure_job()
    if not h or not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        ph = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))   # SET_QUOTA | TERMINATE
        if not ph:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(h, ph))
        finally:
            k32.CloseHandle(ph)
    except Exception:
        return False


def _kill_all_on_exit():
    """atexit 钩子：宿主正常退出时把所有 worker 收掉，不留残余进程。"""
    try:
        insts = list(_instances.values())
    except Exception:
        insts = []
    pids = []
    for inst in insts:
        pid = inst.pid or (inst.proc.pid if inst.proc is not None else 0)
        pids.append((inst, pid))
        try:
            if inst.client is not None:
                inst.client._req("/shutdown", {}, timeout=0.8)
        except Exception:
            pass
    for inst, pid in pids:
        if pid and _pid_alive(pid):
            _kill_pid(pid)
        if pid:
            try:
                os.remove(os.path.join(_WORKER_LOG_DIR, "state_%d.json" % int(pid)))
            except OSError:
                pass
    try:
        _instances.clear()
    except Exception:
        pass


atexit.register(_kill_all_on_exit)


class _WorkerClient:
    """worker 子进程的控制通道客户端（HTTP + JSON，全部走 127.0.0.1）。

    只实现父进程真正用到的那几个方法，语义与原来的 DesktopPetWindow 一一对应，
    所以 tool.py 里所有 handler 的原有调用方式不需要改。
    """

    def __init__(self, port: int, proc=None):
        self.port = int(port)
        self.proc = proc
        self._base = "http://127.0.0.1:%d" % self.port

    def _req(self, path, payload=None, timeout=3.0):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(self._base + path, data=data,
                                     headers=headers,
                                     method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8")) if body else {}

    # --- 状态 ---
    def state(self, timeout=1.0):
        """状态快照；worker 挂了/端口不通返回 None（调用方据此判断失联）。"""
        try:
            return self._req("/state", timeout=timeout)
        except Exception:
            return None

    # --- 命令（与旧 DesktopPetWindow API 同名同义）---
    def send_command(self, cmd: dict):
        try:
            self._req("/cmd", {"cmd": cmd}, timeout=3.0)
            return True
        except Exception:
            return False

    def send_command_sync(self, cmd: dict, timeout: float = 5.0) -> dict:
        try:
            res = self._req("/cmd_sync", {"cmd": cmd, "timeout": max(0.5, float(timeout) - 1.0)},
                            timeout=max(2.0, float(timeout) + 1.0))
            out = res.get("result")
            return out if isinstance(out, dict) else {"raw": out}
        except Exception as e:
            return {"error": "worker 无响应：%s" % (e,)}

    def reset_model_ready(self):
        try:
            self._req("/reset_ready", {}, timeout=2.0)
        except Exception:
            pass

    # --- 配音（转发给 worker 内的 TTSService）---
    def tts(self, payload: dict, timeout: float = 5.0) -> dict:
        try:
            return self._req("/tts", payload, timeout=max(2.0, float(timeout)))
        except Exception as e:
            return {"ok": False, "error": "worker 无响应：%s" % (e,)}

    def tts_status(self, detail: bool = False) -> dict:
        path = "/tts?detail=1" if detail else "/tts"
        try:
            return self._req(path, None, timeout=2.0)
        except Exception as e:
            return {"ok": False, "error": "worker 无响应：%s" % (e,)}

    def is_running(self) -> bool:
        if self.proc is not None and self.proc.poll() is not None:
            return False
        return self.state(0.6) is not None

    def is_visible(self):
        st = self.state(1.0)
        return st.get("visible") if st else None

    @property
    def model_error(self):
        st = self.state(1.0)
        return st.get("error") if st else None

    def wait_model_ready(self, timeout: float = 20.0) -> bool:
        """轮询 /state，直到模型真正就绪。加载慢不算失败，真报错才提前放弃。"""
        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            st = self.state(0.8)
            if st:
                if st.get("ready"):
                    return True
                if not st.get("alive", True):
                    return False
                if st.get("error"):
                    # 页面报上来的是真错误（模型 404 / Live2D 解析失败），不等了
                    return False
            elif self.proc is not None and self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def shutdown(self, timeout: float = 6.0) -> bool:
        """让 worker 自己退出（优雅），超时再 terminate/kill —— 杀进程即释放内存。"""
        try:
            self._req("/shutdown", {}, timeout=2.0)
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc is None or self.proc.poll() is not None:
                return True
            time.sleep(0.1)
        try:
            self.proc.terminate()
        except Exception:
            pass
        for _ in range(10):
            if self.proc.poll() is not None:
                return True
            time.sleep(0.1)
        try:
            self.proc.kill()
        except Exception:
            pass
        return True


def _key(model_file: str) -> str:
    """实例键：model3.json 绝对路径的规范化形式（大小写不敏感）。"""
    return os.path.normcase(os.path.abspath(model_file))


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _tail(path: str, limit: int = 900) -> str:
    """读 worker 日志尾部（启动失败时用来解释原因）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit * 3))
            raw = f.read()
        return raw.decode("utf-8", "replace")[-limit:].replace("\n", " | ")
    except Exception:
        return ""


def _worker_python() -> str:
    """worker 用哪个解释器：优先宿主自己的 python（已装 PyQt5），并做一次 import 验证。"""
    if _WORKER_PY["exe"]:
        return _WORKER_PY["exe"]
    cands: list[str] = []
    exe = sys.executable or ""
    if exe and re.search(r"python", os.path.basename(exe), re.I):
        # 宿主用 pythonw.exe 时优先同目录 python.exe：有控制台入口，出问题有日志
        if "pythonw" in os.path.basename(exe).lower():
            sib = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(sib):
                cands.append(sib)
        cands.append(exe)
    for name in ("python.exe", "python3.exe", "python"):
        p = shutil.which(name)
        if p and p not in cands:
            cands.append(p)
    for c in cands:
        try:
            r = subprocess.run([c, "-c", "import PyQt5.QtWebEngineWidgets"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30, creationflags=_NO_WINDOW)
            if r.returncode == 0:
                _WORKER_PY["exe"] = c
                return c
        except Exception:
            continue
    return cands[0] if cands else "python"


# === 父进程回连通道 ===
# worker 里的右键「关闭」要真正结束自己（释放内存），但 worker 删不掉父进程注册表里的
# 条目，否则父进程会留着一个已死实例，后续 model_path 路由全乱。所以父亲侧起一个小服务，
# worker 关闭前回调 /worker_self_close，由父进程走 _destroy 收走它。
_PARENT_SRV = {"httpd": None, "port": 0}


def _parent_server() -> int:
    """懒启动回连服务（第一次 spawn worker 时才起），返回端口。"""
    if _PARENT_SRV["port"]:
        return _PARENT_SRV["port"]
    import http.server
    import socketserver
    import urllib.parse

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _reply(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n).decode("utf-8", "replace")) if n else {}
            except Exception:
                data = {}
            if path == "/worker_self_close":
                # 先回包再动手：worker 是先发请求、等父进程收走它，先杀会让它收到断连错误
                self._reply({"ok": True})
                try:
                    _close_from_worker(int(data.get("pid") or 0), str(data.get("model") or ""))
                except Exception:
                    pass
                return
            if path == "/worker_close_all":
                # 右键「全部关闭」：所有模型一起收走（发起者自己也会被结束）
                self._reply({"ok": True})
                try:
                    _close_all_from_worker()
                except Exception:
                    pass
                return
            self._reply({"error": "unknown endpoint: %s" % path}, 404)

    class _Srv(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True     # 父进程只有一份，复用端口无冲突

    httpd = _Srv(("127.0.0.1", 0), _Handler)
    _PARENT_SRV["httpd"] = httpd
    _PARENT_SRV["port"] = int(httpd.server_address[1])
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                     daemon=True).start()
    return _PARENT_SRV["port"]


def _close_from_worker(pid: int, model: str):
    """worker 自己点了右键「关闭」：在注册表里认出它并销毁（worker 进程随之结束）。"""
    with _lock:
        inst = None
        if pid:
            for cand in list(_instances.values()):
                cand_pid = cand.pid or (cand.proc.pid if cand.proc is not None else 0)
                if cand_pid and cand_pid == pid:
                    inst = cand
                    break
        if inst is None and model:
            insts = _find_insts(model)
            inst = insts[0] if insts else None
        if inst is None:
            # 注册表里没有它（比如本次进程还没收养）：worker 已自行退出，下一次扫描会清掉条目
            return
        _destroy(inst)


def _close_all_from_worker():
    """worker 右键「全部关闭」：把注册表里所有模型都销毁（含发起它的那个进程）。"""
    with _lock:
        _prune_dead()
        for inst in list(_instances.values()):
            _destroy(inst)


def _prune_dead():
    """清掉已经死掉的实例（右键关闭 / 任务管理器杀掉 / 崩溃），并把遗留的存活 worker 收养回来。"""
    for key, inst in list(_instances.items()):
        if inst.proc is not None:
            if inst.proc.poll() is not None:
                _instances.pop(key, None)
        elif not _pid_alive(inst.pid):
            _instances.pop(key, None)
    _adopt_orphans()


def _rekey(inst: "_PetInst", model_file: str):
    """worker 里用右键换过模型：登记新的路由键，但**不动**注册表主键。

    主键含 pid，天然唯一；如果换成「和另一个实例同一个模型」，两条记录必须都留着，
    否则先打开的那个会从注册表里被顶掉（这正是 list_open_models 漏报的根源之一）。
    """
    # 换了模型：旧模型的忙碌/失败标记作废，避免切走再切回来被旧状态卡住。
    try:
        old_key = _key(inst.model_file or "")
        if old_key:
            _MOTION_BUSY.pop(old_key, None)
            _DEFER_ERRORS.pop(old_key, None)
    except Exception:
        pass
    inst.keys.add(_key(model_file))
    inst.model_file = model_file
    inst.model_dir = str(Path(model_file).parent)
    inst.name = _short_model_name(model_file)
    inst.model_path = model_file


def _find_insts(model_file: str) -> list:
    """按模型路径找实例（可能 0 个 / 1 个 / 多个同模型窗口）。"""
    key = _key(model_file)
    direct = _instances.get(key)
    if direct is not None:
        return [direct]
    return [i for i in _instances.values() if key in i.keys]


def _scan_worker_states() -> list:
    """扫描 worker 自述文件，返回仍然活着、能应答的条目（含本进程管的）。"""
    out = []
    try:
        names = [n for n in os.listdir(_WORKER_LOG_DIR) if n.startswith("state_") and n.endswith(".json")]
    except OSError:
        return out
    for name in names:
        path = os.path.join(_WORKER_LOG_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = None
        pid = int((d or {}).get("pid") or 0)
        port = int((d or {}).get("control_port") or 0)
        if not pid or not _pid_alive(pid):
            # 进程已经没了：这是过期自述文件，删掉免得每次都扫一遍
            try:
                os.remove(path)
            except Exception:
                pass
            continue
        out.append({"pid": pid, "state": d, "file": path})
    return out


def _adopt_orphans():
    """把「父进程重启前拉起、还在屏幕上渲染」的 worker 收回注册表。

    注册表只在内存里，插件进程一重启就会空空如也，而窗口还在动 —— 这时
    list_open_models 会把它们全漏掉。找回后这些实例照常可被 model_path 定位、
    可 close，右键「关闭」也通过 /set_parent_port 重新指向本进程的回连端口。
    """
    try:
        scanned = _scan_worker_states()
    except Exception:
        return
    known_pids = {i.pid for i in _instances.values()}
    for item in scanned:
        pid = item["pid"]
        if pid in known_pids:
            continue
        d = item["state"]
        model_file = str(d.get("model") or "")
        if not model_file or not os.path.isfile(model_file):
            continue
        port = int(d.get("control_port") or 0)
        tag = "pet_" + hashlib.sha1(_key(model_file).encode("utf-8")).hexdigest()[:10]
        inst = _PetInst(model_file, model_file, tag)
        inst.key = _uniq_key(model_file, pid)
        inst.pid = pid
        inst.port = port
        inst.orphan = True
        inst.proc = None
        inst.client = _WorkerClient(port, None)
        inst.track = bool(d.get("track", True))
        inst.ready = bool(d.get("ready"))
        inst.pos.update(x=float(d.get("x") or 0.5), y=float(d.get("y") or 0.8))
        inst.scale = float(d.get("scale") or 1.0)
        st = inst.client.state(1.2)
        if not st:
            continue        # 端口不应答：不是能控制的窗口，不收养
        _instances[inst.key] = inst
        # 把回连端口改成自己的，右键「关闭 / 全部关闭」继续有效
        try:
            inst.client._req("/set_parent_port", {"port": _parent_server()}, timeout=2.0)
        except Exception:
            pass


def _spawn(inst: "_PetInst", track: bool) -> str:
    """拉起 worker 子进程并等到控制通道能应答。成功返回 ""，失败返回原因。"""
    py = _worker_python()
    port = _free_port()
    inst.port = port
    inst.track = bool(track)
    try:
        os.makedirs(_WORKER_LOG_DIR, exist_ok=True)
    except OSError as e:
        return "无法创建 worker 日志目录：%s" % (e,)
    inst.log_path = os.path.join(_WORKER_LOG_DIR, inst.tag + ".log")
    args = [py, os.path.join(_PLUGIN_DIR, "pet_window.py"), "--worker",
            "--model-dir", inst.model_dir,
            "--control-port", str(port),
            "--profile-tag", inst.tag,
            "--parent-port", str(_parent_server()),
            "--track", "1" if inst.track else "0",
            "--x", str(inst.pos["x"]), "--y", str(inst.pos["y"]), "--scale", str(inst.scale)]
    try:
        logf = open(inst.log_path, "wb")
    except OSError as e:
        return "无法写入 worker 日志：%s" % (e,)
    try:
        # 合并 stderr 到日志：worker 启动失败（缺 PyQt5 / 模型坏了）全靠这份日志解释
        inst.proc = subprocess.Popen(
            args, cwd=_PLUGIN_DIR, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    except Exception as e:
        return "启动 worker 失败（python=%s）：%s" % (py, e)
    finally:
        try:
            logf.close()
        except Exception:
            pass
    inst.client = _WorkerClient(port, inst.proc)
    # 立刻挂进 Job：此后宿主无论正常退出、崩溃还是被强杀，内核都会把它一起收走
    _assign_to_job(inst.proc.pid)

    deadline = time.time() + 35.0
    while time.time() < deadline:
        if inst.proc.poll() is not None:
            return "worker 启动后立即退出（exit=%s）：%s" % (inst.proc.returncode, _tail(inst.log_path))
        if inst.client.state(0.8) is not None:
            return ""
        time.sleep(0.1)
    return "worker 控制通道 35s 未就绪：%s" % _tail(inst.log_path)


def _create_inst(model_path: str, model_file: str, x=None, y=None, scale=None,
                 track=None) -> dict:
    """新建一个模型实例（拉起 worker），并等模型真正加载完成。"""
    _prune_dead()
    if len(_instances) >= MAX_INSTANCES:
        return {"ok": False, "action": "show", "count": len(_instances),
                "error": "同屏模型已达上限 %d 个（每个实例各占一份独立内存）" % MAX_INSTANCES,
                "open": _open_brief(),
                "hint": "先 action=close(model_path=...) 关掉不用的模型，再开新的"}
    n = len(_instances)
    if x is None:
        # 级联错位：新实例默认左右错开，不叠在上一个身上（显式给 x/y 时以调用方为准）
        x = 0.5 + _CASCADE_DX[min(n, len(_CASCADE_DX) - 1)]
    if y is None:
        y = 0.8
    try:
        x = max(0.0, min(1.0, float(x)))
        y = max(0.0, min(1.0, float(y)))
        if scale is None:
            # 启动高度固定：未显式给 scale 时沿用上次滚轮/命令设置的高度，
            # 不回到默认 1.0，避免每次重开模型忽大忽小。
            try:
                scale = float(_load_ui_cfg().get("scale") or 1.0)
            except (TypeError, ValueError):
                scale = 1.0
        scale = max(MIN_SCALE, min(MAX_SCALE, float(scale)))
    except (TypeError, ValueError):
        return {"ok": False, "action": "show", "error": "x / y / scale 必须是数字"}

    tag_key = _key(model_file)
    tag = "pet_" + hashlib.sha1(tag_key.encode("utf-8")).hexdigest()[:10]
    inst = _PetInst(model_path, model_file, tag)
    inst.pos.update(x=x, y=y)
    inst.scale = scale
    # 第一个实例跟随鼠标，后面的默认不跟随（多窗口共用一个光标会互相推挤遮挡）
    if track is None:
        track = (n == 0)

    err = _spawn(inst, track=track)
    if err:
        if inst.proc is not None and inst.proc.poll() is None:
            try:
                inst.proc.kill()
            except Exception:
                pass
        return {"ok": False, "action": "show", "error": err,
                "hint": "worker 日志：%s" % inst.log_path}

    # 主键带 pid：同一个模型开两个窗口也不会互相挤掉对方的注册表条目
    # 只等很短一段：窗口此时已经出现，但模型可能还在加载。
    # 若继续等完整 75s，智能体的工具调用会一直显示“执行中”，甚至先被工具层超时截断。
    attempts = 0
    ready = False
    last_error = ""
    while True:
        attempts += 1
        inst.pid = inst.proc.pid if inst.proc is not None else 0
        inst.key = _uniq_key(model_file, inst.pid)
        _instances[inst.key] = inst
        ready = inst.client.wait_model_ready(2.0)
        inst.ready = bool(ready)
        if ready:
            break
        last_error = inst.client.model_error or ""
        # 模型还在加载：保留实例，先返回，让智能体/前端结束这次“执行中”
        if not last_error:
            res = {"ok": True, "visible": True, "model_ready": False,
                   "loading": True, "count": len(_instances),
                   "model_name": inst.name,
                   "hint": "窗口已显示，模型仍在加载；稍后用 list_open_models 查 ready，或再调用一次 show。"}
            return res
        # 明确的加载错误才清理并重试；否则保留实例继续加载
        _instances.pop(inst.key, None)
        _destroy(inst)              # 拆掉这个失败的 worker，别留着占显存
        if attempts > SHOW_RETRY_MAX or not _retryable_load_error(last_error):
            break
        # 重开一个全新 worker 进程（新 Chromium / 新 WebGL 上下文）再试
        time.sleep(SHOW_RETRY_DELAY_S)
        inst = _PetInst(model_path, model_file, tag)
        inst.pos.update(x=x, y=y)
        inst.scale = scale
        inst.track = bool(track)
        err = _spawn(inst, track=track)
        if err:
            last_error = err
            break

    res = {"ok": True, "model_ready": bool(ready),
           "visible": bool(ready), "count": len(_instances),
           "model_name": inst.name}
    if not ready:
        res["ok"] = False
        res["error"] = last_error or "模型加载超时"
        if attempts > 1:
            res["attempts"] = attempts
        res["hint"] = ("模型加载失败（共尝试 %d 次）后已清理临时窗口；若是偶发故障"
                       "（渲染进程 WebGL 上下文丢失），再调用一次 action=show 通常就能"
                       "正常显示" % attempts)
    return res


def _destroy(inst: "_PetInst") -> dict:
    """关掉一个实例：先优雅退出 worker，超时再杀——进程结束即彻底释放内存/显存。"""
    info = _inst_info(inst)
    pid = inst.proc.pid if inst.proc is not None else (inst.pid or None)
    try:
        if inst.client is not None:
            inst.client.shutdown(timeout=6.0)
    except Exception:
        pass
    if pid and _pid_alive(pid):
        # 给 worker 留 2s 走完「删自述文件 → os._exit」，别一上来就 TerminateProcess
        t0 = time.time()
        while time.time() - t0 < 2.0 and _pid_alive(pid):
            time.sleep(0.1)
    if pid and _pid_alive(pid):
        _kill_pid(pid)      # 收养来的实例没有 proc 句柄，只能按 pid 收
        time.sleep(0.2)
    # 子进程可能来不及删自己的自述文件（被强杀），父进程知道自己那份的路径，兜底删掉
    if pid:
        try:
            os.remove(os.path.join(_WORKER_LOG_DIR, "state_%d.json" % int(pid)))
        except OSError:
            pass
    # 关窗口时把该模型的忙碌标记一起清掉：否则同一个模型再打开时，
    # 旧的「动画循环中」会把它永久卡在 wait_others 队列里。
    try:
        for k in set(list(getattr(inst, "keys", None) or []) + [_key(inst.model_file or "")]):
            if k:
                _MOTION_BUSY.pop(k, None)
                _DEFER_ERRORS.pop(k, None)
    except Exception:
        pass
    _instances.pop(inst.key, None)
    return {"model": info.get("model"), "model_name": info.get("model_name"), "pid": pid}


def _inst_info(inst: "_PetInst") -> dict:
    return {"model": inst.model_file, "model_name": inst.name}


def _open_brief() -> list:
    """当前已打开的模型清单（报错时给调用方看，便于选 model_path）。"""
    _prune_dead()
    out = []
    for inst in list(_instances.values()):
        out.append({"model": inst.model_file, "model_name": inst.name})
    return out


def _refresh(inst: "_PetInst") -> None:
    """从 worker 拉一次位置/缩放/就绪状态（用户拖拽滚轮后父进程的镜像会过期）。

    顺带处理「用户在窗口里右键换了模型」：真实模型变了就把注册表键换掉，
    否则按新 model_path 再也定位不到这个实例。
    """
    st = inst.client.state(1.0) if inst.client is not None else None
    if not st:
        return
    try:
        inst.pos.update(x=float(st.get("x", inst.pos["x"])),
                        y=float(st.get("y", inst.pos["y"])))
        inst.scale = float(st.get("scale", inst.scale))
        inst.ready = bool(st.get("ready", inst.ready))
    except (TypeError, ValueError):
        pass
    st_model = str(st.get("model") or "")
    if st_model and st_model != inst.model_file:
        try:
            if os.path.isfile(st_model):
                _rekey(inst, st_model)
        except Exception:
            pass


def _target(kwargs, action):
    """按 model_path 解析出目标实例：返回 (inst, None) 或 (None, 错误返回体)。

    model_path 可填模型名（list_open_models 的 model_name）或模型文件地址；
    省略 model_path 时只有「屏幕上恰好一个模型」才允许隐式路由 —— 这样单模型
    场景智能体不用每次都带 model_path，而多模型时绝不猜、直接报错并列出候选。
    """
    _prune_dead()
    mp = str(kwargs.get("model_path") or "").strip()
    if mp:
        try:
            model_file = _resolve_model_path(mp)
        except ValueError as e:
            return None, {"ok": False, "action": action, "error": str(e)}
        insts = _find_insts(model_file)
        if not insts:
            return None, {"ok": False, "action": action, "visible": False,
                          "error": "模型未打开：%s" % model_file,
                          "open": _open_brief(),
                          "hint": ("先 action=show(model_path=...) 打开它；"
                                   "已打开的模型见 open 列表")}
        if len(insts) > 1:
            return None, {"ok": False, "action": action, "count": len(_instances),
                          "open": _open_brief(),
                          "error": ("用这个 model_path 能匹配到 %d 个窗口（同一个模型开了多个），"
                                    "无法确定操作对象" % len(insts)),
                          "hint": "先 action=close(model_path=...) 关掉多余的，或直接 close 全部"}
        inst = insts[0]
    elif len(_instances) == 1:
        inst = next(iter(_instances.values()))
    elif not _instances:
        return None, _no_window()
    else:
        return None, {"ok": False, "action": action, "count": len(_instances),
                      "open": _open_brief(),
                      "error": "当前打开了 %d 个模型，必须用 model_path 指定操作对象（模型名或文件地址）"
                               % len(_instances),
                      "hint": "model_path 可填 open 列表里的 model_name 或 model_path"}
    _refresh(inst)
    return inst, None


@contextlib.contextmanager
def _bound(inst: "_PetInst"):
    """把目标实例的字段镜像到模块级状态，让既有 handler 逻辑原样复用。

    run() 全程持锁，同一时刻只有一个实例处于「绑定」状态，所以这层镜像安全。
    """
    global _window, _current_model, _loaded_model, _current_pos, _current_scale, _stream_active
    prev = (_window, _current_model, _loaded_model, _current_pos, _current_scale, _stream_active)
    _window = inst.client
    _current_model = inst.model_path
    _loaded_model = inst.model_file
    _current_pos = inst.pos
    _current_scale = inst.scale
    _stream_active = inst.streaming
    try:
        yield inst
    finally:
        # handler 可能改了缩放/流式状态：写回实例，下一次调用才不会串味
        try:
            inst.scale = float(_current_scale)
            inst.streaming = bool(_stream_active)
        except (TypeError, ValueError):
            pass
        (_window, _current_model, _loaded_model, _current_pos,
         _current_scale, _stream_active) = prev


def run(**kwargs):
    action = kwargs.get("action", "show")
    # 需要窗口的操作先过自检：不通过就拒绝加载，绝不进入创建 WebEngine 的路径
    if action not in _WINDOWLESS_ACTIONS:
        pf = preflight()
        if not pf["ok"]:
            return _finish({
                "ok": False,
                "stage": "preflight",
                "loaded": False,
                "error": pf["error"],
                "hint": pf["hint"],
                "problems": pf["problems"],
            })
    if action == "tts_status":
        # 状态查询是只读的高频路径：直接读目标 worker，不占工具层 _lock。
        # 否则状态轮询会挡住 _defer_action 的后台派发，放大换人开口延迟。
        inst, err = _target(kwargs, action)
        if err:
            return _finish(err)
        return _finish(_cmd_tts_status(kwargs, inst=inst))
    with _lock:
        try:
            return _finish(_dispatch(action, kwargs))
        except Exception as e:
            import traceback
            return _prune_empty({"error": str(e), "traceback": traceback.format_exc()})


def _finish(res):
    """统一出口：去掉 action 回显（调用方本来就知道调了什么），再裁空字段。"""
    if isinstance(res, dict):
        res.pop("action", None)
        if "error" in res:
            res.setdefault("ok", False)
    return _prune_empty(res)


def _other_live_insts(inst) -> list:
    """除目标自己以外、仍在运行的实例。

    wait_others 的本意是「等别的模型」，只有确实存在别的窗口时才需要排队。
    把目标自己算进去会变成「命令等自己空闲」的自锁：上一条循环动画把自己
    标成永久忙之后，后续 speak/play_animation 永远等不到执行。
    """
    out = []
    for other in list(_instances.values()):
        if other is inst:
            continue
        try:
            if other.client is not None and other.client.is_running():
                out.append(other)
        except Exception:
            pass
    return out


def _dispatch(action: str, kwargs: dict):
    # 与实例无关 / 自己管理实例的操作先摘出来
    if action == "preflight":
        return _cmd_preflight(kwargs)
    if action == "show":
        return _handle_show(kwargs)
    if action == "switch":
        return _handle_switch(kwargs)
    if action == "close":
        return _handle_close(kwargs)
    if action == "close_all":
        return _cmd_close_all(kwargs)
    if action == "list_models":
        return _cmd_list_models(kwargs)
    if action == "list_open_models":
        return _cmd_list_open_models(kwargs)
    if action == "list_servers":
        return _cmd_list_servers(kwargs)
    if action == "tts_setup":
        return _cmd_tts_setup(kwargs)
    if action == "tts_config":
        return _cmd_tts_config(kwargs)
    if action == "hide" and not _instances and not kwargs.get("model_path"):
        # 什么都没开时 hide 不算错：它就是「屏幕已经是空的」
        return {"ok": True, "visible": False}

    inst, err = _target(kwargs, action)
    if err:
        return err

    if action in _WAIT_OTHERS_ACTIONS:
        # 默认 wait_others=true：多模型 speak/play_animation 默认串行排队，
        # 仅当显式传 false 才跳过等待（如争吵场景需要同时发声）。
        _wo = kwargs.get("wait_others")
        should_wait = _wo is None or _as_bool(_wo)
        # 单模型没有「其它模型」可等：直接执行。仅凭这一条就能消掉
        # 「循环动画后自己把自己锁死」的主场景。
        if should_wait and not _other_live_insts(inst):
            should_wait = False
        if should_wait:
            # 关键：不再在工具调用里同步轮询其它模型。命令立即返回 accepted，
            # 后台线程等其它模型空闲后再真正下发，智能体的下一条命令不会被卡住。
            return _defer_action(inst, action, kwargs)
        # 新的非等待命令到来：作废之前还在等待的同类命令，避免旧命令迟到执行。
        _invalidate_deferred(inst, action)
    if action == "stop_speak":
        _invalidate_deferred(inst, "speak")
    if action == "stop_animation":
        _invalidate_deferred(inst, "play_animation")

    handlers = {
        "hide": _cmd_hide, "toggle": _cmd_toggle,
        "move": _cmd_move, "resize": _cmd_resize, "update": _cmd_update,
        "get_params": _cmd_get_params, "search_params": _cmd_search_params,
        "set_params": _cmd_set_params,
        "express": _cmd_express,
        "start_stream": _cmd_start_stream, "append_frames": _cmd_append_frames,
        "stop_stream": _cmd_stop_stream,
        "set_sound": _cmd_set_sound,
        "list_motions": _cmd_list_motions,
        "list_expressions": _cmd_list_expressions,
        "play_animation": _cmd_play_animation,
        "stop_animation": _cmd_stop_animation,
        # 配音：本地 Qwen3-TTS / 在线 Edge，字幕逐字高亮 + BGM + 动画同播
        "speak": _cmd_speak, "stop_speak": _cmd_stop_speak,
        "tts_status": _cmd_tts_status, "tts_unload": _cmd_tts_unload,
        "tts_config": _cmd_tts_config, "tts_setup": _cmd_tts_setup,
        # 诊断：读取运行中页面的真实渲染结果（画布 PNG / 原始帧统计）
        "dump_frame": _cmd_dump_frame, "frame_stats": _cmd_frame_stats,
        "debug_info": _cmd_debug_info,
    }
    handler = handlers.get(action)
    if not handler:
        return {"error": "未知 action: %s，可用：%s" % (action, list(handlers.keys()))}
    with _bound(inst):
        return handler(kwargs)


# ===== 窗口 & 模型 =====
_UI_CFG_PATH = Path(_PLUGIN_DIR) / "_pet_ui.json"
_MODEL_LIB_CACHE = {"key": None, "items": [], "ts": 0.0}
_MODEL_LIB_LIMIT = 1000


def _load_ui_cfg() -> dict:
    """读取右键菜单保存的配置（model_root 是用户选择的模型目录）。"""
    try:
        with open(str(_UI_CFG_PATH), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _model_root() -> str:
    root = str(_load_ui_cfg().get("model_root") or "").strip()
    return root if root and os.path.isdir(root) else ""


def _scan_model_library(root: str, ttl: float = 300.0) -> list:
    """扫描右键菜单设置的模型目录，返回 [(唯一显示名, model3.json 绝对路径)]。

    只向下 2 层、跳过 `_` 开头的临时目录；名字用模型所在目录名，重名时
    先补父目录，仍冲突再补序号 —— 保证「list_models 拿到的名字」能反查回
    唯一路径，show 时直接传名字即可。
    """
    root_abs = os.path.abspath(root)
    if not os.path.isdir(root_abs):
        return []
    try:
        root_mtime = os.stat(root_abs).st_mtime_ns
    except OSError:
        root_mtime = 0
    key = (os.path.normcase(root_abs), root_mtime)
    now = time.monotonic()
    if (_MODEL_LIB_CACHE.get("key") == key
            and now - float(_MODEL_LIB_CACHE.get("ts") or 0) < ttl):
        return _MODEL_LIB_CACHE["items"]
    root_depth = root_abs.rstrip("\\/").count(os.sep)
    found = []
    try:
        for cur, dirs, files in os.walk(root_abs):
            depth = os.path.abspath(cur).count(os.sep) - root_depth
            dirs[:] = [d for d in dirs if not d.startswith("_") and depth < 2]
            for fn in files:
                if fn.lower().endswith(".model3.json"):
                    found.append(os.path.join(cur, fn))
                    if len(found) >= _MODEL_LIB_LIMIT:
                        break
            if len(found) >= _MODEL_LIB_LIMIT:
                break
    except Exception:
        pass
    base_names = {}
    for path in found:
        base = os.path.basename(os.path.dirname(path)) or \
            os.path.splitext(os.path.basename(path))[0]
        base_names[path] = base
    counts = {}
    for base in base_names.values():
        counts[base] = counts.get(base, 0) + 1
    items = []
    used = set()
    for path in sorted(base_names):
        base = base_names[path]
        if counts.get(base, 0) > 1:
            parent = os.path.basename(os.path.dirname(os.path.dirname(path)))
            if parent:
                base = "%s (%s)" % (base, parent)
        name = base
        n = 2
        while name.casefold() in used:
            name = "%s #%d" % (base, n)
            n += 1
        used.add(name.casefold())
        items.append((name, path))
    items.sort(key=lambda t: t[0].casefold())
    _MODEL_LIB_CACHE.update({"key": key, "items": items, "ts": now})
    return items


def _library_model_file(name: str) -> str:
    """按 list_models 的名字在配置目录里找唯一 model3.json。找不到返回空串。"""
    root = _model_root()
    if not root:
        return ""
    hits = [path for n, path in _scan_model_library(root)
            if str(n).casefold() == str(name or "").casefold()]
    return hits[0] if len(hits) == 1 else ""


def _resolve_model_path(model_path: str) -> str:
    """把调用方的 model_path 解析成 model3.json 绝对路径。

    支持四种写法：
      1. 已打开模型的 model_name（list_open_models 返回的字段，文件地址还没拿到时最方便）
      2. list_models 返回的模型目录名（右键菜单设置的模型目录）
      3. 模型目录
      4. 直接指定 model3.json
    """
    raw = str(model_path or "").strip()
    if not raw:
        raise ValueError("model_path 不能为空")
    low = raw.casefold()
    named = []
    for inst in list(_instances.values()):
        names = {str(inst.name or "").casefold(),
                 Path(inst.model_file).name.casefold(),
                 Path(inst.model_file).stem.casefold()}
        if low in names:
            named.append(inst)
    if named:
        files = {inst.model_file for inst in named}
        if len(files) > 1:
            raise ValueError("模型名 %r 对应多个不同模型，请改用 model_path 文件地址：%s"
                             % (raw, ", ".join(sorted(files))))
        return next(iter(files))
    p = Path(raw).resolve()
    if p.is_file() and p.suffix == ".json":
        return str(p)
    if p.is_dir():
        json_files = list(p.glob("*.model3.json"))
        if json_files:
            return str(json_files[0])
    lib = _library_model_file(raw)
    if lib:
        return lib
    raise ValueError("找不到 model3.json：%s" % raw)


def _handle_show(kwargs):
    """show = 幂等：没开就新建一个实例，已开就显示并应用位置/缩放。"""
    _prune_dead()
    mp = str(kwargs.get("model_path") or "").strip()
    if mp:
        try:
            model_file = _resolve_model_path(mp)
        except ValueError as e:
            return {"ok": False, "action": "show", "error": str(e)}
        insts = _find_insts(model_file)
        if not insts:
            return _create_inst(mp, model_file, kwargs.get("x"), kwargs.get("y"),
                                kwargs.get("scale"))
        # 同一个模型开了多个窗口时，show 只复用最早那个（幂等，不无限加窗口）
        inst = insts[0]
    elif not _instances:
        if not _DEFAULT_MODEL_DIR.exists():
            return {"ok": False, "action": "show",
                    "error": "未指定 model_path，且默认模型不存在：%s" % _DEFAULT_MODEL_DIR}
        default_file = _resolve_model_path(str(_DEFAULT_MODEL_DIR))
        return _create_inst(str(_DEFAULT_MODEL_DIR), default_file,
                            kwargs.get("x"), kwargs.get("y"), kwargs.get("scale"))
    elif len(_instances) == 1:
        inst = next(iter(_instances.values()))
    else:
        return {"ok": False, "action": "show", "count": len(_instances),
                "open": _open_brief(),
                "error": "当前已打开 %d 个模型，show 需要用 model_path 指定显示哪个"
                         % len(_instances),
                "hint": "model_path 取 open 列表里的 model 或 model_name"}

    _refresh(inst)
    try:
        nx = float(kwargs.get("x")) if kwargs.get("x") is not None else inst.pos["x"]
        ny = float(kwargs.get("y")) if kwargs.get("y") is not None else inst.pos["y"]
        ns = float(kwargs.get("scale")) if kwargs.get("scale") is not None else inst.scale
    except (TypeError, ValueError):
        return {"ok": False, "action": "show", "error": "x / y / scale 必须是数字"}
    nx = max(0.0, min(1.0, nx))
    ny = max(0.0, min(1.0, ny))
    ns = max(MIN_SCALE, min(MAX_SCALE, ns))

    inst.client.send_command({"type": "show", "x": nx, "y": ny, "scale": ns})
    inst.pos.update(x=nx, y=ny)
    inst.scale = ns

    res = {"ok": True, "visible": True, "model_ready": bool(inst.ready),
           "count": len(_instances), "model_name": inst.name}
    return res


def _handle_close(kwargs):
    """close 带 model_path = 关掉这一个；不带 = 关掉全部（进程结束，内存还给系统）。"""
    _prune_dead()
    mp = str(kwargs.get("model_path") or "").strip()
    closed: list = []
    if mp:
        try:
            model_file = _resolve_model_path(mp)
        except ValueError as e:
            return {"ok": False, "action": "close", "error": str(e)}
        insts = _find_insts(model_file)
        if not insts:
            return {"ok": True, "action": "close", "count": len(_instances),
                    "visible": bool(_instances), "closed": [],
                    "warning": "该模型本来就没打开：%s" % model_file}
        # 同一个模型开了多个窗口时，close(model_path) 把它们全部关掉（语义是「关掉这个模型」）
        for inst in insts:
            closed.append((_destroy(inst) or {}).get("model_name"))
    else:
        for inst in list(_instances.values()):
            closed.append(_destroy(inst))

    remaining = list(_instances.values())
    visible = False
    for inst in remaining:
        st = inst.client.state(0.8) or {}
        if st.get("visible"):
            visible = True
            break
    res = {"ok": True, "closed": closed, "count": len(remaining),
           "visible": visible}
    return res


def _cmd_close_all(kwargs):
    """把当前所有模型一起关掉（每个模型一个进程，全部结束后内存整份释放）。"""
    _prune_dead()
    closed = []
    for inst in list(_instances.values()):
        closed.append((_destroy(inst) or {}).get("model_name"))
    res = {"ok": True, "closed": closed,
           "count": len(_instances), "visible": False}
    if not closed:
        res["warning"] = "当前没有任何模型在渲染"
    return res


def _handle_switch(kwargs):
    """switch = 把某个实例换成另一个模型（关旧 + 在原位置建新），不是新增。"""
    _prune_dead()
    mp = str(kwargs.get("model_path") or "").strip()
    if not mp:
        return {"ok": False, "action": "switch", "error": "switch 需要 model_path（新模型）"}
    try:
        new_file = _resolve_model_path(mp)
    except ValueError as e:
        return {"ok": False, "action": "switch", "error": str(e)}

    src = str(kwargs.get("from_model_path") or kwargs.get("old_model_path") or "").strip()
    old = None
    if src:
        try:
            old_file = _resolve_model_path(src)
        except ValueError as e:
            return {"ok": False, "action": "switch", "error": str(e)}
        insts = _find_insts(old_file)
        if not insts:
            return {"ok": False, "action": "switch", "error": "要替换的模型未打开：%s" % old_file,
                    "open": _open_brief()}
        old = insts[0]      # 同模型多窗口时换掉最早那个
    elif len(_instances) == 1:
        old = next(iter(_instances.values()))
    elif len(_instances) > 1:
        return {"ok": False, "action": "switch", "count": len(_instances),
                "open": _open_brief(),
                "error": "当前打开了 %d 个模型，switch 需要 from_model_path 指定替换哪个"
                         % len(_instances),
                "hint": "只想新增模型请用 action=show(model_path=...)"}

    if old is not None and old.model_file == new_file:
        return {"ok": True, "count": len(_instances),
                "model_name": old.name,
                "warning": "目标模型与要替换的模型相同，未做任何改动"}
    if old is None and _find_insts(new_file):
        return {"ok": False, "action": "switch", "count": len(_instances),
                "error": "该模型已在屏幕上（一个模型对应一个实例）", "open": _open_brief()}

    # 沿用旧实例的位置/缩放（位置接着原来的槽位，不会跳到默认角落）
    if old is not None:
        x = kwargs.get("x") if kwargs.get("x") is not None else old.pos["x"]
        y = kwargs.get("y") if kwargs.get("y") is not None else old.pos["y"]
        scale = kwargs.get("scale") if kwargs.get("scale") is not None else old.scale
        track = old.track
        replaced = _inst_info(old)
        _destroy(old)
    else:
        x, y, scale = kwargs.get("x"), kwargs.get("y"), kwargs.get("scale")
        track = None
        replaced = None

    res = _create_inst(mp, new_file, x, y, scale, track=track)
    # 替换成功即为最终结果；旧模型信息不回传（要看当前清单用 list_open_models）
    res.pop("action", None)
    return res


def _cmd_list_models(kwargs):
    """列出右键菜单设置的模型目录下的模型名（只返回名字，不返回路径）。

    show(model_path=<名字>) 会用同一份目录解析回 model3.json，所以这个列表
    和加载用的是同一套名字，不需要智能体再拼路径。
    """
    root = _model_root()
    if not root:
        return {
            "ok": False,
            "error": "尚未设置模型目录",
            "hint": "请先在桌宠右键菜单选择「模型目录」，再调用 list_models。",
        }
    items = _scan_model_library(root)
    names = [name for name, _path in items]
    res = {"ok": True, "count": len(names), "models": names}
    if not names:
        res["warning"] = "模型目录里没有找到 *.model3.json"
    return res


def _cmd_list_open_models(kwargs):
    """查询当前打开了哪些模型（各自的位置/缩放/可见/就绪/帧率）。

    除了注册表里的实例，还会扫描 worker 自述文件把「插件进程重启前拉起、仍在屏幕
    上渲染」的遗留窗口收养进来 —— 否则那种窗口会被漏报（用户看到的就是这份清单
    少了几个）。
    """
    _prune_dead()
    models = []
    for inst in list(_instances.values()):
        st = inst.client.state(1.0) or {}
        # 右键菜单换过模型时，worker 的 state.model 才是当前真实文件；
        # 先同步路由键，保证返回的 model_path 和 model_name 对应同一个模型。
        st_model = str(st.get("model") or "")
        if st_model and st_model != inst.model_file:
            try:
                if os.path.isfile(st_model):
                    _rekey(inst, st_model)
            except Exception:
                pass
        models.append({
            "model_name": st.get("model_name") or inst.name,
            "model_path": inst.model_file,
            "x": st.get("x", inst.pos["x"]),
            "y": st.get("y", inst.pos["y"]),
            "scale": st.get("scale", inst.scale),
            "visible": bool(st.get("visible")),
            "ready": bool(st.get("ready", inst.ready)),
            "streaming": inst.streaming,
        })
    res = {"ok": True, "count": len(models), "models": models}
    return res


# ===== 显隐 =====
def _cmd_hide(kwargs):
    """只隐藏（不销毁）：模型仍占内存，恢复用 show。要释放请用 close。"""
    _window.send_command({"type": "hide"})
    res = {"ok": True, "visible": False}
    return res


def _cmd_toggle(kwargs):
    was = _visible_state()
    _window.send_command({"type": "toggle"})
    res = {"ok": True}
    if isinstance(was, bool):
        res["visible"] = not was
    return res


# ===== 布局 =====
def _cmd_move(kwargs):
    global _current_pos, _window
    if not _window:
        return _no_window()
    x = kwargs.get("x")
    y = kwargs.get("y")
    dx = kwargs.get("dx", 0)
    dy = kwargs.get("dy", 0)

    new_x = (x if x is not None else _current_pos["x"]) + dx
    new_y = (y if y is not None else _current_pos["y"]) + dy
    raw_x, raw_y = new_x, new_y
    new_x = max(0.0, min(1.0, new_x))
    new_y = max(0.0, min(1.0, new_y))
    _current_pos.update(x=new_x, y=new_y)

    _window.send_command({"type": "setPosition", "x": new_x, "y": new_y})
    res = {"ok": True, "visible": _visible_state()}
    # 只有在「入参不是最终位置」时才回坐标：用了 dx/dy，或越界被夹取
    if x is None or abs(new_x - raw_x) > 1e-6:
        res["x"] = new_x
    if y is None or abs(new_y - raw_y) > 1e-6:
        res["y"] = new_y
    return res


def _cmd_resize(kwargs):
    global _current_scale, _window
    if not _window:
        return _no_window()
    scale = kwargs.get("scale")
    dscale = kwargs.get("dscale", 0)
    new_scale = (scale if scale is not None else _current_scale) + dscale
    raw_scale = new_scale
    new_scale = max(MIN_SCALE, min(MAX_SCALE, new_scale))
    _current_scale = new_scale
    _window.send_command({"type": "setScale", "scale": new_scale})
    res = {"ok": True, "visible": _visible_state()}
    if scale is None or abs(new_scale - raw_scale) > 1e-6:
        res["scale"] = new_scale
    return res


def _cmd_update(kwargs):
    """同时更新位置 + 大小（连续输出用）。"""
    r1 = _cmd_move(kwargs)
    r2 = _cmd_resize(kwargs)
    res = {"ok": True, "visible": r2.get("visible", r1.get("visible"))}
    pos = {}
    if "x" in r1:
        pos["x"] = r1["x"]
    if "y" in r1:
        pos["y"] = r1["y"]
    if pos:
        res["pos"] = pos
    if "scale" in r2:
        res["scale"] = r2["scale"]
    return res


# ===== 参数索引 / 格式化 / 模型信息 =====
def _num(v):
    """数值收敛到最多 3 位小数：-4.974799633026123 → -4.975，-30.0 → -30。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    r = round(f, 3)
    if r == 0:
        return 0
    if r == int(r):
        return int(r)
    return r


def _short_model_name(model_file):
    """符玄.model3.json → 符玄"""
    name = Path(model_file).name
    for suffix in (".model3.json", ".json"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _model_info(name_only=False):
    """当前实际已加载的模型：{"model": 绝对路径, "model_name": 短名}。

    name_only=True 时只给 model_name（get_params/search_params 用，省地址长度）。
    未加载 / 已 close 时返回 {}（配合 _prune_empty 整体消失）。
    """
    if not _loaded_model:
        return {}
    if _window is not None:
        try:
            if not _window.is_running():
                return {}
        except Exception:
            pass
    base = {"model": _loaded_model, "model_name": _short_model_name(_loaded_model)}
    if name_only:
        return {"model_name": base["model_name"]}
    return base


def _find_cdi3(model_file):
    """按 model3.json 定位同目录的 cdi3.json（模型自定义参数的名称来源）。"""
    d = Path(model_file).parent
    name = Path(model_file).name
    base = name[:-len(".model3.json")] if name.endswith(".model3.json") else Path(model_file).stem
    cand = d / f"{base}.cdi3.json"
    if cand.is_file():
        return cand
    files = sorted(d.glob("*.cdi3.json"))
    return files[0] if files else None


def _build_param_index(model_file):
    """{参数Id: {"alias": 中文名, "group": ParamGroupNN}}，按文件 mtime 缓存。"""
    if not model_file:
        return {}
    cdi = _find_cdi3(model_file)
    if not cdi:
        return {}
    try:
        key = (str(cdi), cdi.stat().st_mtime)
    except OSError:
        return {}
    cached = _PARAM_ALIAS_CACHE.get(key)
    if cached is not None:
        return cached
    index = {}
    try:
        data = json.loads(cdi.read_text(encoding="utf-8-sig"))
        for p in data.get("Parameters") or []:
            pid = p.get("Id")
            if not pid:
                continue
            alias = (p.get("Name") or "").strip()
            group = (p.get("GroupId") or "").strip()
            if alias or group:
                index[pid] = {"alias": alias, "group": group}
    except Exception:
        return {}
    _PARAM_ALIAS_CACHE.clear()
    _PARAM_ALIAS_CACHE[key] = index
    return index


def _ensure_param_meta():
    """模型全部参数及其取值的「名字 + 范围」：([{name,min,max}], 错误字典)。

    结果按模型分别缓存（多实例下各模型参数集不同，来回操作不同模型时不用重取）。
    """
    global _PARAM_META
    if not _window:
        return None, _no_window()
    model_key = _loaded_model or ""
    items = _PARAM_META.get(model_key)
    if items:
        return items, None
    raw = _window.send_command_sync({"type": "getParams"})
    if not isinstance(raw, dict):
        return None, {"ok": False, "action": "get_params", "error": "无效的 getParams 返回"}
    if raw.get("error"):
        return None, {"ok": False, "action": "get_params",
                      "error": "模型尚未加载完成，请稍后重试"}
    items = []
    for p in raw.get("params") or []:
        name = p.get("name")
        if not name:
            continue
        items.append({"name": name, "min": _num(p.get("min")), "max": _num(p.get("max"))})
    if items:
        # 只保留本模型的缓存（同屏最多 6 个，不必做 LRU）
        _PARAM_META[model_key] = items
    return items, None


def _param_record(name, meta, index):
    """一条件：name + 可选 alias + 可选 range，不给当前值与默认值。"""
    rec = {"name": name}
    info = index.get(name) or {}
    alias = info.get("alias") or _STD_ALIAS.get(name)
    if alias:
        rec["alias"] = alias
    lo, hi = meta.get("min"), meta.get("max")
    if lo is not None and hi is not None:
        rec["range"] = [lo, hi]
    return rec


def _param_sort_key(rec):
    """稳定排序：标准可读参数在前，Param+数字 / ArtMesh 变形参数统一沉底。"""
    name = rec["name"]
    vague = bool(_RE_VAGUE_PARAM.match(name)) or "artmesh" in name.lower()
    tier = 2 if vague else (0 if rec.get("alias") else 1)
    return (tier, name)


def _dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def _fit_page(page, base, cap=_PARAMS_MAX_CHARS):
    """把一页参数裁到 base+page 序列化后不超过 cap 字符（二分，返回裁剪后列表）。"""
    if not page:
        return []
    lo, hi, best = 1, len(page), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        probe = dict(base)
        probe["shown"] = mid
        probe["params"] = page[:mid]
        probe.pop("next_offset", None)
        if len(_dumps(probe)) <= cap:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return page[:best] if best else page[:1]


def _resolve_param_keys(key, index, names):
    """把调用方写的参数名归一化成模型里的真实参数 id（可能多个：同族整体控制）。

    接受：真实 id（ParamMouthOpenY）、去 Param 前缀短名（MouthOpenY）、
    口语别名（嘴 / 眨眼）、大小写不敏感。
    """
    if not key:
        return []
    k = str(key).strip()
    if not k:
        return []
    if k in names:
        return [k]
    lower = k.lower()
    case = [n for n in names if n.lower() == lower]
    if case:
        return case[:1]
    # 口语别名（精确命中；"眨眼"这类会映射到左右眼两个参数）
    pop = _POP_ALIAS.get(k)
    if pop:
        hits = [p for p in pop if p in names]
        if hits:
            return hits
    # 去 Param 前缀短名（唯一命中才采用）
    short = lower[5:] if lower.startswith("param") else lower
    hits = [n for n in names
            if (n.lower()[5:] if n.lower().startswith("param") else n.lower()) == short]
    if len(hits) == 1:
        return hits
    # cdi3 中文名精确匹配：同名不同组（"外紫环 转" = Param421/422）按同族整体设置
    ex = [pid for pid, info in index.items()
          if (info.get("alias") or "").strip().lower() == lower and pid in names]
    if ex:
        return ex[:6]
    # 中文名包含匹配（唯一命中才采用，避免"嘴"这种一词多义误设一堆参数）
    inc = [pid for pid, info in index.items()
           if lower in (info.get("alias") or "").strip().lower() and pid in names]
    if len(inc) == 1:
        return inc
    # 标准参数内置中文别名
    std = [n for n, a in _STD_ALIAS.items() if a.lower() == lower and n in names]
    if std:
        return std[:1]
    return []


def _resolve_param_key(key, index, names):
    """单值版本：解析不出返回 None（供提示/预览使用）。"""
    hits = _resolve_param_keys(key, index, names)
    return hits[0] if hits else None


def _resolve_params_dict(params, index, names):
    """归一化 {"键": 值}；同族多命中会展开成多个真实参数。

    返回 (真实名dict, 无法解析的键列表)。
    """
    ok, unknown = {}, []
    for k, v in (params or {}).items():
        keys = _resolve_param_keys(k, index, names)
        if not keys:
            unknown.append(str(k))
            continue
        for real in keys:
            ok[real] = v
    return ok, unknown


# ===== 捏脸参数 =====
def _cmd_get_params(kwargs):
    """参数清单：name + alias(可选) + range(可选)，支持 offset/limit 分片。

    - 默认返回全部（limit 省略）；
    - 一页序列化后超过 _PARAMS_MAX_CHARS 时自动截断并给 next_offset，
      调用方顺着 next_offset 一段段读即可，不会被框架层静默砍掉。
    """
    if not _window:
        return _no_window()
    items, err = _ensure_param_meta()
    if err:
        return err

    index = _build_param_index(_loaded_model or "")
    full = []
    for m in items:
        full.append(_param_record(m["name"], m, index))
    full.sort(key=_param_sort_key)

    total = len(full)
    try:
        offset = int(kwargs.get("offset") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "action": "get_params",
                "error": f"offset 必须是整数：{kwargs.get('offset')}"}
    if offset < 0:
        offset = 0
    limit = kwargs.get("limit")
    if limit is not None and str(limit).strip() != "":
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return {"ok": False, "action": "get_params",
                    "error": f"limit 必须是整数：{kwargs.get('limit')}"}
        if limit <= 0:
            limit = None

    if offset >= total:
        res = {"ok": True, "total": total}
        res.update(_model_info(name_only=True))
        res["hint"] = f"offset 超出总数（共 {total} 个参数），已从头开始读请传 offset=0"
        return res

    page = full[offset:] if limit is None else full[offset:offset + limit]
    next_off = None
    if limit is None or offset + limit < total:
        next_off = offset + len(page)

    base = {"ok": True, "total": total}
    base.update(_model_info(name_only=True))

    fitted = _fit_page(page, base)
    truncated = len(fitted) < len(page)
    if truncated:
        page = fitted
        next_off = offset + len(page)

    res = {"ok": True, "total": total}
    res["shown"] = len(page)
    res["params"] = page
    res.update(_model_info(name_only=True))
    if next_off is not None and next_off < total:
        res["next_offset"] = next_off
    if truncated:
        res["truncated"] = True
    return res


def _cmd_search_params(kwargs):
    """一次搜索多个参数：query 用 , ; | 、 换行 分隔，按词分组返回。

    返回每组的 name/alias/range（与 get_params 同结构）+ 末尾 params 汇总，
    调用方拿到后可直接拼 stream_data 做自定义动画。
    """
    if not _window:
        return _no_window()
    raw_query = kwargs.get("query")
    if not raw_query or not str(raw_query).strip():
        return {"ok": False, "action": "search_params",
                "error": "search_params 需要 query（如 \"紫环 转, 嘴, 眨眼\"）"}

    items, err = _ensure_param_meta()
    if err:
        return err
    index = _build_param_index(_loaded_model or "")
    recs = [_param_record(m["name"], m, index) for m in items]
    names = [r["name"] for r in recs]

    try:
        limit = int(kwargs.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 30))

    queries = [q.strip() for q in _RE_MULTI_QUERY.split(str(raw_query)) if q.strip()]
    if not queries:
        return {"ok": False, "action": "search_params", "error": "query 没有有效内容"}

    groups = []
    flat = []

    def _score(term, rec):
        name = rec["name"].lower()
        alias = (rec.get("alias") or "").lower()
        t = term.lower()
        if "artmesh" in name and "artmesh" not in t:
            return 0
        # 口语别名直击：「眨眼」→ ParamEyeLOpen/ROpen，不依赖模型自己的命名
        pop = _POP_ALIAS.get(term.strip())
        if pop and rec["name"] in pop:
            return 95
        score = 0
        if alias and alias == t:
            score = 100
        elif alias and t in alias:
            score = 80
        elif t in name:
            score = 60
        else:
            words = [w for w in re.split(r"[\s_]+", t) if w]
            if len(words) > 1:
                hit = sum(1 for w in words if w in alias or w in name)
                score = 40 * hit
        if score and _RE_VAGUE_PARAM.match(rec["name"]):
            score -= 30
        return score

    for term in queries:
        hits = []
        for rec in recs:
            s = _score(term, rec)
            if s > 0:
                hits.append((s, rec))
        if not hits:
            # 整串没命中：退化为按空格分词再搜（中文别名含空格时仍能救回来）
            words = [w for w in re.split(r"[\s]+", term) if len(w) > 1]
            if len(words) > 1:
                for rec in recs:
                    s = sum(_score(w, rec) for w in words)
                    if s > 0:
                        hits.append((s, rec))
        hits.sort(key=lambda t: (-t[0], _param_sort_key(t[1])))
        picked = [rec for _, rec in hits[:limit]]
        group = {"q": term}
        if picked:
            group["total"] = len(hits)
            group["hits"] = picked
            for rec in picked:
                if rec["name"] not in flat:
                    flat.append(rec["name"])
        groups.append(group)

    res = {"ok": True, "action": "search_params", "queries": groups}
    res.update(_model_info(name_only=True))
    if flat:
        res["params"] = flat
        res["hint"] = "range 为可用取值区间；params 可直接填进 start_stream 的 stream_data"
    return res


def _cmd_set_params(kwargs):
    global _window, _stream_active
    if not _window:
        return _no_window()
    params_str = kwargs.get("params", "{}")
    smooth_ms = kwargs.get("smooth_ms", 200)
    try:
        params = json.loads(params_str)
    except json.JSONDecodeError as e:
        return {"error": f"params JSON 解析失败: {e}"}
    if not isinstance(params, dict):
        return {"error": "params 必须是 JSON 对象，如 '{\"ParamAngleX\": 15}'"}

    # 名称归一化：真实 id / 去 Param 前缀短名 / 中文别名（含 cdi3 名称）都能认
    items, _ = _ensure_param_meta()
    index = _build_param_index(_loaded_model or "")
    names = [m["name"] for m in (items or [])]
    unknown = []
    if names:
        params, unknown = _resolve_params_dict(params, index, names)
    if not params:
        return {"ok": False, "action": "set_params", "unknown": unknown,
                "error": "没有任何参数能解析成当前模型的参数名",
                "hint": "先用 search_params 查可用参数，如 search_params(query=\"嘴\")"}

    _window.send_command({"type": "setParams", "params": params, "smoothMs": smooth_ms})
    res = {"ok": True, "applied": len(params)}
    if unknown:
        res["unknown"] = unknown
    if _visible_state() is False:
        # 参数已写入但模型不可见：告诉 AI 现在看不到效果，show 后会立即生效
        res["warning"] = "桌宠当前隐藏，参数已写入但看不到效果；调用 action=show 后会立即按此参数显示"
    elif _stream_active:
        res["warning"] = "流式动画运行中，此设置可能被覆盖"
    return res


def _wait_value(value) -> float:
    """wait 参数统一入口：秒，默认 0，上限 30；非法值按 0。"""
    try:
        sec = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(30.0, sec))


def _wait_after_tts(value) -> None:
    """动作/表情/动画的 wait：先等当前配音（含排队唱歌）播完，再等 N 秒。"""
    sec = _wait_value(value)
    if sec <= 0:
        return
    if _window is not None:
        try:
            while True:
                st = _window.tts_status()
                if not isinstance(st, dict) or st.get("ok") is False:
                    break
                if not st.get("speaking") and not st.get("queued"):
                    break
                time.sleep(0.15)
        except Exception:
            pass
    time.sleep(sec)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


# 内置动画的近似时长（与 renderer.html 的 BUILTIN_ANIM 关键帧保持同源）：
# wait_others 只需要判断「动作大概什么时候结束」，不需要逐帧精确。
_BUILTIN_MOTION_SECONDS = {
    "nod": 0.7, "shake": 0.72, "tilt": 0.9,
    "sway": 1.3, "look_around": 2.28, "cheer": 1.1,
}
_MOTION_DURATION_CACHE: dict = {}


def _motion_duration_seconds(model_file: str, motion_name: str) -> float:
    """读 model3.json 指向的 .motion3.json，取 Meta.Duration（秒）。"""
    name = str(motion_name or "").strip()
    if not model_file or not name:
        return 1.2
    try:
        mtime = os.stat(model_file).st_mtime_ns
    except OSError:
        mtime = 0
    ck = (os.path.normcase(os.path.abspath(model_file)), mtime, name.casefold())
    if ck in _MOTION_DURATION_CACHE:
        return _MOTION_DURATION_CACHE[ck]
    file_path = ""
    try:
        with open(model_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        motions = ((data.get("FileReferences") or {}).get("Motions") or {})
        base = name.replace("\\", "/")
        group_req = None
        if "/" in base:
            prefix, base = base.rsplit("/", 1)
            group_req = None if prefix == "default" else prefix
        hits = []
        for group, items in motions.items():
            if group_req is not None and str(group) != group_req:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                rel = str(item.get("File") or "")
                stem = Path(rel).stem
                # motion 文件名是 xxx.motion3.json，Path.stem 只去掉 .json，
                # 还会留下 .motion3；动画名对应的是 xxx，这里再剥一层。
                if stem.lower().endswith(".motion3"):
                    stem = stem[:-8]
                if stem.casefold() == base.casefold():
                    hits.append(rel)
        if len(hits) == 1:
            file_path = str(Path(model_file).parent / hits[0])
    except Exception:
        file_path = ""
    dur = 1.2
    if file_path and os.path.isfile(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                meta = (json.load(f).get("Meta") or {})
            dur = float(meta.get("Duration") or 1.2) + 0.25
        except Exception:
            pass
    dur = max(0.2, min(120.0, dur))
    _MOTION_DURATION_CACHE[ck] = dur
    return dur


def _mark_motion_busy(seconds: float, loop: bool = False) -> None:
    """记录当前实例上一个显式动作何时结束（跨模型 wait_others 判断用）。"""
    try:
        key = _key(_loaded_model or "")
        if not key:
            return
        if loop:
            # 循环动画只按短暂占用记（见 _LOOP_BUSY_SECONDS 的说明），不写 inf。
            _MOTION_BUSY[key] = {
                "until": time.monotonic() + _LOOP_BUSY_SECONDS,
                "loop": True,
            }
        else:
            _MOTION_BUSY[key] = {
                "until": time.monotonic() + max(0.2, float(seconds)),
                "loop": False,
            }
    except Exception:
        pass


def _clear_motion_busy() -> None:
    try:
        _MOTION_BUSY.pop(_key(_loaded_model or ""), None)
    except Exception:
        pass


def _motion_busy_label(model_file: str) -> str:
    rec = _MOTION_BUSY.get(_key(model_file))
    if not rec:
        return ""
    if time.monotonic() >= float(rec.get("until") or 0):
        _MOTION_BUSY.pop(_key(model_file), None)
        return ""
    if rec.get("loop"):
        return "动画循环中"
    return "动画播放中"


def _fifo_head_valid_token() -> int | None:
    """返回 FIFO 中第一个 valid（存在且未 invalidated 且未 executed）的 token。
    头部那些已经 executed 或 invalidated 的会被顺便清掉。"""
    with _deferred_lock:
        while _deferred_fifo:
            t = _deferred_fifo[0]
            rec = _deferred_tokens.get(t)
            if rec and not rec.get("invalidated") and not rec.get("executed"):
                return t
            # 头部无效/已执行，从 FIFO 移除
            _deferred_fifo.pop(0)
        return None


def _wait_others_idle(target_inst, token: int, timeout: float = 300.0):
    """等待时机：这条 defer 命令能安全下发的条件。

    需要同时满足：
    1. 自己是全局 FIFO 队首（保证跨模型按入队顺序串行）
    2. 其它模型的 worker 当前空闲（跨模型不抢话；不含目标自己）
    返回 None=可以执行；返回 dict=超时错误体。
    """
    target_key = _model_key_of(target_inst)
    all_insts = list(_instances.values())
    t0 = time.monotonic()
    while True:
        # —— 检查 FIFO 队首（跨模型串行的核心）
        head = _fifo_head_valid_token()
        if head is not None and head != token:
            # 我不是队首，等前面的命令跑完
            with _deferred_lock:
                ahead_rec = _deferred_tokens.get(head, {})
                ahead_key = ahead_rec.get("model_key", "")
            head_name = ahead_key
            for inst in all_insts:
                if _model_key_of(inst) == ahead_key:
                    head_name = inst.name or ahead_key
                    break
            busy = ["%s(排在我前面的命令尚未执行)" % head_name]
            if time.monotonic() - t0 >= timeout:
                return {
                    "ok": False,
                    "error": "等待队首执行超时（%d 秒）" % int(timeout),
                    "waiting": busy,
                    "hint": "需要跳过等待请用 wait_others=false。",
                }
            time.sleep(0.15)
            continue

        # —— 队首了（或 FIFO 暂时空了），现在检查其它 worker 的状态。
        # 注意：**不检查目标自己**。目标自己的忙闲由命令本身处理（speak 走 mode=queue、
        # play_animation 走 priority），把它算进等待条件会造成「等自己空闲」的死锁。
        busy = []
        for inst in all_insts:
            if inst is target_inst:
                continue
            if not inst.client.is_running():
                continue
            label = _motion_busy_label(inst.model_file)
            try:
                st = inst.client.tts_status()
            except Exception:
                st = {}
            if isinstance(st, dict) and st.get("ok") is not False:
                if st.get("speaking"):
                    label = label or "配音中"
                try:
                    if int(st.get("queued") or 0) > 0:
                        label = label or "排队配音中"
                except (TypeError, ValueError):
                    pass
            if label:
                busy.append("%s(%s)" % (inst.name, label))
        if not busy:
            return None
        if time.monotonic() - t0 >= timeout:
            return {
                "ok": False,
                "error": "等待 worker 空闲超时（%d 秒）" % int(timeout),
                "waiting": busy,
                "hint": "需要同时发声请设 wait_others=false；循环动画请先 stop_animation。",
            }
        time.sleep(0.15)


def _model_key_of(inst) -> str:
    """实例的跨模型唯一标识（与 _key 同源）。"""
    return _key(getattr(inst, "model_file", "") or "")


def _deferred_new(inst, action: str) -> int:
    """创建独立 defer 记录并入 FIFO；每条独立互不覆盖。"""
    global _deferred_seq
    with _deferred_lock:
        _deferred_seq += 1
        token = _deferred_seq
        model_key = _model_key_of(inst)
        _deferred_tokens[token] = {
            "model_key": model_key,
            "action": action,
            "invalidated": False,
            "executed": False,
            "created_at": time.monotonic(),
        }
        _deferred_fifo.append(token)
        # 新命令入队：清掉该模型上一次的失败留痕，避免旧错误一直挂在状态里
        try:
            _DEFER_ERRORS.pop(model_key, None)
        except Exception:
            pass
        return token


def _deferred_status_for(inst) -> dict:
    """把后台 defer 队列位置映射成 tts_status 可读字段。

    关键：命令已经 accepted，但还没真正下发到 worker，worker 自己的
    speaking/queued 都还是空的。这里补上 deferred 信息，智能体才不会误判。
    """
    if inst is None:
        return {}
    model_key = _model_key_of(inst)
    with _deferred_lock:
        fifo = list(_deferred_fifo)
        records = {k: dict(v) for k, v in _deferred_tokens.items()}
    active = []
    for t in fifo:
        rec = records.get(t)
        if rec and not rec.get("invalidated") and not rec.get("executed"):
            active.append(t)
    mine = [t for t in active if records[t].get("model_key") == model_key]
    err = _DEFER_ERRORS.get(model_key) or {}
    if not mine:
        # 队列已空：如果上一次 deferred 是超时/异常收场的，把原因留给调用方看
        if err:
            return {
                "deferred": False,
                "deferred_last_error": err.get("error") or "",
                "deferred_last_action": err.get("action") or "",
                "deferred_last_ago_ms": max(
                    0, int((time.time() - float(err.get("ts") or 0)) * 1000)),
            }
        return {}
    token = mine[0]
    rec = records[token]
    position = active.index(token) + 1
    try:
        since_ms = max(0, int((time.monotonic() - float(rec.get("created_at") or 0)) * 1000))
    except Exception:
        since_ms = 0
    out = {
        "deferred": True,
        "deferred_action": rec.get("action") or "",
        "deferred_position": position,
        "deferred_count": len(mine),
        "deferred_ahead": position - 1,
        "deferred_since_ms": since_ms,
    }
    if err:
        out["deferred_last_error"] = err.get("error") or ""
        out["deferred_last_action"] = err.get("action") or ""
    return out


def _deferred_valid(token: int) -> bool:
    """检查 token 是否有效（record 存在且未被 invalidate）。"""
    with _deferred_lock:
        rec = _deferred_tokens.get(token)
        return bool(rec and not rec.get("invalidated"))


def _invalidate_deferred(inst, action: str) -> None:
    """标记目标模型上指定 action 的所有 defer 记录为无效（但 FIFO 位置保留）。"""
    model_key = _model_key_of(inst)
    with _deferred_lock:
        for rec in _deferred_tokens.values():
            if rec.get("model_key") == model_key and rec.get("action") == action:
                rec["invalidated"] = True


def _deferred_cleanup_token(token: int) -> None:
    """worker 线程退出时清理：从 tokens 表移除 + 从 FIFO 移除（如果还在）。"""
    with _deferred_lock:
        _deferred_tokens.pop(token, None)
        try:
            _deferred_fifo.remove(token)
        except ValueError:
            pass


def _prepare_deferred_speech(inst, token: int, kwargs: dict) -> None:
    """后台把等待中的下一条说话先合成到目标模型缓存，不播放。

    只处理 speak、非唱歌、mode=queue。触发条件：
    - 命令还在 deferred 队列里（未被作废、未执行）；
    - 目标模型当前空闲（speaking=false 且 queued=0）。
    这样 A 说话时先把 B 的下一句算好，B 说话时再先把 A 的下一句算好。
    真正轮到时，_cmd_speak 的 speak(mode=queue) 会直接命中 service 的预取缓存。
    """
    if not isinstance(kwargs, dict):
        return
    if str(kwargs.get("mode") or "queue").strip().lower() != "queue":
        return
    if bool(kwargs.get("sing", False)):
        return
    text = html.unescape(str(kwargs.get("text") or ""))
    if not text.strip():
        return
    try:
        speed = None if kwargs.get("speed") is None else float(kwargs.get("speed"))
    except (TypeError, ValueError):
        speed = None
    # prepare 直接调 worker，不经过 _cmd_speak 的参数兜底：这里补一次 key 解析，
    # 否则预设里没有明文 key 时预合成会以“MiMo 没有 API Key”静默失败。
    backend = str(kwargs.get("backend") or "auto")
    api_key = str(kwargs.get("api_key") or "").strip()
    if not api_key and backend in ("mimo", "auto", ""):
        try:
            from tts.engine_mimo import find_api_key, load_settings
            api_key = find_api_key(load_settings())
        except Exception:
            api_key = ""
    payload = {
        "op": "prepare",
        "text": text,
        "backend": backend,
        "voice": str(kwargs.get("voice") or ""),
        "speed": speed,
        "api_key": api_key,
        "style": str(kwargs.get("style") or ""),
        "dialect": str(kwargs.get("dialect") or ""),
    }
    # 给“本来就是队首、马上要执行”的命令一个直接执行窗口，避免重复合成。
    time.sleep(0.2)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not _deferred_valid(token):
            return
        with _deferred_lock:
            rec = _deferred_tokens.get(token) or {}
            if rec.get("executed"):
                return
        if not inst.client.is_running():
            return
        try:
            st = inst.client.tts_status()
        except Exception:
            st = {}
        if isinstance(st, dict) and st.get("ok") is not False:
            if not st.get("speaking") and int(st.get("queued") or 0) == 0:
                try:
                    inst.client.tts(payload, timeout=30.0)
                except Exception:
                    pass
                return
        time.sleep(0.15)


def _record_defer_error(inst, action: str, waited) -> None:
    """后台等待超时/失败留痕。

    前台工具调用早已返回 accepted，后台如果只 ``return``，调用方和用户都看不到
    任何失败信号（本次事故就是这样：四条命令静默消失）。这里写日志，并把原因
    挂到 _DEFER_ERRORS，由 tts_status 的 deferred 字段回读。
    """
    try:
        key = _model_key_of(inst)
        info = {
            "action": action,
            "error": (waited or {}).get("error") or "deferred 超时",
            "waiting": (waited or {}).get("waiting") or [],
            "ts": time.time(),
        }
        _DEFER_ERRORS[key] = info
        print("[desktop_pet] deferred %s 失败（%s）：%s %s" % (
            action, getattr(inst, "name", ""), info["error"],
            ",".join(str(x) for x in info["waiting"])), flush=True)
    except Exception:
        pass


def _defer_action(inst, action: str, kwargs: dict):
    """wait_others：不阻塞工具调用，后台等到时机成熟后再执行本条命令。"""
    token = _deferred_new(inst, action)
    if action == "speak":
        # 排队期间提前合成：目标模型空闲时先把下一句算好，轮到它时直接出声音。
        threading.Thread(target=_prepare_deferred_speech,
                         args=(inst, token, kwargs), daemon=True,
                         name="desktop_pet_prepare_%s" % token).start()

    def _worker():
        try:
            while True:
                if not _deferred_valid(token):
                    return  # 被 invalidate
                waited = _wait_others_idle(inst, token)
                if waited is None:
                    break   # 时机成熟：自己是 FIFO 队首，其它 worker 都空闲
                _record_defer_error(inst, action, waited)
                return      # 等待超时
            if not _deferred_valid(token):
                return
            # 标记已执行：FIFO 清理逻辑会自动跳过这条，让后面的命令成为队首
            with _deferred_lock:
                rec = _deferred_tokens.get(token)
                if rec:
                    rec["executed"] = True
            # _bound 会改模块级 _window；必须和普通工具调用共用同一把锁
            with _lock:
                if not _deferred_valid(token):
                    return
                with _bound(inst):
                    if action == "speak":
                        _cmd_speak(kwargs)
                    elif action == "play_animation":
                        _cmd_play_animation(kwargs)
        except Exception as e:
            # 命令本身已在工具层返回 accepted；执行期失败不阻塞后续命令，
            # 但必须留痕，不能像以前那样静默吞掉。
            try:
                print("[desktop_pet] deferred %s 执行异常：%r" % (action, e), flush=True)
            except Exception:
                pass
        finally:
            _deferred_cleanup_token(token)

    threading.Thread(target=_worker, daemon=True,
                     name="desktop_pet_deferred_%s" % action).start()
    return {
        "ok": True,
        "deferred": True,
        "queued": True,
        "waiting_others": True,
        "target": getattr(inst, "name", ""),
        "hint": "命令已受理，后台等待时机成熟后执行",
    }


def _cmd_express(kwargs):
    global _window
    blocked = _require_visible("express")
    if blocked:
        return blocked
    _wait_after_tts(kwargs.get("wait"))
    expression = kwargs.get("expression")
    if expression:
        # expression 可以是模型自带表情名，也可以是内置预设 id（happy/sad/...）；
        # 先按名字试，失败再按内置预设试（模型没有该表情时自动兜底）。
        res = _window.send_command_sync(
            {"type": "setExpression", "sync": True, "name": str(expression)})
        if isinstance(res, dict) and res.get("ok"):
            return {"ok": True}
        res2 = _window.send_command_sync(
            {"type": "setExpression", "sync": True, "builtin": str(expression)})
        if isinstance(res2, dict) and res2.get("ok"):
            return {"ok": True}
        builtins = (res2 or {}).get("builtin_expressions") if isinstance(res2, dict) else None
        if isinstance(res, dict):
            return _prune_empty({
                "ok": False,
                "error": res.get("error") or "模型没有该表情，也不是内置预设：%s" % expression,
                "builtin_available": builtins,
            })
        return {"error": "无效的 setExpression 返回"}
    idx = kwargs.get("expression_index", 0)
    _window.send_command({"type": "setExpression", "index": idx})
    return {"ok": True}


# ===== 动作 & 表情（查看 / 播放）=====
def _motion_base_name(file_path) -> str:
    """motion3 文件路径 → 动画名：motions/touch_head.motion3.json → touch_head。"""
    name = str(file_path or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    low = name.lower()
    for suffix in (".motion3.json", ".motion.json", ".json"):
        if low.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or "motion"


def _cmd_list_motions(kwargs):
    """列出模型自带动作名 + 内置动画预设。"""
    if not _window:
        return _no_window()
    res = _window.send_command_sync({"type": "getAssets", "sync": True})
    if not isinstance(res, dict):
        return {"error": "无效的 getAssets 返回", "raw": str(res)}
    if res.get("error"):
        return res
    if res.get("model_loaded") is False:
        return {"ok": False, "error": "模型尚未加载完成，请稍后重试"}

    entries = []
    seen = set()
    for g in res.get("motions", []) or []:
        group_name = g.get("group") or ""
        for file_path in (g.get("files") or []):
            name = _motion_base_name(file_path)
            # 同组重复的同名文件只保留一条；跨组同名才需要组名前缀。
            key = (group_name, name)
            if key in seen:
                continue
            seen.add(key)
            entries.append((group_name, name))

    # 动画名优先直接给名字。只有跨组重名时，才用「组名/动画名」消歧；
    # 空名组显示为 default/动画名，避免 "group": "" 被裁剪后无法调用。
    name_groups = {}
    for group_name, name in entries:
        name_groups.setdefault(name, set()).add(group_name)

    animations = []
    for group_name, name in entries:
        duplicate = len(name_groups.get(name, ())) > 1
        # 列表直接给可播放的字符串：唯一名用动画名，跨组同名用「组名/动画名」。
        animations.append(f"{(group_name or 'default')}/{name}" if duplicate else name)

    return {
        "ok": True,
        "model_name": _short_model_name(_loaded_model or ""),
        "motion_groups": res.get("motion_groups", 0),
        "motion_total": len(entries),
        "animations": animations,
        "builtin_animations": res.get("builtin_animations", []),
        "current_motion": res.get("current_motion"),
    }


def _cmd_list_expressions(kwargs):
    """列出模型自带表情 + 内置表情预设。"""
    if not _window:
        return _no_window()
    res = _window.send_command_sync({"type": "getAssets", "sync": True})
    if not isinstance(res, dict):
        return {"error": "无效的 getAssets 返回", "raw": str(res)}
    if res.get("error"):
        return res
    if res.get("model_loaded") is False:
        return {"ok": False, "error": "模型尚未加载完成，请稍后重试"}
    return {
        "ok": True,
        "model_name": _short_model_name(_loaded_model or ""),
        "expressions": res.get("expressions", []),
        "builtin_expressions": res.get("builtin_expressions", []),
        "active_expression": res.get("active_expression"),
    }


def _cmd_play_animation(kwargs):
    """播放模型自带动画（motion_name）或内置动画预设（animation）。"""
    global _window, _stream_active
    blocked = _require_visible("play_animation")
    if blocked:
        return blocked
    _wait_after_tts(kwargs.get("wait"))

    animation = kwargs.get("animation")
    motion_name = str(kwargs.get("motion_name") or "").strip()
    priority = kwargs.get("priority") or "normal"
    loop = bool(kwargs.get("loop", False))

    if not animation and not motion_name:
        return {"error": "play_animation 需要 animation（内置动画 id）"
                         "或 motion_name（list_motions 的 animations[] 字符串）"}
    if priority not in ("idle", "normal", "force"):
        priority = "normal"

    cmd = {"type": "playMotion", "sync": True, "priority": priority, "loop": loop}
    if kwargs.get("reuse_same"):
        cmd["reuseSame"] = True
    if kwargs.get("lip_lock"):
        cmd["lipLock"] = True
    if kwargs.get("mute"):
        cmd["mute"] = True
    if animation:
        cmd["builtin"] = str(animation)
    elif motion_name:
        cmd["motionName"] = motion_name

    res = _window.send_command_sync(cmd)
    if not isinstance(res, dict):
        return {"error": "无效的 playMotion 返回", "raw": str(res)}
    ok = bool(res.get("ok"))
    out = {"ok": ok}
    if ok:
        out["has_sound"] = bool(res.get("sound"))
        if res.get("reused"):
            out["reused"] = True
        # 动画不算在 TTS 状态里：记一个到期时间，供其它模型 wait_others 判断空闲。
        if animation:
            _mark_motion_busy(_BUILTIN_MOTION_SECONDS.get(str(animation), 1.2), loop)
        elif motion_name:
            _mark_motion_busy(_motion_duration_seconds(_loaded_model or "", motion_name), loop)
        # 内置动画走流式参数引擎（无限循环时与流式动画同义：set_params 会被覆盖）
        _stream_active = bool(loop and animation)
    else:
        out["error"] = res.get("error") or "动作播放失败（先用 list_motions 查名称）"
    return out


def _cmd_stop_animation(kwargs):
    """停止当前动作（模型动作 + 内置动画），参数回到鼠标跟随/捏脸控制。"""
    global _window, _stream_active
    if not _window:
        return _no_window()
    res = _window.send_command_sync({"type": "stopMotion", "sync": True})
    _stream_active = False
    _clear_motion_busy()
    return {"ok": True}


# ===== 配音（TTS）=====
_DEFAULT_VOICE = "Serena"


def _cmd_speak(kwargs):
    """播放配音：本地 sherpa / 在线 Edge / 小米 MiMo；字幕逐字高亮、可带 BGM、可同播动画。

    MiMo 的 API Key 走 api_key 参数随请求携带（不落盘），适合放在工具参数预设里。
    """
    global _stream_active
    blocked = _require_visible("speak")
    if blocked:
        return blocked
    # 换行是歌词/台词的天然断句边界，必须保留：压成空格会让整首歌词变成"一句"，
    # 字幕就会一次性全摊出来。行内多余空格压成一个。
    raw = html.unescape(str(kwargs.get("text") or "")).replace("\r", "\n")
    text = "\n".join(" ".join(ln.split()) for ln in raw.split("\n") if ln.strip())
    if not text:
        return {"ok": False, "action": "speak", "error": "text 为空"}
    anim = str(kwargs.get("animation") or "").strip()
    motion_name = str(kwargs.get("motion_name") or "").strip()
    # speak 的 animation 是自带动画名（不是内置动画 id）；motion_name 是等价的显式写法。
    native_name = motion_name or anim
    if native_name:
        # 排队（wait_others=true）路径先返回 accepted，后台执行结果不会回传给智能体；
        # 若这里直接拒绝，表现就是“命令成功、动画播放、配音静默”。对误带的
        # [[nod]]/[[tilt]] 等内置标记做一次清洗：自带动画优先，不再触发额外动画。
        cleaned = re.sub(r"\[\[[^\]]*\]\]", "", text)
        cleaned = "\n".join(" ".join(ln.split()) for ln in cleaned.split("\n") if ln.strip())
        text = cleaned
        if not text:
            return {"ok": False, "action": "speak",
                    "error": "指定自带动画时，清理 [[...]] 动画标记后 text 为空；请只保留台词"}
    if native_name and "[[" in text:
        return {"ok": False, "action": "speak",
                "error": "指定自带动画时，text 中不能包含 [[...]] 动画标记；"
                         "请只保留台词，或省略自带动画后在 text 中使用动画标记"}
    mode = str(kwargs.get("mode") or "queue").strip().lower()
    if mode not in ("replace", "queue"):
        return {"ok": False, "action": "speak",
                "error": "mode 只支持 replace / queue（省略默认 queue）"}
    backend = str(kwargs.get("backend") or "local").strip().lower()
    if backend in ("online", "edge-tts", "azure"):
        backend = "edge"
    elif backend in ("mimo", "mimo-tts", "xiaomi", "mi"):
        backend = "mimo"
    # auto 不在这一层落地：交给 service 按 online.provider 选（默认 edge），
    # 这样加/换在线服务商只改配置，不用回来改这里。
    if backend not in ("local", "edge", "mimo", "auto"):
        return {"ok": False, "action": "speak",
                "error": "backend 只支持 local / edge / mimo / auto"}

    wait_s = _wait_value(kwargs.get("wait"))
    blocking = bool(kwargs.get("blocking", False))
    # 动画同播：先把自带动画推起来，并静音动作自带配音，避免与 TTS 两路声音打架。
    # speak 的 animation / motion_name 都按自带动画名解析；内置动画只在 text 标记里用。
    motion_res = None
    if native_name:
        try:
            motion_res = _cmd_play_animation(dict(
                kwargs, animation="", motion_name=native_name,
                mute=True, wait=0, lip_lock=True, reuse_same=True))
        except Exception as e:
            motion_res = {"ok": False, "error": str(e)}
        if not motion_res.get("ok"):
            return {"ok": False, "action": "speak",
                    "error": motion_res.get("error") or "自带动画不存在"}

    payload = {
        "op": "speak", "text": text, "backend": backend,
        "voice": str(kwargs.get("voice") or ""),   # 留空 = 由 service 按 backend 取该后端默认音色
        # 在线 key 随请求带过去（backend=auto 可能落到任意在线服务商，所以也带上）；
        # 本地配音不传，免得把密钥无关地搬来搬去
        "api_key": (str(kwargs.get("api_key") or "") if backend in ("mimo", "auto") else ""),
        # 唱歌：只有 MiMo 支持，其他后端由 service 明确拒绝
        "sing": bool(kwargs.get("sing", False)),
        # MiMo 的自然语言表演指令（放 role=user，按次生效、不写盘）：
        # 情绪/语气/角色扮演/导演模式都写在这一次请求里
        "style": (str(kwargs.get("style") or "") if backend in ("mimo", "auto") else ""),
        # 方言风格（MiMo 标签控制：东北话/四川话/河南话/粤语；"普通话"=不加标签）
        "dialect": ((str(kwargs.get("dialect") or "")
                     if str(kwargs.get("dialect") or "") != "普通话" else "")
                    if backend in ("mimo", "auto") else ""),
        # 不传语速 = 交给 service 取该后端默认（本地 0.90，在线 1.0）
        "speed": (None if kwargs.get("speed") is None else float(kwargs.get("speed"))),
        "bgm": str(kwargs.get("bgm") or ""),
        # 不传音量 = 交给 service 读 config（默认 0.75 / duck 0.55）
        "bgm_volume": (None if kwargs.get("bgm_volume") is None
                       else float(kwargs.get("bgm_volume"))),
        "bgm_duck": (None if kwargs.get("bgm_duck") is None
                     else float(kwargs.get("bgm_duck"))),
        "subtitle": bool(kwargs.get("subtitle", True)),
        "lip_sync": bool(kwargs.get("lip_sync", True)),
        "mode": mode,
        "wait": wait_s,
        "blocking": blocking,
    }
    timeout = float(kwargs.get("timeout") or (
        240.0 if blocking else max(5.0, wait_s + 10.0)))
    res = _window.tts(payload, timeout=timeout)
    if not isinstance(res, dict):
        return {"ok": False, "raw": str(res)[:200]}
    out = {"ok": bool(res.get("ok"))}
    if not out["ok"]:
        out["error"] = res.get("error") or "配音失败"
    else:
        if res.get("queued"):
            out["queued"] = True
            out["position"] = res.get("position")
        if res.get("delayed"):
            # wait>0 且当前没有可排队的上一句：定时器延迟出声，命令已立即返回。
            out["delayed"] = True
            out["wait"] = res.get("wait")
        if res.get("warning"):
            out["warning"] = res["warning"]
    if motion_res is not None and not motion_res.get("ok"):
        out["motion_error"] = motion_res.get("error") or "动画播放失败"
    elif motion_res is not None and motion_res.get("reused"):
        out["motion_reused"] = True
    return out


def _cmd_stop_speak(kwargs):
    if not _window:
        return _no_window()
    res = _window.tts({"op": "stop"})
    res.setdefault("action", "stop_speak")
    return res


def _cmd_tts_status(kwargs, inst=None):
    """配音运行状态：默认只回决策字段，detail=true 才带全量诊断。

    inst 由 run() 的轻量状态通道直接传入：不经过全局 _lock/_window，
    避免状态轮询和后台 defer 派发互相抢锁。
    """
    if inst is not None:
        res = inst.client.tts_status(detail=bool(kwargs.get("detail", False)))
    else:
        if not _window:
            return _no_window()
        res = _window.tts_status(detail=bool(kwargs.get("detail", False)))
    res.setdefault("action", "tts_status")
    target = inst
    if target is None:
        target = next((x for x in list(_instances.values())
                       if _key(x.model_file) == _key(_loaded_model or "")), None)
    res.update(_deferred_status_for(target))
    return res


def _cmd_tts_unload(kwargs):
    """卸载本地模型（释放 ~2.2GB 显存）；下次 speak 会自动重新拉起。"""
    if not _window:
        return _no_window()
    res = _window.tts({"op": "unload"}, timeout=20)
    res.setdefault("action", "tts_unload")
    return res


def _cmd_tts_config(kwargs):
    """读/写配音配置（tts_config.json）：不给参数就返回当前配置。"""
    from tts.config import DEFAULTS, deep_merge, load_config, save_config
    patch_keys = ("model_dir", "dtype", "voice", "bgm", "bgm_volume", "backend",
                  "idle_unload_s", "min_free_vram_mb", "prebuffer_ms", "max_new_tokens",
                  # 在线服务商：provider=edge|mimo，mimo 是 MiMo 的整段配置（可局部覆盖）
                  "provider", "mimo", "online")
    if not any(k in kwargs for k in patch_keys):
        return {"ok": True, "config": load_config()}
    patch = {k: v for k, v in kwargs.items() if k in patch_keys}
    # provider 与 mimo 是 online 段的子项，直接写顶层会把配置写散，这里归位成 online.*
    online_patch = {}
    for k in ("provider", "mimo"):
        if k in patch:
            online_patch[k] = patch.pop(k)
    if online_patch:
        cur = patch.get("online")
        patch["online"] = deep_merge(cur if isinstance(cur, dict) else {}, online_patch)
    cfg = deep_merge(load_config(), patch)
    save_config(cfg)
    return {"ok": True, "saved": True}


def _cmd_tts_setup(kwargs):
    """装环境/下模型自检：mode=check|install|download|synth（走 tools/tts_setup.py）。"""
    mode = str(kwargs.get("mode") or "check").lower()
    if mode not in ("check", "install", "download", "synth"):
        return {"ok": False, "error": "mode 只支持 check / install / download / synth"}
    script = str(Path(__file__).resolve().parent / "tools" / "tts_setup.py")
    py = str(kwargs.get("python") or sys.executable)
    args = [py, script, "--" + mode]
    for k in ("model_dir", "variant", "revision"):
        if kwargs.get(k):
            args += ["--" + k.replace("_", "-"), str(kwargs[k])]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=1800,
                              cwd=str(Path(__file__).resolve().parent))
        return {"ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-4000:], "stderr": (proc.stderr or "")[-2000:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ===== 诊断（运行中真实渲染）=====
def _cmd_dump_frame(kwargs):
    """诊断：导出当前画布的 PNG（dataURL），用于核对运行中的真实渲染结果。"""
    if not _window:
        return _no_window()
    res = _window.send_command_sync({"type": "dumpFrame", "sync": True}, timeout=20)
    if isinstance(res, dict) and res.get("raw"):
        url = res["raw"]
    elif isinstance(res, str):
        url = res
    else:
        return {"ok": False, "error": "dumpFrame 无有效返回", "raw": str(res)[:200]}
    return {"ok": bool(url), "dataUrl": url}


def _cmd_debug_info(kwargs):
    """诊断：Core 版本 / 画布 / 模型 / drawable 顶点与 UV 健康度。"""
    if not _window:
        return _no_window()
    res = _window.send_command_sync({"type": "getDebugInfo", "sync": True}, timeout=15)
    return res if isinstance(res, dict) else {"ok": False, "raw": str(res)[:200]}


def _cmd_list_servers(kwargs):
    """诊断：列出每个实例的模型 HTTP 服务地址（排查加载失败 / 端口冲突）。

    多实例后每个 worker 各自起服务，所以这里按实例汇总，而不是查本进程的模块变量。
    """
    out = []
    for inst in list(_instances.values()):
        st = inst.client.state(0.8) or {}
        out.append({
            "model": st.get("model") or inst.model_file,
            "model_name": st.get("model_name") or inst.name,
            "dir": inst.model_dir,
            "url": st.get("http_url"),
            "pid": inst.proc.pid if inst.proc is not None else None,
        })
    return {"ok": True, "count": len(out), "servers": out}


def _cmd_frame_stats(kwargs):
    """诊断：采样最近一帧原始缓冲（非空像素 / 预乘 alpha 校验）。"""
    if not _window:
        return _no_window()
    res = _window.send_command_sync({"type": "getFrameStats", "sync": True}, timeout=20)
    return res if isinstance(res, dict) else {"ok": False, "raw": str(res)[:200]}


# ===== 流式参数动画 =====
def _cmd_start_stream(kwargs):
    global _window, _stream_active
    blocked = _require_visible("start_stream")
    if blocked:
        return blocked

    stream_str = kwargs.get("stream_data", "")
    if not stream_str:
        return {"error": "start_stream 需要 stream_data 参数"}

    try:
        data = json.loads(stream_str)
    except json.JSONDecodeError as e:
        return {"error": f"stream_data JSON 解析失败: {e}"}

    # 验证
    params = data.get("params", [])
    values = data.get("values", [])
    interval = data.get("interval", "500ms")
    repeat = data.get("repeat", 0)

    if not params or not values:
        return {"error": "stream_data 需要 params 和 values 字段"}

    # 参数名归一化：别名 / 短名 → 真实 id，再原样回写给 JS
    items, _ = _ensure_param_meta()
    index = _build_param_index(_loaded_model or "")
    names = [m["name"] for m in (items or [])]
    if names:
        resolved, unknown = [], []
        for p in params:
            r = _resolve_param_key(p, index, names)
            if r:
                resolved.append(r)
            else:
                unknown.append(str(p))
    if names:
        resolved, unknown = [], []
        for p in params:
            ks = _resolve_param_keys(p, index, names)
            if ks:
                resolved.extend(ks)
            else:
                unknown.append(str(p))
        if unknown:
            return {"ok": False, "action": "start_stream", "unknown": unknown,
                    "error": "stream_data.params 有无法解析的参数名",
                    "hint": "先用 search_params 查准确名称，或直接用其返回的 name 字段"}
        if len(resolved) != len(params):
            # 别名被展开成多个参数（"眨眼"→左右眼），values 的列数就对不上了
            return {"ok": False, "action": "start_stream", "expanded": resolved,
                    "error": "参数名展开后数量变了，values 每行列数必须与展开后的 params 对应",
                    "hint": "用 expanded 里的真实参数名重写 params 与 values（每行加对应的列）"}
        if resolved != list(params):
            data["params"] = resolved
            stream_str = _dumps(data)
            params = resolved

    if not all(len(row) == len(params) for row in values):
        return {"error": f"values 每行长度必须与 params ({len(params)}) 一致"}

    _stream_active = True
    _window.send_command({
        "type": "startStream",
        "streamData": stream_str
    })

    interval_ms = _parse_interval(interval)
    per_pass_ms = len(values) * interval_ms

    if repeat == -1:
        duration_desc = "无限循环（直到 stop_stream 或新 start_stream）"
    else:
        actual_repeat = max(1, repeat)
        duration_desc = f"{len(values)} 帧 × {actual_repeat} 遍 × {interval_ms}ms = {per_pass_ms * actual_repeat}ms"

    return {
        "ok": True,
        "frames_count": len(values),
        "duration_ms": (per_pass_ms if repeat == -1 else per_pass_ms * max(1, repeat)),
    }


def _cmd_append_frames(kwargs):
    global _window, _stream_active
    blocked = _require_visible("append_frames")
    if blocked:
        return blocked
    if not _stream_active:
        return {"warning": "没有活动的流式动画，请先 start_stream 启动一个"}

    eof = kwargs.get("eof", False)

    # 优先取 values_only（更轻量）
    values_only = kwargs.get("values_only")
    cmd = {"type": "appendFrames", "eof": eof}

    if values_only:
        try:
            rows = json.loads(values_only)
            if not all(isinstance(row, list) for row in rows):
                return {"error": "values_only 必须是二维数组"}
            cmd["valuesOnly"] = values_only
        except json.JSONDecodeError as e:
            return {"error": f"values_only JSON 解析失败: {e}"}
    elif kwargs.get("stream_data"):
        cmd["streamData"] = kwargs["stream_data"]
    else:
        return {"error": "append_frames 需要 values_only 或 stream_data"}

    _window.send_command(cmd)

    if eof:
        _stream_active = False

    return {"ok": True, "stream_active": _stream_active}


def _cmd_stop_stream(kwargs):
    global _window, _stream_active
    if _window:
        _window.send_command({"type": "stopStream"})
    _stream_active = False
    return {"ok": True}


def _cmd_set_sound(kwargs):
    """全局开关模型动作的配音（motion3.json 的 Sound 字段）。"""
    if not _window:
        return _no_window()
    on = kwargs.get("sound")
    if on is None:
        on = True
    res = _window.send_command_sync({"type": "setSound", "sync": True, "on": bool(on)})
    if not isinstance(res, dict) or not res.get("ok", True):
        return {"ok": False, "error": (res or {}).get("error") or "设置动作配音失败"}
    return {"ok": True}


# ===== 工具函数 =====
def _prune_empty(o):
    """递归裁掉空字段：None / 空白字符串 / 空列表 / 空字典一律不出现。

    数字 0 与布尔 False 是有效值，必须保留（visible=False、shown=0 都靠它表达状态）。
    """
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            pv = _prune_empty(v)
            if pv is None:
                continue
            if isinstance(pv, str) and not pv.strip():
                continue
            if isinstance(pv, (list, tuple, dict)) and len(pv) == 0:
                continue
            out[k] = pv
        return out
    if isinstance(o, list):
        cleaned = [_prune_empty(x) for x in o]
        return [x for x in cleaned
                if not (x is None or (isinstance(x, str) and not x.strip()))]
    if isinstance(o, tuple):
        return tuple(_prune_empty(x) for x in o)
    return o


def _no_window():
    return {"ok": False, "count": 0, "visible": False,
            "error": "当前没有任何模型在渲染（桌宠未启动）",
            "hint": "先调用 action=show（可带 model_path；省略 model_path 用插件自带的符玄）"}


def _visible_state():
    """当前可见性：True/False；窗口不存在或取不到状态时返回 None。"""
    if not _window:
        return None
    try:
        return bool(_window.is_visible())
    except Exception:
        return None


def _hidden_info(action):
    """桌宠不可见时「需要看到模型」的操作统一返回：明确说清原因与恢复方式。"""
    return {
        "ok": False,
        "action": action,
        "visible": False,
        "error": (f"桌宠当前处于隐藏/关闭状态，模型没有显示，{action} 的效果看不到"
                  "（本次操作未执行）"),
        "hint": ("先调用 action=show（可带 x/y/scale，或用 model_path 换模型）让桌宠"
                 "显示出来，再重试本操作；也可以用 action=list_motions / list_expressions "
                 "先查可用项，show 之后再播放"),
    }


def _require_visible(action):
    """「需要模型可见」的动作前置校验：窗口不存在 → _no_window；已隐藏 → _hidden_info。"""
    if not _window:
        return _no_window()
    if _visible_state() is False:
        return _hidden_info(action)
    return None


def _parse_interval(s: str) -> int:
    """'500ms' → 500, '1s' → 1000"""
    s = s.strip().lower()
    if s.endswith("ms"):
        return int(s[:-2])
    if s.endswith("s"):
        return int(float(s[:-1]) * 1000)
    return int(s) if s.isdigit() else 500
