"""github_connector 插件 —— GitHub 连接

为智能体提供 GitHub 仓库搜索、克隆拉取、远程项目信息读取、仓库元信息获取与
本地项目提交能力，打通智能体与 GitHub 仓库的感知与操作全链路。

调用签名 (由 tool.yaml 的 parameters 决定)：
    run(action="set_token", token="ghp_xxx")
    run(action="check_auth")
    run(action="search_repos", query="python web", language="python", sort="stars", per_page=30, page=1)
    run(action="get_repo_info", repo="owner/repo")
    run(action="read_readme", repo="owner/repo", ref="main")
    run(action="list_files", repo="owner/repo", path="src", ref="main", recursive=False)
    run(action="read_file", repo="owner/repo", path="package.json", ref="main")
    run(action="clone_repo", repo="owner/repo", dest="/path", branch="main", depth=1, protocol="https")
    run(action="push_project", path="/workspace", message="fix: bug", remote="...", branch="main")
    run(action="create_repo", name="my-repo", description="...", private=True, auto_init=False)
    run(action="get_rate_limit")

11 个 action：
set_token / check_auth / search_repos / get_repo_info / read_readme /
list_files / read_file / clone_repo / push_project / create_repo / get_rate_limit
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 允许独立 import（不依赖 backend 作为包）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

# 延迟导入 httpx（复用项目已有依赖）
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

# ==============================================================
# 配置常量
# ==============================================================
GITHUB_API_BASE = "https://api.github.com"
GITHUB_HTTP_TIMEOUT = 15          # API 请求超时（秒）
GITHUB_CLONE_TIMEOUT = 300        # 克隆超时（秒）
GITHUB_PUSH_TIMEOUT = 60          # 推送超时（秒）
MAX_TEXT_LENGTH = 20000           # 单文件/README 文本截断阈值
MAX_LIST_ITEMS = 500              # 文件列表条目上限
RATE_LIMIT_WARN_THRESHOLD = 50    # 限流告警阈值
DEFAULT_BRANCH = "main"
DEFAULT_VISIBILITY = True         # 建仓默认私有
USER_AGENT = "AgentCluster-GitHubConnector"

# README 候选文件名（按优先级）
README_CANDIDATES = ("README.md", "README.rst", "README.txt", "README",
                     "readme.md", "readme.rst", "readme.txt", "readme")

# ==============================================================
# 模块级状态（进程级，多次 run() 调用间保持）
# ==============================================================
_token: str | None = None
_token_lock = threading.Lock()
_rate_state: dict = {"limit": 0, "remaining": 0, "reset_at": None}

# ==============================================================
# 错误码体系（17 个，大写蛇形字符串）
# ==============================================================
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_INVALID = "AUTH_INVALID"
AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
RATE_LIMITED = "RATE_LIMITED"
REPO_NOT_FOUND = "REPO_NOT_FOUND"
REPO_EXISTS = "REPO_EXISTS"
PATH_NOT_FOUND = "PATH_NOT_FOUND"
REF_NOT_FOUND = "REF_NOT_FOUND"
BRANCH_NOT_FOUND = "BRANCH_NOT_FOUND"
NETWORK_ERROR = "NETWORK_ERROR"
GIT_NOT_FOUND = "GIT_NOT_FOUND"
PUSH_FAILED = "PUSH_FAILED"
PUSH_REJECTED = "PUSH_REJECTED"
DEST_CONFLICT = "DEST_CONFLICT"
CLONE_TIMEOUT = "CLONE_TIMEOUT"
GH_SERVER_ERROR = "GH_SERVER_ERROR"
PARAM_INVALID = "PARAM_INVALID"
NO_REMOTE = "NO_REMOTE"
SSH_AUTH_FAILED = "SSH_AUTH_FAILED"


# ==============================================================
# 统一结果构造
# ==============================================================
def _build_result(ok: bool, data: Any = None, error: str | None = None,
                  code: str | None = None, **extra: Any) -> dict:
    """构造统一返回结构 {ok, data?, error?, code?, ...}。

    成功时返回 {"ok": True, "data": ...}；失败时返回 {"ok": False, "error": "...", "code": "..."}。
    当限流剩余 < 阈值且非限流错误时，自动注入 rate_limit_warning 字段。
    """
    result: dict = {"ok": ok}
    if ok:
        result["data"] = data
    else:
        result["error"] = error if error is not None else "未知错误"
        if code is not None:
            result["code"] = code
    # 限流告警（仅在已获取过限流信息 limit>0 且 remaining 低于阈值时注入，非限流错误本身）
    if (ok and code != RATE_LIMITED
            and _rate_state.get("limit", 0) > 0
            and _rate_state.get("remaining", 0) < RATE_LIMIT_WARN_THRESHOLD):
        result["rate_limit_warning"] = {
            "remaining": _rate_state.get("remaining", 0),
            "reset_at": _rate_state.get("reset_at"),
        }
    if extra:
        result.update(extra)
    return result


# ==============================================================
# 令牌与 URL 脱敏
# ==============================================================
def _mask_token(token: str | None) -> str:
    """令牌脱敏：长度 > 8 时前4+****+后4，否则 ****。"""
    if not token:
        return "****"
    if len(token) > 8:
        return token[:4] + "****" + token[-4:]
    return "****"


def _mask_url(url: str) -> str:
    """URL 脱敏：移除 token@ 凭据部分，保留协议与路径。"""
    if not url:
        return url
    # 处理 https://ghp_xxx@github.com/... → https://github.com/...
    if "://" in url and "@" in url:
        proto, rest = url.split("://", 1)
        if "@" in rest:
            cred, path = rest.split("@", 1)
            return f"{proto}://{path}"
    return url


# ==============================================================
# 仓库全名校验与文本处理
# ==============================================================
def _validate_repo_fullname(repo: str) -> tuple[str, str] | None:
    """校验 repo 含且仅含一个 /，返回 (owner, repo)；非法返回 None。"""
    if not repo or not isinstance(repo, str):
        return None
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _truncate_text(text: str, max_length: int = MAX_TEXT_LENGTH) -> dict:
    """文本截断：返回 {content, truncated, total_length}。"""
    total = len(text)
    if total <= max_length:
        return {"content": text, "truncated": False, "total_length": total}
    return {"content": text[:max_length], "truncated": True, "total_length": total}


def _decode_content(content_b64: str, size: int) -> dict:
    """解码 GitHub API 返回的 base64 文件内容。

    成功返回 {content, truncated, total_length, size, encoding}；
    二进制返回 {content: None, is_binary: True, size, message}。
    """
    if not content_b64:
        return {"content": None, "is_binary": True, "size": size,
                "message": "该文件为空或为二进制，无法以文本返回"}
    try:
        raw = base64.b64decode(content_b64)
    except Exception:
        return {"content": None, "is_binary": True, "size": size,
                "message": "该文件为二进制，无法以文本返回"}
    # 尝试 UTF-8 解码
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"content": None, "is_binary": True, "size": size,
                "message": "该文件为二进制，无法以文本返回"}
    truncated = _truncate_text(text)
    return {
        "content": truncated["content"],
        "truncated": truncated["truncated"],
        "total_length": truncated["total_length"],
        "size": size,
        "encoding": "utf-8",
    }


# ==============================================================
# 认证与令牌管理
# ==============================================================
def _set_token_internal(token: str) -> None:
    """线程安全地写入模块级令牌。"""
    global _token
    with _token_lock:
        _token = token


def _get_token() -> str | None:
    """线程安全地读取模块级令牌。"""
    with _token_lock:
        return _token


def _require_auth() -> dict | None:
    """认证前置守卫：已设置令牌返回 None，未设置返回认证错误 dict。"""
    if _get_token() is None:
        return _build_result(False, error="未设置认证令牌，请先调用 set_token", code=AUTH_REQUIRED)
    return None


def _build_auth_header() -> dict:
    """构造认证头。ghp_ 开头用 token 方式，github_pat_ 开头用 Bearer 方式。"""
    token = _get_token()
    if not token:
        return {}
    if token.startswith("github_pat_"):
        return {"Authorization": f"Bearer {token}"}
    # classic token (ghp_) 及其他前缀默认用 token 方式
    return {"Authorization": f"token {token}"}


# ==============================================================
# GitHub API 统一封装与限流管理
# ==============================================================
def _update_rate_limit(headers: Any) -> None:
    """从响应头读取限流状态并更新模块级 _rate_state。"""
    try:
        limit = headers.get("X-RateLimit-Limit")
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if limit is not None:
            _rate_state["limit"] = int(limit)
        if remaining is not None:
            _rate_state["remaining"] = int(remaining)
        if reset is not None:
            try:
                reset_ts = int(reset)
                _rate_state["reset_at"] = datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                _rate_state["reset_at"] = None
    except Exception:
        pass


def _check_rate_limit() -> dict | None:
    """检查限流：remaining=0 时返回 RATE_LIMITED 错误，否则 None。"""
    if _rate_state.get("remaining", 0) == 0 and _rate_state.get("limit", 0) > 0:
        return _build_result(
            False,
            error=f"GitHub API 限流，请在 {_rate_state.get('reset_at')} 后重试",
            code=RATE_LIMITED,
            reset_at=_rate_state.get("reset_at"),
        )
    return None


def _github_request(method: str, path: str, *, params: dict | None = None,
                    json_body: dict | None = None, require_auth: bool = True) -> dict:
    """GitHub REST API v3 统一请求封装。

    成功返回 {"ok": True, "status": int, "json": body, "headers": headers}；
    失败返回 {"ok": False, "error": str, "code": str}。
    """
    if httpx is None:
        return _build_result(False, error="httpx 未安装，请执行 pip install httpx", code=NETWORK_ERROR)
    if require_auth:
        auth_err = _require_auth()
        if auth_err is not None:
            return auth_err
    # 构造 URL，强制 HTTPS
    url = GITHUB_API_BASE + path
    if not url.startswith("https://"):
        return _build_result(False, error="仅支持 HTTPS 请求", code=NETWORK_ERROR)
    # 构造请求头
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    headers.update(_build_auth_header())
    try:
        resp = httpx.request(
            method.upper(), url,
            params=params, json=json_body,
            headers=headers, timeout=GITHUB_HTTP_TIMEOUT,
        )
    except httpx.TimeoutException:
        return _build_result(False, error="GitHub API 请求超时", code=NETWORK_ERROR)
    except httpx.ConnectError:
        return _build_result(False, error="无法连接 GitHub，请检查网络", code=NETWORK_ERROR)
    except httpx.HTTPError:
        return _build_result(False, error="GitHub API 请求失败", code=NETWORK_ERROR)
    except Exception:
        return _build_result(False, error="GitHub API 请求异常", code=NETWORK_ERROR)
    # 更新限流状态
    _update_rate_limit(resp.headers)
    status = resp.status_code
    # 解析响应体
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    # 状态码映射错误码
    if status >= 200 and status < 300:
        return {"ok": True, "status": status, "json": body, "headers": dict(resp.headers)}
    # 错误信息（从 GitHub 响应体提取，不含令牌）
    err_msg = "GitHub API 错误"
    if isinstance(body, dict) and "message" in body:
        err_msg = str(body["message"])
    if status == 401:
        return _build_result(False, error=f"认证失败：{err_msg}", code=AUTH_INVALID)
    if status == 403:
        # 限流或权限不足
        if _rate_state.get("remaining", 1) == 0:
            return _build_result(False, error=f"API 限流：{err_msg}", code=RATE_LIMITED,
                                 reset_at=_rate_state.get("reset_at"))
        return _build_result(False, error=f"权限不足：{err_msg}", code=AUTH_FORBIDDEN)
    if status == 404:
        return _build_result(False, error=f"资源不存在：{err_msg}", code=REPO_NOT_FOUND)
    if status == 422:
        return _build_result(False, error=f"请求不合法（可能已存在）：{err_msg}", code=REPO_EXISTS)
    if status >= 500:
        return _build_result(False, error=f"GitHub 服务异常：{err_msg}", code=GH_SERVER_ERROR)
    return _build_result(False, error=f"GitHub API 返回 {status}：{err_msg}", code=NETWORK_ERROR)


# ==============================================================
# 本地 Git 操作封装
# ==============================================================
def _check_git_available() -> bool:
    """检测本地是否安装 git。"""
    return shutil.which("git") is not None


def _startupinfo() -> Any:
    """Windows 下隐藏控制台窗口的 startupinfo。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _git_run(args: list, cwd: str | None = None, timeout: int = 60) -> dict:
    """执行 git 命令。

    返回 {"ok": bool, "stdout": str, "stderr": str, "returncode": int}；
    超时返回 {"ok": False, "error": ..., "code": CLONE_TIMEOUT}；
    git 未安装返回 {"ok": False, "error": ..., "code": GIT_NOT_FOUND}。
    """
    cmd = ["git"] + args
    si = _startupinfo() if sys.platform == "win32" else None
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, startupinfo=si,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Git 操作超时", "code": CLONE_TIMEOUT,
                "stdout": "", "stderr": "", "returncode": -1}
    except FileNotFoundError:
        return {"ok": False, "error": "本地未安装 Git，请先安装", "code": GIT_NOT_FOUND,
                "stdout": "", "stderr": "", "returncode": -1}
    except Exception as e:
        return {"ok": False, "error": f"Git 命令执行失败：{e}", "code": PUSH_FAILED,
                "stdout": "", "stderr": "", "returncode": -1}


