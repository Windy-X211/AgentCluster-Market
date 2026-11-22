# coding=utf-8
"""打包/发布插件的"文件型依赖"资产。

为什么需要它：
  electron_runtime 有 282MB、单个 exe 190MB，**放不进市场 git 仓库**
  （GitHub 单文件 >100MiB 直接拒推）。这类东西必须打成 zip 挂到 GitHub Release，
  由 manifest 的 dependencies.assets 引用，宿主在安装时自动下载。

用法：
  # 只打包 + 打印 sha256（默认，不联网、安全）
  python tools/build_release.py

  # 打包后顺便上传到 Release（需要环境变量 GITHUB_TOKEN，仓库默认 AgentCluster-Market）
  python tools/build_release.py --upload --tag assets-v1

输出（供直接粘贴进 manifest.yaml）：
  {"id": "electron_runtime", "kind": "zip", "url": "...", "sha256": "...", "size": 123}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 插件根
DEFAULT_OUT = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AgentCluster" / "_release"

# 参与打包的资产：id -> (源目录, 打包内顶层目录名, 需要排除的名字)
BUNDLES = {
    "electron_runtime": {
        "src": ROOT / "electron_runtime",
        "top": "electron_runtime",
        "exclude": ["debug.log"],
        "note": "Electron 运行时（Hime Display.exe + Chromium 内核）",
    },
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(asset_id: str, out_dir: Path, level: int = 6) -> dict:
    spec = BUNDLES[asset_id]
    src: Path = spec["src"]
    if not src.is_dir():
        raise SystemExit(f"源目录不存在：{src}")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{asset_id}-win32-x64.zip"
    out = out_dir / name
    if out.exists():
        out.unlink()

    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"打包 {asset_id}: {len(files)} 个文件 / {total / 1048576:.1f} MB → {out}")
    done = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as zf:
        for p in files:
            rel = p.relative_to(src)
            if any(part in spec.get("exclude", []) for part in rel.parts):
                continue
            arc = Path(spec["top"]) / rel
            info = zipfile.ZipInfo(str(arc).replace("\\", "/"))
            info.compress_type = zipfile.ZIP_DEFLATED
            with p.open("rb") as rf:
                zf.writestr(info, rf.read())
            done += 1
            if done % 200 == 0:
                print(f"  … {done}/{len(files)}", flush=True)

    size = out.stat().st_size
    digest = sha256_of(out)
    print(f"完成：{out}  ({size / 1048576:.1f} MB, sha256={digest[:16]}…)")
    return {"id": asset_id, "kind": "zip", "file": str(out), "sha256": digest, "size": size}


def upload(asset: dict, tag: str, repo: str, token: str) -> str:
    """创建（或复用）Release，上传资产，返回浏览器下载地址。"""
    import urllib.request

    def api(url: str, method: str = "GET", data: bytes | None = None,
            ctype: str = "application/json") -> dict:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode() or "{}")

    release = api(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    if not release.get("id"):
        release = api(f"https://api.github.com/repos/{repo}/releases", "POST",
                      json.dumps({"tag_name": tag, "name": f"Assets {tag}",
                                  "body": "插件文件型依赖资产（由 build_release.py 上传）",
                                  "draft": False, "prerelease": False}).encode())
    upload_url = str(release["upload_url"]).split("{")[0]
    filename = Path(asset["file"]).name
    with open(asset["file"], "rb") as f:
        payload = f.read()
    print(f"上传 {filename} ({len(payload) / 1048576:.1f} MB) → {repo}@{tag}")
    res = api(f"{upload_url}?name={filename}", "POST", payload,
              ctype="application/zip")
    return str(res.get("browser_download_url") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="electron_runtime", choices=sorted(BUNDLES))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--level", type=int, default=6)
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--tag", default="assets-v1")
    ap.add_argument("--repo", default="Windy-X211/AgentCluster-Market")
    args = ap.parse_args()

    asset = build(args.id, Path(args.out), args.level)
    if args.upload:
        token = os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            print("缺少 GITHUB_TOKEN，跳过上传（包已生成）", file=sys.stderr)
        else:
            asset["url"] = upload(asset, args.tag, args.repo, token)
            print("下载地址:", asset["url"])

    asset.pop("file", None)
    print("\n# 粘贴进 manifest.yaml 的 dependencies.assets：")
    print(json.dumps(asset, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
