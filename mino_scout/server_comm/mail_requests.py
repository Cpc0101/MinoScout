"""Single-use, case-local mailbox boundaries. No credentials or message bodies in receipts."""
from __future__ import annotations

import time
import uuid


def settings(case, policy, recipient):
    from mino_scout.plugins.state import plain_value, secret_value
    from mino_scout.plugins.mail_otp import DEFAULT_IMAP_HOST, parse_imap_host
    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    recipient = str(recipient or "").strip()
    if not recipient:
        raise ScriptAbort("邮箱取码缺少本次账号的收件地址", reason_code="otp_recipient_required", status="blocked")
    inbox = str(policy.get("otp_inbox_address") or "").strip() or plain_value("mail", "gmail", "inbox_address")
    raw_host = plain_value("mail", "gmail", "imap_server") or DEFAULT_IMAP_HOST
    password = secret_value(str(getattr(case.core, "node_id", "") or ""), "mail", "gmail", "app_password")
    if not inbox or not password:
        raise ScriptAbort("节点未配置可用的收信邮箱", reason_code="otp_environment_unavailable", status="blocked")
    try:
        host, port = parse_imap_host(raw_host)
    except ValueError:
        raise ScriptAbort("IMAP 服务器配置无效", reason_code="otp_environment_unavailable", status="blocked") from None
    return {"inbox_address": inbox, "imap_host": f"{host}:{port}", "app_password": password,
            "to_address": recipient}


def _scope(config, account_id):
    return (str(account_id or ""), *(config[key].lower() for key in ("imap_host", "inbox_address", "to_address")))


def prepare(case, policy, recipient, account_id=""):
    from copy import deepcopy
    from mino_scout.plugins.mail_otp import capture_otp_boundary, MailOtpError
    from mino_scout.runtime.script_exec import ScriptAbort
    from mino_scout.server_comm.otp_progress import case_lock, Progress

    config = settings(case, policy, recipient)
    scope = _scope(config, account_id)
    mailbox = scope[1:]
    with case_lock(case):
        if mailbox in case._otp_active:
            raise ScriptAbort("该邮箱请求正在准备或取码；请读取已有进度，不要并行取码",
                              reason_code="otp_request_in_progress", status="blocked")
        case._otp_active.add(mailbox)
        case._otp_requests = {key: value for key, value in case._otp_requests.items() if value["scope"][1:] != mailbox}
    end = time.monotonic() + 15
    if case.deadline:
        end = min(end, case.deadline)
    progress = Progress(case, state='preparing', deadline=end).start()
    try:
        boundary = capture_otp_boundary(**{key: config[key] for key in ("inbox_address", "imap_host", "app_password")},
                                        should_stop=case._guard, max_wait_ms=max(0, int((end - time.monotonic()) * 1000)),
                                        progress=progress.stats)
        case._guard()
        request = uuid.uuid4().hex
        expires = time.monotonic() + 90
        if case.deadline:
            expires = min(expires, case.deadline)
        with case_lock(case):
            case._otp_requests[request] = {"scope": scope, "boundary": boundary, "policy": deepcopy(policy),
                                           "started": time.time(), "expires": expires}
        progress.bind(request)
        progress.deadline = expires
        progress.finish('prepared')
        return request
    except BaseException as exc:
        reason = getattr(exc, 'reason_code', '') or (str(exc) if isinstance(exc, MailOtpError) else '')
        progress.finish('cancelled' if reason == 'cancelled' else 'timed_out' if reason == 'script_timeout' else 'blocked', reason)
        if isinstance(exc, MailOtpError):
            raise ScriptAbort(str(exc), reason_code="otp_environment_unavailable", status="blocked") from None
        raise
    finally:
        with case_lock(case):
            case._otp_active.discard(mailbox)


def consume(case, policy, recipient, account_id, request):
    from mino_scout.runtime.script_exec import ScriptAbort
    from mino_scout.server_comm.otp_progress import case_lock

    case._guard()
    if request is None:
        raise ScriptAbort("请在发送验证码前调用 case.scout.otp_request(account)，取码时传 request=返回值",
                          reason_code="otp_request_required", status="blocked")
    if not isinstance(request, str):
        raise ScriptAbort("验证码请求不存在、已使用或不属于当前执行", reason_code="otp_request_invalid", status="blocked")
    # Reject missing tokens before reading credentials; serialize check+pop.
    with case_lock(case):
        if request not in case._otp_requests:
            raise ScriptAbort("验证码请求不存在、已使用或不属于当前执行", reason_code="otp_request_invalid", status="blocked")
        config = settings(case, policy, recipient)
        state = case._otp_requests[request]
        if time.monotonic() >= state["expires"]:
            case._otp_requests.pop(request)
            raise ScriptAbort("验证码请求已过期，请重新建立请求后发码", reason_code="otp_request_expired", status="blocked")
        if state["scope"] != _scope(config, account_id) or state["policy"] != policy:
            raise ScriptAbort("验证码请求的账号、收件邮箱或策略已变化", reason_code="otp_request_mismatch", status="blocked")
        mailbox = state["scope"][1:]
        if mailbox in case._otp_active:
            raise ScriptAbort("该邮箱已有正在执行的请求，请读取已有进度", reason_code="otp_request_in_progress", status="blocked")
        case._otp_active.add(mailbox)
        case._otp_requests.pop(request)
        return config, state


def finish(case, window):
    from mino_scout.server_comm.otp_progress import case_lock
    with case_lock(case):
        case._otp_active.discard(window["scope"][1:])
