"""MinoNexus ⇄ MinoScout shared wire contract; stdlib only.

Contract: the synchronized Nexus/Scout protocol documents and this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Literal, Optional, get_args, get_origin

PROTOCOL_VERSION = 2


ExecutionMode = Literal["agent", "v5"]


def execution_mode(value: Any) -> ExecutionMode:
    """Validate the current product mode; never infer it from a contract version."""
    if not isinstance(value, str) or value not in ("agent", "v5"):
        raise ValueError("execution_mode_unsupported")
    return value


class MsgType(str, Enum):
    REGISTER = "REGISTER"
    REGISTERED = "REGISTERED"
    HEARTBEAT = "HEARTBEAT"
    EXECUTE = "EXECUTE"
    RESULT = "RESULT"


class EventStatus(str, Enum):
    """事件执行终态。取值与上游 MiniOrangeServer 的
    server/services/ai/regression/schemas.py::EventStatus 逐字一致（小写），
    以免搬迁时到处做名字翻译。

    **V7 合同：EXECUTE 只选定一个 executor，任何状态都结束本次尝试。**
    Scout 不做 fallback、不改序、不追加通道；换路是 Nexus 的决策。

    | status   | 语义 |
    |----------|------|
    | pass     | 做成了 |
    | blocked  | 需要人介入（Scout 不问人，上报 Nexus） |
    | declined | 我不适合做这条，让位 —— 不算故障 |
    | fail     | 真故障（设备报错、超时、元素找不到） |
    | skipped  | 本次未执行（前置不满足等） |

    `declined` 与 `fail` 的区别只在**是否算故障**（污染失败统计与归因），
    不在"要不要换通道"。
    """

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    DECLINED = "declined"


# 框架层 capability_id。N→S 是节点指令，S→N 是节点事件，形状与设备能力相同。
# Strategy is task metadata; actual point_policy remains node/coordinate.
OBSERVATION_STRATEGIES = ("visual", "dom_preferred")
OBSERVATION_CONTRACT_VERSION = 1

NODE_COMMAND_CAPS = frozenset({
    "node.stop", "node.restart", "node.update", "node.log_tail",
    "node.sleep", "node.wake",
    "node.plugin_install", "node.plugin_remove", "node.plugin_config",
    "node.config_export", "node.config_import",
    "node.tools", "node.tool_task_submit", "node.tool_task_get",
})
# 事件白名单已删除：Scout 发什么事件由发送侧决定，Nexus 收到后按自己的
# `resources/node_registry.py:_NODE_EVENT_HANDLERS` 查表处理；处理不了的回 FAIL。

OBSERVE_CAPS = {
    "screenshot": "screenshot",
    "hierarchy": "hierarchy",
    "app_version": "get_app_version",
    "foreground_app": "get_foreground_app",
    "get_app_version": "get_app_version",
    "get_foreground_app": "get_foreground_app",
}


# ---------------- 载荷 ----------------


@dataclass
class ExecutorManifest:
    id: str  # adb | remote | ios_wda | playwright
    available: bool
    provides: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class DeviceManifest:
    sn: str
    platform: str
    channels: dict[str, str] = field(default_factory=dict)
    model: str = ""
    os: str = ""
    session_key: str = ""
    session_generation: str = ""
    desktop_state: str = ""
    blocked_reason: str = ""
    capabilities: Optional[list[str]] = None
    readiness: Optional[dict[str, Any]] = None


@dataclass
class Register:
    node_id: str
    token: str
    platform: str
    scout_version: str
    account_isolation_version: int = 0
    protocol_version: int = PROTOCOL_VERSION
    arch: str = ""
    hostname: str = ""
    studio_id: str = ""
    executors: list[ExecutorManifest] = field(default_factory=list)
    devices: list[DeviceManifest] = field(default_factory=list)
    # None：旧 Scout 没报。空列表也会编码出去，用来和「没这个字段」区分。
    plugins: Optional[list[dict[str, Any]]] = None
    plugins_ready: Optional[bool] = None
    tool_tasks_version: int = 0  # explicit download-centre contract, not a version guess
    record_api_version: int = 0  # 1: durable script.submit and Scout-local record APIs
    plugin_warning: str = ""
    runtime_environment: str = ""
    # None：旧节点没报运行能力。不能用版本号猜测是否支持当前焦点输入。
    runtime_capabilities: Optional[list[dict[str, Any]]] = None
    host: dict[str, Any] = field(default_factory=dict)
    scout_process_generation: str = ""
    capability_revision: str = ""
    execution_binding_version: int = 0
    platform_contract_version: int = 0


@dataclass
class Registered:
    accepted: bool
    nexus_version: str
    protocol_version: int = PROTOCOL_VERSION
    session_token: str = ""
    heartbeat_interval_sec: int = 15
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    node_id: str = ""
    platform_contract_version: int = 0
    node_token: str = ""
    owner_user_id: str = ""
    node_connection_generation: str = ""


@dataclass
class DeviceWorkload:
    sn: str
    run_id: str = ""
    step_idx: int = -1
    capability_id: str = ""
    lanes_total: Optional[int] = None
    lanes_free: Optional[int] = None
    lanes_quarantined: Optional[int] = None


@dataclass
class Heartbeat:
    node_id: str
    uptime_sec: int
    busy: bool = False
    active_runs: list[str] = field(default_factory=list)
    device_delta: list[DeviceManifest] = field(default_factory=list)
    device_workload: list[DeviceWorkload] = field(default_factory=list)
    scout_version: str = ""
    host: dict[str, Any] = field(default_factory=dict)
    plugins: Optional[list[dict[str, Any]]] = None
    plugins_ready: Optional[bool] = None
    tool_tasks_version: int = 0  # explicit download-centre contract, not a version guess
    record_api_version: int = 0
    plugin_warning: str = ""
    runtime_environment: str = ""
    runtime_capabilities: Optional[list[dict[str, Any]]] = None
    executors: Optional[list[ExecutorManifest]] = None
    executor_snapshot_version: int = 0  # missing/unsupported; sender must explicitly set 1
    scout_process_generation: str = ""
    node_connection_generation: str = ""
    capability_revision: str = ""
    execution_binding_version: int = 0
    platform_contract_version: int = 0


@dataclass
class Execute:
    """能力调用与框架指令的统一请求。N→S 或 S→N。"""

    run_id: str
    step_idx: int
    sn: str
    capability_id: str
    params: dict[str, Any] = field(default_factory=dict)
    executor_order: list[str] = field(default_factory=list)  # device actions require exactly one selected executor
    low_level: dict[str, Any] = field(default_factory=dict)
    selected_impl: dict[str, Any] = field(default_factory=dict)
    device_hint: dict[str, Any] = field(default_factory=dict)
    timeout_sec: float = 30.0
    device_id: str = ""
    platform: str = ""  # mac | win | android | ios | web
    execution_contract_version: str = ""
    binding_id: str = ""
    binding_revision: int = 0
    input_contract_version: int = 0
    operation_ref: str = ""
    node_connection_generation: str = ""
    scout_process_generation: str = ""
    capability_revision: str = ""


@dataclass
class Attempt:
    executor: str
    status: EventStatus
    elapsed_ms: int = 0
    error: str = ""


@dataclass
class Result:
    """EXECUTE 的应答，靠信封的 reply_to 区分是哪一条。"""

    run_id: str
    status: EventStatus
    step_idx: int = -1
    summary: str = ""
    error: str = ""
    executor_used: str = ""
    source: str = ""
    elapsed_ms: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    image_base64: str = ""
    image_mime: str = ""
    width: int = 0
    height: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScriptRun:
    """脚本运行载荷。只含冻结字段，经现有 EXECUTE.params 往返，不是新的消息类型。

    Scout 自己按 repo_url / branch / revision 拉取脚本仓库；Nexus 不打包文件。
    """

    run_id: str
    session_id: str
    case_id: str
    sn: str
    platform: str
    repo_url: str
    branch: str = ""
    revision: str = ""
    script_path: str = ""
    task_id: str = ""
    attempt_id: str = ""
    owner_user_id: str = ""
    execution_timeout_sec: float = 600.0
    execution_kind: str = "case"
    workspace: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    source_context: dict[str, Any] = field(default_factory=dict)
    debug_origin: dict[str, Any] = field(default_factory=dict)


SCRIPT_RUN_CAPABILITY = "script.run"
SCRIPT_SUBMIT_CAPABILITY = "script.submit"
RECORD_API_VERSION = 1
RECORD_RESOURCE_STATUS_CAPABILITY = "record.resource_status"
RECORD_RESOURCE_STOP_CONTRACT = "script_resource_stop.v1"
RECORD_REPORT_VERSION = 1  # Optional bounded business projection in record.sync.
SCRIPT_RUN_FIELDS = ("run_id", "session_id", "case_id", "sn", "platform", "repo_url", "branch", "revision", "script_path",
                     "task_id", "attempt_id", "owner_user_id", "execution_timeout_sec", "inputs", "source_context", "execution_kind", "workspace", "debug_origin")
_SCRIPT_RUN_FIELDS = SCRIPT_RUN_FIELDS


def validate_runtime_data(value: Any, *, max_bytes: int = 16384) -> dict[str, Any]:
    """Portable bounded business values; never page handles or credentials."""
    import json
    if not isinstance(value, dict):
        raise ValueError("runtime_data_invalid")
    secret_keys = {"password", "passwd", "otp", "otp_code", "token", "api_key", "authorization", "cookie", "cookies", "access_token", "refresh_token", "secret"}
    def check(item, depth=0):
        if depth > 8:
            raise ValueError("runtime_data_too_deep")
        if isinstance(item, dict):
            for key, val in item.items():
                if not isinstance(key, str) or not key or len(key) > 128 or key.lower() in secret_keys or key.startswith("__"):
                    raise ValueError("runtime_data_key_invalid")
                check(val, depth + 1)
        elif isinstance(item, list):
            for val in item:
                check(val, depth + 1)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError("runtime_data_value_invalid")
    check(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("runtime_data_value_invalid") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("runtime_data_too_large")
    return json.loads(encoded)


def validate_case_inputs(value: Any) -> dict[str, Any]:
    data = validate_runtime_data({} if value is None else value, max_bytes=65536)
    if any(not isinstance(inputs, dict) for inputs in data.values()):
        raise ValueError("case_inputs_invalid")
    return {key: validate_runtime_data(inputs) for key, inputs in data.items()}


def validate_source_context(value: Any) -> dict[str, Any]:
    data = validate_runtime_data({} if value is None else value)
    if not data:
        return {}
    if set(data) - {"parent_task_id", "purpose", "data_need"} or data.get("purpose") not in {"prepare", "resume"}:
        raise ValueError("source_context_invalid")
    if not isinstance(data.get("parent_task_id"), str) or not data["parent_task_id"] or len(data["parent_task_id"]) > 255:
        raise ValueError("source_context_invalid")
    if not isinstance(data.get("data_need", ""), str) or len(data.get("data_need", "")) > 2000:
        raise ValueError("source_context_invalid")
    return data


def validate_data_summary(value: Any) -> dict[str, Any]:
    outputs = validate_runtime_data(value.get("data_outputs", {}))
    missing = value.get("data_missing", [])
    if not isinstance(missing, list) or len(missing) > 20:
        raise ValueError("data_missing_invalid")
    validate_runtime_data({"missing": missing})
    for need in missing:
        if not isinstance(need, dict) or set(need) - {"name", "description", "kind", "attributes"} or not isinstance(need.get("name"), str) or not need["name"] or not isinstance(need.get("description", ""), str) or not isinstance(need.get("kind", ""), str) or not isinstance(need.get("attributes", {}), dict):
            raise ValueError("data_missing_invalid")
    return {"data_outputs": outputs, "data_missing": missing}


def dump_script_run(payload: ScriptRun) -> dict[str, Any]:
    return _encode(payload)


def load_script_run(raw: dict[str, Any]) -> ScriptRun:
    """未知字段忽略。缺冻结字段则拒绝。"""
    if not isinstance(raw, dict):
        raise ValueError("script_run_invalid")
    known = {name: raw[name] for name in _SCRIPT_RUN_FIELDS if name in raw}
    if "inputs" in known:
        known["inputs"] = validate_runtime_data(known["inputs"])
    if "source_context" in known:
        known["source_context"] = validate_source_context(known["source_context"])
    try:
        body = _decode(ScriptRun, known)
    except (TypeError, ValueError) as exc:
        raise ValueError("script_run_invalid") from exc
    if not isinstance(body, ScriptRun):
        raise ValueError("script_run_invalid")
    from pathlib import PurePosixPath
    from urllib.parse import urlsplit
    import math

    if body.execution_kind not in {"case", "debug"}:
        raise ValueError("execution_kind_invalid")
    if not isinstance(body.workspace, str) or len(body.workspace) > 4096 or any(ord(c) < 32 for c in body.workspace):
        raise ValueError("workspace_invalid")
    if body.execution_kind == "debug" and (not body.workspace or body.repo_url or body.branch or body.revision):
        raise ValueError("debug_workspace_required")
    if body.execution_kind == "case" and body.workspace:
        raise ValueError("formal_workspace_managed")
    for key in ("run_id", "session_id", "case_id", "sn", "platform") + (("repo_url",) if body.execution_kind == "case" else ()):
        value = getattr(body, key)
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("script_run_incomplete")
    for key in ("branch", "revision"):
        value = getattr(body, key)
        if not isinstance(value, str) or value.startswith("-") or len(value) > 200 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("script_run_revision_invalid")
    if body.repo_url.startswith("-") or any(c.isspace() for c in body.repo_url):
        raise ValueError("script_run_repo_invalid")
    parsed = urlsplit(body.repo_url)
    if parsed.scheme in {"http", "https"} and (parsed.username is not None or parsed.password is not None):
        raise ValueError("script_run_repo_invalid")
    if not isinstance(body.script_path, str):
        raise ValueError("script_run_path_invalid")
    body.script_path = body.script_path.replace("\\", "/")
    if body.script_path and (body.script_path.startswith(("/", "~")) or ":" in body.script_path
                            or ".." in PurePosixPath(body.script_path).parts
                            or any(ord(c) < 32 for c in body.script_path) or not body.script_path.endswith(".py")):
        raise ValueError("script_run_path_invalid")
    for key in ("task_id", "attempt_id", "owner_user_id"):
        if not isinstance(getattr(body, key), str):
            raise ValueError("script_run_identity_invalid")
    try:
        budget = float(body.execution_timeout_sec)
    except (TypeError, ValueError) as exc:
        raise ValueError("script_run_timeout_invalid") from exc
    if not math.isfinite(budget) or not 1 <= budget <= 86400:
        raise ValueError("script_run_timeout_invalid")
    body.execution_timeout_sec = budget
    body.inputs = validate_runtime_data(body.inputs)
    body.source_context = validate_source_context(body.source_context)
    body.debug_origin = validate_debug_origin(body.debug_origin)
    return body


def validate_debug_origin(value: Any) -> dict[str, Any]:
    import re
    if value == {} or value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"session_id", "checkpoint_id", "script_path", "workspace_hash", "script_hash", "stage"}:
        raise ValueError("debug_origin_invalid")
    if any(not isinstance(v, str) or not v or len(v) > 4096 or any(ord(c) < 32 for c in v) for v in value.values()):
        raise ValueError("debug_origin_invalid")
    if value["stage"] != "path_validated" or any(not re.fullmatch(r"[0-9a-f]{64}", value[k]) for k in ("workspace_hash", "script_hash")):
        raise ValueError("debug_origin_unverified")
    return dict(value)


PAYLOAD_BY_TYPE: dict[MsgType, type] = {
    MsgType.REGISTER: Register,
    MsgType.REGISTERED: Registered,
    MsgType.HEARTBEAT: Heartbeat,
    MsgType.EXECUTE: Execute,
    MsgType.RESULT: Result,
}

# ACK 必须的消息（发送方超时未收到应答需按 docs/PROTOCOL.md §6 重试）
ACK_REQUIRED: frozenset[MsgType] = frozenset(
    {
        MsgType.REGISTER,
        MsgType.EXECUTE,
    }
)


# ---------------- 信封与编解码 ----------------


@dataclass
class Envelope:
    type: MsgType
    msg_id: str
    ts: str
    payload: Any
    v: int = PROTOCOL_VERSION
    reply_to: Optional[str] = None


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        out = {}
        for f in fields(value):
            raw = getattr(value, f.name)
            if f.name in ("data", "inputs", "source_context", "debug_origin") and not raw:
                continue
            if f.name in ("device_id", "platform", "node_id", "node_token") and not raw and not (isinstance(value, Register) and f.name == "platform"):
                continue
            if f.name == "host" and not raw:
                continue
            if f.name == "plugins" and raw is None:
                continue
            if f.name in ("runtime_capabilities", "capabilities", "readiness", "executors") and raw is None:
                continue
            if f.name in ("os", "session_key", "session_generation", "desktop_state", "blocked_reason") and not raw:
                continue
            out[f.name] = _encode(raw)
        return out
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


_HINT_CACHE: dict[type, dict[str, Any]] = {}


def _resolve_hints(cls: type) -> dict[str, Any]:
    """dataclass 的 f.type 在 `from __future__ import annotations` 下是字符串，这里解析成真类型。"""
    if cls not in _HINT_CACHE:
        import typing

        _HINT_CACHE[cls] = typing.get_type_hints(cls, globalns=globals())
    return _HINT_CACHE[cls]


def _decode(tp: Any, value: Any) -> Any:
    origin = get_origin(tp)
    if origin is list:
        (inner,) = get_args(tp) or (Any,)
        return [_decode(inner, v) for v in value or []]
    if origin is dict:
        return dict(value or {})
    args = get_args(tp)
    if origin is not None and type(None) in args:  # Optional[X]
        if value is None:
            return None
        return _decode(next(a for a in args if a is not type(None)), value)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if is_dataclass(tp):
        hints = _resolve_hints(tp)
        kwargs = {n: _decode(t, value[n]) for n, t in hints.items() if n in (value or {})}
        return tp(**kwargs)  # 未知字段按 §7 忽略
    return value


def dumps(env: Envelope) -> dict[str, Any]:
    """Envelope → 可 json.dumps 的 dict。"""
    return {
        "v": env.v,
        "type": env.type.value,
        "msg_id": env.msg_id,
        "reply_to": env.reply_to,
        "ts": env.ts,
        "payload": _encode(env.payload),
    }


def loads(raw: dict[str, Any]) -> Envelope:
    """dict → Envelope。未知 type 抛 UnknownMessageType，调用方应记 warn 并忽略（§7）。"""
    try:
        mtype = MsgType(raw["type"])
    except ValueError as exc:
        raise UnknownMessageType(raw.get("type")) from exc
    payload_cls = PAYLOAD_BY_TYPE[mtype]
    body = raw.get("payload") or {}
    hints = _resolve_hints(payload_cls)
    kwargs = {n: _decode(t, body[n]) for n, t in hints.items() if n in body}
    return Envelope(
        v=int(raw.get("v", PROTOCOL_VERSION)),
        type=mtype,
        msg_id=raw["msg_id"],
        reply_to=raw.get("reply_to"),
        ts=raw["ts"],
        payload=payload_cls(**kwargs),
    )


class UnknownMessageType(Exception):
    """收到未知 type。按 docs/PROTOCOL.md §7，接收方记 warn 并忽略，不可关连接。"""


@dataclass
class ImEnvelope:
    channel: str
    instance_id: str
    tenant: str
    chat_id: str
    chat_type: str
    sender_id: str
    msg_id: str
    text: str = ""
    mentioned: bool = False
    event_type: str = "message"
    reply_to: str = ""
    thread_id: str = ""
    target_message_id: str = ""
    revision: str = ""
    delivery_key: str = ""
    received_at: int = 0
    reply_ctx: dict[str, Any] = field(default_factory=dict)
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    callback: dict[str, Any] = field(default_factory=dict)
