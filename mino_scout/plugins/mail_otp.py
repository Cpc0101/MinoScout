"""从收信邮箱取验证码。走标准 IMAP（隐式 TLS），不绑定邮箱服务商。"""
from __future__ import annotations

import email
import hashlib
import imaplib
import errno
import socket
import ssl
import re
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime, getaddresses
from html.parser import HTMLParser
from typing import Any, Optional

OTP_BODY_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
_OTP_HINT_RE = re.compile(
    r"\b(?:code|otp|pin|one[ -]?time password)\b|验证码|校验码|动态口令|一次性密码|認証コード|確認コード",
    re.IGNORECASE,
)
_OTP_CONTEXT_RE = re.compile(
    r"(?:\b(?:code|otp|pin|one[ -]?time password)\b|验证码|校验码|动态口令|一次性密码|認証コード|確認コード)"
    r"\s*(?:(?:is|为|是)\s*)?[:：=\-\s]*[\"'“‘]?(\d{4,8})(?!\d)",
    re.IGNORECASE,
)
# 过滤只用得到这些头；命中过滤条件的邮件才取正文。
_HEADER_FIELDS = "DATE FROM SUBJECT TO DELIVERED-TO X-ORIGINAL-TO ENVELOPE-TO"
_SCAN_LIMIT = 40
_IO_TIMEOUT_S = 5.0
_UID_RE = re.compile(rb"\bUID (\d+)")
_IMAP_HOST_RE = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_IMAP_TLS_PORT = 993
_GROUPED_APP_PASSWORD_RE = re.compile(r"(?:[A-Za-z]\s*){16}")
DEFAULT_IMAP_HOST = "imap.gmail.com"


class MailOtpError(Exception):
    def __init__(self, code, *, diagnostics=None):
        super().__init__(code)
        self.diagnostics = dict(diagnostics or {})


class _WindowExpired(Exception):
    """等待窗口到期。不是传输故障，按窗口内的邮件情况归类。"""


def _transport_error(exc, phase):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return 'otp_imap_tls_certificate_failed', False
    if isinstance(exc, ssl.SSLError):
        return 'otp_imap_tls_failed', False
    if isinstance(exc, socket.gaierror):
        return 'otp_imap_dns_failed', exc.errno == socket.EAI_AGAIN
    if isinstance(exc, TimeoutError):
        return ('otp_imap_connect_timeout' if phase == 'connect' else 'otp_imap_read_timeout'), True
    if isinstance(exc, imaplib.IMAP4.abort):
        return 'otp_imap_connection_lost', True
    if isinstance(exc, imaplib.IMAP4.error):
        return ('otp_imap_auth_failed' if phase == 'login' else 'otp_imap_protocol_failed'), False
    if isinstance(exc, ConnectionRefusedError):
        return 'otp_imap_connection_refused', True
    if isinstance(exc, ConnectionError):
        return 'otp_imap_connection_lost', True
    if isinstance(exc, OSError) and exc.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return 'otp_imap_network_unreachable', True
    return 'otp_imap_connection_failed', False


def normalize_app_password(raw: str) -> str:
    """应用专用密码常按 4 组显示，粘贴会带空格：16 个字母的去掉空白，其余密码原样保留。"""
    text = str(raw or "").strip()
    return re.sub(r"\s+", "", text) if _GROUPED_APP_PASSWORD_RE.fullmatch(text) else text


def parse_imap_host(raw: str) -> tuple[str, int]:
    """`imap.example.com` 或 `imap.example.com:993`。只接受主机名，不接受协议头和路径。"""
    host, sep, port_text = str(raw or "").strip().partition(":")
    port = _IMAP_TLS_PORT
    if sep:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ValueError("IMAP 服务器端口无效")
        port = int(port_text)
    if not _IMAP_HOST_RE.fullmatch(host):
        raise ValueError("IMAP 服务器要写成 imap.example.com 或 imap.example.com:993")
    return host.lower(), port


def _tls_context() -> ssl.SSLContext:
    """校验服务器证书和主机名。CA 与 Scout 其他出站连接同源：已配置的 SSL_CERT_FILE，否则 certifi。"""
    from mino_scout.runtime.startup import configure_tls_environment

    try:
        bundle = configure_tls_environment()["SSL_CERT_FILE"]
    except RuntimeError as exc:
        raise ssl.SSLError("CA bundle is missing") from exc
    return ssl.create_default_context(cafile=bundle)


