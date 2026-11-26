"""Scout 把脚本里的账号、验证码和模型请求交给 Nexus。"""
from __future__ import annotations

import math
from typing import Any


class NexusCallbackError(RuntimeError):
    """Local transport diagnosis; never implies a callback can be replayed."""

    CODES = frozenset({"nexus_unreachable", "nexus_disconnected", "nexus_timeout",
                       "nexus_transport_error", "nexus_invalid_reply", "nexus_invalid_budget", "nexus_reply_identity_mismatch"})

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in self.CODES else "nexus_transport_error"
        super().__init__(self.code)


def callback_budget(value):
    try:
        budget = float(value)
    except (TypeError, ValueError, OverflowError):
        raise NexusCallbackError("nexus_invalid_budget") from None
    if isinstance(value, bool) or not math.isfinite(budget) or not 0 < budget <= 86400:
        raise NexusCallbackError("nexus_invalid_budget")
    return budget


def validate_reply(request, reply):
    from mino_scout.server_comm import protocol as P
    if not isinstance(reply, P.Result):
        raise NexusCallbackError("nexus_invalid_reply")
    if (reply.run_id != request.run_id or type(reply.step_idx) is not int
            or reply.step_idx != request.step_idx):
        raise NexusCallbackError("nexus_reply_identity_mismatch")
    status = getattr(reply.status, "value", reply.status)
    if (not isinstance(status, str) or status not in {"pass", "fail", "blocked", "declined", "skipped"}
            or (reply.data is not None and not isinstance(reply.data, dict))
            or (reply.extra is not None and not isinstance(reply.extra, dict))):
        raise NexusCallbackError("nexus_invalid_reply")
    return reply


def pipe_call(pipe, request, timeout):
    import time
    import uuid
    from dataclasses import asdict
    from mino_scout.server_comm import protocol as P

    timeout = callback_budget(timeout)
    request_id = uuid.uuid4().hex
    deadline = time.monotonic() + timeout + 2
    pipe.send({"kind": "call", "request": asdict(request), "timeout": timeout,
               "callback_request_id": request_id})
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not pipe.poll(remaining):
            raise NexusCallbackError("nexus_timeout")
        message = pipe.recv()
        if not isinstance(message, dict):
            raise NexusCallbackError("nexus_invalid_reply")
        if message.get("callback_request_id") != request_id:
            continue  # A late reply cannot satisfy the next callback.
        if message.get("callback_error"):
            raise NexusCallbackError(message["callback_error"])
        raw = message.get("reply")
        if raw is None:
            raise NexusCallbackError("nexus_unreachable")
        try:
            reply = P.Result(**raw)
        except (TypeError, ValueError):
            raise NexusCallbackError("nexus_invalid_reply") from None
        return validate_reply(request, reply)


def call(case, capability: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    from mino_scout.server_comm import protocol as P
    from mino_scout.runtime.script_exec import ScriptAbort

    ask = getattr(case.core, "nexus_call", None)
    if not callable(ask):
        raise ScriptAbort("无法回调 Nexus", reason_code="nexus_unreachable", status="blocked")
    try:
        timeout = callback_budget(timeout)
    except NexusCallbackError as exc:
        raise ScriptAbort("Nexus 回调预算无效，尚未发送", reason_code=exc.code, status="blocked") from None
    request = P.Execute(
        run_id=case.run_id, step_idx=-1, sn=case._sn, capability_id=capability, platform=case._platform,
        params={"session_id": case.session_id, "node_id": getattr(case.core, "node_id", ""),
                **({"task_id": case._recorder.task_id, "attempt_id": case._recorder.attempt_id} if case._recorder else {}), **params},
        timeout_sec=timeout,
    )
    try:
        reply = ask(request, timeout)
    except Exception as exc:
        code = exc.code if isinstance(exc, NexusCallbackError) else "nexus_transport_error"
        raise ScriptAbort("Nexus 回调传输失败（未重试）", reason_code=code, status="blocked",
                          details={"nexus_error": {"capability_id": capability,
                                   "exception_type": type(exc).__name__, "outcome": "unknown"}}) from None
    if reply is None:
        raise ScriptAbort("Nexus 回调未取得应答（结果未知，未重试）",
                          reason_code="nexus_unreachable", status="blocked",
                          details={"nexus_error": {"capability_id": capability, "outcome": "unknown"}})
    try:
        validate_reply(request, reply)
    except NexusCallbackError as exc:
        raise ScriptAbort("Nexus 回调应答无效或身份不匹配", reason_code=exc.code, status="blocked") from None
    status = str(getattr(getattr(reply, "status", None), "value", getattr(reply, "status", "")) or "")
    if status != "pass":
        from mino_scout.runtime.secret_redaction import sanitize

        reason = str(getattr(reply, "summary", "") or getattr(reply, "error", "") or "Nexus 没有接受这次回调")
        diagnostic = sanitize({
            "capability_id": capability,
            "operation": str(params.get("op") or "")[:80],
            "code": str(getattr(reply, "error", "") or "")[:80],
        })
        raise ScriptAbort(sanitize(reason)[:300], reason_code="nexus_rejected", status="blocked", details={"nexus_error": diagnostic})
    data = dict(getattr(reply, "data", None) or {})
    extra = getattr(reply, "extra", None)
    if isinstance(extra, dict):
        nested = extra.get("data")
        if isinstance(nested, dict):
            data = {**nested, **data}
    return data
