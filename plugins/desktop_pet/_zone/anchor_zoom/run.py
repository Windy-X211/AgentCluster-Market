"""启动 anchor-zoom 测试窗口。

把 demo 目录里的 index.html / main.js 用 Electron 拉起来，
提供一个可双击运行的入口，方便随时测"按锚点缩放"两种模式。
"""
import os
import sys
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 两个 Electron 路径：优先用桌宠自己打包的那个（和桌宠环境一致）
_CANDIDATES = [
    os.path.join(HERE, "..", "electron_runtime", "Hime Display.exe"),
    r"F:\PythonProjects\TraeProjects\智能体集群\frontend\node_modules\electron\dist\electron.exe",
]

def find_electron():
    for p in _CANDIDATES:
        if os.path.isfile(p):
            return p
    return None

def main():
    exe = find_electron()
    if not exe:
        print("[anchor-zoom] 没找到 Electron.exe，先跑一下："
              " npx --yes electron@latest 装个到当前目录")
        sys.exit(1)

    ud = os.path.expandvars(r"C:\Users\yangq\AppData\Roaming\anchor-zoom-demo")
    args = [exe, "--disable-gpu", "--no-sandbox", ".",
            "--user-data-dir=" + ud]

    # 用 Popen + DETACHED_PROCESS 让窗口进程独立于这个 Python 进程，
    # 关掉 Python 也不关窗口。
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        args, cwd=HERE,
        creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    if proc.poll() is not None:
        print(f"[anchor-zoom] 启动失败，退出码={proc.returncode}")
        sys.exit(2)
    print(f"[anchor-zoom] 启动成功 pid={proc.pid}")

if __name__ == "__main__":
    main()
