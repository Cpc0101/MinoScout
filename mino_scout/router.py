"""执行 Nexus 明确选择的唯一执行器；任何结果均返回，不降级换路。"""
from __future__ import annotations

import time
from typing import Any, Optional

from mino_scout.executors.base import (
    DeviceRef,
    Executor,
    ExecutorContext,
    EventStatus,
    make_event_result,
    now_iso,
)
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.runtime.execution_boundary import normalize_ui_result, ui_write
from mino_scout.schemas import EventResult, PlanEvent

TAG = "Router"




class Router:
    """Scout 侧的动作分发。

    与上游 `CapabilityRouter` 的关键区别：**不做选路决策**。
    `executor_order` 由 Nexus 算好塞进 `EXECUTE` 载荷（CLAUDE.md §1 约束 3），
    Router 只执行唯一指定项，不自行改序、不自行追加或降级。
    """

    def __init__(self, executors: dict[str, Executor]):
        self.executors = executors

    def _canonical_executor(self, ex: str) -> str:
        """`desktop` 是 mac/win 的旧 executor id，只做入站兼容。"""
        if str(ex or "") == "desktop":
            for candidate in ("mac", "win"):
                if candidate in self.executors:
                    return candidate
        return str(ex or "")

    # ---------- 对外 ----------

    def dispatch(
        self,
        event: PlanEvent,
        device: DeviceRef,
        *,
        run_id: str = "",
        step_idx: int = -1,
        shared: Optional[dict[str, Any]] = None,
    ) -> EventResult:
        started = now_iso()
        t0 = time.time()

        order = [self._canonical_executor(ex) for ex in (event.executor_order or [])]
        if len(order) != 1:
            return self._synthetic_fail(event, "executor_order_must_select_one", started_at=started)
        impl = dict(event.selected_impl or {})
        impl_exec = self._canonical_executor(str(impl.get("executor") or ""))
        if impl_exec != order[0]:
            return self._synthetic_fail(event, "selected_impl_executor_mismatch", started_at=started)
        if order != list(event.executor_order or []) or impl_exec != impl.get("executor"):
            impl["executor"] = impl_exec
            event = event.model_copy(update={"executor_order": order, "selected_impl": impl})
        ordered = self._resolve_order(event)
        compatible = device.compatible_executors
        skipped = [ex for ex in ordered if ex not in compatible]
        if skipped:
            SLog.w(
                TAG,
                f"忽略与 sn={device.sn} platform={device.platform} 不符的 executor: {skipped}",
            )
        ordered = [ex for ex in ordered if ex in compatible]
        if not ordered:
            reason = (
                f"router: executor {skipped} 不服务 sn={device.sn} "
                f"（platform={device.platform}，该设备只接受 {sorted(compatible)}）"
                if skipped
                else self._no_route_reason(event)
            )
            return self._synthetic_fail(event, reason, started_at=started)

        ctx = ExecutorContext(
            device=device,
            run_id=run_id,
            step_idx=step_idx,
            shared=shared if shared is not None else {},
            selected_impl=event.selected_impl or None,
        )

        ex_id = ordered[0]
        stop_check = ctx.shared.get("write_stopped")
        if ui_write(event.capability_id) and callable(stop_check) and stop_check():
            result = make_event_result(event, status=EventStatus.BLOCKED, executor_used="router",
                started_at=started, elapsed_ms=int((time.time() - t0) * 1000),
                summary="运行已停止后续写动作，未执行本次动作",
                raw_response={"execution": {"execution_started": "no", "phase": "admission", "reason_code": "run_write_stopped"},
                              "execution_control": {"stop_run": True, "reason_code": "run_write_stopped", "resource_isolated": True}})
            result.attempts = []
            return result

        executor = self.executors[ex_id]  # _resolve_order 已保证存在且 supports
        a_t0 = time.time()
        try:
            result = executor.execute(event, ctx)
        except Exception as exc:
            # executor 契约是「永不抛」。真抛了说明它有 bug —— 兜住，
            # 缺少写边界证明时由 normalize_ui_result 收口为 blocked。
            SLog.e(TAG, f"executor {ex_id} 违反契约抛了异常 cap={event.capability_id}: {exc!r}")
            result = make_event_result(
                event,
                status=EventStatus.FAIL,
                executor_used=ex_id,
                started_at=started,
                elapsed_ms=int((time.time() - a_t0) * 1000),
                summary=f"executor {ex_id} raised (契约违规)",
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed = result.elapsed_ms or int((time.time() - t0) * 1000)
        result = normalize_ui_result(result, capability_id=event.capability_id,
            run_id=run_id, step_idx=step_idx)
        # 唯一一次尝试。attempts 保留为单元素，供 Nexus 的 trace 字段兼容。
        result.attempts = [{
            "executor": ex_id,
            "status": _status_value(result.status),
            "elapsed_ms": elapsed,
            "error": (result.error or "")[:200],
        }]
        result.elapsed_ms = elapsed
        return result

    # ---------- 内部 ----------

    def _resolve_order(self, event: PlanEvent) -> list[str]:
        """过滤出「本机确实注册了、且 supports 这条 cap」的 executor。

        Scout 只接受唯一执行器；这里保留列表形态只为把「未注册 / 不支持」
        与「设备类型不匹配」区分成不同的失败原因。**不排序、不补充。**
        """
        out: list[str] = []
        for ex_id in event.executor_order or []:
            if ex_id in out:
                continue
            executor = self.executors.get(ex_id)
            if executor is None:
                SLog.w(TAG, f"Nexus 指定了本机没有的 executor: {ex_id}")
                continue
            try:
                # low_level 一起传进去：没有 Python 分支但 Nexus 给了可执行的
                # low_level 声明时，executor 也应答"支持"（见 base.Executor.supports）
                if not executor.supports(event.capability_id, event.low_level or None):
                    continue
            except Exception as exc:  # supports() 也不该抛
                SLog.e(TAG, f"{ex_id}.supports() 抛异常: {exc!r}")
                continue
            out.append(ex_id)
        return out

    def _no_route_reason(self, event: PlanEvent) -> str:
        """把「无路可走」的原因说清楚 —— 这是排查 Nexus/Scout 不同步的第一现场。"""
        want = list(event.executor_order or [])
        if not want:
            return (
                f"router: EXECUTE 未携带 executor_order（cap={event.capability_id}）。"
                "Nexus 必须算好选路再下发，Scout 不自行推断"
            )
        have = sorted(self.executors)
        missing = [e for e in want if e not in self.executors]
        if missing:
            return (
                f"router: Nexus 指定的 executor {missing} 本机未注册"
                f"（cap={event.capability_id}，本机有 {have}）"
            )
        return (
            f"router: executor {want} 都不 supports cap={event.capability_id}"
            "（Nexus 的能力目录与 Scout 实现不同步？）"
        )

    def _synthetic_fail(self, event: PlanEvent, msg: str, *, started_at: str = "") -> EventResult:
        SLog.w(TAG, msg)
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used="router",
            started_at=started_at or now_iso(),
            elapsed_ms=0,
            summary=msg,
            error=msg,
            raw_response={"execution":{"execution_started":"no","phase":"admission","reason_code":"executor_route_invalid"}},
        )


def _status_value(status: Any) -> str:
    return status.value if isinstance(status, EventStatus) else str(status)
