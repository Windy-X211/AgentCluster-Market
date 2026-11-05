"""send_email 插件 — 邮箱全功能管理（发送/读取/清理/整理/监听）

action 分发：send / list / read / search / folders / delete / clean / move / mark / organize
             / listen_wait
"""
import base64
import imaplib
import json
import os
import re
import smtplib
import threading
import time
import traceback
from email import encoders, message_from_bytes
from email.header import decode_header, make_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses
from typing import Any

# ===== 预设（SMTP + IMAP）=====
SMTP_PRESETS: dict[str, dict[str, Any]] = {
    "qq":     {"smtp_server": "smtp.qq.com",         "smtp_port": 465, "use_ssl": True,  "imap_server": "imap.qq.com",         "imap_port": 993},
    "163":    {"smtp_server": "smtp.163.com",        "smtp_port": 465, "use_ssl": True,  "imap_server": "imap.163.com",        "imap_port": 993},
    "gmail":  {"smtp_server": "smtp.gmail.com",      "smtp_port": 465, "use_ssl": True,  "imap_server": "imap.gmail.com",      "imap_port": 993},
    "outlook":{"smtp_server": "smtp.office365.com",  "smtp_port": 587, "use_ssl": False, "imap_server": "outlook.office365.com","imap_port": 993},
    "exmail": {"smtp_server": "smtp.exmail.qq.com",  "smtp_port": 465, "use_ssl": True,  "imap_server": "imap.exmail.qq.com",  "imap_port": 993},
}

TRASH_CANDIDATES = ["已删除", "Trash", "Deleted", "Deleted Items", "已删除邮件"]
FLAG_MAP = {
    "seen":      ("+FLAGS", "\\Seen"),
    "unseen":    ("-FLAGS", "\\Seen"),
    "flagged":   ("+FLAGS", "\\Flagged"),
    "unflagged": ("-FLAGS", "\\Flagged"),
    "deleted":   ("+FLAGS", "\\Deleted"),
}


# ===== modified UTF-7 编解码（RFC 3501 5.1.3，用于中文文件夹名）=====
def _utf7_encode(s: str) -> str:
    result: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "&":
            result.append("&-")
            i += 1
        elif 0x20 <= ord(ch) <= 0x7E:
            result.append(ch)
            i += 1
        else:
            j = i
            while j < n and s[j] != "&" and not (0x20 <= ord(s[j]) <= 0x7E):
                j += 1
            seg = s[i:j].encode("utf-16-be")
            b64 = base64.b64encode(seg).decode("ascii").rstrip("=")
            result.append("&" + b64 + "-")
            i = j
    return "".join(result)


def _utf7_decode(s: str) -> str:
    result: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "&":
            if i + 1 < n and s[i + 1] == "-":
                result.append("&")
                i += 2
            else:
                j = i + 1
                while j < n and s[j] != "-":
                    j += 1
                b64 = s[i + 1:j]
                pad = (4 - len(b64) % 4) % 4
                seg = base64.b64decode(b64 + "=" * pad)
                result.append(seg.decode("utf-16-be"))
                i = j + 1
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


# ===== 邮件头解析 =====
def _decode_hdr(raw: Any) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except Exception:
        return str(raw)


def _parse_addrs(raw: Any) -> list[dict]:
    if not raw:
        return []
    try:
        pairs = getaddresses([str(raw)])
    except Exception:
        return []
    return [{"name": _decode_hdr(n), "addr": a} for n, a in pairs if a]


def _parse_flags(raw: Any) -> list[str]:
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="replace")
    m = re.search(r"FLAGS \(([^)]*)\)", raw)
    return m.group(1).split() if m else []


# ===== IMAP 辅助 =====
def _resolve_smtp(kw: dict) -> tuple[str, int, bool]:
    provider = kw.get("provider", "")
    smtp_server = kw.get("smtp_server", "") or ""
    smtp_port = kw.get("smtp_port")
    use_ssl = kw.get("use_ssl")
    if provider and provider in SMTP_PRESETS:
        p = SMTP_PRESETS[provider]
        if not smtp_server:
            smtp_server = p["smtp_server"]
        if not smtp_port:
            smtp_port = p["smtp_port"]
        if use_ssl is None:
            use_ssl = p["use_ssl"]
    smtp_port = int(smtp_port or 465)
    if use_ssl is None:
        use_ssl = True
    return smtp_server, smtp_port, bool(use_ssl)


