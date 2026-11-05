"""sd_webui 插件 —— 连接本地 Stable Diffusion WebUI 生成图片

调用签名（由 tool.yaml 的 parameters 决定）：
    run(action="txt2img", prompt="...", negative_prompt="...", steps=28, cfg_scale=7,
        width=512, height=512, seed=-1, sampler_name="DPM++ 2M Karras", ...)
    run(action="img2img", init_images="<base64 或路径>", prompt="...", denoising_strength=0.75, ...)
    run(action="list", what="models")
    run(action="progress" / "interrupt" / "get_options" / "set_options", ...)

实现上用标准库（urllib + base64 + json），不依赖第三方包。
生成图片默认保存到 <项目根>/outputs/sd_webui/（可由 config.json 的 output_dir 或 save_dir 参数覆盖）。

SD WebUI 需以 --api 启动，默认 API 地址 http://127.0.0.1:7860。
"""
from __future__ import annotations

import base64
import json as _json
import sys
import time as _time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许独立 import（不依赖 backend 作为包）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent
_PLUGIN_DIR: Path = Path(__file__).resolve().parent

DEFAULT_BASE_URL = "http://127.0.0.1:7860"
DEFAULT_OUTPUT_DIR = "outputs/sd_webui"

# 生图可能很慢，连接/读取超时放长
_HTTP_TIMEOUT = 600


# ==============================================================
# 配置读取（可选 config.json，存默认 base_url / output_dir）
# ==============================================================
def _load_config() -> dict[str, Any]:
    cfg_fp = _PLUGIN_DIR / "config.json"
    if cfg_fp.exists():
        try:
            return _json.loads(cfg_fp.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _resolve_base_url(kwargs: dict) -> str:
    cfg = _load_config()
    return str(kwargs.get("base_url") or cfg.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")


def _resolve_save_dir(kwargs: dict) -> str:
    cfg = _load_config()
    d = str(kwargs.get("save_dir") or cfg.get("output_dir") or DEFAULT_OUTPUT_DIR)
    p = Path(d)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return str(p)


# ==============================================================
# HTTP 请求封装
# ==============================================================
def _api(base_url: str, path: str, payload: dict | None = None,
         method: str = "GET", timeout: int = _HTTP_TIMEOUT) -> dict[str, Any]:
    url = base_url + path
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = _json.dumps(payload).encode("utf-8")
    if method.upper() == "POST":
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return {"ok": True, "status": r.status, "data": _json.loads(body)}
            except Exception:
                return {"ok": True, "status": r.status, "data": body}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:800]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}：{detail}"}
    except urllib.error.URLError as e:
        return {
            "ok": False,
            "error": f"无法连接 SD WebUI（{url}）：{e.reason}。"
                     f"请确认本地生图模型已启动、且以 --api 参数启动、API 地址正确（当前 base_url={base_url}）。",
        }
    except Exception as e:
        return {"ok": False, "error": f"请求失败：{e}"}


