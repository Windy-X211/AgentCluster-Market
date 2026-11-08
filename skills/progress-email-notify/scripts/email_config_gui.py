"""email_config_gui.py — 进度邮件通知技能的邮箱配置窗口

首次使用时由智能体运行，弹出 tkinter 窗口让用户填写 SMTP 配置；
支持"测试发送"验证可用性；保存后写入用户级目录
%LOCALAPPDATA%/AgentCluster/userdata/email_config.json（不在技能文件夹内，避免分享泄露），
之后 send_progress_email.py 会读取该配置发信，不再弹窗。

运行： python email_config_gui.py
"""
import json
import os
import smtplib
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from tkinter import (
    Tk, Label, Entry, Button, StringVar, OptionMenu, messagebox, Frame,
)

import smtp_utils as _smtp

# ---- 配置文件路径（用户级目录，不在技能文件夹内，防止分享技能时泄露）-----
# 固定存到 %LOCALAPPDATA%\AgentCluster\userdata\，目录不存在则自动创建；
# 绝不回退到技能目录 —— 技能目录会被整体分享，配置含授权码不能随之泄露。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "AgentCluster", "userdata"
)
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(CONFIG_DIR, "email_config.json")

# ---- 常用邮箱预设 ----------------------------------------------------------
# value 形如 "host|port|ssl_mode"，ssl_mode: ssl / starttls / none
PRESETS = {
    "QQ 邮箱":   "smtp.qq.com|465|ssl",
    "163 邮箱":  "smtp.163.com|465|ssl",
    "126 邮箱":  "smtp.126.com|465|ssl",
    "Gmail":     "smtp.gmail.com|587|starttls",
    "Outlook":   "smtp.office365.com|587|starttls",
    "自定义":     "||none",
}


def load_existing_config() -> dict:
    """若配置文件已存在则读取，否则返回空 dict。"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    """把配置写入 email_config.json（覆盖）。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def smtp_connect(host: str, port: int, user: str, password: str, ssl_mode: str):
    """根据 ssl_mode 建立连接并登录，返回已登录的 SMTP 对象。

    底层委托 smtp_utils：自动按端口尝试 ssl / starttls / plain 多种组合，
    规避端口与加密方式不匹配导致的 SMTPServerDisconnected。
    """
    server, _mode = _smtp.connect_login(host, port, user, password, pref=ssl_mode)
    return server


def send_test_mail(host: str, port: int, user: str, password: str,
                   to_addr: str, nick: str, ssl_mode: str) -> tuple:
    """发送一封测试邮件。成功返回 (True, '')，失败返回 (False, errmsg)。"""
    ok, info = _smtp.send_mail(
        host=host, port=port, user=user, password=password, to_addr=to_addr,
        subject="【测试】AgentCluster 进度邮件通知配置成功",
        body=(
            "这是一封测试邮件。\n\n"
            "如果你收到了这封邮件，说明你的 SMTP 邮箱配置已生效。\n"
            "后续智能体执行长任务时，会按你设定的节奏把进度推送到这个邮箱。\n\n"
            "—— AgentCluster 进度邮件通知技能"
        ),
        nick=nick, pref=ssl_mode,
    )
    if ok:
        return True, "测试邮件发送成功，请去收件箱确认。"
    return False, f"发送失败：{info}"


