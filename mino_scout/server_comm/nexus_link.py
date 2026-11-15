"""Scout 把脚本里的账号、验证码和模型请求交给 Nexus。"""
from __future__ import annotations

from typing import Any


def call(case, capability: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    from mino_scout.server_comm import protocol as P
    from mino_scout.runtime.script_exec import ScriptAbort

    ask = getattr(case.core, "nexus_call", None)
    if not callable(ask):
        raise ScriptAbort("无法回调 Nexus", reason_code="nexus_unreachable")
    reply = ask(P.Execute(
        run_id=case.run_id, step_idx=-1, sn=case._sn, capability_id=capability, platform=case._platform,
        params={"session_id": case.session_id, "node_id": getattr(case.core, "node_id", ""),
                **({"task_id": case._recorder.task_id, "attempt_id": case._recorder.attempt_id} if case._recorder else {}), **params},
        timeout_sec=timeout,
    ), timeout)
    status = str(getattr(getattr(reply, "status", None), "value", getattr(reply, "status", "")) or "")
    if reply is None or status != "pass":
        reason = str(getattr(reply, "error", "") or getattr(reply, "summary", "") or "Nexus 没有接受这次回调")
        raise ScriptAbort(reason[:300], reason_code="nexus_rejected")
    data = dict(getattr(reply, "data", None) or {})
    extra = getattr(reply, "extra", None)
    if isinstance(extra, dict):
        nested = extra.get("data")
        if isinstance(nested, dict):
            data = {**nested, **data}
    return data
