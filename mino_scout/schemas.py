"""执行侧数据模型。

port(scout): 源自 MiniOrangeServer `server/services/ai/regression/schemas.py`，
**只搬 Scout 真正用到的三个**：`PlanEvent`、`EventResult`、`CapturedScreen`。

上游那个文件还有 `PlanResult` / `ReplanResult` / `AgentDecision` / `CaseSpec` /
`RunReport` 等等 —— 那些是 LLM 的输入输出和编排结果，归 Nexus。

`EventStatus` **不在这里定义**，从 `mino_scout.server_comm.protocol` import —— 一个定义、一处维护，
且 protocol.py 是零依赖的（守门脚本要用裸 python3 跑）。

字段与上游逐字一致（含 `extra="allow"`），这样搬来的 executor 代码不用改。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from mino_scout.server_comm.protocol import EventStatus

__all__ = ["EventStatus", "PlanEvent", "EventResult", "CapturedScreen"]


class PlanEvent(BaseModel):
    """一个待执行的动作。

    上游是「规划出的单个事件」，由 AI 产出。在拆分后的架构里它由 Nexus 从
    `EXECUTE` 载荷构造后下发 —— 对 Scout 而言它就是「这一步要做什么」。

    `extra="allow"` 是刻意保留的：协议 §7 规定接收方必须忽略不认识的字段，
    Nexus 加字段时 Scout 不该崩。
    """

    model_config = ConfigDict(extra="allow")

    seq: int = Field(..., description="本事件在 plan 中的执行顺序（1-based）")
    capability_id: str = Field(..., description="要执行的能力 id")
    event_kind: str = Field("", description="冗余字段（=capability.event_kind），便于 trace 检索")
    params: dict[str, Any] = Field(default_factory=dict, description="坐标为 0-1000 千分比")
    case_step_index: Optional[int] = Field(None, description="挂回哪条用例步骤；前置/收尾可为 null")
    needs_vlm: bool = Field(False, description="上游语义：是否需附截图给 VLM。Scout 只透传")
    expected_executor: str = Field("", description="Nexus 选定的的唯一 executor（与 executor_order[0] 一致）")
    ai_reasoning: str = Field("", description="Nexus 为什么选这条；Scout 原样回填进 EventResult")
    label: str = Field("", description="审计文案，例：『点击一键登录按钮』")

    # ---- 以下是拆分新增，上游没有 ----
    # Scout 不读能力目录，所以执行所需的一切由 Nexus 塞进来（CLAUDE.md §1 约束 3）
    executor_order: list[str] = Field(default_factory=list, description="唯一执行器；Scout 只执行这一个，不 fallback")
    low_level: dict[str, Any] = Field(default_factory=dict, description="抄自能力目录 YAML")
    selected_impl: dict[str, Any] = Field(default_factory=dict, description="impl 元数据")
    device_hint: dict[str, Any] = Field(default_factory=dict, description="含凭据，禁止入日志")


class EventResult(BaseModel):
    """单个动作执行后的结果。

    字段与上游逐字一致 —— Nexus 侧的 trace、覆盖度、时间线都按这个形状读。
    `status` 是五态（小写），语义见 docs/PROTOCOL.md §4.6.1。
    """

    model_config = ConfigDict(extra="allow")

    seq: int
    capability_id: str
    event_kind: str = ""
    lane: str = Field("", description="prep | step | expect：服务用例哪一列。Scout 原样透传")
    status: EventStatus
    executor_used: str = Field("", description="实际执行的 executor id。不可省略")
    elapsed_ms: int = 0
    summary: str = Field("", description="一句话总结，写入 UI / trace")
    error: str = Field("", description="非 pass 时的错误原文。不得含凭据")

    ai_reasoning: str = Field("", description="原 PlanEvent 的 reasoning，便于一并审计")
    plan_event: dict[str, Any] = Field(default_factory=dict, description="原 PlanEvent 序列化")

    vlm_meta: dict[str, Any] = Field(default_factory=dict, description="上游用于 LOCATE/ASSERT 细节")
    screenshot_path: str = Field("", description="若抓了截图，Scout 本地路径")
    thumb: str = Field("", description="上游在 router 里填。拆分后缩略图归 Nexus，Scout 留空")

    raw_response: dict[str, Any] = Field(default_factory=dict, description="执行器原始返回，debug 用")

    started_at: str = ""
    finished_at: str = ""

    # ---- 拆分新增：fallback 链的逐次记录，回填进 RESULT.attempts ----
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class CapturedScreen(BaseModel):
    """一帧屏幕。

    port(scout): 源自 `server/services/regression/screen.py` 的同名 dataclass，
    改成 pydantic 以便直接进 `RESULT` 载荷。字段名保持一致。
    """

    model_config = ConfigDict(extra="allow")

    ok: bool = False
    source: str = Field("", description="adb / remote / ios_wda / playwright / remote_cached")
    image_base64: str = ""
    image_mime: str = ""
    width: int = 0
    height: int = 0
    path: str = Field("", description="Scout 本地落盘路径，用后即删")
    error: str = ""
    elapsed_ms: int = 0
    # 字段名沿用协议（历史命名），与远程通道无关：存平台侧截图诊断。
    remote_detail: dict[str, Any] = Field(default_factory=dict)

    def has_image(self) -> bool:
        return bool(self.ok and self.image_base64)
