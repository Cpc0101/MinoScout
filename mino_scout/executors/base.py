"""Executor 接口与共享执行上下文。

port(scout): 源自 MiniOrangeServer `server/services/regression/executors/base.py`。

与上游的差异：
  - `ExecutorContext.run_context` 上游是 `RunContext` 对象（会查库、跑连通性探测）。
    Scout 不查库，改成 `device: DeviceRef` —— 由 `EXECUTE.device_hint` 构造。
  - 去掉 `case_brief`（喂 HITL composer / persona prompt 用的，那两个 executor 归 Nexus）。
  - `Executor.execute()` 的契约不变：**永不抛异常，返回五态之一。**
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable

from mino_scout.schemas import EventResult, EventStatus, PlanEvent


@dataclass
class DeviceRef:
    """执行一个动作需要知道的设备信息。全部来自 `EXECUTE` 载荷，Scout 不回查 Nexus。

    替代上游的 `RunContext` —— 后者会查 MDevice 表、跑 connectivity_probe，
    那些在拆分后分别归 Nexus（数据）和 Scout 的 probe/ 模块（探测）。
    """

    sn: str
    platform: str = ""          # android | ios | web | mac | win
    adb_serial: str = ""               # Android adb serial（device_hint 优先，缺省回退 sn）
    udid: str = ""                     # iOS
    password: str = ""                 # 锁屏密码。禁止入日志
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_web(self) -> bool:
        from mino_scout.platform.web.playwright_hub import is_web_slot

        return is_web_slot(self.sn, self.platform)

    @property
    def compatible_executors(self) -> frozenset[str]:
        """这台 sn 允许的通道。Web 只有 playwright，安卓/iOS 没有 playwright。"""
        if self.is_web:
            return frozenset({"playwright"})
        plat = str(self.platform or "").strip().lower()
        if plat == "mac":
            return frozenset({"mac"})
        if plat == "win":
            return frozenset({"win"})
        if plat == "android":
            return frozenset({"adb"})
        if plat == "ios":
            return frozenset({"ios_wda"})
        return frozenset()


@dataclass
class ExecutorContext:
    """传给每个 `Executor.execute()` 的上下文容器。"""

    device: DeviceRef
    run_id: str = ""
    step_idx: int = -1

    # 同一次派发内可被 executor 复用的 KV（如 write_stopped 回调）
    shared: dict[str, Any] = field(default_factory=dict)

    # Nexus 从能力目录抄来的 Implementation 元数据（dict 形态，避免依赖目录模型）
    selected_impl: Optional[dict[str, Any]] = None


@runtime_checkable
class Executor(Protocol):
    """所有设备执行通道的统一接口。"""

    id: str  # adb | ios_wda | playwright | mac | win

    def supports(self, capability_id: str, low_level: Optional[dict[str, Any]] = None) -> bool:
        """该 executor 是否处理这条 capability。

        Router 会用它做硬校验。必须便宜、纯判断、**不碰设备** —— 它只在这条
        请求被派发前调用一次（Scout 不做 fallback）。

        `low_level` 是 `EXECUTE` 载荷里 Nexus 抄来的 low_level 段。
        **这个参数是相对上游的一处有意改动**：上游 `AdbExecutor.supports()` 通过
        `plugin_registry.get_capability()` 回查能力目录，判断"这条 cap 虽然没有
        Python 分支，但声明了可执行的 adb low_level，所以我支持"。Scout 不读目录
        （CLAUDE.md §1 约束 3），所以改成由 Nexus 把 low_level 直接传进来。

        这条通路是「加 YAML 就多一个能力」的支撑（见 docs/EXECUTORS.md §3），
        丢了它就退化成"每个能力都要改 Python"。
        """
        ...

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        """跑这条事件，返回 EventResult。

        **必须捕获自身全部异常，永不抛。** 返回五态之一（见 docs/CONVENTIONS.md §2）：
          pass     做成了
          blocked  需要人介入 —— 结束本次尝试
          declined 我不适合做这条，让位 —— 不算故障
          fail     真故障
          skipped  本次未执行
        """
        ...


# ============== 共享辅助 ==============


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 上游叫 _now_iso（模块私有），搬来的代码可能引用它，保留别名
_now_iso = now_iso


def make_event_result(
    event: PlanEvent,
    *,
    status: EventStatus,
    executor_used: str,
    started_at: str,
    elapsed_ms: int,
    summary: str = "",
    error: str = "",
    vlm_meta: Optional[dict[str, Any]] = None,
    screenshot_path: str = "",
    raw_response: Optional[dict[str, Any]] = None,
    attempts: Optional[list[dict[str, Any]]] = None,
) -> EventResult:
    """统一构造 EventResult，避免每个 executor 重复填字段。

    字段填法与上游 `base.make_event_result` 一致，额外多一个 `attempts`
    （fallback 链记录，回填进协议的 `RESULT.attempts`）。
    """
    from mino_scout.runtime.secret_redaction import sanitize
    return EventResult(
        seq=event.seq,
        capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=status,
        executor_used=executor_used,
        elapsed_ms=elapsed_ms,
        summary=sanitize(summary),
        error=sanitize(error),
        ai_reasoning=event.ai_reasoning or "",
        plan_event=sanitize(event.model_dump(exclude_none=True)),
        vlm_meta=vlm_meta or {},
        screenshot_path=screenshot_path,
        raw_response=sanitize(raw_response or {}),
        started_at=started_at,
        finished_at=now_iso(),
        attempts=attempts or [],
    )
