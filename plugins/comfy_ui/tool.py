"""ComfyUI 生图插件 —— 连接本地/任意位置 ComfyUI：已有画布填提示词生图 / 智能体从零绘制画布。

纯标准库实现。画布来源优先级：界面草稿 > 最近执行历史 > 内置动态模板。
"""

import json
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_OUTPUT_DIR = "outputs/comfy_ui"
_HTTP_TIMEOUT = 30
_POLL_INTERVAL = 2.0
_LAUNCH_WAIT = 180

_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parent.parent

_WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
_SAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "KSampler (Simple)", "SamplerCustom"}
_CONTROL_VALUES = ("fixed", "randomize", "increment", "decrement")

_OI_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


class _ComfyError(Exception):
    pass


# ---------------------------------------------------------------- 基础设施

def _load_config() -> dict:
    try:
        return json.loads(_PLUGIN_DIR.joinpath("config.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _resolve_base_url(kwargs: dict) -> str:
    cfg = _load_config()
    return str(kwargs.get("base_url") or cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def _resolve_save_dir(kwargs: dict) -> Path:
    cfg = _load_config()
    d = kwargs.get("save_dir") or cfg.get("output_dir") or DEFAULT_OUTPUT_DIR
    p = Path(str(d))
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _api(base_url: str, path: str, payload: Any = None, method: str | None = None,
         timeout: float = _HTTP_TIMEOUT, raw: bool = False) -> Any:
    url = base_url + path
    data = None
    if payload is not None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if method is None:
            method = "POST"
    if method is None:
        method = "GET"
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:600].decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise _ComfyError(f"ComfyUI HTTP {e.code} {path}: {detail}")
    except urllib.error.URLError as e:
        raise _ComfyError(f"无法连接 ComfyUI（{url}）：{getattr(e, 'reason', e)}。"
                          f"请确认服务已启动；地址可在插件目录 config.json 的 base_url 修改")
    if raw:
        return body
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise _ComfyError("ComfyUI 返回了非 JSON 响应")


def _get_object_info(base_url: str) -> dict:
    if _OI_CACHE["data"] and time.time() - _OI_CACHE["ts"] < 60:
        return _OI_CACHE["data"]
    data = _api(base_url, "/object_info", timeout=120)
    _OI_CACHE.update(ts=time.time(), data=data)
    return data


def _choices(oi: dict, node: str, field: str) -> list:
    inp = (oi.get(node, {}) or {}).get("input", {}) or {}
    spec = (inp.get("required", {}) or {}).get(field) or (inp.get("optional", {}) or {}).get(field)
    if not spec:
        return []
    v = spec[0]
    return v if isinstance(v, list) else []


def _default_of(oi: dict, node: str, field: str) -> Any:
    spec = ((oi.get(node, {}).get("input", {}) or {}).get("required", {}) or {}).get(field)
    if not spec or len(spec) < 2 or not isinstance(spec[1], dict):
        return None
    return spec[1].get("default")


# ---------------------------------------------------------------- 自动启动

def _ensure_server(base_url: str, cfg: dict, auto_launch: Any = None) -> dict:
    try:
        _api(base_url, "/system_stats", timeout=5)
        return {"ok": True, "wasRunning": True}
    except _ComfyError:
        pass
    if auto_launch is None:
        auto_launch = bool(cfg.get("auto_launch", True))
    if not auto_launch:
        return {"ok": False,
                "error": f"ComfyUI 未运行（{base_url}）。可先启动服务再重试，或传 autoLaunch=true 自动启动"}
    cmd = str(cfg.get("launch_command") or "").strip()
    if not cmd:
        return {"ok": False, "error": "ComfyUI 未运行，且 config.json 未配置 launch_command，无法自动启动"}
    try:
        log_dir = _PROJECT_ROOT / "outputs" / "comfy_ui"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = (log_dir / "comfyui.log").resolve()
        inner = f'cmd /c ""{cmd}" >> "{log_path}" 2>&1"'
        subprocess.Popen(f'start "ComfyUI" /min {inner}', shell=True)
    except Exception as e:
        return {"ok": False, "error": f"执行启动命令失败：{e}（检查 config.json 的 launch_command）"}
    deadline = time.time() + _LAUNCH_WAIT
    while time.time() < deadline:
        time.sleep(3)
        try:
            _api(base_url, "/system_stats", timeout=5)
            return {"ok": True, "wasRunning": False, "launched": True}
        except _ComfyError:
            continue
    return {"ok": False,
            "error": f"已执行启动命令，但 {_LAUNCH_WAIT} 秒内 ComfyUI 未就绪。"
                     f"请手动检查服务状态（首次启动加载模型可能较慢，稍后可用 action=status 重试）"}


# ---------------------------------------------------------------- 工作流格式

def _is_api_format(wf: Any) -> bool:
    if not isinstance(wf, dict) or not wf:
        return False
    for v in wf.values():
        if not (isinstance(v, dict) and "class_type" in v and isinstance(v.get("inputs"), dict)):
            return False
    return True


def _input_defs(oi: dict, class_type: str) -> dict:
    info = oi.get(class_type, {})
    inp = dict((info.get("input", {}) or {}).get("required", {}) or {})
    inp.update((info.get("input", {}) or {}).get("optional", {}) or {})
    return inp


def _is_link_type(spec: Any) -> bool:
    t = spec[0] if isinstance(spec, list) and spec else spec
    if isinstance(t, list):
        return False
    return t not in _WIDGET_TYPES


def _ui_to_api(ui: dict, oi: dict) -> dict:
    if not isinstance(ui, dict) or not isinstance(ui.get("nodes"), list):
        raise _ComfyError("不是 UI 格式工作流（缺少 nodes 数组）")
    links: dict[Any, Any] = {}
    for l in ui.get("links") or []:
        if isinstance(l, (list, tuple)) and len(l) >= 6:
            links[l[0]] = l
    unknown = sorted({str(n.get("type")) for n in ui["nodes"] if n.get("type") not in oi})
    if unknown:
        raise _ComfyError(f"画布包含当前 ComfyUI 未安装的节点类型：{unknown[:10]}。"
                          f"请改用其他画布来源，或先在 ComfyUI 安装对应自定义节点")
    api: dict[str, Any] = {}
    for node in ui["nodes"]:
        nid = str(node.get("id"))
        ct = node.get("type")
        inp = _input_defs(oi, ct)
        api_inputs: dict[str, Any] = {}
        for slot in node.get("inputs") or []:
            lid = slot.get("link")
            name = slot.get("name")
            if lid is None or name is None:
                continue
            l = links.get(lid)
            if l:
                api_inputs[name] = [str(l[1]), int(l[2])]
        wv = node.get("widgets_values")
        if isinstance(wv, dict):
            for name, spec in inp.items():
                if name in wv and name not in api_inputs and not _is_link_type(spec):
                    api_inputs[name] = wv[name]
        elif isinstance(wv, list):
            wi = 0
            for name, spec in inp.items():
                if name in api_inputs or _is_link_type(spec):
                    continue
                if wi >= len(wv):
                    break
                api_inputs[name] = wv[wi]
                wi += 1
                if name in ("seed", "noise_seed") and wi < len(wv) and wv[wi] in _CONTROL_VALUES:
                    wi += 1
        api[nid] = {"class_type": ct, "inputs": api_inputs}
    if not api:
        raise _ComfyError("画布为空（没有节点）")
    return api


def _api_to_ui(api: dict, oi: dict, title: str = "Agent Canvas") -> dict:
    depth: dict[str, int] = {}

    def dep(nid: str, stack: tuple = ()) -> int:
        if nid in depth:
            return depth[nid]
        if nid in stack:
            return 0
        d = 0
        for v in api[nid].get("inputs", {}).values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and str(v[0]) in api:
                d = max(d, dep(str(v[0]), stack + (nid,)) + 1)
        depth[nid] = d
        return d

    for nid in api:
        dep(nid)
    cols: dict[int, list] = {}
    for nid in api:
        cols.setdefault(depth.get(nid, 0), []).append(nid)

    id_map: dict[str, int] = {}
    next_extra = 1000
    for nid in api:
        if str(nid).isdigit():
            id_map[nid] = int(nid)
        else:
            id_map[nid] = next_extra
            next_extra += 1

    outs: dict[str, list] = {}
    for nid, node in api.items():
        info = oi.get(node.get("class_type"), {})
        odefs = info.get("output", []) or []
        onames = info.get("output_name", []) or odefs
        outs[nid] = [{"name": onames[i] if i < len(onames) else f"OUT_{i}",
                      "type": odefs[i] if i < len(odefs) else "*", "links": []}
                     for i in range(max(len(odefs), 1))]

    ui_nodes: list = []
    ui_links: list = []
    link_id = 0
    for nid, node in api.items():
        ct = node.get("class_type")
        inp = _input_defs(oi, ct)
        node_inputs: list = []
        widgets: list = []
        for name, spec in inp.items():
            t = spec[0] if isinstance(spec, list) and spec else spec
            tname = t if isinstance(t, str) else "COMBO"
            v = (node.get("inputs") or {}).get(name, "__missing__")
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and str(v[0]) in api:
                link_id += 1
                slot_idx = len(node_inputs)
                node_inputs.append({"name": name, "type": tname, "link": link_id})
                origin, oslot = str(v[0]), int(v[1])
                otype = outs[origin][oslot]["type"] if oslot < len(outs[origin]) else tname
                ui_links.append([link_id, id_map[origin], oslot, id_map[nid], slot_idx, otype])
                outs[origin][oslot]["links"].append(link_id)
            elif v != "__missing__":
                widgets.append(v)
                if name in ("seed", "noise_seed"):
                    widgets.append("fixed")
        d = depth.get(nid, 0)
        idx = cols[d].index(nid)
        ui_nodes.append({
            "id": id_map[nid], "type": ct,
            "pos": [40 + d * 360, 100 + idx * 300],
            "size": {"0": 320, "1": max(120, 38 * len(node_inputs) + 30 * len(widgets) + 60)},
            "flags": {}, "order": d * 100 + idx, "mode": 0,
            "inputs": node_inputs, "outputs": outs[nid],
            "properties": {}, "widgets_values": widgets,
        })
    last_node = max(id_map.values()) if id_map else len(api)
    return {"last_node_id": last_node, "last_link_id": link_id, "nodes": ui_nodes,
            "links": ui_links, "groups": [], "config": {}, "extra": {},
            "version": 0.4, "title": title}


# ---------------------------------------------------------------- 画布获取

def _list_userdata_workflows(base_url: str) -> list:
    data = _api(base_url, "/api/userdata?dir=workflows")
    names = []
    for item in data or []:
        if isinstance(item, dict):
            names.append(str(item.get("path") or item.get("name") or "").replace("\\", "/"))
        else:
            names.append(str(item))
    return sorted({n for n in names if n})


def _fetch_userdata_workflow(base_url: str, name: str) -> dict:
    name = name.replace("\\", "/")
    if not name.startswith("workflows/"):
        name = f"workflows/{name}"
    quoted = urllib.parse.quote(name, safe="")
    data = _api(base_url, f"/api/userdata/{quoted}")
    if not isinstance(data, dict):
        raise _ComfyError(f"工作流 {name} 内容异常")
    return data


def _save_userdata_workflow(base_url: str, name: str, ui: dict) -> str:
    if not name.endswith(".json"):
        name += ".json"
    name = name.replace("\\", "/")
    if not name.startswith("workflows/"):
        name = f"workflows/{name}"
    body = json.dumps(ui, ensure_ascii=False).encode("utf-8")
    quoted = urllib.parse.quote(name, safe="")
    last_err: Exception | None = None
    for method in ("POST", "PUT"):
        try:
            _api(base_url, f"/api/userdata/{quoted}", payload=body, method=method, timeout=60)
            return name.split("/", 1)[1]
        except _ComfyError as e:
            last_err = e
            if "HTTP 405" not in str(e) and "HTTP 404" not in str(e):
                raise
    raise last_err if last_err else _ComfyError("保存失败")


def _delete_userdata_workflow(base_url: str, name: str) -> None:
    name = name.replace("\\", "/")
    if not name.startswith("workflows/"):
        name = f"workflows/{name}"
    quoted = urllib.parse.quote(name, safe="")
    _api(base_url, f"/api/userdata/{quoted}", method="DELETE")


def _acquire_draft(base_url: str, k: dict) -> dict:
    names = _list_userdata_workflows(base_url)
    if not names:
        raise _ComfyError("ComfyUI 服务端没有已保存的工作流草稿（在界面中搭好画布后会自动保存）")
    want = k.get("workflowName")
    if want:
        cand = [n for n in names if n == want or n == f"{want}.json" or n.endswith(f"/{want}.json")]
        if not cand:
            raise _ComfyError(f"未找到工作流 {want}，可用：{names[:20]}")
    else:
        prefer = [n for n in names if n.endswith("Unsaved Workflow.json")]
        cand = prefer or names[:1]
    name = cand[0]
    ui = _fetch_userdata_workflow(base_url, name)
    api = _ui_to_api(ui, _get_object_info(base_url))
    return {"api": api, "source": "draft", "name": name}


def _acquire_history(base_url: str, k: dict) -> dict:
    hid = k.get("historyId")
    if hid:
        data = _api(base_url, f"/history/{urllib.parse.quote(str(hid))}")
        entry = data.get(str(hid)) if isinstance(data, dict) else None
        if not entry:
            raise _ComfyError(f"历史中不存在条目 {hid}")
        pid, e = str(hid), entry
    else:
        data = _api(base_url, "/history?max_items=100")
        entries = [(str(p), e) for p, e in (data or {}).items()
                   if (e.get("status") or {}).get("status_str") == "completed"]
        if not entries:
            raise _ComfyError("没有已完成的执行历史")
        pid, e = entries[-1]
    prompt = e.get("prompt")
    if not _is_api_format(prompt):
        raise _ComfyError("历史条目中的工作流不是 API 格式，无法复用")
    return {"api": prompt, "source": "history", "name": pid}


def _acquire_workflow(base_url: str, source: str, k: dict) -> dict:
    if source == "file":
        path = k.get("filePath")
        if not path:
            raise _ComfyError("workflowSource=file 需要同时传 filePath（本地工作流 JSON 路径）")
        try:
            wf = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise _ComfyError(f"读取工作流文件失败：{e}")
        api = wf if _is_api_format(wf) else _ui_to_api(wf, _get_object_info(base_url))
        return {"api": api, "source": "file", "name": Path(path).name}
    if source == "draft":
        return _acquire_draft(base_url, k)
    if source == "history":
        return _acquire_history(base_url, k)
    if source == "template":
        return {"api": _build_template(base_url, k), "source": "template", "name": "builtin-txt2img"}
    if source == "auto":
        notes = []
        for fn in (_acquire_draft, _acquire_history):
            try:
                r = fn(base_url, k)
                r["note"] = "; ".join(notes) if notes else None
                return r
            except _ComfyError as e:
                notes.append(f"{'界面草稿' if fn is _acquire_draft else '执行历史'}不可用：{e}")
        api = _build_template(base_url, k)
        return {"api": api, "source": "template", "name": "builtin-txt2img", "note": "; ".join(notes)}
    raise _ComfyError(f"未知 workflowSource：{source}（可选 auto/draft/history/template/file）")


# ---------------------------------------------------------------- 提示词注入

def _walk_chain(api: dict, nid: str, seen: set | None = None) -> list:
    seen = seen if seen is not None else set()
    if nid in seen or nid not in api:
        return []
    seen.add(nid)
    out = [nid]
    for v in api[nid].get("inputs", {}).values():
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
            out += _walk_chain(api, str(v[0]), seen)
    return out


def _find_prompt_nodes(api: dict) -> tuple[list, list]:
    pos: list = []
    neg: list = []
    for nid, node in api.items():
        if node.get("class_type") in _SAMPLER_TYPES:
            for key, acc in (("positive", pos), ("negative", neg)):
                v = (node.get("inputs") or {}).get(key)
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                    for t in _walk_chain(api, str(v[0])):
                        if api[t].get("class_type") == "CLIPTextEncode" and t not in acc:
                            acc.append(t)
    if not pos and not neg:
        encs = [nid for nid, n in api.items() if n.get("class_type") == "CLIPTextEncode"]
        for nid in encs:
            text = str((api[nid].get("inputs") or {}).get("text", "")).lower()
            if any(w in text for w in ("negative", "worst quality", "low quality", "负面", "反向")):
                neg.append(nid)
            else:
                pos.append(nid)
        if len(encs) == 1 and not pos:
            pos = encs
    return pos, neg


def _inject_prompts(api: dict, prompt: Any, negative: Any) -> dict:
    pos, neg = _find_prompt_nodes(api)
    filled = {"positive": [], "negative": [], "negativeSkipped": False}
    if prompt is not None:
        if not pos:
            raise _ComfyError("未在画布中找到正向提示词节点（CLIPTextEncode）。"
                              "可用 action=inspectWorkflow 查看画布结构，或用 action=drawCanvas 重新搭建")
        for nid in pos:
            api[nid]["inputs"]["text"] = str(prompt)
        filled["positive"] = pos
    if negative is not None:
        if not neg:
            filled["negativeSkipped"] = True
        else:
            for nid in neg:
                api[nid]["inputs"]["text"] = str(negative)
            filled["negative"] = neg
    return {"positive": pos, "negative": neg, "filled": filled}


# ---------------------------------------------------------------- 参数覆盖

_FIELD_MAP = {"steps": "steps", "cfg": "cfg", "sampler": "sampler_name",
              "scheduler": "scheduler", "denoise": "denoise"}
_LOADER_MAP = {"checkpoint": ("CheckpointLoaderSimple", "ckpt_name"),
               "unet": ("UNETLoader", "unet_name"),
               "clip": ("CLIPLoader", "clip_name"),
               "vae": ("VAELoader", "vae_name")}
_LATENT_TYPES = ("EmptyLatentImage", "EmptySD3LatentImage", "EmptyLatent")


def _collect_overrides(k: dict) -> dict:
    ov: dict = {}
    for key in ("checkpoint", "unet", "clip", "vae", "lora", "loraStrength",
                "steps", "cfg", "sampler", "scheduler", "denoise",
                "width", "height", "batchSize"):
        if k.get(key) is not None:
            ov[key] = k[key]
    return ov


def _apply_overrides(api: dict, ov: dict) -> tuple[list, list]:
    applied: list = []
    ignored: list = []
    samplers = [nid for nid, n in api.items() if n.get("class_type") in ("KSampler", "KSamplerAdvanced")]
    latents = [nid for nid, n in api.items() if n.get("class_type") in _LATENT_TYPES]
    lora_nodes = [nid for nid, n in api.items()
                  if n.get("class_type") in ("LoraLoader", "LoraLoaderModelOnly")]
    for key, val in ov.items():
        if key in _FIELD_MAP:
            field = _FIELD_MAP[key]
            if not samplers:
                ignored.append(f"{key}（画布无 KSampler/KSamplerAdvanced）")
                continue
            hit = False
            for nid in samplers:
                if field in api[nid]["inputs"]:
                    api[nid]["inputs"][field] = val
                    hit = True
            (applied if hit else ignored).append(f"{key}={val}" if hit else f"{key}（采样器无字段 {field}）")
        elif key in ("width", "height", "batchSize"):
            if not latents:
                ignored.append(f"{key}（画布无 EmptyLatent 系节点）")
                continue
            field = {"width": "width", "height": "height", "batchSize": "batch_size"}[key]
            for nid in latents:
                api[nid]["inputs"][field] = int(val)
            applied.append(f"{key}={val}")
        elif key in _LOADER_MAP:
            ctype, field = _LOADER_MAP[key]
            targets = [nid for nid, n in api.items() if n.get("class_type") == ctype]
            if not targets:
                ignored.append(f"{key}（画布无 {ctype} 节点）")
                continue
            for nid in targets:
                api[nid]["inputs"][field] = val
            applied.append(f"{key}={val}")
        elif key in ("lora", "loraStrength"):
            if not lora_nodes:
                ignored.append(f"{key}（画布无 LoRA 加载节点；如需新增请用 drawCanvas 重建画布）")
                continue
            for nid in lora_nodes:
                if key == "lora":
                    api[nid]["inputs"]["lora_name"] = val
                else:
                    api[nid]["inputs"]["strength_model"] = float(val)
                    if "strength_clip" in api[nid]["inputs"]:
                        api[nid]["inputs"]["strength_clip"] = float(val)
            applied.append(f"{key}={val}")
    return applied, ignored


def _set_seed(api: dict, seed: int) -> list:
    hit = []
    for nid, n in api.items():
        if n.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            for f in ("seed", "noise_seed"):
                if f in (n.get("inputs") or {}):
                    n["inputs"][f] = int(seed)
                    hit.append(nid)
                    break
    return hit


# ---------------------------------------------------------------- 内置模板

def _guess_clip_type(oi: dict, clip_name: str) -> str | None:
    spec = ((oi.get("CLIPLoader", {}).get("input", {}) or {}).get("required", {}) or {}).get("type")
    if not spec or not isinstance(spec[0], list) or not spec[0]:
        return None
    toks = {t for t in re.split(r"[^a-z0-9]+", str(clip_name).lower()) if t}
    generic = {"sd", "sdxl", "x", "xl", "base", "v", "fp8", "gguf", "safetensors",
               "text", "encoder", "clip", "model", "diffusion"}
    for c in spec[0]:
        ctoks = {t for t in re.split(r"[^a-z0-9]+", str(c).lower()) if t}
        if (toks & ctoks) - generic:
            return c
    return None


def _build_template(base_url: str, k: dict) -> dict:
    oi = _get_object_info(base_url)
    ck = k.get("checkpoint")
    ckpts = _choices(oi, "CheckpointLoaderSimple", "ckpt_name")
    steps = int(k.get("steps") or 20)
    cfg = float(k.get("cfg") or 6.0)
    w = int(k.get("width") or 1024)
    h = int(k.get("height") or 1024)
    bs = int(k.get("batchSize") or 1)
    sampler = str(k.get("sampler") or "euler")
    sched = str(k.get("scheduler") or "normal")
    denoise = float(k.get("denoise") or 1.0)
    pos_text = str(k.get("prompt") or "")
    neg_text = str(k.get("negativePrompt") or "worst quality, low quality, bad anatomy")
    if ckpts or ck:
        if not ck:
            ck = ckpts[0]
        elif ckpts and ck not in ckpts:
            raise _ComfyError(f"checkpoint {ck} 不存在，可用：{ckpts[:15]}")
        api = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ck}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": pos_text, "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text, "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": bs}},
            "5": {"class_type": "KSampler", "inputs": {
                "seed": 0, "steps": steps, "cfg": cfg, "sampler_name": sampler,
                "scheduler": sched, "denoise": denoise,
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
            "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "agent_cluster", "images": ["6", 0]}},
        }
        if k.get("lora"):
            loras = _choices(oi, "LoraLoader", "lora_name")
            if loras and k["lora"] not in loras:
                raise _ComfyError(f"LoRA {k['lora']} 不存在，可用：{loras[:15]}")
            api["8"] = {"class_type": "LoraLoader", "inputs": {
                "lora_name": k["lora"], "strength_model": float(k.get("loraStrength") or 1.0),
                "strength_clip": float(k.get("loraStrength") or 1.0),
                "model": ["1", 0], "clip": ["1", 1]}}
            api["5"]["inputs"]["model"] = ["8", 0]
            for enc in ("2", "3"):
                api[enc]["inputs"]["clip"] = ["8", 1]
        return api
    unets = _choices(oi, "UNETLoader", "unet_name")
    clips = _choices(oi, "CLIPLoader", "clip_name")
    vaes = _choices(oi, "VAELoader", "vae_name")
    unet = k.get("unet") or (unets[0] if unets else None)
    clip = k.get("clip") or (clips[0] if clips else None)
    vae = k.get("vae") or (vaes[0] if vaes else None)
    if not (unet and clip and vae):
        raise _ComfyError("没有可用模型：checkpoints 为空，且三件套（diffusion_models/text_encoders/vae）"
                          "不齐全。请先在 ComfyUI 的 models 目录放置模型，或用 filePath 提供工作流文件")
    clip_type = _guess_clip_type(oi, str(clip)) or _default_of(oi, "CLIPLoader", "type") or "flux"
    api = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": clip_type}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": pos_text, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_text, "clip": ["2", 0]}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": bs}},
        "7": {"class_type": "KSampler", "inputs": {
            "seed": 0, "steps": steps, "cfg": cfg, "sampler_name": sampler,
            "scheduler": sched, "denoise": denoise,
            "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "agent_cluster", "images": ["8", 0]}},
    }
    if k.get("lora"):
        loras = _choices(oi, "LoraLoaderModelOnly", "lora_name")
        if loras and k["lora"] not in loras:
            raise _ComfyError(f"LoRA {k['lora']} 不存在，可用：{loras[:15]}")
        api["10"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "lora_name": k["lora"], "strength_model": float(k.get("loraStrength") or 1.0),
            "model": ["1", 0]}}
        api["7"]["inputs"]["model"] = ["10", 0]
    return api


