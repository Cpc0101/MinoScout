"""Adapter for current UI execution receipts; no device or policy decisions."""
from __future__ import annotations
from typing import Any

# Only these framework/read implementations are known not to mutate UI.
READS = frozenset({"write_stop_status", "probe", "probe_device_state", "screenshot", "hierarchy", "get_foreground_app", "get_app_version",
                   "read_web_auth", "read_web_target_facts", "read_device_data"})

# 装包/卸载的成败由 adb / devicectl 给出终态，不是「界面效果未知」。
# 它们仍是写动作（要串行、要受 stop 检查），但不该按界面写去封存资源：
# 签名不对、包缺失这类明确失败只应报 FAIL，不能把设备判成结果未知。
PACKAGE_WRITES = frozenset({"install", "install_apk", "install_app", "uninstall_app"})


def ui_write(cap: str) -> bool:
    return bool(cap) and cap not in READS and cap != "cancel_run" and not cap.startswith(("node.", "plugin."))


def started(raw: Any) -> str:
    data = raw if isinstance(raw, dict) else {}
    receipt = data.get("execution")
    value = receipt.get("execution_started") if isinstance(receipt, dict) else data.get("execution_started")
    if value is True or type(value) is str and value == "yes":
        return "yes"
    if value is False or type(value) is str and value == "no":
        return "no"
    return "unknown"


def normalize_ui_result(result, *, capability_id: str, run_id: str, step_idx: int):
    """Consume explicit producer receipts; an arbitrary PASS proves no boundary."""
    if not ui_write(capability_id):
        return result
    from mino_scout.schemas import EventStatus
    detail = dict(result.raw_response or {})
    boundary = started(detail)
    if str(capability_id or "") in PACKAGE_WRITES:
        # 只有「说不清有没有开始」才算未知；明确了失败的装包就是失败。
        unsafe = boundary == "unknown"
    else:
        unsafe = boundary == "unknown" or (result.status is not EventStatus.PASS and boundary != "no")
    if unsafe:
        result.status = EventStatus.BLOCKED
        result.summary = "写动作结果未核验，已停止后备执行并保留资源隔离"
        detail["execution_control"] = {
            **(detail.get("execution_control") if isinstance(detail.get("execution_control"), dict) else {}),
            "stop_run": True, "state": "write_stopped", "reason_code": "write_outcome_unknown",
            "stop_confirmed": False, "resource_isolated": True,
            "action_ref": {"run_id": run_id, "step_idx": step_idx},
        }
    receipt = detail.get("execution") if isinstance(detail.get("execution"), dict) else {}
    detail["execution"] = {**receipt, "execution_started": boundary,
        "phase": receipt.get("phase") or "executor",
        "reason_code": "write_outcome_unknown" if unsafe else receipt.get("reason_code", "")}
    result.raw_response = detail
    return result
