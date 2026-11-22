# coding=utf-8
"""TTS 环境自举：建私有 venv、装依赖、下模型、自检、写配置。

为什么必须私有 venv：
  宿主环境是 transformers 5.2.0，而 qwen-tts 0.1.1 硬依赖 transformers==4.57.3，
  直接装会降级宿主，影响其他插件。私有 venv 用 --system-site-packages 复用 torch。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tts.config import (  # noqa: E402
    CONFIG_PATH, DEFAULT_MODEL_DIR, VENV_PY, load_config, save_config,
)

HOST_PY = sys.executable

PIP_PKGS = [
    "transformers==4.57.3",
    "huggingface-hub==0.36.2",
    "accelerate==1.12.0",
    "librosa==1.0.1",
    "soundfile",
    "einops",
    "sox==1.5.0",
    "soxr",
    "onnxruntime",
    "pooch",
]

QWEN_PKG = "qwen-tts==0.1.1"
REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
VARIANTS = {
    "custom_voice": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    "voice_design": "Qwen/Qwen3-TTS-12Hz-0.6B-VoiceDesign",
    "base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
}


def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


# ---------------------------------------------------------------- install
def install():
    venv_dir = VENV_PY.parent.parent
    if not VENV_PY.is_file():
        print("创建 venv: %s" % venv_dir)
        run([HOST_PY, "-m", "venv", "--system-site-packages", str(venv_dir)])
    py = str(VENV_PY)

    run([py, "-m", "pip", "install", "--upgrade", "pip", "-q"])
    run([py, "-m", "pip", "install", "-q"] + PIP_PKGS)
    # qwen-tts 的依赖里 transformers 版本被锁死，这里只装它自己
    run([py, "-m", "pip", "install", "-q", "--no-deps", QWEN_PKG])
    print("依赖安装完成")


# ---------------------------------------------------------------- download
def download(model_dir: str, variant: str = "custom_voice", revision: str = "master"):
    repo = VARIANTS.get(variant, VARIANTS["custom_voice"])
    os.makedirs(model_dir, exist_ok=True)
    print("下载 %s -> %s" % (repo, model_dir))
    from modelscope import snapshot_download
    path = snapshot_download(repo_id=repo, local_dir=model_dir, revision=revision)
    print("模型就绪: %s" % path)
    return path


# ---------------------------------------------------------------- check
def check(model_dir: str, synth: bool = False):
    ok = True

    def line(name, good, extra=""):
        nonlocal ok
        ok = ok and good
        print("%s %-26s %s" % ("[OK]" if good else "[--]", name, extra))

    line("宿主 python", True, HOST_PY)
    line("私有 venv", VENV_PY.is_file(), str(VENV_PY))
    if VENV_PY.is_file():
        code = (
            "import torch, transformers, librosa, soundfile, sox, onnxruntime\n"
            "print('torch', torch.__version__, 'cuda', torch.cuda.is_available())\n"
            "print('transformers', transformers.__version__)\n"
            "import qwen_tts; print('qwen_tts ok')\n"
        )
        try:
            r = subprocess.run([str(VENV_PY), "-c", code], capture_output=True, text=True, timeout=180)
            print(r.stdout.strip())
            if r.returncode != 0:
                print(r.stderr.strip()[-1500:])
                line("venv 依赖导入", False)
            else:
                line("venv 依赖导入", True)
        except Exception as e:
            line("venv 依赖导入", False, str(e))

    main_w = Path(model_dir) / "model.safetensors"
    voc_w = Path(model_dir) / "speech_tokenizer" / "model.safetensors"
    line("主权重", main_w.is_file(), "%.2fGB" % (main_w.stat().st_size / 1024 ** 3) if main_w.is_file() else "缺失")
    line("声码器", voc_w.is_file(), "%.2fGB" % (voc_w.stat().st_size / 1024 ** 3) if voc_w.is_file() else "缺失")
    line("配置", (Path(model_dir) / "config.json").is_file(), model_dir)

    if synth and main_w.is_file():
        print("试合成一句 ...")
        r = subprocess.run(
            [str(VENV_PY), str(ROOT / "tools" / "tts_daemon.py"), "--model-dir", model_dir],
            capture_output=True, text=True, timeout=60,
        )
        print("[self-test 输出] " + (r.stdout or "").strip()[:300] + (r.stderr or "").strip()[-300:])
        # 用 daemon 做一次 speak 自检
        try:
            proc = subprocess.Popen(
                [str(VENV_PY), str(ROOT / "tools" / "tts_daemon.py"), "--model-dir", model_dir, "--daemon-exit", "30"],
                stdout=subprocess.PIPE, text=True,
            )
            line = proc.stdout.readline()
            port = json.loads(line.split(" ", 1)[1])["port"] if line.startswith("READY") else 0
            if port:
                import socket
                with socket.create_connection(("127.0.0.1", port), timeout=120) as c:
                    c.sendall('{"op":"speak","text":"你好，这是一次自检。","speaker":"Serena"}\n'.encode())
                    buf = c.recv(65536).decode("utf-8", "replace")
                    print("首包: " + buf.splitlines()[0][:200])
                    print("自检" + ("通过" if '"ev":"ready"' in buf or '"ev":"seg"' in buf else "未通过"))
                    ok = ok and ('"ev":"seg"' in buf or '"ev":"ready"' in buf)
            proc.kill()
        except Exception as e:
            print("自检异常: %s" % e)
            ok = False

    print("\n结论: " + ("环境可用" if ok else "仍有问题"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--synth", action="store_true", help="自检时跑一次真实合成")
    ap.add_argument("--model-dir", default="")
    ap.add_argument("--variant", choices=list(VARIANTS), default="custom_voice")
    ap.add_argument("--revision", default="master")
    args = ap.parse_args()

    model_dir = args.model_dir or str((load_config().get("local") or {}).get("model_dir") or DEFAULT_MODEL_DIR)

    rc = 0
    if args.install:
        install()
    if args.download:
        download(model_dir, args.variant, args.revision)
        save_config({"local": {"model_dir": model_dir, "variant": args.variant}})
        print("已写入 %s" % CONFIG_PATH)
    if args.check or not any([args.install, args.download]):
        rc = check(model_dir, args.synth)
    return rc


if __name__ == "__main__":
    sys.exit(main())