# ---------------------------------------------------------------- 执行与下载

def _submit(base_url: str, api: dict) -> dict:
    data = _api(base_url, "/prompt", payload={"prompt": api, "client_id": "agent-cluster"}, timeout=60)
    if data.get("node_errors"):
        errs = []
        for nid, err in (data["node_errors"] or {}).items():
            errs.append(f"节点{nid}({err.get('type')}): {json.dumps(err.get('errors'), ensure_ascii=False)[:300]}")
        raise _ComfyError("工作流校验失败：" + "; ".join(errs)[:1500])
    if not data.get("prompt_id"):
        raise _ComfyError(f"提交失败：{json.dumps(data, ensure_ascii=False)[:400]}")
    return data


def _wait_result(base_url: str, prompt_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _api(base_url, f"/history/{urllib.parse.quote(str(prompt_id))}", timeout=60)
        entry = data.get(prompt_id) if isinstance(data, dict) else None
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                raise _ComfyError("执行失败：" + json.dumps(status.get("messages"), ensure_ascii=False)[:800])
            if status.get("completed"):
                return entry
        time.sleep(_POLL_INTERVAL)
    raise _ComfyError(f"等待 {int(timeout)} 秒未完成（可能仍在执行或队列积压）。"
                      f"可用 action=queue 查看队列，action=interrupt 中断")


def _download_images(base_url: str, entry: dict, kwargs: dict, seed: Any) -> list:
    images = []
    for out in (entry.get("outputs") or {}).values():
        for img in (out.get("images") or []) if isinstance(out, dict) else []:
            if isinstance(img, dict) and img.get("filename"):
                images.append(img)
    if not images:
        raise _ComfyError("执行完成但没有输出图片（检查画布是否包含 SaveImage/PreviewImage 节点）")
    save_dir = _resolve_save_dir(kwargs)
    save_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    files = []
    for i, img in enumerate(images):
        q = urllib.parse.urlencode({"filename": img["filename"],
                                    "subfolder": img.get("subfolder") or "",
                                    "type": img.get("type") or "output"})
        body = _api(base_url, f"/view?{q}", raw=True, timeout=180)
        suffix = Path(str(img["filename"])).suffix or ".png"
        fp = save_dir / f"comfy_{ts}_{seed if seed is not None else 'x'}_{i}{suffix}"
        fp.write_bytes(body)
        files.append(str(fp.resolve()))
    return files


def _execute(base_url: str, api: dict, k: dict, cfg: dict) -> dict:
    inj = _inject_prompts(api, k.get("prompt"), k.get("negativePrompt"))
    applied, ignored = _apply_overrides(api, _collect_overrides(k))
    seed = k.get("seed")
    if seed is None and k.get("randomizeSeed", True):
        seed = random.randint(0, 2 ** 48 - 1)
    if seed is not None:
        if _set_seed(api, int(seed)):
            applied.append(f"seed={seed}")
        else:
            ignored.append("seed（画布无 KSampler/KSamplerAdvanced）")
    timeout = float(k.get("timeout") or cfg.get("wait_timeout") or 600)
    t0 = time.time()
    sub = _submit(base_url, api)
    entry = _wait_result(base_url, sub["prompt_id"], timeout)
    out = {"promptId": sub.get("prompt_id"), "seed": seed,
           "elapsedSeconds": round(time.time() - t0, 1),
           "overridesApplied": applied, "overridesIgnored": ignored,
           "promptNodes": inj}
    files = _download_images(base_url, entry, k, seed)
    if files:
        out["file"] = files[0]
        out["files"] = files
        out["image_data_url"] = f"/api/fs/image?path={urllib.parse.quote(files[0], safe='')}"
        out["purpose"] = "share"
        out["count"] = len(files)
    return out


# ---------------------------------------------------------------- action handlers

def _handle_status(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    cfg = _load_config()
    srv = _ensure_server(base_url, cfg, k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    stats = _api(base_url, "/system_stats")
    sysinfo = stats.get("system", {}) or {}
    devices = [{"name": d.get("name"), "type": d.get("type"),
                "vramTotal": d.get("vram_total"), "vramFree": d.get("vram_free")}
               for d in stats.get("devices", [])]
    q = _api(base_url, "/queue")
    try:
        wf_count = len(_list_userdata_workflows(base_url))
    except _ComfyError:
        wf_count = 0
    return {"ok": True, "action": "status", "baseUrl": base_url,
            "launched": bool(srv.get("launched")),
            "comfyuiVersion": sysinfo.get("comfyui_version"),
            "pythonVersion": sysinfo.get("python_version"),
            "pytorchVersion": sysinfo.get("pytorch_version"),
            "devices": devices,
            "queue": {"running": len(q.get("queue_running") or []),
                      "pending": len(q.get("queue_pending") or [])},
            "savedWorkflows": wf_count,
            "autoLaunch": bool(cfg.get("auto_launch", True))}


def _handle_generate(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    cfg = _load_config()
    srv = _ensure_server(base_url, cfg, k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    source = k.get("workflowSource") or "auto"
    wf = _acquire_workflow(base_url, source, k)
    out = _execute(base_url, wf["api"], k, cfg)
    out.update({"ok": True, "action": "generate", "source": wf["source"],
                "canvas": wf.get("name"),
                "prompt": k.get("prompt"), "negativePrompt": k.get("negativePrompt")})
    if wf.get("note"):
        out["sourceNote"] = wf["note"]
    if srv.get("launched"):
        out["launched"] = True
    return out


def _handle_list_workflows(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    cfg = _load_config()
    srv = _ensure_server(base_url, cfg, k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    drafts = _list_userdata_workflows(base_url)
    hist = _api(base_url, "/history?max_items=30") or {}
    items = []
    for pid, e in list(hist.items())[-10:][::-1]:
        st = e.get("status") or {}
        outs = sum(len(o.get("images") or []) for o in (e.get("outputs") or {}).values()
                   if isinstance(o, dict))
        items.append({"historyId": str(pid), "status": st.get("status_str"),
                      "nodeCount": len(e.get("prompt") or {}), "images": outs})
    oi = _get_object_info(base_url)
    ck = _choices(oi, "CheckpointLoaderSimple", "ckpt_name")
    tmpl: dict = {"checkpoints": len(ck)}
    if not ck:
        tmpl.update({"unet": len(_choices(oi, "UNETLoader", "unet_name")),
                     "clip": len(_choices(oi, "CLIPLoader", "clip_name")),
                     "vae": len(_choices(oi, "VAELoader", "vae_name"))})
    return {"ok": True, "action": "listWorkflows", "drafts": drafts,
            "history": items, "template": tmpl}


def _handle_inspect(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    cfg = _load_config()
    srv = _ensure_server(base_url, cfg, k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    source = k.get("workflowSource") or "auto"
    wf = _acquire_workflow(base_url, source, k)
    api = wf["api"]
    pos, neg = _find_prompt_nodes(api)
    nodes = []
    for nid, node in api.items():
        ins = {}
        for name, v in (node.get("inputs") or {}).items():
            ins[name] = f"->{v[0]}.{v[1]}" if isinstance(v, list) else str(v)[:80]
        nodes.append({"id": nid, "type": node.get("class_type"), "inputs": ins})
    return {"ok": True, "action": "inspectWorkflow", "source": wf["source"],
            "canvas": wf.get("name"), "nodeCount": len(api), "nodes": nodes,
            "promptNodes": {"positive": pos, "negative": neg}}


def _parse_workflow_json(wj: Any) -> dict:
    if isinstance(wj, dict):
        wf = wj
    elif isinstance(wj, str):
        try:
            wf = json.loads(wj)
        except json.JSONDecodeError as e:
            raise _ComfyError(f"workflowJson 不是合法 JSON：{e}")
    else:
        raise _ComfyError("workflowJson 需为 API 格式工作流的 JSON 字符串")
    if not _is_api_format(wf):
        raise _ComfyError("workflowJson 需为 API 格式（每个节点含 class_type 与 inputs）；"
                          "UI 格式工作流请改用 filePath 提供")
    return wf


def _handle_draw_canvas(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    cfg = _load_config()
    srv = _ensure_server(base_url, cfg, k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    wj, fp = k.get("workflowJson"), k.get("filePath")
    if wj is not None:
        api = _parse_workflow_json(wj)
        mode = "custom"
    elif fp:
        try:
            wf = json.loads(Path(str(fp)).read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise _ComfyError(f"读取工作流文件失败：{e}")
        api = wf if _is_api_format(wf) else _ui_to_api(wf, _get_object_info(base_url))
        mode = "custom"
    else:
        api = _build_template(base_url, k)
        mode = "template"
    out: dict = {"ok": True, "action": "drawCanvas", "mode": mode, "nodeCount": len(api)}
    save_name = k.get("saveName")
    if save_name:
        try:
            ui = _api_to_ui(api, _get_object_info(base_url), title=str(save_name))
            out["savedTo"] = _save_userdata_workflow(base_url, str(save_name), ui)
            out["saveHint"] = "已保存到 ComfyUI 服务端，可在网页界面左侧「工作流」菜单中打开"
        except Exception as e:
            out["saveWarning"] = f"画布已构建，但保存到 ComfyUI 失败（不影响执行）：{e}"
    if k.get("run", True):
        if mode == "template" and not k.get("prompt"):
            raise _ComfyError("drawCanvas 模板模式立即出图需要 prompt（正向提示词）")
        out.update(_execute(base_url, api, k, cfg))
    return out


def _trim_input_spec(spec: Any) -> dict:
    t = spec[0] if isinstance(spec, list) and spec else spec
    out: dict = {"type": t if isinstance(t, str) else "COMBO"}
    if isinstance(t, list):
        out["options"] = t[:60]
        out["optionCount"] = len(t)
    if len(spec) > 1 and isinstance(spec[1], dict):
        d = spec[1].get("default")
        if d is not None:
            out["default"] = d
    return out


def _handle_node_info(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    srv = _ensure_server(base_url, _load_config(), k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    oi = _get_object_info(base_url)
    nt, kw = k.get("nodeType"), k.get("keyword")
    if nt:
        info = oi.get(str(nt))
        if not info:
            raise _ComfyError(f"未找到节点 {nt}，可用 keyword 参数按关键词搜索节点名")
        inp = info.get("input", {}) or {}
        trimmed = {sec: {name: _trim_input_spec(spec) for name, spec in (inp.get(sec) or {}).items()}
                   for sec in ("required", "optional") if inp.get(sec)}
        return {"ok": True, "action": "nodeInfo", "nodeType": str(nt),
                "input": trimmed, "output": info.get("output", []),
                "outputNames": info.get("output_name", [])}
    if kw:
        hits = [n for n in oi if str(kw).lower() in n.lower()][:60]
        return {"ok": True, "action": "nodeInfo", "keyword": str(kw),
                "matches": hits, "total": len(hits)}
    return {"ok": True, "action": "nodeInfo", "nodeCount": len(oi),
            "hint": "传 nodeType 查询单个节点定义（含参数默认值与可选值），或传 keyword 搜索节点名"}


def _handle_queue(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    srv = _ensure_server(base_url, _load_config(), k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    q = _api(base_url, "/queue")
    running = [item[1] if isinstance(item, list) and len(item) > 1 else item
               for item in (q.get("queue_running") or [])]
    pending = [item[1] if isinstance(item, list) and len(item) > 1 else item
               for item in (q.get("queue_pending") or [])]
    return {"ok": True, "action": "queue",
            "runningCount": len(running), "pendingCount": len(pending),
            "running": [str(x) for x in running[:10]],
            "pending": [str(x) for x in pending[:10]]}


def _handle_interrupt(k: dict) -> dict:
    base_url = _resolve_base_url(k)
    srv = _ensure_server(base_url, _load_config(), k.get("autoLaunch"))
    if not srv.get("ok"):
        return srv
    _api(base_url, "/interrupt", payload={}, method="POST")
    return {"ok": True, "action": "interrupt", "message": "已发送中断请求，正在执行的任务将被取消"}


_ACTIONS = {
    "status": _handle_status,
    "generate": _handle_generate,
    "listWorkflows": _handle_list_workflows,
    "inspectWorkflow": _handle_inspect,
    "drawCanvas": _handle_draw_canvas,
    "nodeInfo": _handle_node_info,
    "queue": _handle_queue,
    "interrupt": _handle_interrupt,
}


def run(action: str = "", **kwargs: Any) -> dict:
    if not action:
        return {"ok": False, "error": "action 不能为空"}
    handler = _ACTIONS.get(action)
    if not handler:
        return {"ok": False, "error": f"未知 action：{action}，可选：{sorted(_ACTIONS)}"}
    try:
        return handler(kwargs)
    except _ComfyError as e:
        return {"ok": False, "action": action, "error": str(e)}
    except Exception as e:
        return {"ok": False, "action": action, "error": f"执行 {action} 失败：{e}"}