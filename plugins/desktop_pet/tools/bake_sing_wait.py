# -*- coding: utf-8 -*-
"""烘焙"唱歌等待期"的说明配音：`_tts/sing_wait/<音色>.wav`。

唱歌是整曲一次推理，实测 0.29~0.37 秒/字：50 字歌词要等 15 秒左右才出第一声。
这段时间桌宠会先放背景音，并说一句"我先清一下嗓子，稍等一会儿"——这句不能实时合成，
否则又多一次请求、还可能排在唱歌请求后面，所以提前烘焙成文件，唱歌时直接读。

用法（在插件根目录下）：
    python tools/bake_sing_wait.py --voice 冰糖 --api-key sk-xxxx
    python tools/bake_sing_wait.py --voice 茉莉                  # key 从配置/环境变量取
    python tools/bake_sing_wait.py --voice 冰糖 --line "稍等，我调一下气息就唱。" --force

产物：_tts/sing_wait/冰糖.wav（24kHz 单声道 16bit）。换音色后重新跑一次即可。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import soundfile as sf  # noqa: E402
import numpy as np  # noqa: E402

from tts.config import load_config  # noqa: E402
from tts.engine_mimo import find_api_key, load_settings, synth_segment  # noqa: E402

CACHE_DIR = ROOT / "_tts" / "sing_wait"
DEFAULT_STYLE = "轻声、亲昵，语速放慢一点，像在安抚对方"


def main() -> int:
    ap = argparse.ArgumentParser(description="烘焙唱歌等待期的说明配音")
    ap.add_argument("--voice", default="", help="MiMo 音色名（默认取配置里的 voice）")
    ap.add_argument("--line", default="", help="等待台词（默认取 online.mimo.sing_wait_line）")
    ap.add_argument("--style", default=DEFAULT_STYLE, help="自然语言风格（写进 role=user）")
    ap.add_argument("--api-key", default="", help="临时 key；不传则走配置/文件/环境变量")
    ap.add_argument("--force", action="store_true", help="已存在也重新生成")
    args = ap.parse_args()

    cfg = load_config()
    st = load_settings(cfg)
    voice = args.voice or str(st.get("voice") or "冰糖")
    line = args.line or str(st.get("sing_wait_line") or "").strip()
    if not line:
        print("没有等待台词：online.mimo.sing_wait_line 为空")
        return 2
    key = args.api_key or find_api_key(st)
    if not key:
        print("没有 MiMo key：用 --api-key，或在配置/环境变量里提供")
        return 2

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / ("".join(c for c in voice if c not in '\\/:*?"<>|').strip() + ".wav")
    if out.is_file() and not args.force:
        print("已存在：%s（要重做加 --force）" % out)
        return 0

    print("合成等待配音 voice=%s line=%r" % (voice, line), flush=True)
    pcm, sr, _words, ms = synth_segment(line, voice=voice, speed=1.0, sr=24000,
                                        settings=st, api_key=key, style=args.style)
    # 首尾各留 60ms 余量，给播放侧 4ms 淡入淡出留空间
    pad = max(1, int(sr * 0.06))
    pcm = np.concatenate([np.zeros(pad, dtype=np.int16), pcm, np.zeros(pad, dtype=np.int16)])
    sf.write(str(out), pcm, sr, subtype="PCM_16")
    print("已写：%s（%.2fs，%.1f KB，合成耗时 %.0fms）" % (
        out, pcm.size / sr, out.stat().st_size / 1024.0, ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
