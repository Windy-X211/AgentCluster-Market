"""http_request 插件执行代码 — 对应 manifest.yaml + tool.yaml

调用签名 (由 tool.yaml 的 parameters 决定)：
    run(method="GET", url="https://...", headers={} | None, body="", timeout=30)

实现上用 httpx（同步，跟原来 backend/plugins/http_request.py 保持一致）。
"""
import sys
from pathlib import Path
from typing import Any

# 允许独立 import（不依赖 backend 作为包）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
try:
    import httpx
except ImportError:
    httpx = None


def run(method: str = "GET", url: str = "", headers: dict | str | None = None,
        body: str = "", timeout: int = 30) -> dict[str, Any]:
    """发起 HTTP 请求。返回 {'status': int, 'body': str, 'headers': dict}；失败时返回 {'error': str}。"""
    if httpx is None:
        return {"error": "httpx 未安装，请 pip install httpx"}
    if not url:
        return {"error": "url 不能为空"}

    # headers 兼容 string（旧版可能从 json 里解析不出来）
    h: dict[str, str]
    if isinstance(headers, dict):
        h = {str(k): str(v) for k, v in headers.items()}
    elif isinstance(headers, str) and headers.strip():
        try:
            import json as _j
            h = _j.loads(headers)
        except Exception:
            h = {}
    else:
        h = {"Content-Type": "application/json"} if body else {}

    try:
        r = httpx.request(method.upper(), url, headers=h, content=body or None, timeout=int(timeout))
        return {
            "status": r.status_code,
            "body": r.text[:8000],
            "headers": dict(r.headers),
        }
    except Exception as e:
        return {"error": str(e)}