def _announce_client(mail) -> None:
    """服务器声明支持 ID 时报上客户端名（RFC 2971）。网易系邮箱不先报就拒绝打开收件箱。"""
    if "ID" not in (getattr(mail, "capabilities", None) or ()):
        return
    try:
        mail.xatom("ID", '("name" "MinoScout")')
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error:
        pass  # 服务器不接受这条声明：不影响已完成的登录，后续命令照常


def extract_otp_from_text(text: str, *, subject: str = "") -> str:
    body = str(text or "")
    if not body.strip():
        return ""
    codes = set(OTP_BODY_RE.findall(body))
    subject_codes = set(OTP_BODY_RE.findall(str(subject or "")))
    if _OTP_HINT_RE.search(str(subject or "")) and subject_codes:
        # A unique subject code must also occur in the visible body. Conflicting
        # subject/body values must not fall back to another body candidate.
        if len(subject_codes) == 1:
            code = next(iter(subject_codes))
            return code if code in codes else ""
        return ""
    contextual = set(_OTP_CONTEXT_RE.findall(body))
    # Without a confirming subject, keep distinct body numbers ambiguous.
    # A lone number still needs a code label: a copyright year is not an OTP.
    if len(codes) == 1 and contextual == codes:
        return next(iter(codes))
    if not contextual and _OTP_HINT_RE.search(str(subject or "")) and len(codes) == 1:
        code = next(iter(codes))
        return "" if len(code) == 4 and 1900 <= int(code) <= 2099 else code
    return ""