# ==============================================================
# Action 处理函数
# ==============================================================
def _handle_set_token(kwargs: dict) -> dict:
    """set_token：设置 GitHub Personal Access Token。"""
    token = kwargs.get("token")
    if not token or not isinstance(token, str):
        return _build_result(False, error="token 参数为空或不合法", code=PARAM_INVALID)
    _set_token_internal(token.strip())
    result: dict = {"ok": True, "message": "令牌已设置", "token_masked": _mask_token(token)}
    # 格式非标准时附带警告
    if not (token.startswith("ghp_") or token.startswith("github_pat_")):
        result["warning"] = "令牌格式非标准，建议检查（标准前缀为 ghp_ 或 github_pat_）"
    return result


def _handle_check_auth(kwargs: dict) -> dict:
    """check_auth：验证当前令牌是否有效。"""
    auth_err = _require_auth()
    if auth_err is not None:
        return auth_err
    resp = _github_request("GET", "/user")
    if not resp.get("ok"):
        return resp
    user = resp.get("json", {})
    return _build_result(True, data={
        "authenticated": True,
        "username": user.get("login"),
        "token_masked": _mask_token(_get_token()),
    })


def _handle_get_rate_limit(kwargs: dict) -> dict:
    """get_rate_limit：查询 API 剩余配额与重置时间。"""
    resp = _github_request("GET", "/rate_limit", require_auth=False)
    if resp.get("ok"):
        body = resp.get("json", {})
        core = body.get("resources", {}).get("core", {})
        return _build_result(True, data={
            "limit": core.get("limit", _rate_state.get("limit", 0)),
            "remaining": core.get("remaining", _rate_state.get("remaining", 0)),
            "reset_at": _rate_state.get("reset_at"),
        })
    # 查询本身被限流，返回缓存数据
    if _rate_state.get("limit", 0) > 0:
        return _build_result(True, data={
            "limit": _rate_state.get("limit", 0),
            "remaining": _rate_state.get("remaining", 0),
            "reset_at": _rate_state.get("reset_at"),
            "note": "基于缓存数据",
        })
    return resp


