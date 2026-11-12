"""脚本向 Nexus 写入账号、步骤、失败和证据。验证码和模型在本节点完成。"""
from __future__ import annotations

from typing import Any


def account(case):
    from types import SimpleNamespace

    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    data = case._callback("script.callback", {"op": "account"}, 15)
    identity = str(data.get("id") or data.get("account_id") or "").strip()
    if not identity:
        raise ScriptAbort("这条用例没有租到账号", reason_code="resource_account_missing")
    case._mail_to = str(data.get("email") or "")
    return SimpleNamespace(
        id=identity,
        email=case._mail_to,
        phone=str(data.get("phone") or ""),
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
        otp=str(data.get("otp") or ""),
        ident=str(data.get("ident") or ""),
        otp_policy=dict(data.get("login_policy") or {}),
    )


def _gmail_code(case, policy=None) -> str:
    import time

    from mino_scout.plugins.gmail_otp import GmailOtpError, fetch_otp_via_imap
    from mino_scout.plugins.state import plain_value, secret_value
    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    policy = policy if isinstance(policy, dict) else {}
    node_id = str(getattr(case.core, "node_id", "") or "")
    inbox = plain_value("mail", "gmail", "inbox_address")
    password = secret_value(node_id, "mail", "gmail", "app_password")
    if not inbox or not password:
        raise ScriptAbort("这个账号没有固定验证码，这台节点也没配 Gmail 收件箱：请配固定码或配收信",
                          reason_code="otp_environment_unavailable")
    started = float(getattr(case, "_started", 0) or 0)
    remain_ms = int(policy.get("otp_max_wait_ms") or 30_000)
    if case.deadline:
        remain_ms = max(0, min(remain_ms, int((case.deadline - time.monotonic()) * 1000)))
    if remain_ms <= 0:
        raise ScriptAbort("没有取到验证码", reason_code="otp_environment_unavailable")

    def stopped() -> bool:
        case._guard()
        return False

    try:
        code = fetch_otp_via_imap(
            inbox_address=inbox,
            app_password=password,
            to_address=str(getattr(case, "_mail_to", "") or ""),
            since_ts=started or None,
            from_allowlist=list(policy.get("otp_from_allowlist") or []),
            subject_contains=str(policy.get("otp_subject_contains") or ""),
            poll_interval_ms=int(policy.get("otp_poll_interval_ms") or 200),
            max_wait_ms=min(remain_ms, 30_000),
            should_stop=stopped,
        )
    except GmailOtpError as exc:
        raise ScriptAbort(str(exc), reason_code="otp_environment_unavailable") from exc
    if not str(code or "").strip():
        raise ScriptAbort("没有取到验证码", reason_code="otp_environment_unavailable")
    return str(code).strip()


def otp(case, account=None) -> str:
    """取验证码：账号自带码 → 脚本仓 config.yaml 的 otp.code → 否则停下来问人。

    固定码写在脚本仓（按 app / env 的 otp.code）；不写就表示没有固定码，需要人工提供。
    取码策略不再依赖 Nexus 账号上的 login_policy。
    """
    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    code = str(getattr(account, "otp", "") or "").strip()
    if code:
        return code
    fixed = ""
    getter = getattr(case, "repo_otp_code", None)
    if callable(getter):
        fixed = str(getter() or "").strip()
    if fixed:
        return fixed
    raise ScriptAbort(
        "config.yaml 没有 otp.code，也没有账号自带验证码：请向人要本次验证码，或把测试用固定码写进对应应用/环境的 otp.code",
        reason_code="otp_requires_human",
    )


def otp_mail(case) -> str:
    """兼容旧脚本：只走 Gmail 收信。"""
    return _gmail_code(case, {})


def llm(case, prompt: str, text: str = "", schema: dict | None = None) -> Any:
    from mino_scout.server_comm.model_call import complete

    case._guard()
    return complete(case, prompt, text=text, schema=schema)


def vlm(case, prompt: str, verdict: str = "") -> str:
    from mino_scout.server_comm.model_call import see

    case._guard()
    if case._shot is None or not getattr(case._shot, "has_image", lambda: False)():
        case.screenshot()
    return see(case, prompt, verdict=verdict)


def vlm_frame(case, index: int, prompt: str, verdict: str = "") -> str:
    from mino_scout.runtime.script_exec import ScriptAbort
    from mino_scout.server_comm.model_call import see

    case._guard()
    try:
        case._shot = case._frames[int(index)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ScriptAbort(f"没有第 {index} 帧", reason_code="observation_missing") from exc
    return see(case, prompt, verdict=verdict, index=int(index))


def note(case, text: str, **fields: Any) -> None:
    from mino_scout.runtime.script_exec import _HIDDEN

    case._guard()
    body = str(text or "")[:500]
    safe = {str(key): value for key, value in fields.items()
            if str(key) not in _HIDDEN and (isinstance(value, (str, int, float, bool)) or value is None)}
    case._callback("script.callback", {"op": "note", "text": body, "fields": safe}, 15)
