"""接受脚本运行载荷。核对 run、确认渠道程序，自己拉取脚本仓库，再执行脚本。

Nexus 只下发仓库地址、分支、提交和设备；仓库由 Scout 用本机 git 凭据拉取。
不解析用例业务，不选择下一步点哪里，也不在这次运行里下载安装包。

回调沿用现有 EXECUTE，不新增消息类型：
script.lease 核对这次 run 仍占用本节点和本 sn；
script.session 把 tool/result 交给 Nexus，截图放在 event 的 image_* 里；
script.callback 的 op 只取 account、note、step、fail。验证码和模型在本节点完成。
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from mino_scout.server_comm import protocol as P
from mino_scout.executors.base import make_event_result, now_iso
from mino_scout.runtime.platform_contract import PlatformContractError, target_platform
from mino_scout.schemas import EventStatus, PlanEvent

_LOCK = threading.Lock()
_ACTIVE: set[tuple[str, str]] = set()


def accept(core, req: P.Execute, stop: threading.Event | None = None):
    stop = stop or threading.Event()
    key: tuple[str, str] | None = None
    root: Path | None = None
    try:
        payload = _payload(req)
        key = (payload.run_id, payload.case_id)
        if not _claim(key):
            key = None
            return _result(req, EventStatus.PASS, "进行中", started="no", phase="admission", state="running", case_id=payload.case_id)
        _lease(core, payload)
        missing = _missing(payload.platform)
        if missing:
            return _result(req, EventStatus.DECLINED, f"缺 {missing}", started="no", phase="admission", state="declined",
                           case_id=payload.case_id, reason=f"缺 {missing}")
        from mino_scout.runtime import script_repo
        root, payload.revision = script_repo.checkout(payload.repo_url, payload.branch, payload.revision)
        payload.script_path = script_repo.locate_script(root, payload.case_id, payload.script_path)
        timeout = float(req.timeout_sec or 0) or 30.0
        from mino_scout.runtime import script_exec
        outcome = script_exec.run(core, _with_hint(payload, req), root, deadline=time.monotonic() + timeout, stop=stop)
        status = EventStatus.PASS if outcome["status"] == "pass" else EventStatus.FAIL
        started = "yes" if outcome.get("touched") else "no"
        return _result(req, status, str(outcome.get("summary") or ""), error=str(outcome.get("error") or ""),
                       started=started, phase="completed" if started == "yes" else "script", state=outcome["status"],
                       case_id=payload.case_id, reason=str(outcome.get("reason_code") or ""))
    except ValueError as exc:
        return _result(req, EventStatus.DECLINED, str(exc)[:300], started="no", phase="admission", state="declined",
                       reason=str(getattr(exc, "code", "") or str(exc)[:300]))
    except Exception as exc:
        return _result(req, EventStatus.FAIL, f"脚本运行失败：{type(exc).__name__}", error=f"{type(exc).__name__}: {exc}"[:400],
                       started="no", phase="admission", state="fail")
    finally:
        if key is not None:
            _release(key)
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def timeout_result(req: P.Execute):
    return _result(req, EventStatus.FAIL, "脚本运行超时", error="脚本运行超时", started="unknown", phase="script", state="fail", reason="script_timeout")


def _payload(req: P.Execute) -> P.ScriptRun:
    raw = dict(req.params or {})
    nested = raw.get("payload")
    if isinstance(nested, dict) and "repo_url" in nested and "run_id" not in raw:
        raw = dict(nested)
    try:
        payload = P.load_script_run(raw)
    except ValueError as exc:
        raise ValueError("脚本运行载荷无效") from exc
    for name in ("run_id", "session_id", "case_id", "sn", "repo_url"):
        if not str(getattr(payload, name) or "").strip():
            raise ValueError("脚本运行载荷无效")
    try:
        payload.platform = target_platform(payload.platform)
    except PlatformContractError as exc:
        raise ValueError("脚本运行载荷的平台无效") from exc
    if req.run_id and req.run_id != payload.run_id:
        raise ValueError("run_id 与运行载荷不一致")
    if req.sn and req.sn != payload.sn:
        raise ValueError("设备与 sn 不符")
    if req.platform and req.platform != payload.platform:
        raise ValueError("平台与运行载荷不一致")
    return payload


def _with_hint(payload: P.ScriptRun, req: P.Execute):
    payload.device_hint = dict(req.device_hint or {})
    return payload


def _lease(core, payload: P.ScriptRun) -> None:
    ask = getattr(core, "nexus_call", None)
    if not callable(ask):
        raise ValueError("无法向 Nexus 核对运行占用")
    reply = ask(P.Execute(
        run_id=payload.run_id,
        step_idx=-1,
        sn=payload.sn,
        capability_id="script.lease",
        platform=payload.platform,
        params={"session_id": payload.session_id, "case_id": payload.case_id, "node_id": getattr(core, "node_id", ""), "op": "lease"},
        timeout_sec=10,
    ), 10)
    status = str(getattr(getattr(reply, "status", None), "value", getattr(reply, "status", "")) or "")
    if reply is None or status != "pass":
        reason = str(getattr(reply, "error", "") or getattr(reply, "summary", "") or "运行占用核对失败")
        raise ValueError(reason[:300])
    data = dict(getattr(reply, "data", None) or {})
    extra = getattr(reply, "extra", None)
    if isinstance(extra, dict) and isinstance(extra.get("data"), dict):
        data = {**extra["data"], **data}
    if data.get("occupied") is False:
        raise ValueError("本节点或设备已不再被这次运行占用")
    if data.get("sn") and str(data.get("sn")) != payload.sn:
        raise ValueError("设备与 sn 不符")
    if data.get("node_id") and str(data.get("node_id")) != str(getattr(core, "node_id", "")):
        raise ValueError("节点与这次运行不符")


def _missing(platform: str) -> str:
    from mino_scout.tools.update.channel_packages import missing_item
    return missing_item(platform)


def _claim(key: tuple[str, str]) -> bool:
    with _LOCK:
        if key in _ACTIVE:
            return False
        _ACTIVE.add(key)
        return True


def _release(key: tuple[str, str]) -> None:
    with _LOCK:
        _ACTIVE.discard(key)


def _result(req, status, summary, *, error="", started, phase, state, case_id="", reason=""):
    event = PlanEvent(seq=req.step_idx if req.step_idx >= 0 else 0, capability_id="script_run", params={})
    return make_event_result(
        event,
        status=status,
        executor_used="script",
        started_at=now_iso(),
        elapsed_ms=0,
        summary=summary,
        error=error,
        raw_response={
            "execution": {"execution_started": started, "phase": phase, "reason_code": reason},
            "script_run": {"state": state, "case_id": case_id},
        },
    )