def _handle_search_repos(kwargs: dict) -> dict:
    """search_repos：按关键词/语言/排序检索 GitHub 仓库。"""
    query = kwargs.get("query")
    if not query:
        return _build_result(False, error="query 参数为空", code=PARAM_INVALID)
    per_page = kwargs.get("per_page", 30)
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        return _build_result(False, error="per_page 必须为整数", code=PARAM_INVALID)
    if not 1 <= per_page <= 100:
        return _build_result(False, error="per_page 必须在 [1, 100] 范围内", code=PARAM_INVALID)
    page = kwargs.get("page", 1)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1
    sort = kwargs.get("sort", "stars")
    if sort not in ("stars", "forks", "updated"):
        sort = "stars"
    language = kwargs.get("language")
    # 构造查询参数
    q = query
    if language:
        q = f"{query} language:{language}"
    resp = _github_request("GET", "/search/repositories", require_auth=False,
                           params={"q": q, "sort": sort, "per_page": per_page, "page": page})
    if not resp.get("ok"):
        return resp
    body = resp.get("json", {})
    items = []
    for it in body.get("items", []):
        items.append({
            "full_name": it.get("full_name"),
            "description": it.get("description"),
            "language": it.get("language"),
            "stars": it.get("stargazers_count", 0),
            "forks": it.get("forks_count", 0),
            "updated_at": it.get("updated_at"),
            "html_url": it.get("html_url"),
        })
    return _build_result(True, data={
        "total_count": body.get("total_count", 0),
        "page": page,
        "per_page": per_page,
        "items": items,
    })


