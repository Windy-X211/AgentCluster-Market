"""smtp_utils.py — SMTP 健壮连接与发送工具（配置 GUI 与发送 CLI 共用）

针对 SMTPServerDisconnected: Connection unexpectedly closed 等问题：
自动按端口推断并依次尝试 ssl / starttls / plain 多种加密组合，
避免"465 配了 STARTTLS、587 配了 SSL"这类配置不匹配导致的断连。
同时把底层异常翻译成中文友好提示，便于 GUI 弹窗和 CLI 输出。
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---- 常见异常的友好提示 ----------------------------------------------------
USER_HINT = {
    "SMTPServerDisconnected": "服务器主动断开连接，通常是端口与加密方式不匹配"
                              "（465 应选 SSL，587 应选 STARTTLS），已自动尝试其他组合",
    "SMTPAuthenticationError": "认证失败：请确认使用的是邮箱 SMTP 授权码，而非登录密码",
    "socket.timeout": "连接超时：请检查网络，或防火墙/公司网关是否拦截该端口",
    "TimeoutError": "连接超时：请检查网络，或防火墙/公司网关是否拦截该端口",
    "ConnectionRefusedError": "连接被拒绝：请确认 SMTP 服务器地址与端口是否正确，"
                              "或该邮箱未开启 SMTP 服务",
    "SSLError": "SSL 握手失败：端口与加密方式可能不匹配，已自动尝试其他组合",
    "gaierror": "无法解析服务器地址：请检查 SMTP 服务器域名是否正确",
    "OSError": "网络错误：连接被中断或重置，请检查网络与端口",
}


def _hint(exc: Exception) -> str:
    """把异常转成用户可读的中文提示（未知类型则返回原始信息）。"""
    name = type(exc).__name__
    if name in USER_HINT:
        return USER_HINT[name]
    return str(exc) or name


def _candidate_modes(port, pref: str):
    """生成有序的 (port, mode) 尝试列表：配置倾向优先，端口推断补充。"""
    port = int(port)
    seq = []

    def _add(p, m):
        t = (int(p), m)
        if t not in seq:
            seq.append(t)

    if pref == "ssl":
        _add(port, "ssl")
    elif pref == "starttls":
        _add(port, "starttls")
    elif pref == "none":
        _add(port, "none")

    # 按端口补全常见组合（465→ssl 优先；587→starttls 优先；25→starttls/明文）
    if port == 465:
        _add(465, "ssl")
        _add(465, "starttls")
    elif port == 587:
        _add(587, "starttls")
        _add(587, "none")
        _add(587, "ssl")
    elif port == 25:
        _add(25, "starttls")
        _add(25, "none")
    else:
        _add(port, "ssl")
        _add(port, "starttls")

    return seq


def connect_login(host: str, port, user: str, password: str,
                  pref: str = "ssl", timeout: int = 20):
    """依次尝试多种加密组合建立连接并登录。

    成功返回 (server, mode)；全部失败抛出带中文提示的 RuntimeError
    （内含各组合的失败摘要，方便定位）。
    """
    errors = []
    for p, mode in _candidate_modes(port, pref):
        server = None
        try:
            if mode == "ssl":
                server = smtplib.SMTP_SSL(host, p, timeout=timeout)
            else:
                server = smtplib.SMTP(host, p, timeout=timeout)
                if mode == "starttls":
                    server.starttls()
            server.login(user, password)
            return server, mode
        except Exception as e:
            errors.append((p, mode, e))
            if server is not None:
                try:
                    server.close()
                except Exception:
                    pass

    last = errors[-1][2]
    detail = "；".join(f"{p}/{m}: {_hint(e)}" for p, m, e in errors[-3:])
    raise RuntimeError(
        f"SMTP 连接失败（已尝试 {len(errors)} 种加密组合，"
        f"最后一次：{_hint(last)}）。{detail}"
    )


def send_mail(host: str, port, user: str, password: str, to_addr: str,
              subject: str, body: str, nick: str = "", pref: str = "ssl",
              html: bool = False, timeout: int = 20):
    """完整发送一封邮件（自动重试多种加密组合）。

    成功返回 (True, 成功消息)；失败返回 (False, 中文错误消息)。不抛异常。
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{nick} <{user}>" if nick else user
    msg["To"] = to_addr
    msg["Subject"] = subject
    if html:
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    server = None
    try:
        server, mode = connect_login(host, port, user, password, pref, timeout)
        server.sendmail(user, [to_addr], msg.as_string())
        return True, f"邮件发送成功（采用 {mode} 加密）"
    except Exception as e:
        return False, f"{type(e).__name__}: {_hint(e)}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass