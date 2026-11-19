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
    from mino_scout.plugins.mail_otp import capture_otp_boundary, MailOtpError
    from mino_scout.runtime.script_exec import ScriptAbort

    config = settings(case, policy, recipient)
    scope = _scope(config, account_id)
    # Re-preparing this recipient invalidates any unconsumed older request.
    case._otp_requests = {key: value for key, value in case._otp_requests.items() if value["scope"][1:] != scope[1:]}
    budget = 15000
    if case.deadline:
        budget = max(0, min(budget, int((case.deadline - time.monotonic()) * 1000)))
    try:
        boundary = capture_otp_boundary(**{key: config[key] for key in ("inbox_address", "imap_host", "app_password")},
                                        should_stop=case._guard, max_wait_ms=budget)
    except MailOtpError as exc:
        raise ScriptAbort(str(exc), reason_code="otp_environment_unavailable", status="blocked") from None
    case._guard()
    request = uuid.uuid4().hex
    from copy import deepcopy
    case._otp_requests[request] = {"scope": scope, "boundary": boundary, "policy": deepcopy(policy),
                                   "started": time.time(), "expires": time.monotonic() + 90}
    return request


def consume(case, policy, recipient, account_id, request):
    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    if request is None:
        raise ScriptAbort("请在发送验证码前调用 case.scout.otp_request(account)，取码时传 request=返回值",
                          reason_code="otp_request_required", status="blocked")
    if not isinstance(request, str) or request not in case._otp_requests:
        raise ScriptAbort("验证码请求不存在、已使用或不属于当前执行", reason_code="otp_request_invalid", status="blocked")
    config = settings(case, policy, recipient)
    state = case._otp_requests[request]
    if time.monotonic() >= state["expires"]:
        case._otp_requests.pop(request)
        raise ScriptAbort("验证码请求已过期，请重新建立请求后发码", reason_code="otp_request_expired", status="blocked")
    if state["scope"] != _scope(config, account_id) or state["policy"] != policy:
        raise ScriptAbort("验证码请求的账号、收件邮箱或策略已变化", reason_code="otp_request_mismatch", status="blocked")
    # One fetch operation, including its bounded reconnects, consumes the token.
    case._otp_requests.pop(request)
    return config, state