class ConfigGUI:
    """邮箱配置窗口。"""

    def __init__(self, root: Tk):
        self.root = root
        self.existing = load_existing_config()

        root.title("AgentCluster · 邮箱配置")
        root.geometry("460x420")
        root.resizable(False, False)

        row = 0
        # 预设
        Label(root, text="常用邮箱预设：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.preset_var = StringVar(value="QQ 邮箱")
        self.preset_menu = OptionMenu(root, self.preset_var, *PRESETS.keys(), command=self.on_preset_change)
        self.preset_menu.grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # SMTP 服务器
        Label(root, text="SMTP 服务器：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.host_var = StringVar()
        Entry(root, textvariable=self.host_var, width=28).grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # 端口
        Label(root, text="端口：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.port_var = StringVar()
        Entry(root, textvariable=self.port_var, width=10).grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # 加密方式
        Label(root, text="加密方式：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.ssl_var = StringVar(value="ssl")
        OptionMenu(root, self.ssl_var, "ssl", "starttls", "none").grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # 发件邮箱
        Label(root, text="发件邮箱：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.user_var = StringVar()
        Entry(root, textvariable=self.user_var, width=28).grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # 授权码
        Label(root, text="授权码/密码：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.pass_var = StringVar()
        Entry(root, textvariable=self.pass_var, width=28, show="*").grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # 发件昵称
        Label(root, text="发件昵称(可选)：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.nick_var = StringVar()
        Entry(root, textvariable=self.nick_var, width=28).grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # 收件邮箱
        Label(root, text="收件邮箱：").grid(row=row, column=0, sticky="e", padx=8, pady=6)
        self.to_var = StringVar()
        Entry(root, textvariable=self.to_var, width=28).grid(row=row, column=1, sticky="w", padx=8, pady=6)
        row += 1

        # 按钮
        btn_frame = Frame(root)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=14)
        Button(btn_frame, text="测试发送", width=12, command=self.on_test).pack(side="left", padx=6)
        Button(btn_frame, text="保存并退出", width=12, command=self.on_save).pack(side="left", padx=6)
        Button(btn_frame, text="退出", width=8, command=root.destroy).pack(side="left", padx=6)
        row += 1

        # 状态提示
        self.status_var = StringVar(value="")
        Label(root, textvariable=self.status_var, fg="#888").grid(row=row, column=0, columnspan=2)

        # 如有旧配置，回填字段（密码留空，需用户重填）
        if self.existing:
            self._fill_from(self.existing, keep_pass=False)
            self.status_var.set("已读取旧配置（授权码需重新填写后保存）。")

        # 触发一次预设填充
        self.on_preset_change(self.preset_var.get())

    # ---- 预设联动 ----------------------------------------------------------
    def on_preset_change(self, name: str):
        val = PRESETS.get(name, "||none")
        host, port, ssl_mode = val.split("|")
        # 只在用户尚未手动改动、或选"自定义"以外时覆盖
        if host:
            self.host_var.set(host)
        if port:
            self.port_var.set(port)
        if ssl_mode and ssl_mode != "none":
            self.ssl_var.set(ssl_mode)

    def _fill_from(self, cfg: dict, keep_pass: bool = False):
        self.host_var.set(cfg.get("host", ""))
        self.port_var.set(str(cfg.get("port", "")))
        self.ssl_var.set(cfg.get("ssl", "ssl"))
        self.user_var.set(cfg.get("user", ""))
        if keep_pass:
            self.pass_var.set(cfg.get("password", ""))
        self.nick_var.set(cfg.get("nick", ""))
        self.to_var.set(cfg.get("to", ""))

    def _collect(self) -> dict:
        return {
            "host": self.host_var.get().strip(),
            "port": self.port_var.get().strip(),
            "ssl": self.ssl_var.get().strip() or "ssl",
            "user": self.user_var.get().strip(),
            "password": self.pass_var.get().strip(),
            "nick": self.nick_var.get().strip(),
            "to": self.to_var.get().strip(),
        }

    def _validate(self, cfg: dict, need_pass: bool = True) -> str:
        """返回空串表示校验通过；否则返回错误描述。"""
        if not cfg["host"]:
            return "请填写 SMTP 服务器。"
        if not cfg["port"] or not cfg["port"].isdigit():
            return "端口需为数字。"
        if not cfg["user"]:
            return "请填写发件邮箱。"
        if need_pass and not cfg["password"]:
            return "请填写授权码/密码。"
        if not cfg["to"]:
            return "请填写收件邮箱。"
        return ""

    # ---- 按钮 --------------------------------------------------------------
    def on_test(self):
        cfg = self._collect()
        err = self._validate(cfg, need_pass=True)
        if err:
            messagebox.showwarning("提示", err)
            return
        self.status_var.set("正在发送测试邮件…")
        self.root.update()

        # 放线程里跑，避免阻塞 UI
        def worker():
            ok, msg = send_test_mail(
                cfg["host"], cfg["port"], cfg["user"], cfg["password"],
                cfg["to"], cfg["nick"], cfg["ssl"],
            )
            self.root.after(0, lambda: self._after_test(ok, msg))

        threading.Thread(target=worker, daemon=True).start()

    def _after_test(self, ok: bool, msg: str):
        if ok:
            self.status_var.set("✅ " + msg)
            messagebox.showinfo("成功", msg)
        else:
            self.status_var.set("❌ " + msg)
            messagebox.showerror("失败", msg)

    def on_save(self):
        cfg = self._collect()
        # 若用户没填密码但已有旧密码，沿用旧密码
        if not cfg["password"] and self.existing.get("password"):
            cfg["password"] = self.existing["password"]
        err = self._validate(cfg, need_pass=True)
        if err:
            messagebox.showwarning("提示", err)
            return
        try:
            save_config(cfg)
            self.status_var.set("✅ 配置已保存。")
            messagebox.showinfo("成功", f"配置已保存到：\n{CONFIG_PATH}\n\n后续进度邮件将使用此配置发送。")
            self.root.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))


def main():
    root = Tk()
    ConfigGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