class _MailText(HTMLParser):
    """Read text content, excluding markup and non-body code/styles."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "style", "script", "template"}:
            self.hidden.append(tag)
        elif not self.hidden and tag in {"br", "p", "div", "td", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[:self.hidden.index(tag)]
        elif not self.hidden and tag in {"p", "div", "td", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _visible_mail_text(text: str, content_type: str) -> str:
    if content_type != "text/html":
        return text
    parser = _MailText()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def _decode_mime_header(value: str) -> str:
    parts: list[str] = []
    for frag, enc in decode_header(value or ""):
        if isinstance(frag, bytes):
            try:
                parts.append(frag.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                # 头里是未编码的 8 位字节，或声明了不认识的字符集：按 UTF-8 读，不让一封邮件打断取码。
                parts.append(frag.decode("utf-8", errors="replace"))
        else:
            parts.append(str(frag))
    return "".join(parts).strip()


def _message_datetime(msg: email.message.Message) -> Optional[datetime]:
    # 头里带未编码的 8 位字节时 msg.get 返回的不是 str，先解成文本再解析。
    raw = _decode_mime_header(msg.get("Date") or "")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _addresses_match_header(header_val: str, want: str) -> bool:
    want_l = str(want or "").strip().lower()
    if not want_l:
        return True
    return want_l in {address.strip().lower() for _, address in getaddresses([_decode_mime_header(header_val)])}


def _from_allowed(msg: email.message.Message, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    from_h = _decode_mime_header(msg.get("From") or "").lower()
    return any(str(a).strip().lower() in from_h for a in allowlist if str(a).strip())


def _subject_ok(msg: email.message.Message, needle: str) -> bool:
    sub = _decode_mime_header(msg.get("Subject") or "")
    n = str(needle or "").strip()
    if not n:
        return True
    return n in sub


def _to_recipient_ok(msg: email.message.Message, to_address: str) -> bool:
    want = str(to_address or "").strip()
    if not want:
        return True
    for key in ("To", "Delivered-To", "X-Original-To", "Envelope-To"):
        if _addresses_match_header(msg.get(key) or "", want):
            return True
    return False


def _collect_text_parts(msg: email.message.Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = str(part.get_content_type() or "").lower()
            if ctype not in ("text/plain", "text/html") or part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            chunks.append(_visible_mail_text(text, ctype))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            chunks.append(_visible_mail_text(text, msg.get_content_type()))
    return "\n".join(chunks)


def _imap_since_date(since_ts: Optional[float]) -> str:
    """SEARCH SINCE 只比日期，且按服务器自己的时区算。往前多取一天，准确时间由邮件头再筛。"""
    if since_ts is None:
        since_ts = time.time() - 3600
    dt = datetime.fromtimestamp(float(since_ts) - 30 - 86400, tz=timezone.utc)
    return dt.strftime("%d-%b-%Y")


def _close(mail) -> None:
    if mail is None:
        return
    try:
        if mail.sock is not None:
            mail.sock.settimeout(0.1)
        mail.logout()
    except Exception:
        try:
            mail.shutdown()
        except Exception:
            pass


def _open_inbox(host: str, port: int, inbox_address: str, password: str, *, guard, io_timeout, stats):
    stats['phase'] = 'connect'
    mail = imaplib.IMAP4_SSL(host, port, ssl_context=_tls_context(), timeout=io_timeout())
    try:
        guard(mail)
        stats['phase'] = 'login'
        mail.login(inbox_address, password)
        guard(mail)
        stats['phase'] = 'select'
        _announce_client(mail)
        guard(mail)
        typ, _ = mail.select("INBOX", readonly=True)
        if typ != "OK":
            # imaplib 对 NO 不抛错；不拦下来，后面的 SEARCH 会报成别的阶段。
            raise imaplib.IMAP4.error("INBOX is not available")
    except BaseException:
        _close(mail)
        raise
    return mail


def _literals_by_uid(rows) -> dict[bytes, bytes]:
    """把 UID FETCH 的应答整理成 {uid: 字面量}。UID 可能在字面量之前或之后。"""
    rows = list(rows or [])
    out: dict[bytes, bytes] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, tuple) or len(row) < 2 or not isinstance(row[1], (bytes, bytearray)):
            continue
        match = _UID_RE.search(bytes(row[0] or b""))
        if match is None and index + 1 < len(rows) and isinstance(rows[index + 1], (bytes, bytearray)):
            match = _UID_RE.search(bytes(rows[index + 1]))
        if match is not None:
            out[match.group(1)] = bytes(row[1])
    return out


def _single_literal(rows, uid: bytes) -> Optional[bytes]:
    """单封 UID FETCH 的字面量。应答里认不出 UID 时，只有一段字面量就用它。"""
    found = _literals_by_uid(rows).get(uid)
    if found is not None:
        return found
    literals = [bytes(row[1]) for row in (rows or [])
                if isinstance(row, tuple) and len(row) >= 2 and isinstance(row[1], (bytes, bytearray))]
    return literals[0] if len(literals) == 1 else None


def _mailbox_number(mail, field: str) -> int:
    _, values = mail.response(field)
    if not values or not isinstance(values[0], bytes) or not values[0].isdigit() or int(values[0]) < 1:
        raise MailOtpError("otp_mailbox_boundary_unavailable")
    return int(values[0])


def capture_otp_boundary(*, inbox_address: str, imap_host: str, app_password: str,
                         should_stop=None, max_wait_ms: int = 15000) -> dict:
    """Read UIDVALIDITY/UIDNEXT before the script requests a new code. Never sends mail."""
    end = time.monotonic() + max(0, max_wait_ms) / 1000
    stats = {}
    mail = None
    def check():
        if should_stop and should_stop():
            raise MailOtpError("otp_wait_stopped")
        if time.monotonic() >= end:
            raise MailOtpError("otp_prepare_timeout")
    def timeout():
        check()
        return max(0.001, min(_IO_TIMEOUT_S, end - time.monotonic()))
    def guard(client):
        check()
        if client.sock is not None:
            client.sock.settimeout(timeout())
    try:
        host, port = parse_imap_host(imap_host or DEFAULT_IMAP_HOST)
        check()
        mail = _open_inbox(host, port, inbox_address, normalize_app_password(app_password),
                           guard=guard, io_timeout=timeout, stats=stats)
        result = {"uid_validity": _mailbox_number(mail, "UIDVALIDITY"),
                  "min_uid": _mailbox_number(mail, "UIDNEXT")}
        check()
        return result
    except ValueError:
        raise MailOtpError("otp_imap_host_invalid") from None
    except (imaplib.IMAP4.error, OSError) as exc:
        raise MailOtpError(_transport_error(exc, stats.get("phase", "connect"))[0]) from None
    finally:
        _close(mail)


def _poll_inbox(
    mail,
    *,
    to_address: str,
    since_ts: Optional[float],
    from_allowlist: list[str],
    subject_contains: str,
    seen: set,
    in_window: set,
    matched: set,
    refresh: bool,
    guard,
    stats: dict,
    consumed_messages: Optional[set[str]] = None,
    min_uid: int = 1,
) -> str:
    """在已打开的连接上看一遍新邮件。

    先批量取过滤用的头，命中收件人/发件人/主题/时间窗口的才取正文。`seen` 是已有定论、
    不再看的邮件；头或正文没取回来的不进 `seen`，下一轮重取。`in_window` / `matched`
    按邮件去重计数。三个集合只在一次取码请求内有效，不跨请求、不跨用例。
    """
    guard(mail)
    stats['phase'] = 'search'
    if refresh:
        mail.noop()
        guard(mail)
    typ, data = mail.uid("SEARCH", f"(UID {min_uid}:* SINCE {_imap_since_date(since_ts)})")
    stats["polls"] = stats.get("polls", 0) + 1
    if typ != "OK" or not data or not data[0]:
        return ""
    # Bound each batch, not the entire search window. Later polls continue past
    # already classified mail, including messages for other shared-inbox users.
    fresh = sorted((uid for uid in data[0].split() if uid not in seen and int(uid) >= min_uid), key=int, reverse=True)[:_SCAN_LIMIT]
    if not fresh:
        return ""
    guard(mail)
    stats['phase'] = 'fetch'
    typ, rows = mail.uid("FETCH", b",".join(fresh).decode("ascii"),
                         f"(BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})])")
    if typ != "OK":
        return ""
    headers = _literals_by_uid(rows)
    for uid in sorted(fresh, key=int, reverse=True):
        raw = headers.get(uid)
        if raw is None:
            continue  # 这封的头没取回来：不下结论，下一轮再取
        msg = email.message_from_bytes(raw)
        if since_ts is not None:
            mdt = _message_datetime(msg)
            if mdt is not None and mdt.timestamp() > time.time() + 30:
                continue  # 发件时间超前：先不下结论，后面的轮询再看
            if mdt is None or mdt.timestamp() < float(since_ts) - 30:
                seen.add(uid)
                continue
        # 只统计时间窗口内的邮件：窗口外的旧邮件不算“收到了但过滤不匹配”。
        in_window.add(uid)
        stats["messages"] = len(in_window)
        if (not _from_allowed(msg, from_allowlist) or not _subject_ok(msg, subject_contains)
                or not _to_recipient_ok(msg, to_address)):
            seen.add(uid)
            continue
        matched.add(uid)
        stats["matched"] = len(matched)
        guard(mail)
        typ, rows = mail.uid("FETCH", uid.decode("ascii"), "(BODY.PEEK[])")
        body = _single_literal(rows, uid) if typ == "OK" else None
        if not body:
            continue  # 正文没取回来：不下结论，下一轮再取
        seen.add(uid)
        fingerprint = hashlib.sha256(body).hexdigest()
        if consumed_messages is not None and fingerprint in consumed_messages:
            continue
        full_message = email.message_from_bytes(body)
        code = extract_otp_from_text(
            _collect_text_parts(full_message), subject=_decode_mime_header(full_message.get("Subject") or ""),
        )
        if code:
            if consumed_messages is not None:
                consumed_messages.add(fingerprint)
            return code
    return ""


def fetch_otp_via_imap(
    *,
    inbox_address: str,
    imap_host: str = "",
    app_password: str,
    to_address: str = "",
    since_ts: Optional[float] = None,
    from_allowlist: Optional[list[str]] = None,
    subject_contains: str = "",
    poll_interval_ms: int = 200,
    max_wait_ms: int = 90_000,
    should_stop=None,
    deadline_ts: Optional[float] = None,
    diagnostics: Optional[dict] = None,
    progress: Optional[dict] = None,
    consumed_messages: Optional[set[str]] = None,
    mailbox_boundary: Optional[dict] = None,
) -> str:
    """在等待窗口内从收件箱取验证码。`imap_host` 是 `主机` 或 `主机:端口`，留空用 Gmail，只走隐式 TLS。

    整个请求复用一条只读连接，只有传输故障才重连（全程最多两次）。窗口到期按邮件情况
    归类为未收到 / 过滤不匹配 / 正文无验证码，不报成传输超时。

    `diagnostics` 只写协议约定的 connection_attempts / reconnects / error_code / phase；
    `progress` 是本机诊断用的计数（polls / messages / matched），不进协议载荷。
    """
    allow = [str(x).strip() for x in (from_allowlist or []) if str(x).strip()]
    if mailbox_boundary is not None and (not isinstance(mailbox_boundary, dict) or any(
            type(mailbox_boundary.get(key)) is not int or mailbox_boundary[key] < 1 for key in ("uid_validity", "min_uid"))):
        raise MailOtpError("otp_mailbox_boundary_invalid")
    interval = max(0.2, int(poll_interval_ms or 200) / 1000.0)
    deadline = time.time() + max(0.0, int(max_wait_ms) / 1000.0)
    if deadline_ts:
        deadline = min(deadline, float(deadline_ts))
    monotonic_end = time.monotonic() + max(0.0, deadline - time.time())
    def remaining():
        return min(deadline - time.time(), monotonic_end - time.monotonic())
    def check():
        if should_stop and should_stop():
            raise MailOtpError("otp_wait_stopped")
        if remaining() <= 0:
            raise _WindowExpired()
    def io_timeout():
        return max(0.001, min(_IO_TIMEOUT_S, remaining()))
    def guard(mail):
        check()
        if mail.sock is not None:
            mail.sock.settimeout(io_timeout())
    password = normalize_app_password(app_password)
    try:
        host, port = parse_imap_host(imap_host or DEFAULT_IMAP_HOST)
    except ValueError as exc:
        raise MailOtpError("IMAP 服务器配置无效") from exc
    if not inbox_address or not password:
        raise MailOtpError("收信邮箱未配置")
    stats = progress if progress is not None else {}
    stats.update(polls=0, messages=0, matched=0)
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(connection_attempts=0, reconnects=0)
    transport_failures = 0
    last_error_code = ""
    seen: set = set()
    in_window: set = set()
    matched: set = set()
    mail = None
    try:
        while remaining() > 0:
            if should_stop and should_stop():
                raise MailOtpError("任务已取消或已超过用例时间")
            delay = interval
            try:
                check()
                refresh = mail is not None
                if mail is None:
                    diagnostics['connection_attempts'] += 1
                    mail = _open_inbox(host, port, inbox_address, password,
                                       guard=guard, io_timeout=io_timeout, stats=stats)
                    if mailbox_boundary is not None and _mailbox_number(mail, "UIDVALIDITY") != mailbox_boundary["uid_validity"]:
                        raise MailOtpError("otp_mailbox_changed")
                code = _poll_inbox(
                    mail,
                    to_address=to_address,
                    since_ts=since_ts,
                    from_allowlist=allow,
                    subject_contains=subject_contains,
                    seen=seen, in_window=in_window, matched=matched,
                    refresh=refresh, guard=guard, stats=stats,
                    consumed_messages=consumed_messages,
                    min_uid=mailbox_boundary["min_uid"] if mailbox_boundary else 1,
                )
                if code:
                    if should_stop and should_stop():
                        raise MailOtpError("otp_wait_stopped")
                    return code
                last_error_code = ""
            except _WindowExpired:
                break
            except (imaplib.IMAP4.error, OSError) as exc:
                phase = stats.get('phase', 'connect')
                error_code, retryable = _transport_error(exc, phase)
                _close(mail)
                mail = None
                if should_stop and should_stop():
                    diagnostics.update(error_code=error_code, phase=phase)
                    raise MailOtpError('otp_wait_stopped', diagnostics=diagnostics) from None
                if isinstance(exc, TimeoutError) and remaining() <= 0.05:
                    # 读超时被压到了剩余窗口：这是窗口到期，不是传输故障。
                    break
                diagnostics.update(error_code=error_code, phase=phase)
                transport_failures += 1
                last_error_code = error_code
                # At most two reconnections over the entire request; success does not
                # reset this budget. No email sends and no parent-operation replay.
                if not retryable or transport_failures > 2 or remaining() <= 0:
                    raise MailOtpError(error_code, diagnostics=diagnostics) from None
                diagnostics['reconnects'] += 1
                delay = min(2.0 ** (transport_failures - 1), max(0.0, remaining()))
            slept = 0.0
            while slept < delay and remaining() > 0:
                if should_stop and should_stop():
                    raise MailOtpError("任务已取消或已超过用例时间")
                step = max(0.0, min(0.5, delay - slept, remaining()))
                time.sleep(step)
                slept += step
    finally:
        _close(mail)
    if should_stop and should_stop():
        raise MailOtpError("任务已取消或已超过用例时间")
    if last_error_code:
        raise MailOtpError(last_error_code, diagnostics=diagnostics)
    if stats.get('matched'):
        raise MailOtpError('otp_mail_code_not_found')
    if stats.get('messages'):
        raise MailOtpError('otp_mail_filter_no_match')
    raise MailOtpError('otp_mail_not_received')
