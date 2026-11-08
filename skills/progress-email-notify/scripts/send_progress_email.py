"""send_progress_email.py — 进度邮件发送脚本（CLI）

由智能体在长任务执行过程中调用，读取用户级目录
%LOCALAPPDATA%/AgentCluster/userdata/email_config.json 的 SMTP 配置，
把进度邮件发送到配置的收件邮箱，输出 JSON 结果便于智能体判断成败。

用法：
    python send_progress_email.py --subject "标题" --body "正文"
    python send_progress_email.py --subject "标题" --body-file path/to/snapshot.txt

参数：
    --subject   邮件主题（必填）
    --body      邮件正文（与 --body-file 二选一）
    --body-file 正文文件路径（读取后作为正文）
    --to        可选：覆盖配置中的收件邮箱
    --html      可选：正文是否按 HTML 发送（默认纯文本）

输出（stdout 一行 JSON）：
    成功： {"ok": true, "message": "...", "to": "..."}
    失败： {"ok": false, "error": "..."}
"""
import argparse
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import smtp_utils as _smtp

# ---- 配置文件路径（用户级目录，不在技能文件夹内，防止分享技能时泄露）-----
# 固定读 %LOCALAPPDATA%\AgentCluster\userdata\email_config.json；绝不回退到技能目录。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "AgentCluster", "userdata"
)
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(CONFIG_DIR, "email_config.json")


def _emit(obj: dict):
    """输出一行 JSON 给调用方（智能体）。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            "邮箱配置不存在。请先运行 email_config_gui.py 完成首次配置。"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def send(cfg: dict, subject: str, body: str, to_override: str = "",
         html: bool = False) -> dict:
    """发送一封邮件。成功返回结果 dict，失败抛异常。"""
    host = cfg["host"]
    port = int(cfg["port"])
    ssl_mode = cfg.get("ssl", "ssl")
    user = cfg["user"]
    password = cfg["password"]
    nick = cfg.get("nick", "")
    to_addr = (to_override or cfg.get("to") or "").strip()
    if not to_addr:
        raise ValueError("收件邮箱为空，请在配置中填写或用 --to 指定。")

    # 交给 smtp_utils：自动按端口尝试 ssl/starttls/plain，规避端口与加密不匹配导致的断连
    ok, info = _smtp.send_mail(
        host=host, port=port, user=user, password=password, to_addr=to_addr,
        subject=subject, body=body, nick=nick, pref=ssl_mode, html=html,
    )
    if ok:
        return {"ok": True, "message": info, "to": to_addr}
    raise RuntimeError(info)


def main():
    parser = argparse.ArgumentParser(description="发送进度邮件")
    parser.add_argument("--subject", required=True, help="邮件主题")
    parser.add_argument("--body", default=None, help="邮件正文（纯文本）")
    parser.add_argument("--body-file", default=None, help="正文文件路径")
    parser.add_argument("--to", default="", help="覆盖收件邮箱")
    parser.add_argument("--html", action="store_true", help="正文按 HTML 发送")
    args = parser.parse_args()

    # 正文来源
    if args.body_file:
        if not os.path.exists(args.body_file):
            _emit({"ok": False, "error": f"正文文件不存在：{args.body_file}"})
            return
        with open(args.body_file, "r", encoding="utf-8") as f:
            body = f.read()
    elif args.body is not None:
        body = args.body
    else:
        _emit({"ok": False, "error": "必须提供 --body 或 --body-file"})
        return

    # 读取配置
    try:
        cfg = load_config()
    except Exception as e:
        _emit({"ok": False, "error": str(e)})
        return

    # 发送（最多重试 1 次）
    last_err = None
    for attempt in range(2):
        try:
            result = send(cfg, args.subject, body, to_override=args.to, html=args.html)
            _emit(result)
            return
        except Exception as e:
            last_err = e
            # 仅对网络类错误重试，认证错误不重试
            msg = str(e).lower()
            if "auth" in msg or "credential" in msg or "login" in msg:
                break

    _emit({"ok": False, "error": f"{type(last_err).__name__}: {last_err}"})


if __name__ == "__main__":
    main()
