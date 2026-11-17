"""脚本向 Nexus 写入账号、步骤、失败和证据。验证码和模型在本节点完成。"""
from __future__ import annotations

from typing import Any

_OTP_WAIT_DEFAULT_MS = 30_000
_OTP_WAIT_CEILING_MS = 90_000


def account(case, requirements=None, prefer=None):
    from types import SimpleNamespace

    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    requirements = requirements if requirements is not None else case._source_snapshot.get("account_requirements")
    params = {"op": "account"}
    if requirements is not None or prefer is not None:
        if requirements is not None and not isinstance(requirements, dict):
            raise ScriptAbort("requirements 必须是 all/prefer 对象", reason_code="account_requirements_invalid", status="blocked")
        filters = dict(requirements or {})
        if set(filters) - {"all", "prefer"}:
            raise ScriptAbort("requirements 只允许 all/prefer", reason_code="account_requirements_invalid", status="blocked")
        if prefer is not None:
            existing = filters.get("prefer", [])
            if not isinstance(existing, list) or not isinstance(prefer, list):
                raise ScriptAbort("prefer 必须是条件列表", reason_code="account_requirements_invalid", status="blocked")
            filters["prefer"] = [*existing, *prefer]
        for key, rows in filters.items():
            if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) - {"facet", "op", "value"} or not row.get("facet") or not row.get("op") for row in rows):
                raise ScriptAbort("账号筛选条件无效", reason_code="account_requirements_invalid", status="blocked")
        params["requirements"] = filters
    data = case._callback("script.callback", params, 15)
    from mino_scout.runtime.secret_redaction import remember
    for key in ("password", "otp", "token"):
        remember(data.get(key))
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

    from mino_scout.plugins.mail_otp import MailOtpError, fetch_otp_via_imap
    from mino_scout.plugins.state import plain_value, secret_value
    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    policy = policy if isinstance(policy, dict) else {}
    node_id = str(getattr(case.core, "node_id", "") or "")
    inbox = str(policy.get("otp_inbox_address") or "").strip() or plain_value("mail", "gmail", "inbox_address")
    imap_host = plain_value("mail", "gmail", "imap_server")
    password = secret_value(node_id, "mail", "gmail", "app_password")
    if not inbox or not password:
        raise ScriptAbort("这个账号没有可用的 Nexus 验证码策略，这台节点也没配收信邮箱",
                          reason_code="otp_environment_unavailable")
    started = float(getattr(case, "_started", 0) or 0)
    # 等待窗口以 Nexus 登录策略为准；上限与 plugin.gmail.fetch_otp 一致，并受用例截止时间约束。
    remain_ms = min(int(policy.get("otp_max_wait_ms") or _OTP_WAIT_DEFAULT_MS), _OTP_WAIT_CEILING_MS)
    if case.deadline:
        remain_ms = max(0, min(remain_ms, int((case.deadline - time.monotonic()) * 1000)))
    if remain_ms <= 0:
        raise ScriptAbort("没有取到验证码", reason_code="otp_environment_unavailable")

    def stopped() -> bool:
        case._guard()
        return False

    diagnostics: dict = {}
    progress: dict = {}
    t0 = time.monotonic()
    try:
        code = fetch_otp_via_imap(
            inbox_address=inbox,
            imap_host=imap_host,
            app_password=password,
            to_address=str(getattr(case, "_mail_to", "") or ""),
            since_ts=started or None,
            from_allowlist=list(policy.get("otp_from_allowlist") or []),
            subject_contains=str(policy.get("otp_subject_contains") or ""),
            poll_interval_ms=int(policy.get("otp_poll_interval_ms") or 200),
            max_wait_ms=remain_ms,
            should_stop=stopped,
            diagnostics=diagnostics,
            progress=progress,
        )
    except MailOtpError as exc:
        _record_gmail_wait(case, t0, remain_ms, diagnostics, progress, error=str(exc))
        raise ScriptAbort(str(exc), reason_code="otp_environment_unavailable") from exc
    code = str(code or "").strip()
    if not code:
        _record_gmail_wait(case, t0, remain_ms, diagnostics, progress, error="没有取到验证码")
        raise ScriptAbort("没有取到验证码", reason_code="otp_environment_unavailable")
    _record_gmail_wait(case, t0, remain_ms, diagnostics, progress)
    return code


def _record_gmail_wait(case, started: float, wait_ms: int, diagnostics: dict, progress: dict, error: str = "") -> None:
    """把这次邮箱取码的耗时和轮询情况写进执行记录。不含验证码、邮箱地址和密码。"""
    import time

    report = getattr(case, "_report", None)
    if not callable(report):
        return
    from mino_scout.runtime.script_exec import ActionResult

    data: dict[str, Any] = {"source": "gmail", "max_wait_ms": int(wait_ms)}
    for key in ("connection_attempts", "reconnects"):
        if type(diagnostics.get(key)) is int:
            data[key] = diagnostics[key]
    for key in ("error_code", "phase"):
        if isinstance(diagnostics.get(key), str) and diagnostics[key]:
            data[key] = diagnostics[key]
    for key in ("polls", "messages", "matched"):
        if type(progress.get(key)) is int:
            data[key] = progress[key]
    if error:
        data["reason_code"] = "otp_environment_unavailable"
    result = ActionResult(ok=not error, summary=error or "已取到验证码", status="fail" if error else "pass", data=data)
    report("otp", result, "core", int((time.monotonic() - started) * 1000))


