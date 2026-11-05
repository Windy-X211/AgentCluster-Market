"""MCP 服务端 —— stdio 启动入口。

供外部 MCP 客户端（Claude Desktop / Cursor / Codex 等）以子进程方式启动：

    {
      "mcpServers": {
        "agentcluster": {
          "command": "python",
          "args": ["<...>/AppData/extensions/mcp_server/mcp_stdio.py"]
        }
      }
    }

stdio 模式在本机进程内直接读写 AppData，不经过 HTTP，也不需要 Token。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def _find_backend_root() -> Path | None:
    for p in [_DIR, *list(_DIR.parents)]:
        cand = p / "backend"
        if (cand / "main.py").exists():
            return cand
    return None


def _mute_stdout() -> int:
    """把 stdout 重定向到 stderr，返回真实 stdout 的 fd 副本。

    MCP stdio 传输独占 fd 1（JSON-RPC 帧）；business 代码/依赖的 print 若落到
    stdout 会污染协议帧（MCP 客户端报 "Failed to parse JSONRPC message"）。
    因此启动早期先把 fd 1 指向 stderr，等真正 run() 前再恢复给 SDK。
    """
    real_fd = os.dup(1)
    try:
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    except OSError:
        pass
    return real_fd


def _restore_stdout(real_fd: int) -> None:
    """恢复 fd 1 为真实 stdout，并让 sys.stdout 直接以 fd 1 为底层（SDK 据此接管）。"""
    try:
        os.dup2(real_fd, 1)
    except OSError:
        return
    try:
        sys.stdout = os.fdopen(1, "w", encoding="utf-8", buffering=1, closefd=False)
    except OSError:
        pass


def main() -> int:
    # 关键：业务代码/依赖的 stdout 输出会污染 JSON-RPC 帧，先全部导流到 stderr
    real_stdout_fd = _mute_stdout()
    backend_root = _find_backend_root()
    if backend_root is None:
        print("[mcp_stdio] 找不到 backend 目录，无法启动", file=sys.stderr)
        return 1
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    vendor = backend_root / "mcp_vendor"
    if vendor.exists() and str(vendor) not in sys.path:
        sys.path.append(str(vendor))

    # 初始化数据仓库（与主后端一致：AppData 根目录）
    try:
        from core.store import init_store
        init_store()
    except Exception as e:
        print(f"[mcp_stdio] 初始化数据仓库失败：{e}", file=sys.stderr)

    # 复用拓展 backend.py 中已注册的工具
    try:
        spec = importlib.util.spec_from_file_location("_mcp_server_backend_stdio", _DIR / "backend.py")
        if spec is None or spec.loader is None:
            print("[mcp_stdio] 无法加载 backend.py", file=sys.stderr)
            return 1
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[mcp_stdio] 加载 backend.py 失败：{e}", file=sys.stderr)
        return 1

    server = getattr(mod, "_mcp", None)
    if server is None:
        print(f"[mcp_stdio] MCP SDK 不可用：{getattr(mod, '_MCP_ERROR', '')}", file=sys.stderr)
        return 1
    # 把 fd 1 交还给 SDK（SDK 会再把它重定向到 stderr，仅用它承载协议帧）
    _restore_stdout(real_stdout_fd)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())