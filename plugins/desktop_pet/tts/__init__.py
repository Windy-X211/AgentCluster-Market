# coding=utf-8
"""桌宠配音（TTS）—— 与具体引擎无关的公共模块。

本包内所有模块都只依赖 numpy / 标准库（engine_qwen 例外，它只在
_tts_env 里被 import），因此可以被宿主进程、worker 进程和守护进程共用。
"""