def _handle_get_repo_info(kwargs: dict) -> dict:
    """get_repo_info：获取仓库元信息（含最近提交）。"""
    repo = kwargs.get("repo")
    parsed = _validate_repo_fullname(repo)
    if parsed is None:
        return _build_result(False, error="repo 参数不合法，需 owner/repo 格式", code=PARAM_INVALID)
    owner, name = parsed
    resp = _github_request("GET", f"/repos/{owner}/{name}", require_auth=False)
    if not resp.get("ok"):
        return resp
    r = resp.get("json", {})
    info = {
        "full_name": r.get("full_name"),
        "description": r.get("description"),
        "language": r.get("language"),
        "stars": r.get("stargazers_count", 0),
        "forks": r.get("forks_count", 0),
        "watchers": r.get("watchers_count", 0),
        "open_issues": r.get("open_issues_count", 0),
        "default_branch": r.get("default_branch"),
        "visibility": r.get("visibility"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "pushed_at": r.get("pushed_at"),
        "html_url": r.get("html_url"),
        "clone_url": r.get("clone_url"),
    }
    # 获取最近提交（失败不影响主信息）
    latest_commit = None
    cresp = _github_request("GET", f"/repos/{owner}/{name}/commits",
                            require_auth=False, params={"per_page": 1})
    if cresp.get("ok"):
        commits = cresp.get("json", [])
        if isinstance(commits, list) and commits:
            c = commits[0]
            commit_info = c.get("commit", {})
            latest_commit = {
                "sha": c.get("sha"),
                "message": commit_info.get("message", "").split("\n")[0] if commit_info.get("message") else None,
                "author": commit_info.get("author", {}).get("name"),
                "date": commit_info.get("author", {}).get("date"),
            }
    info["latest_commit"] = latest_commit
    return _build_result(True, data=info)


def _handle_read_readme(kwargs: dict) -> dict:
    """read_readme：按候选顺序读取 README 文件内容。"""
    repo = kwargs.get("repo")
    parsed = _validate_repo_fullname(repo)
    if parsed is None:
        return _build_result(False, error="repo 参数不合法，需 owner/repo 格式", code=PARAM_INVALID)
    owner, name = parsed
    ref = kwargs.get("ref")
    params = {"ref": ref} if ref else None
    for readme_name in README_CANDIDATES:
        resp = _github_request("GET", f"/repos/{owner}/{name}/contents/{readme_name}",
                               require_auth=False, params=params)
        if resp.get("ok"):
            fobj = resp.get("json", {})
            decoded = _decode_content(fobj.get("content", ""), fobj.get("size", 0))
            return _build_result(True, data={
                "filename": readme_name,
                **decoded,
            })
        # 404 只是该文件不存在，继续尝试下一个；其他错误直接返回
        code = resp.get("code")
        if code != REPO_NOT_FOUND:
            # ref 不存在映射
            if code == REPO_NOT_FOUND and ref:
                return _build_result(False, error=f"分支/引用 {ref} 不存在", code=REF_NOT_FOUND)
            return resp
    # 全部 README 候选均未找到
    return _build_result(True, data={"content": None, "message": "该仓库无 README 文件"})


def _handle_list_files(kwargs: dict) -> dict:
    """list_files：列出仓库目录下的文件与子目录。"""
    repo = kwargs.get("repo")
    parsed = _validate_repo_fullname(repo)
    if parsed is None:
        return _build_result(False, error="repo 参数不合法，需 owner/repo 格式", code=PARAM_INVALID)
    owner, name = parsed
    path = kwargs.get("path", "")
    ref = kwargs.get("ref")
    recursive = kwargs.get("recursive", False)
    # recursive=True 时优先用 Git Trees API
    if recursive and ref:
        tresp = _github_request("GET", f"/repos/{owner}/{name}/git/trees/{ref}",
                                require_auth=False, params={"recursive": 1})
        if tresp.get("ok"):
            tree = tresp.get("json", {}).get("tree", [])
            items = []
            for node in tree:
                items.append({
                    "name": node.get("path", "").split("/")[-1],
                    "type": "file" if node.get("type") == "blob" else "dir",
                    "path": node.get("path"),
                    "size": node.get("size", 0),
                })
            truncated = len(items) > MAX_LIST_ITEMS
            if truncated:
                items = items[:MAX_LIST_ITEMS]
            return _build_result(True, data={"items": items, "truncated": truncated})
    # 普通列目录
    api_path = f"/repos/{owner}/{name}/contents/{path}" if path else f"/repos/{owner}/{name}/contents"
    params = {"ref": ref} if ref else None
    resp = _github_request("GET", api_path, require_auth=False, params=params)
    if not resp.get("ok"):
        code = resp.get("code")
        if code == REPO_NOT_FOUND and path:
            return _build_result(False, error=f"路径 {path} 不存在", code=PATH_NOT_FOUND)
        if code == REPO_NOT_FOUND and ref:
            return _build_result(False, error=f"分支/引用 {ref} 不存在", code=REF_NOT_FOUND)
        return resp
    body = resp.get("json", [])
    # 响应为列表时是目录；为对象时是文件
    if isinstance(body, dict):
        # path 指向的是文件
        return _build_result(True, data={
            "items": [{
                "name": body.get("name"),
                "type": "file",
                "path": body.get("path"),
                "size": body.get("size", 0),
            }],
            "truncated": False,
        })
    items = []
    for it in body if isinstance(body, list) else []:
        items.append({
            "name": it.get("name"),
            "type": it.get("type", "file"),
            "path": it.get("path"),
            "size": it.get("size", 0),
        })
    # recursive=True 但未用 trees API（无 ref）时，对 dir 递归展开
    if recursive and not ref:
        items = _expand_dirs(owner, name, items, params)
    truncated = len(items) > MAX_LIST_ITEMS
    if truncated:
        items = items[:MAX_LIST_ITEMS]
    return _build_result(True, data={"items": items, "truncated": truncated})


def _expand_dirs(owner: str, name: str, items: list, params: dict | None, depth: int = 0) -> list:
    """递归展开子目录（深度限制避免过深）。"""
    if depth > 5:
        return items
    result = []
    for it in items:
        result.append(it)
        if it.get("type") == "dir":
            sub_path = it.get("path", "")
            sub_resp = _github_request("GET", f"/repos/{owner}/{name}/contents/{sub_path}",
                                       require_auth=False, params=params)
            if sub_resp.get("ok"):
                sub_body = sub_resp.get("json", [])
                if isinstance(sub_body, list):
                    sub_items = [{
                        "name": s.get("name"),
                        "type": s.get("type", "file"),
                        "path": s.get("path"),
                        "size": s.get("size", 0),
                    } for s in sub_body]
                    result.extend(_expand_dirs(owner, name, sub_items, params, depth + 1))
    return result


def _handle_read_file(kwargs: dict) -> dict:
    """read_file：读取仓库内指定文件内容。"""
    repo = kwargs.get("repo")
    parsed = _validate_repo_fullname(repo)
    if parsed is None:
        return _build_result(False, error="repo 参数不合法，需 owner/repo 格式", code=PARAM_INVALID)
    owner, name = parsed
    path = kwargs.get("path")
    if not path:
        return _build_result(False, error="path 参数为空", code=PARAM_INVALID)
    ref = kwargs.get("ref")
    params = {"ref": ref} if ref else None
    resp = _github_request("GET", f"/repos/{owner}/{name}/contents/{path}",
                           require_auth=False, params=params)
    if not resp.get("ok"):
        code = resp.get("code")
        if code == REPO_NOT_FOUND:
            return _build_result(False, error=f"文件路径 {path} 不存在", code=PATH_NOT_FOUND)
        return resp
    fobj = resp.get("json", {})
    decoded = _decode_content(fobj.get("content", ""), fobj.get("size", 0))
    return _build_result(True, data={"filename": fobj.get("name"), **decoded})


def _handle_clone_repo(kwargs: dict) -> dict:
    """clone_repo：克隆或拉取仓库到本地。"""
    repo = kwargs.get("repo")
    parsed = _validate_repo_fullname(repo)
    if parsed is None:
        return _build_result(False, error="repo 参数不合法，需 owner/repo 格式", code=PARAM_INVALID)
    owner, name = parsed
    dest = kwargs.get("dest")
    if not dest:
        return _build_result(False, error="dest 参数为空", code=PARAM_INVALID)
    branch = kwargs.get("branch")
    depth = kwargs.get("depth")
    protocol = kwargs.get("protocol", "https")
    if protocol not in ("https", "ssh"):
        protocol = "https"
    if not _check_git_available():
        return _build_result(False, error="本地未安装 Git，请先安装", code=GIT_NOT_FOUND)
    dest_path = Path(dest).resolve()
    token = _get_token()
    # 构造不含令牌的公开 URL（用于比较与清理）
    public_https = f"https://github.com/{owner}/{name}.git"
    ssh_url = f"git@github.com:{owner}/{name}.git"
    start = _time.time()
    # 检测 dest 状态
    if dest_path.exists():
        if (dest_path / ".git").exists():
            # 已是 Git 仓库，检查 remote 是否同一仓库
            remote_resp = _git_run(["remote", "get-url", "origin"], cwd=str(dest_path), timeout=10)
            if remote_resp.get("ok"):
                current_url = remote_resp["stdout"].strip()
                # 判断是否同一仓库（忽略令牌凭据）
                masked = _mask_url(current_url).rstrip("\n")
                if public_https in masked or ssh_url in masked or f"github.com/{owner}/{name}" in masked:
                    # 同一仓库，执行 pull
                    pull_resp = _git_run(["pull"], cwd=str(dest_path), timeout=GITHUB_CLONE_TIMEOUT)
                    duration = round(_time.time() - start, 2)
                    if pull_resp.get("ok"):
                        return _build_result(True, data={
                            "action": "pull", "dest": str(dest_path),
                            "branch": branch, "duration": duration,
                        })
                    return _build_result(False, error=f"拉取失败：{pull_resp.get('stderr', '')}",
                                         code=NETWORK_ERROR)
                # 非同一仓库
                return _build_result(False, error=f"目标目录已是其他 Git 仓库（remote: {_mask_url(current_url)}）",
                                     code=DEST_CONFLICT)
            # remote 获取失败，可能是无 origin
            return _build_result(False, error="目标目录已是 Git 仓库但无法读取 remote", code=DEST_CONFLICT)
        # dest 存在且非 Git 仓库
        if any(dest_path.iterdir()):
            return _build_result(False, error="目标目录非空且非 Git 仓库", code=DEST_CONFLICT)
    # 执行 clone
    dest_path.mkdir(parents=True, exist_ok=True)
    created_by_us = True
    # 构造克隆 URL
    if protocol == "ssh":
        clone_url = ssh_url
    else:
        # HTTPS 嵌入令牌（克隆后清理）
        if token:
            clone_url = f"https://{token}@github.com/{owner}/{name}.git"
        else:
            clone_url = public_https
    # 构造 git clone 命令
    clone_args = ["clone"]
    if depth:
        try:
            clone_args += ["--depth", str(int(depth))]
        except (TypeError, ValueError):
            pass
    if branch:
        clone_args += ["-b", branch]
    clone_args += [clone_url, str(dest_path)]
    clone_resp = _git_run(clone_args, timeout=GITHUB_CLONE_TIMEOUT)
    if not clone_resp.get("ok"):
        # 清理半成品：仅当 dest 不含完整 .git 时才删（clone 可能已成功，仅输出解码失败）
        if created_by_us and dest_path.exists():
            has_git = (dest_path / ".git").exists()
            has_content = any(dest_path.iterdir())
            if not (has_git and has_content):
                shutil.rmtree(dest_path, ignore_errors=True)
        # 错误信息：优先 stderr，为空则回退 _git_run 的 error 字段（如 UnicodeDecodeError 等）
        stderr = clone_resp.get("stderr", "") or clone_resp.get("error", "")
        code = clone_resp.get("code")
        if code == CLONE_TIMEOUT:
            return _build_result(False, error="克隆超时", code=CLONE_TIMEOUT)
        if "not found" in stderr.lower() or "does not exist" in stderr.lower():
            return _build_result(False, error="仓库不存在", code=REPO_NOT_FOUND)
        if "branch" in stderr.lower() and "not found" in stderr.lower():
            return _build_result(False, error=f"分支 {branch} 不存在", code=BRANCH_NOT_FOUND)
        if "permission denied" in stderr.lower() or "auth" in stderr.lower():
            return _build_result(False, error="认证失败或权限不足", code=SSH_AUTH_FAILED)
        return _build_result(False, error=f"克隆失败：{_mask_url(stderr)}", code=NETWORK_ERROR)
    # clone 成功后清理含令牌的 remote URL（HTTPS 模式）
    if protocol == "https" and token:
        _git_run(["remote", "set-url", "origin", public_https], cwd=str(dest_path), timeout=10)
    duration = round(_time.time() - start, 2)
    return _build_result(True, data={
        "action": "clone", "dest": str(dest_path),
        "branch": branch, "duration": duration,
    })


def _handle_create_repo(kwargs: dict) -> dict:
    """create_repo：在 GitHub 上创建新仓库（默认私有）。"""
    name = kwargs.get("name")
    if not name:
        return _build_result(False, error="name 参数为空", code=PARAM_INVALID)
    description = kwargs.get("description")
    private = kwargs.get("private", DEFAULT_VISIBILITY)
    auto_init = kwargs.get("auto_init", False)
    body = {
        "name": name,
        "private": bool(private),
        "auto_init": bool(auto_init),
    }
    if description:
        body["description"] = description
    resp = _github_request("POST", "/user/repos", json_body=body)
    if not resp.get("ok"):
        return resp
    r = resp.get("json", {})
    return _build_result(True, data={
        "full_name": r.get("full_name"),
        "clone_url": r.get("clone_url"),
        "visibility": "private" if r.get("private") else "public",
        "html_url": r.get("html_url"),
    })


def _handle_push_project(kwargs: dict) -> dict:
    """push_project：将本地工作区提交并推送到 GitHub。"""
    path = kwargs.get("path")
    if not path:
        return _build_result(False, error="path 参数为空", code=PARAM_INVALID)
    message = kwargs.get("message")
    if not message:
        return _build_result(False, error="message 参数为空", code=PARAM_INVALID)
    remote = kwargs.get("remote")
    branch = kwargs.get("branch", DEFAULT_BRANCH)
    author_name = kwargs.get("author_name", "AgentCluster")
    author_email = kwargs.get("author_email", "bot@agentcluster")
    if not _check_git_available():
        return _build_result(False, error="本地未安装 Git，请先安装", code=GIT_NOT_FOUND)
    work_dir = Path(path).resolve()
    if not work_dir.exists() or not work_dir.is_dir():
        return _build_result(False, error=f"工作区路径不存在或非目录：{path}", code=PARAM_INVALID)
    cwd = str(work_dir)
    # 检测是否已初始化为 Git 仓库
    if not (work_dir / ".git").exists():
        init_resp = _git_run(["init"], cwd=cwd, timeout=15)
        if not init_resp.get("ok"):
            return _build_result(False, error=f"git init 失败：{init_resp.get('stderr', '')}", code=PUSH_FAILED)
    # add -A
    add_resp = _git_run(["add", "-A"], cwd=cwd, timeout=30)
    if not add_resp.get("ok"):
        return _build_result(False, error=f"git add 失败：{add_resp.get('stderr', '')}", code=PUSH_FAILED)
    # 检测是否有暂存变更
    status_resp = _git_run(["status", "--porcelain"], cwd=cwd, timeout=15)
    if status_resp.get("ok") and not status_resp["stdout"].strip():
        return _build_result(True, data={"pushed": False, "message": "无变更需要提交"})
    # commit（用 -c 临时设置提交身份，不污染全局配置）
    commit_resp = _git_run(
        ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}",
         "commit", "-m", message],
        cwd=cwd, timeout=30,
    )
    if not commit_resp.get("ok"):
        stderr = commit_resp.get("stderr", "")
        if "nothing to commit" in stderr:
            return _build_result(True, data={"pushed": False, "message": "无变更需要提交"})
        return _build_result(False, error=f"git commit 失败：{stderr}", code=PUSH_FAILED)
    committed = True
    # 处理 remote
    remote_resp = _git_run(["remote", "get-url", "origin"], cwd=cwd, timeout=10)
    has_origin = remote_resp.get("ok")
    if not has_origin and not remote:
        return _build_result(False, error="未关联远程仓库且未提供 remote 参数", code=NO_REMOTE, committed=committed)
    if remote:
        # 设置/更新 remote URL（脱敏处理含令牌的 URL）
        clean_remote = _mask_url(remote) if "://" in remote and "@" in remote else remote
        if has_origin:
            _git_run(["remote", "set-url", "origin", remote], cwd=cwd, timeout=10)
        else:
            _git_run(["remote", "add", "origin", remote], cwd=cwd, timeout=10)
    # push
    push_resp = _git_run(["push", "-u", "origin", branch], cwd=cwd, timeout=GITHUB_PUSH_TIMEOUT)
    if not push_resp.get("ok"):
        stderr = push_resp.get("stderr", "")
        code = push_resp.get("code")
        if code == CLONE_TIMEOUT:
            return _build_result(False, error="推送超时", code=CLONE_TIMEOUT, committed=committed)
        if "rejected" in stderr.lower():
            return _build_result(False, error=f"推送被拒绝（可能有冲突）：{_mask_url(stderr)}",
                                 code=PUSH_REJECTED, committed=committed)
        if "permission denied" in stderr.lower() or "403" in stderr:
            return _build_result(False, error="权限不足，推送失败", code=AUTH_FORBIDDEN, committed=committed)
        if "could not resolve" in stderr.lower() or "network" in stderr.lower():
            return _build_result(False, error="网络异常，推送失败", code=NETWORK_ERROR, committed=committed)
        return _build_result(False, error=f"推送失败：{_mask_url(stderr)}", code=PUSH_FAILED, committed=committed)
    # 获取 commit sha
    sha_resp = _git_run(["rev-parse", "HEAD"], cwd=cwd, timeout=10)
    commit_sha = sha_resp["stdout"].strip() if sha_resp.get("ok") else None
    # 获取 remote URL（脱敏）
    final_remote_resp = _git_run(["remote", "get-url", "origin"], cwd=cwd, timeout=10)
    final_remote = _mask_url(final_remote_resp["stdout"].strip()) if final_remote_resp.get("ok") else None
    return _build_result(True, data={
        "commit_sha": commit_sha,
        "branch": branch,
        "remote": final_remote,
        "pushed": True,
    })


# ==============================================================
# handlers 字典（action → 处理函数）
# ==============================================================
handlers = {
    "set_token": _handle_set_token,
    "check_auth": _handle_check_auth,
    "search_repos": _handle_search_repos,
    "get_repo_info": _handle_get_repo_info,
    "read_readme": _handle_read_readme,
    "list_files": _handle_list_files,
    "read_file": _handle_read_file,
    "clone_repo": _handle_clone_repo,
    "push_project": _handle_push_project,
    "create_repo": _handle_create_repo,
    "get_rate_limit": _handle_get_rate_limit,
}


# ==============================================================
# run() —— 插件入口
# ==============================================================
def run(**kwargs) -> dict:
    """供 LLM 调用的主入口。

    Args:
        **kwargs: 由 tool.yaml 的 parameters 定义，必须含 action 字段。

    Returns:
        {"ok": bool, "data": any, "error": str?, "code": str?}
    """
    action = kwargs.get("action", "")
    # 预应用令牌：若 kwargs 携带 token 且非 set_token action，自动写入模块级状态，
    # 使能力参数预设的 token 对所有 action 生效（无需显式调用 set_token）。
    if action != "set_token":
        _preset_token = kwargs.get("token")
        if isinstance(_preset_token, str) and _preset_token.strip():
            _preset_token = _preset_token.strip()
            if _preset_token != _get_token():
                _set_token_internal(_preset_token)
    handler = handlers.get(action)
    if not handler:
        return _build_result(
            False,
            error=f"未知 action: {action}，可用：{list(handlers.keys())}",
            code=PARAM_INVALID,
        )
    try:
        return handler(kwargs)
    except Exception as e:
        # 异常兜底，错误信息不含令牌原文
        msg = str(e)
        token = _get_token()
        if token and token in msg:
            msg = msg.replace(token, _mask_token(token))
        return _build_result(False, error=f"内部错误：{msg}", code=PARAM_INVALID)