def _resolve_imap(kw: dict) -> tuple[str, int]:
    provider = kw.get("provider", "")
    imap_server = kw.get("imap_server", "") or ""
    imap_port = kw.get("imap_port")
    if provider and provider in SMTP_PRESETS:
        p = SMTP_PRESETS[provider]
        if not imap_server:
            imap_server = p["imap_server"]
        if not imap_port:
            imap_port = p["imap_port"]
    imap_port = int(imap_port or 993)
    return imap_server, imap_port


def _check_imap_params(kw: dict) -> dict | None:
    imap_server, _ = _resolve_imap(kw)
    if not imap_server:
        return {"ok": False, "error": "请指定 provider 或 imap_server", "code": "PARAM_MISSING"}
    if not kw.get("username") or not kw.get("password"):
        return {"ok": False, "error": "username 和 password（授权码）必填", "code": "AUTH_REQUIRED"}
    return None


def _imap_connect(imap_server: str, imap_port: int, username: str, password: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(imap_server, imap_port, timeout=30)
    conn.login(username, password)
    return conn


def _select_folder(conn: imaplib.IMAP4_SSL, folder: str):
    return conn.select(_utf7_encode(folder).encode("ascii"))


def _find_folder(conn: imaplib.IMAP4_SSL, candidates: list[str]) -> str | None:
    for name in candidates:
        try:
            typ, _ = conn.select(_utf7_encode(name).encode("ascii"), readonly=True)
            if typ == "OK":
                return name
        except Exception:
            continue
    return None


def _imap_move(conn: imaplib.IMAP4_SSL, uid: str, target_folder: str) -> bool:
    encoded = _utf7_encode(target_folder).encode("ascii")
    try:
        typ, _ = conn.uid("MOVE", uid, encoded)
        if typ == "OK":
            return True
    except Exception:
        pass
    typ, _ = conn.uid("COPY", uid, encoded)
    if typ != "OK":
        return False
    conn.uid("STORE", uid, "+FLAGS", "\\Deleted")
    conn.expunge()
    return True


def _parse_mail(raw_bytes: bytes, include_body: bool = False, download_dir: str | None = None) -> dict:
    msg = message_from_bytes(raw_bytes)
    result: dict[str, Any] = {
        "subject": _decode_hdr(msg.get("Subject", "")),
        "from": _parse_addrs(msg.get("From", "")),
        "to": _parse_addrs(msg.get("To", "")),
        "cc": _parse_addrs(msg.get("Cc", "")),
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
    }
    if include_body:
        text_body = ""
        html_body = ""
        attachments = []
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = part.get_content_disposition()
            filename = part.get_filename()
            if disp == "attachment" or (filename and ctype not in ("text/plain", "text/html")):
                fname = _decode_hdr(filename or "unknown")
                payload = part.get_payload(decode=True) or b""
                att: dict[str, Any] = {"filename": fname, "size": len(payload)}
                if download_dir:
                    os.makedirs(download_dir, exist_ok=True)
                    fpath = os.path.join(download_dir, fname)
                    with open(fpath, "wb") as f:
                        f.write(payload)
                    att["saved_to"] = fpath
                attachments.append(att)
            elif ctype == "text/plain" and not text_body:
                charset = part.get_content_charset() or "utf-8"
                text_body = (part.get_payload(decode=True) or b"").decode(charset, errors="replace")
            elif ctype == "text/html" and not html_body:
                charset = part.get_content_charset() or "utf-8"
                html_body = (part.get_payload(decode=True) or b"").decode(charset, errors="replace")
        result["body"] = text_body
        result["html"] = html_body
        result["attachments"] = attachments
    return result


def _fetch_headers(conn: imaplib.IMAP4_SSL, uid: bytes) -> dict:
    uid_s = uid.decode()
    mail: dict[str, Any] = {"uid": uid_s}
    typ, d = conn.uid("fetch", uid, "(RFC822.HEADER)")
    if typ == "OK" and d and d[0]:
        raw = d[0][1] if isinstance(d[0], tuple) else b""
        if raw:
            parsed = _parse_mail(raw, include_body=False)
            mail.update(parsed)
    typ2, d2 = conn.uid("fetch", uid, "(FLAGS)")
    if typ2 == "OK" and d2 and d2[0]:
        raw2 = d2[0] if isinstance(d2[0], bytes) else (d2[0][0] if isinstance(d2[0], tuple) else b"")
        flags = _parse_flags(raw2)
        mail["seen"] = "\\Seen" in flags
        mail["flagged"] = "\\Flagged" in flags
    return mail


# ===== action handlers =====
def _handle_send(kw: dict) -> dict:
    smtp_server, smtp_port, use_ssl = _resolve_smtp(kw)
    if not smtp_server:
        return {"ok": False, "error": "请指定 provider 或 smtp_server", "code": "PARAM_MISSING"}
    username = kw.get("username", "") or ""
    password = kw.get("password", "") or ""
    if not username or not password:
        return {"ok": False, "error": "username 和 password（授权码）必填", "code": "AUTH_REQUIRED"}
    to_addrs = kw.get("to_addrs", "") or ""
    tos = [t.strip() for t in to_addrs.replace("；", ",").split(",") if t.strip()]
    if not tos:
        return {"ok": False, "error": "收件人 to_addrs 为空", "code": "PARAM_MISSING"}
    ccs = [t.strip() for t in (kw.get("cc", "") or "").replace("；", ",").split(",") if t.strip()]
    bccs = [t.strip() for t in (kw.get("bcc", "") or "").replace("；", ",").split(",") if t.strip()]
    subject = kw.get("subject", "") or "(无主题)"
    body = kw.get("body", "") or ""
    html = kw.get("html", "") or ""
    from_addr = kw.get("from_addr", "") or username
    attachments = [a.strip() for a in (kw.get("attachments", "") or "").split(",") if a.strip()]

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(tos)
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    msg["Subject"] = subject

    if html and body:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)
    elif html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    for fpath in attachments:
        if not os.path.isfile(fpath):
            return {"ok": False, "error": f"附件不存在: {fpath}", "code": "FILE_NOT_FOUND"}
        with open(fpath, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=os.path.basename(fpath))
        msg.attach(part)

    try:
        if use_ssl:
            client = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        else:
            client = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            client.starttls()
        client.login(username, password)
        client.sendmail(from_addr, tos + ccs + bccs, msg.as_string())
        client.quit()
        return {"ok": True, "data": {"sent_to": tos, "cc": ccs, "bcc": bccs}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "SMTP_ERROR"}