def otp(case, account=None) -> str:
    """取验证码：账号自带码 → Nexus 登录策略 → 旧脚本仓固定码 → 人工。

    Nexus 策略支持固定码、邮箱收信和人工。脚本仓 otp.code 只保留为旧仓库兼容兜底，
    新项目应在 Nexus 项目环境配置 login_policies，不把验证码策略写进脚本仓。
    """
    from mino_scout.runtime.script_exec import ScriptAbort

    case._guard()
    code = str(getattr(account, "otp", "") or "").strip()
    if code:
        from mino_scout.runtime.secret_redaction import remember
        remember(code)
        return code

    policy = getattr(account, "otp_policy", None)
    policy = policy if isinstance(policy, dict) else {}
    mode = str(policy.get("otp_mode") or "").strip().lower()
    fixed = str(policy.get("otp_fixed") or "").strip()
    if fixed and mode in {"fixed", "auto"}:
        from mino_scout.runtime.secret_redaction import remember
        remember(fixed)
        return fixed
    if mode == "hitl":
        raise ScriptAbort(
            "Nexus 登录策略要求人工提供本次验证码",
            reason_code="otp_requires_human",
        )
    if mode in {"gmail", "auto"}:
        from mino_scout.plugins.state import plain_value, secret_value
        node_id = str(getattr(case.core, "node_id", "") or "")
        inbox = str(policy.get("otp_inbox_address") or "").strip() or plain_value("mail", "gmail", "inbox_address")
        password = secret_value(node_id, "mail", "gmail", "app_password")
        if inbox and password:
            code = _gmail_code(case, policy)
            from mino_scout.runtime.secret_redaction import remember
            remember(code)
            return code

    fixed = ""
    getter = getattr(case, "repo_otp_code", None)
    if callable(getter):
        fixed = str(getter() or "").strip()
    if fixed:
        from mino_scout.runtime.secret_redaction import remember
        remember(fixed)
        return fixed
    raise ScriptAbort(
        "账号没有自带验证码，Nexus 登录策略也没有可用的固定码或邮箱收信：请在 Nexus 项目环境配置验证码策略，或向人要本次验证码",
        reason_code="otp_requires_human",
    )


def otp_mail(case) -> str:
    """兼容旧脚本：使用节点配置的 IMAP 邮箱收信。"""
    return _gmail_code(case, {})


def llm(case, prompt: str, text: str = "", schema: dict | None = None) -> Any:
    from mino_scout.server_comm.model_call import complete

    case._guard()
    return complete(case, prompt, text=text, schema=schema)


def vlm(case, prompt: str, verdict: str = "") -> str:
    from mino_scout.server_comm.model_call import see

    case._guard()
    # This API means the current screen. Historical frames require vlm_frame.
    case.screenshot()
    return _visual_assertion(case, prompt, verdict, see)


def _visual_assertion(app, prompt, verdict, see, index=None):
    from mino_scout.runtime.script_exec import ScriptAbort
    evidence = {"expected": prompt, "app": app.key, "observation_id": app._observation_id,
                "artifact_id": app._artifact_id, "frame_index": index}
    try:
        result = see(app, prompt, verdict=verdict, **({"index": index} if index is not None else {}))
    except ScriptAbort as exc:
        if verdict == "pass":
            app.case._assert("vlm", "fail" if exc.reason_code == "vlm_failed" else "unknown",
                             **evidence, message=str(exc), reason_code=exc.reason_code)
        if verdict == "pass" and exc.reason_code != "vlm_failed":
            raise ScriptAbort(str(exc), reason_code=exc.reason_code, status="blocked", details=exc.details) from exc
        raise
    if verdict == "pass":
        app.case._assert("vlm", "pass", **evidence, actual=result)
    return result


def vlm_frame(case, index: int, prompt: str, verdict: str = "") -> str:
    from mino_scout.runtime.script_exec import ScriptAbort
    from mino_scout.server_comm.model_call import see

    case._guard()
    try:
        case._shot = case._frames[int(index)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ScriptAbort(f"没有第 {index} 帧", reason_code="observation_missing", status="blocked") from exc
    frame = case._frame_refs[int(index)] if int(index) < len(case._frame_refs) else {}
    case._observation_id = frame.get("observation_id", "")
    case._artifact_id = frame.get("artifact_id", "")
    return _visual_assertion(case, prompt, verdict, see, index=int(index))


def note(case, text: str, **fields: Any) -> None:
    from mino_scout.runtime.script_exec import _HIDDEN

    case._guard()
    body = str(text or "")[:500]
    safe = {str(key): value for key, value in fields.items()
            if str(key) not in _HIDDEN and (isinstance(value, (str, int, float, bool)) or value is None)}
    case._callback("script.callback", {"op": "note", "text": body, "fields": safe}, 15)