# ==============================================================
# 图片保存
# ==============================================================
def _save_images(images_b64: list[str], save_dir: str, seed: Any) -> list[str]:
    out = Path(save_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    files: list[str] = []
    for i, b64 in enumerate(images_b64):
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        fp = out / f"sd_{ts}_{seed}_{i}.png"
        fp.write_bytes(raw)
        files.append(str(fp))
    return files


def _extract_seed_and_info(data: dict) -> tuple[Any, str, str]:
    """从 SD 响应里提取 seed 与简要 infotext。"""
    seed: Any = None
    info_text = ""
    params = data.get("parameters") if isinstance(data, dict) else None
    if isinstance(params, dict):
        seed = params.get("seed")
        info_text = str(params.get("infotext") or params.get("prompt") or "")
    info_raw = data.get("info") if isinstance(data, dict) else None
    if isinstance(info_raw, str) and info_raw:
        try:
            info_obj = _json.loads(info_raw)
            if seed is None:
                seed = info_obj.get("seed")
            if not info_text:
                info_text = str(info_obj.get("infotext") or "")
        except Exception:
            pass
    return seed, info_text[:1000], (_json.dumps(info_raw, ensure_ascii=False)[:1000] if info_raw else "")


# ==============================================================
# 各 action 处理
# ==============================================================
def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return bool(v)


def _normalize_init_images(init_images: str | list | None) -> list[str]:
    """把 init_images 归一化成 base64 列表（img2img 用）。"""
    items: list[str] = []
    if isinstance(init_images, str):
        items = [init_images]
    elif isinstance(init_images, list):
        items = [str(x) for x in init_images]
    result: list[str] = []
    for it in items:
        it = str(it).strip()
        if not it:
            continue
        if it.startswith("data:image/") and ";base64," in it:
            it = it.split(";base64,", 1)[1]
        elif Path(it).exists():
            try:
                it = base64.b64encode(Path(it).read_bytes()).decode("ascii")
            except Exception:
                continue
        result.append(it)
    return result


def _handle_txt2img(kwargs: dict) -> dict:
    base_url = _resolve_base_url(kwargs)
    save_dir = _resolve_save_dir(kwargs)

    payload: dict[str, Any] = {
        "prompt": str(kwargs.get("prompt") or ""),
        "negative_prompt": str(kwargs.get("negative_prompt") or ""),
        "steps": int(kwargs.get("steps", 28)),
        "cfg_scale": float(kwargs.get("cfg_scale", 7.0)),
        "width": int(kwargs.get("width", 512)),
        "height": int(kwargs.get("height", 512)),
        "seed": int(kwargs.get("seed", -1)),
        "batch_size": int(kwargs.get("batch_size", 1)),
        "n_iter": int(kwargs.get("n_iter", 1)),
        "restore_faces": _to_bool(kwargs.get("restore_faces", False)),
        "tiling": _to_bool(kwargs.get("tiling", False)),
    }
    if not payload["prompt"]:
        return {"ok": False, "error": "prompt 不能为空，请先扩写出完整的生图提示词"}

    if kwargs.get("sampler_name"):
        payload["sampler_name"] = str(kwargs["sampler_name"])
    if kwargs.get("scheduler"):
        payload["scheduler"] = str(kwargs["scheduler"])

    if _to_bool(kwargs.get("enable_hr", False)):
        payload["enable_hr"] = True
        payload["hr_scale"] = float(kwargs.get("hr_scale", 2.0))
        if kwargs.get("hr_upscaler"):
            payload["hr_upscaler"] = str(kwargs["hr_upscaler"])
        if kwargs.get("denoising_strength") is not None:
            payload["denoising_strength"] = float(kwargs["denoising_strength"])

    if kwargs.get("override_settings"):
        ov = kwargs["override_settings"]
        if isinstance(ov, str):
            try:
                ov = _json.loads(ov)
            except Exception:
                ov = None
        if isinstance(ov, dict):
            payload["override_settings"] = ov

    res = _api(base_url, "/sdapi/v1/txt2img", payload)
    if not res.get("ok"):
        return res

    data = res["data"]
    images = data.get("images", []) if isinstance(data, dict) else []
    seed, info_text, _ = _extract_seed_and_info(data)
    files = _save_images([str(x) for x in images], save_dir, seed)

    image_data_url = None
    if files:
        from urllib.parse import quote
        image_data_url = f"/api/fs/image?path={quote(str(files[0]), safe='')}"

    return {
        "ok": True,
        "action": "txt2img",
        "file": files[0] if files else None,
        "files": files,
        "image_data_url": image_data_url,
        "purpose": "share" if files else None,
        "count": len(files),
        "seed": seed,
        "prompt": payload["prompt"],
        "negative_prompt": payload["negative_prompt"],
        "width": payload["width"],
        "height": payload["height"],
        "steps": payload["steps"],
        "cfg_scale": payload["cfg_scale"],
        "sampler_name": payload.get("sampler_name"),
        "info": info_text,
    }


def _handle_img2img(kwargs: dict) -> dict:
    base_url = _resolve_base_url(kwargs)
    save_dir = _resolve_save_dir(kwargs)

    init = _normalize_init_images(kwargs.get("init_images"))
    if not init:
        return {"ok": False, "error": "img2img 需要 init_images（参考图 base64 或本地图片路径）"}

    payload: dict[str, Any] = {
        "init_images": init,
        "prompt": str(kwargs.get("prompt") or ""),
        "negative_prompt": str(kwargs.get("negative_prompt") or ""),
        "steps": int(kwargs.get("steps", 28)),
        "cfg_scale": float(kwargs.get("cfg_scale", 7.0)),
        "width": int(kwargs.get("width", 512)),
        "height": int(kwargs.get("height", 512)),
        "seed": int(kwargs.get("seed", -1)),
        "batch_size": int(kwargs.get("batch_size", 1)),
        "n_iter": int(kwargs.get("n_iter", 1)),
        "denoising_strength": float(kwargs.get("denoising_strength", 0.75)),
        "restore_faces": _to_bool(kwargs.get("restore_faces", False)),
        "tiling": _to_bool(kwargs.get("tiling", False)),
    }
    if kwargs.get("sampler_name"):
        payload["sampler_name"] = str(kwargs["sampler_name"])
    if kwargs.get("scheduler"):
        payload["scheduler"] = str(kwargs["scheduler"])
    if kwargs.get("override_settings"):
        ov = kwargs["override_settings"]
        if isinstance(ov, str):
            try:
                ov = _json.loads(ov)
            except Exception:
                ov = None
        if isinstance(ov, dict):
            payload["override_settings"] = ov

    res = _api(base_url, "/sdapi/v1/img2img", payload)
    if not res.get("ok"):
        return res

    data = res["data"]
    images = data.get("images", []) if isinstance(data, dict) else []
    seed, info_text, _ = _extract_seed_and_info(data)
    files = _save_images([str(x) for x in images], save_dir, seed)

    image_data_url = None
    if files:
        from urllib.parse import quote
        image_data_url = f"/api/fs/image?path={quote(str(files[0]), safe='')}"

    return {
        "ok": True,
        "action": "img2img",
        "file": files[0] if files else None,
        "files": files,
        "image_data_url": image_data_url,
        "purpose": "share" if files else None,
        "count": len(files),
        "seed": seed,
        "denoising_strength": payload["denoising_strength"],
        "info": info_text,
    }


def _handle_list(kwargs: dict) -> dict:
    base_url = _resolve_base_url(kwargs)
    what = str(kwargs.get("what") or "models")
    endpoints = {
        "models": "/sdapi/v1/sd-models",
        "samplers": "/sdapi/v1/samplers",
        "schedulers": "/sdapi/v1/schedulers",
        "loras": "/sdapi/v1/loras",
        "embeddings": "/sdapi/v1/embeddings",
        "upscalers": "/sdapi/v1/upscalers",
    }
    path = endpoints.get(what)
    if not path:
        return {"ok": False, "error": f"未知 list 类型：{what}"}

    res = _api(base_url, path)
    if not res.get("ok"):
        return res

    data = res["data"]
    # 提炼成简洁可读文本，方便 LLM 使用
    summary: list[str] = []
    try:
        if what == "models":
            for m in data:
                summary.append(str(m.get("title") or m.get("model_name")))
        elif what in ("samplers", "schedulers"):
            for s in data:
                summary.append(str(s.get("name")))
        elif what == "loras":
            for l in data:
                summary.append(str(l.get("name")))
        elif what == "embeddings":
            loaded = data.get("loaded", {}) if isinstance(data, dict) else {}
            summary = list(loaded.keys())
        elif what == "upscalers":
            for u in data:
                summary.append(str(u.get("name")))
    except Exception:
        summary = []

    return {
        "ok": True,
        "action": "list",
        "what": what,
        "items": summary,
        "count": len(summary),
        "data": _json.dumps(data, ensure_ascii=False)[:6000],
    }


def _handle_progress(kwargs: dict) -> dict:
    base_url = _resolve_base_url(kwargs)
    res = _api(base_url, "/sdapi/v1/progress?skip_current_image=false")
    if not res.get("ok"):
        return res
    data = res["data"]
    return {
        "ok": True,
        "action": "progress",
        "progress": data.get("progress"),
        "eta_relative": data.get("eta_relative"),
        "state": data.get("state"),
        "data": _json.dumps(data, ensure_ascii=False)[:3000],
    }


def _handle_interrupt(kwargs: dict) -> dict:
    base_url = _resolve_base_url(kwargs)
    res = _api(base_url, "/sdapi/v1/interrupt", payload={}, method="POST")
    if not res.get("ok"):
        return res
    return {"ok": True, "action": "interrupt"}


def _handle_get_options(kwargs: dict) -> dict:
    base_url = _resolve_base_url(kwargs)
    res = _api(base_url, "/sdapi/v1/options")
    if not res.get("ok"):
        return res
    data = res["data"]
    key = kwargs.get("option_key")
    if key:
        val = data.get(key) if isinstance(data, dict) else None
        return {"ok": True, "action": "get_options", "option_key": key, "option_value": val}
    return {"ok": True, "action": "get_options", "data": _json.dumps(data, ensure_ascii=False)[:6000]}


def _handle_set_options(kwargs: dict) -> dict:
    base_url = _resolve_base_url(kwargs)
    key = kwargs.get("option_key")
    val = kwargs.get("option_value")
    if not key or val is None:
        return {"ok": False, "error": "set_options 需要 option_key 和 option_value"}
    # 尝试把 value 还原成数字/布尔/JSON
    try:
        val = _json.loads(val)
    except Exception:
        pass
    res = _api(base_url, "/sdapi/v1/options", payload={key: val}, method="POST")
    if not res.get("ok"):
        return res
    return {"ok": True, "action": "set_options", "option_key": key, "option_value": val}


# ==============================================================
# 入口
# ==============================================================
_ACTIONS = {
    "txt2img": _handle_txt2img,
    "img2img": _handle_img2img,
    "list": _handle_list,
    "progress": _handle_progress,
    "interrupt": _handle_interrupt,
    "get_options": _handle_get_options,
    "set_options": _handle_set_options,
}


def run(action: str = "", **kwargs: Any) -> dict[str, Any]:
    if not action:
        return {"ok": False, "error": "action 不能为空"}
    handler = _ACTIONS.get(action)
    if not handler:
        return {"ok": False, "error": f"未知 action：{action}，可选：{sorted(_ACTIONS.keys())}"}
    try:
        return handler(kwargs)
    except Exception as e:
        return {"ok": False, "error": f"执行 {action} 失败：{e}"}