def _handle_list(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"
    limit = int(kw.get("limit", 20) or 20)
    offset = int(kw.get("offset", 0) or 0)
    unseen_only = bool(kw.get("unseen_only", False))

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        _select_folder(conn, folder)
        criteria = ["UNSEEN"] if unseen_only else ["ALL"]
        typ, data = conn.uid("search", None, *criteria)
        if typ != "OK":
            conn.logout()
            return {"ok": False, "error": "搜索失败", "code": "IMAP_ERROR"}
        uids = data[0].split() if data and data[0] else []
        uids.reverse()
        total = len(uids)
        page = uids[offset:offset + limit]
        mails = [_fetch_headers(conn, uid) for uid in page]
        conn.logout()
        return {"ok": True, "data": {"total": total, "folder": folder, "mails": mails}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_read(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    mail_id = kw.get("mail_id", "") or ""
    if not mail_id:
        return {"ok": False, "error": "mail_id 必填", "code": "PARAM_MISSING"}
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"
    download_dir = kw.get("download_dir", "") or ""
    mark_as_read = kw.get("mark_as_read", True)

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        _select_folder(conn, folder)
        typ, data = conn.uid("fetch", mail_id, "(RFC822)")
        if typ != "OK" or not data or not data[0]:
            conn.logout()
            return {"ok": False, "error": "邮件不存在", "code": "NOT_FOUND"}
        raw = data[0][1] if isinstance(data[0], tuple) else b""
        mail = _parse_mail(raw, include_body=True, download_dir=download_dir or None)
        mail["uid"] = mail_id
        if mark_as_read:
            conn.uid("STORE", mail_id, "+FLAGS", "\\Seen")
        conn.logout()
        return {"ok": True, "data": mail}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_search(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"
    limit = int(kw.get("limit", 20) or 20)
    offset = int(kw.get("offset", 0) or 0)

    criteria: list[str] = []
    if kw.get("unseen_only"):
        criteria.append("UNSEEN")
    if kw.get("from_filter"):
        criteria.extend(["FROM", kw["from_filter"]])
    if kw.get("subject_filter"):
        criteria.extend(["SUBJECT", kw["subject_filter"]])
    if kw.get("since_date"):
        try:
            d = time.strptime(kw["since_date"], "%Y-%m-%d")
            criteria.extend(["SINCE", time.strftime("%d-%b-%Y", d)])
        except ValueError:
            pass
    if kw.get("before_date"):
        try:
            d = time.strptime(kw["before_date"], "%Y-%m-%d")
            criteria.extend(["BEFORE", time.strftime("%d-%b-%Y", d)])
        except ValueError:
            pass
    if not criteria:
        criteria = ["ALL"]

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        _select_folder(conn, folder)
        typ, data = conn.uid("search", None, *criteria)
        if typ != "OK":
            conn.logout()
            return {"ok": False, "error": "搜索失败", "code": "IMAP_ERROR"}
        uids = data[0].split() if data and data[0] else []
        uids.reverse()
        total = len(uids)
        page = uids[offset:offset + limit]
        mails = [_fetch_headers(conn, uid) for uid in page]
        conn.logout()
        return {"ok": True, "data": {"total": total, "folder": folder, "mails": mails, "criteria": criteria}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_folders(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        typ, data = conn.list()
        folders = []
        if typ == "OK":
            for item in data:
                if isinstance(item, bytes):
                    item = item.decode("ascii", errors="replace")
                m = re.search(r'"([^"]*)"\s*$', item)
                if m and m.group(1):
                    folders.append(_utf7_decode(m.group(1)))
        conn.logout()
        return {"ok": True, "data": {"folders": folders}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_delete(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    ids = _collect_ids(kw)
    if not ids:
        return {"ok": False, "error": "mail_id 或 mail_ids 必填", "code": "PARAM_MISSING"}
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"
    mode = kw.get("delete_mode", "soft") or "soft"
    target = kw.get("target_folder", "") or ""

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        if mode == "soft":
            trash = target or _find_folder(conn, TRASH_CANDIDATES)
            if not trash:
                conn.logout()
                return {"ok": False, "error": "找不到已删除文件夹，请传 target_folder 或用 delete_mode=hard", "code": "FOLDER_NOT_FOUND"}
            _select_folder(conn, folder)
            moved = sum(1 for uid in ids if _imap_move(conn, uid, trash))
            conn.logout()
            return {"ok": True, "data": {"deleted": moved, "total": len(ids), "mode": "soft", "target": trash}}
        else:
            _select_folder(conn, folder)
            for uid in ids:
                conn.uid("STORE", uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            conn.logout()
            return {"ok": True, "data": {"deleted": len(ids), "mode": "hard"}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_clean(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    clean_folders = kw.get("clean_folders", "") or ""
    folders = [t.strip() for t in clean_folders.split(",") if t.strip()] if clean_folders else ["已删除", "垃圾邮件"]

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        results = []
        for fname in folders:
            typ, _ = _select_folder(conn, fname)
            if typ != "OK":
                results.append({"folder": fname, "error": "文件夹不存在"})
                continue
            typ, data = conn.uid("search", None, "ALL")
            if typ != "OK" or not data or not data[0]:
                results.append({"folder": fname, "cleaned": 0})
                continue
            uids = data[0].split()
            for uid in uids:
                conn.uid("STORE", uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            results.append({"folder": fname, "cleaned": len(uids)})
        conn.logout()
        return {"ok": True, "data": {"results": results}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_move(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    target = kw.get("target_folder", "") or ""
    if not target:
        return {"ok": False, "error": "target_folder 必填", "code": "PARAM_MISSING"}
    ids = _collect_ids(kw)
    if not ids:
        return {"ok": False, "error": "mail_id 或 mail_ids 必填", "code": "PARAM_MISSING"}
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        _select_folder(conn, folder)
        moved = sum(1 for uid in ids if _imap_move(conn, uid, target))
        conn.logout()
        return {"ok": True, "data": {"moved": moved, "total": len(ids), "target": target}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_mark(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    flag = kw.get("flag", "") or ""
    if flag not in FLAG_MAP:
        return {"ok": False, "error": f"flag 必须是 {list(FLAG_MAP.keys())}", "code": "PARAM_MISSING"}
    ids = _collect_ids(kw)
    if not ids:
        return {"ok": False, "error": "mail_id 或 mail_ids 必填", "code": "PARAM_MISSING"}
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"
    op, flag_val = FLAG_MAP[flag]

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        _select_folder(conn, folder)
        marked = 0
        for uid in ids:
            typ, _ = conn.uid("STORE", uid, op, flag_val)
            if typ == "OK":
                marked += 1
        if flag == "deleted":
            conn.expunge()
        conn.logout()
        return {"ok": True, "data": {"marked": marked, "total": len(ids), "flag": flag}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


def _handle_organize(kw: dict) -> dict:
    err = _check_imap_params(kw)
    if err:
        return err
    rule = kw.get("organize_rule", "") or "by_sender"
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"
    limit = int(kw.get("limit", 100) or 100)

    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        _select_folder(conn, folder)
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK":
            conn.logout()
            return {"ok": False, "error": "搜索失败", "code": "IMAP_ERROR"}
        uids = data[0].split() if data and data[0] else []
        uids.reverse()
        uids = uids[:limit]
        mails = [_fetch_headers(conn, uid) for uid in uids]

        groups: dict[str, list[str]] = {}
        if rule == "by_sender":
            for m in mails:
                senders = m.get("from", [])
                if senders:
                    addr = senders[0].get("addr", "unknown")
                    name = addr.split("@")[0] if "@" in addr else addr
                    groups.setdefault(name, []).append(m["uid"])
        elif rule == "by_keyword":
            keyword_map_str = kw.get("keyword_map", "") or "{}"
            try:
                km = json.loads(keyword_map_str)
            except json.JSONDecodeError:
                conn.logout()
                return {"ok": False, "error": "keyword_map 不是有效 JSON", "code": "PARAM_MISSING"}
            for m in mails:
                subj = m.get("subject", "")
                for keyword, target_folder in km.items():
                    if keyword in subj:
                        groups.setdefault(target_folder, []).append(m["uid"])
                        break

        results = []
        for target, uid_list in groups.items():
            moved = sum(1 for uid in uid_list if _imap_move(conn, uid, target))
            results.append({"folder": target, "moved": moved})
        conn.logout()
        return {"ok": True, "data": {"rule": rule, "results": results, "total_mail": len(mails)}}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "IMAP_ERROR"}


# ===== 监听 =====
def _handle_listen_wait(kw: dict) -> dict:
    """同步阻塞等待新邮件：记录监听开始时已有邮件 UID，只返回之后新到的邮件。"""
    err = _check_imap_params(kw)
    if err:
        return err
    imap_server, imap_port = _resolve_imap(kw)
    username, password = kw["username"], kw["password"]
    folder = kw.get("folder", "") or "INBOX"
    timeout = int(kw.get("timeout", 120) or 120)
    interval = max(int(kw.get("check_interval", 30) or 30), 5)
    mark_as_read = kw.get("mark_as_read", True)
    from_filter = (kw.get("from_filter", "") or "").lower()
    subject_filter = (kw.get("subject_filter", "") or "").lower()
    limit = int(kw.get("limit", 1) or 1)

    deadline = time.time() + timeout

    # 获取停止事件（智能体停止时 set，listen_wait 据此自动退出）
    stop_event = None
    try:
        import plugins as _plugins
        stop_event = _plugins.get_stop_event()
    except Exception:
        pass

    # 记录监听开始时已有邮件 UID 作为基准集，只返回之后新到的邮件
    baseline_uids: set[str] = set()
    try:
        conn = _imap_connect(imap_server, imap_port, username, password)
        _select_folder(conn, folder)
        typ, data = conn.uid("search", None, "ALL")
        if typ == "OK":
            if data and data[0]:
                baseline_uids = {uid.decode() for uid in data[0].split()}
        else:
            conn.logout()
            return {"ok": False, "error": "获取基准邮件列表失败", "code": "IMAP_ERROR"}
        conn.logout()
    except Exception as e:
        return {"ok": False, "error": f"连接邮箱失败: {e}", "code": "IMAP_ERROR"}

    while True:
        if stop_event is not None and stop_event.is_set():
            remaining = round(max(0, deadline - time.time()), 1)
            return {"ok": True, "data": {"mails": [], "count": 0, "timeout": False, "stopped": True, "remaining_time": remaining}}
        remaining = deadline - time.time()
        if remaining <= 0:
            return {"ok": True, "data": {"mails": [], "count": 0, "timeout": True, "remaining_time": 0}}
        try:
            conn = _imap_connect(imap_server, imap_port, username, password)
            _select_folder(conn, folder)
            typ, data = conn.uid("search", None, "ALL")
            matched: list[dict] = []
            if typ == "OK" and data and data[0]:
                new_uids = [uid for uid in data[0].split() if uid.decode() not in baseline_uids]
                for uid in new_uids:
                    uid_s = uid.decode()
                    typ2, d = conn.uid("fetch", uid, "(RFC822)")
                    if typ2 != "OK" or not d or not d[0]:
                        continue
                    raw = d[0][1] if isinstance(d[0], tuple) else b""
                    mail = _parse_mail(raw, include_body=True)
                    mail["uid"] = uid_s
                    mail["folder"] = folder
                    if from_filter:
                        senders = mail.get("from", [])
                        sender_str = " ".join(s.get("addr", "") + " " + s.get("name", "") for s in senders).lower()
                        if from_filter not in sender_str:
                            continue
                    if subject_filter and subject_filter not in mail.get("subject", "").lower():
                        continue
                    matched.append(mail)
                    if mark_as_read:
                        conn.uid("STORE", uid, "+FLAGS", "\\Seen")
                    if len(matched) >= limit:
                        break
            conn.logout()
            if matched:
                remaining = round(max(0, deadline - time.time()), 1)
                return {"ok": True, "data": {"mails": matched, "count": len(matched), "timeout": False, "remaining_time": remaining}}
        except Exception:
            pass
        remaining = deadline - time.time()
        if remaining <= 0:
            return {"ok": True, "data": {"mails": [], "count": 0, "timeout": True, "remaining_time": 0}}
        time.sleep(min(interval, remaining))


# ===== 辅助 =====
def _collect_ids(kw: dict) -> list[str]:
    ids: list[str] = []
    mail_ids = kw.get("mail_ids", "") or ""
    if mail_ids:
        ids.extend(t.strip() for t in mail_ids.split(",") if t.strip())
    mail_id = kw.get("mail_id", "") or ""
    if mail_id:
        ids.append(mail_id)
    return ids


# ===== 入口 =====
def _provider_from_addr(addr: str) -> str:
    """按邮箱域名推断 provider，避免只配了 from_addr 时还要手填服务商。"""
    a = (addr or "").strip().lower()
    if "@" not in a:
        return ""
    domain = a.rsplit("@", 1)[1]
    if domain.endswith("exmail.qq.com"):
        return "exmail"
    if domain == "qq.com":
        return "qq"
    if domain == "163.com":
        return "163"
    if domain in ("gmail.com", "googlemail.com"):
        return "gmail"
    if domain in ("outlook.com", "hotmail.com", "live.com", "office365.com"):
        return "outlook"
    return ""


def _normalize_kwargs(kw: dict) -> None:
    """兼容能力参数预设：预设通常只固定 from_addr + password，
    而各 action 内部统一读 username；这里把 from_addr 兜底为 username，
    并按邮箱域名自动补 provider，避免每次调用都因缺参被 AUTH_REQUIRED/PARAM_MISSING 拦下。"""
    if not kw.get("username"):
        kw["username"] = kw.get("from_addr") or ""
    if not kw.get("provider"):
        kw["provider"] = _provider_from_addr(kw.get("username") or kw.get("from_addr") or "")


def run(**kwargs) -> dict[str, Any]:
    action = kwargs.get("action", "")
    if not action and kwargs.get("to_addrs"):
        action = "send"
    _normalize_kwargs(kwargs)

    handlers = {
        "send": _handle_send,
        "list": _handle_list,
        "read": _handle_read,
        "search": _handle_search,
        "folders": _handle_folders,
        "delete": _handle_delete,
        "clean": _handle_clean,
        "move": _handle_move,
        "mark": _handle_mark,
        "organize": _handle_organize,

        "listen_wait": _handle_listen_wait,
    }

    handler = handlers.get(action)
    if not handler:
        return {"ok": False, "error": f"未知 action: {action}，可用: {list(handlers.keys())}", "code": "UNKNOWN_ACTION"}

    try:
        return handler(kwargs)
    except Exception as e:
        return {"ok": False, "error": str(e), "code": "INTERNAL_ERROR", "traceback": traceback.format_exc()}


# 保留历史导入兼容
PLUGIN_NAME = "send_email"
PLUGIN_LABEL = "邮箱管理"
PLUGIN_DESC = "邮箱全功能管理：发送/读取/清理/整理/监听"
PLUGIN_PARAMS = ["action", "provider", "username", "password", "smtp_server", "smtp_port", "use_ssl",
                 "from_addr", "to_addrs", "cc", "bcc", "subject", "body", "html", "attachments",
                 "imap_server", "imap_port", "folder", "target_folder", "mail_id", "mail_ids",
                 "flag", "limit", "offset", "download_dir", "mark_as_read",
                 "from_filter", "subject_filter", "since_date", "before_date", "unseen_only",
                 "delete_mode", "clean_folders", "organize_rule", "keyword_map", "check_interval", "timeout"]
