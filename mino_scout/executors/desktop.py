"""One logical console slot, one persistent Cua SDK runtime, fail-closed readiness.

No window, monitor or browser creates another device. Cua owns platform health,
window capture, input delivery and immutable capture admission.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import math
import ntpath
import platform
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from typing import Any

from mino_scout.server_comm import protocol as P

SDK_VERSION = "0.34.1"
def host_caps() -> tuple[str, ...]:
    if platform.system() == "Windows":
        from mino_scout.platform.win.actions import CAPS
    else:
        from mino_scout.platform.mac.actions import CAPS
    return CAPS
_REASONS = {
    "ready": "",
    "no_session": "无图形桌面会话，请在已登录的桌面内启动 Scout",
    "locked": "桌面已锁定，请解锁后重试",
    "permission_required": "缺少录屏或辅助功能权限，请在系统设置中授权 Scout 后重新探测",
    "driver_missing": f"未安装 Cua Driver {SDK_VERSION} 桌面执行插件",
    "driver_incompatible": f"Cua Driver 版本不兼容，需要 {SDK_VERSION}",
    "unsupported": "当前系统或架构尚未验证 Desktop 执行支持",
    "probe_failed": "桌面探测失败，不能确认截图与输入可用",
}


class DesktopUnavailable(RuntimeError):
    def __init__(self, message: str, *, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.data = dict(data or {})


def host_session() -> dict[str, Any]:
    os_name = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system(), "unknown")
    arch = platform.machine().lower()
    supported = os_name in ("macos", "windows") and arch in ("arm64", "aarch64", "x86_64", "amd64")
    return {"os": os_name, "arch": arch, "hostname": platform.node(),
            "state": "probe_failed" if supported else "unsupported"}


def application_target(package: str, os_name: str) -> dict[str, str]:
    if os_name == "macos":
        from mino_scout.platform.mac.actions import application_target as build
        return build(package)
    if os_name == "windows":
        from mino_scout.platform.win.actions import application_target as build
        return build(package)
    raise DesktopUnavailable(_REASONS["unsupported"])


def application_matches(app: Any, package: str, os_name: str) -> bool:
    target = application_target(package, os_name)
    if "bundle_id" in target or "aumid" in target:
        actual = str(getattr(app, "bundle_id", "") or "")
        return actual == package if os_name == "macos" else actual.casefold() == package.casefold()
    actual = str(getattr(app, "launch_path", "") or "")
    return bool(actual and ntpath.normcase(ntpath.normpath(actual)) == ntpath.normcase(target["path"]))


def action_evidence(result: Any) -> dict[str, Any]:
    action = getattr(result, "action", None)
    if action is None and hasattr(result, "effect"):
        action = result
    record = public_record(action)
    if not isinstance(record, dict):
        try:
            record = json.loads(getattr(result, "structured_json", None) or getattr(result, "raw_json", None) or "{}")
        except (ValueError, TypeError):
            record = {}
    if not isinstance(record, dict):
        return {}
    record = record.get("action", record)
    if not isinstance(record, dict):
        return {}
    # Retain the evidence type, omit free-form details that can echo field contents.
    evidence = record.get("evidence")
    out = {"evidence": [{"kind": str(row.get("kind") or "")[:80], "detail_omitted": bool(row.get("detail"))}
                        for row in evidence[:16] if isinstance(row, dict)]} if isinstance(evidence, list) else {}
    out.update({name: record[name] for name in ("effect", "route") if isinstance(record.get(name), str)})
    if "route" not in out and isinstance(record.get("path"), str):
        out["route"] = record["path"][:80]
    for name, fields in (("delivery", ("mode", "delivered_count")), ("error", ("code",)), ("escalation", ("target", "reason"))):
        child = record.get(name)
        if isinstance(child, dict):
            out[name] = {field: child[field] for field in fields if isinstance(child.get(field), (str, int))}
    return out


def tool_payload(result: Any) -> dict[str, Any]:
    try:
        record = json.loads(getattr(result, "structured_json", None) or getattr(result, "raw_json", None) or "{}")
    except (ValueError, TypeError):
        return {}
    return record if isinstance(record, dict) else {}


def public_record(value: Any) -> Any:
    """Only SDK action evidence; callers must not include input text in diagnostics."""
    from enum import Enum
    if isinstance(value, Enum):
        return value.name.lower()
    if is_dataclass(value):
        return public_record(asdict(value))
    if isinstance(value, dict):
        return {k: public_record(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_record(v) for v in value]
    if hasattr(value, "__dict__"):
        return public_record(vars(value))
    return value


class DesktopRuntime:
    def __init__(self):
        self._focus = {}
        self._targets: dict[str, dict[str, Any]] = {}
        self._boot = uuid.uuid4().hex
        self._identity = ""
        self._epoch = 0
        self._generation = ""
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-desktop")
        self._lock = threading.RLock()
        self._driver = None
        self._loop = None
        self._disabled = False
        self._observations: dict[str, dict[str, Any]] = {}
        self._cancelled: set[str] = set()
        self._sessions: set[str] = set()
        self._worker_ident = None
        self._health_cache = None
        self._health_time = 0.0

    def _clear_focus(self, run_id=None):
        with self._lock:
            for rid in list(self._focus):
                if run_id is None or rid == run_id or rid.startswith(run_id + "::"):
                    self._focus.pop(rid, None)

    def _require_focus(self, run_id, params, obs):
        with self._lock:
            focus = self._focus.get(run_id)
            if not focus or params.get("focus_ref") != focus["ref"]:
                raise DesktopUnavailable("contract_unsupported: 焦点目标已变化或引用失效")
            if time.monotonic() - float(focus.get("created_at") or 0) > 15:
                self._focus.pop(run_id, None)
                raise DesktopUnavailable("焦点引用已过期，请在新截图中重新定位输入字段")
            if focus["target"] != obs["target"] or focus["window_size"] != obs["window_size"]:
                raise DesktopUnavailable("被测窗口或尺寸已变化，请在新截图中重新定位输入字段")
            return dict(focus)

    def state(self) -> dict[str, Any]:
        try:
            return self._submit(self._state, timeout=8)
        except Exception as exc:
            state = host_session()
            state.update(state="probe_failed", session_key="console", session_generation=self._generation,
                         blocked_reason=f"Cua 桌面健康检查失败（{type(exc).__name__}）；请检查节点诊断并重新探测")
            return state

    def _state(self) -> dict[str, Any]:
        state = host_session()
        if state["state"] != "unsupported":
            try:
                version = importlib.metadata.version("cua-driver")
                state["state"] = "ready" if version == SDK_VERSION else "driver_incompatible"
            except importlib.metadata.PackageNotFoundError:
                state["state"] = "driver_missing"
        if state["state"] == "ready":
            report = self._health()
            rows = {row.get("name"): row.get("status") for row in report.get("checks", []) if isinstance(row, dict)}
            if report.get("schema_version") != "1" or not rows:
                state["state"] = "probe_failed"
            elif report.get("driver_version") != SDK_VERSION or report.get("platform") != {"macos": "darwin", "windows": "win32"}[state["os"]]:
                state["state"] = "driver_incompatible"
            elif rows.get("binary_version") != "pass" or rows.get("platform_supported") != "pass":
                state["state"] = "driver_incompatible"
            elif rows.get("session_active") != "pass":
                state["state"] = "no_session"
            elif state["os"] == "macos" and any(rows.get(name) != "pass" for name in ("tcc_accessibility", "tcc_screen_recording")):
                state["state"] = "permission_required"
            elif rows.get("ax_capability") != "pass":
                state["state"] = "probe_failed"
            state["permissions"] = {key: rows[name] == "pass" for key, name in
                                    (("accessibility", "tcc_accessibility"), ("screen_capture", "tcc_screen_recording")) if name in rows}
            state["health_checks"] = rows
            state["health_source"] = "cua"
        with self._lock:
            identity = f"{state['os']}:{state['state']}"
            if identity != self._identity:
                self._identity = identity
                self._epoch += 1
                self._generation = hashlib.sha256(f"{self._boot}:{self._epoch}:{identity}".encode()).hexdigest()[:24]
                self._observations.clear()
                self._targets.clear()
                self._clear_focus()
            if self._disabled:
                state["state"] = "probe_failed"
                state["probe_error"] = "桌面健康检查超时且结果未知；请确认桌面状态后重启 Scout"
            return {**state, "session_key": "console", "session_generation": self._generation,
                    "blocked_reason": state.get("probe_error") or _REASONS[state["state"]]}

    def manifest(self, node_id: str) -> P.DeviceManifest | None:
        from mino_scout.runtime.platform_contract import host_os_from_system, native_sn
        target = host_os_from_system(platform.system())
        if target not in {"mac", "win"}:
            return None
        state = self.state()
        ready = state["state"] == "ready"
        return P.DeviceManifest(
            sn=native_sn(target, node_id), platform=target, model="Mac 桌面" if target == "mac" else "Windows 桌面",
            channels={target: "connected" if ready else "disconnected"},
            session_key="console", session_generation=state["session_generation"],
            desktop_state=state["state"], blocked_reason=state["blocked_reason"],
            capabilities=list(host_caps()) if ready else [],
            readiness={"permissions": state.get("permissions", {}), "arch": state["arch"],
                       "source": "cua", "checks": state.get("health_checks", {}), "capture_scope": "window"},
        )

    def host(self, node_id: str, dev: P.DeviceManifest | None = None) -> dict[str, Any]:
        dev = dev or self.manifest(node_id)
        from mino_scout.runtime.platform_contract import host_os_from_system
        system = host_os_from_system(platform.system())
        if dev is None:
            return {"os": system, "arch": platform.machine(), "hostname": platform.node(),
                    "desktop": {"state": "unsupported", "blocked_reason": _REASONS["unsupported"]}}
        return {"os": system, "arch": platform.machine(), "hostname": platform.node(),
                "desktop": {"state": dev.desktop_state, "blocked_reason": dev.blocked_reason,
                            "session_key": dev.session_key, "session_generation": dev.session_generation,
                            "readiness": dev.readiness}}

    def _submit(self, fn, *args, timeout=15):
        if threading.get_ident() == self._worker_ident:
            if self._disabled:
                raise DesktopUnavailable(_REASONS["probe_failed"])
            return fn(*args)
        with self._lock:
            if self._disabled:
                raise DesktopUnavailable(_REASONS["probe_failed"])
            future = self._pool.submit(self._run, fn, args)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            if future.cancel():
                raise DesktopUnavailable("桌面执行通道繁忙，请重新观察后重试") from exc
            with self._lock:
                # Do not reuse an uncertain native operation or reset its lane.
                self._disabled = True
                self._observations.clear()
                self._targets.clear()
                self._clear_focus()
            raise DesktopUnavailable("桌面操作超时，结果未知；请观察实际状态并重启 Scout") from exc

    def _run(self, fn, args):
        self._worker_ident = threading.get_ident()
        return fn(*args)

    def _sdk(self):
        if self._driver is None:
            import cua_driver as sdk
            self._sdk_module = sdk
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._driver = sdk.CuaDriver.create()
        return self._sdk_module, self._driver

    def _await(self, awaitable):
        try:
            return self._loop.run_until_complete(awaitable)
        except self._sdk_module.DriverError.Tool as exc:
            code = str(exc.error_code or "driver_refused")[:80]
            raise DesktopUnavailable(f"Cua 拒绝 {exc.tool}（{code}）；请重新观察并查看节点诊断", data={
                "cua_action": {"effect": "refused", "error": {"code": code}},
            }) from exc

    def _session(self, run_id):
        sdk, driver = self._sdk()
        if run_id not in self._sessions:
            self._await(driver.start_session(sdk.StartSessionInput(session=run_id, capture_scope=None, cursor_theme=None, cursor_motion=None)))
            self._sessions.add(run_id)
        return sdk, driver

    def _health(self):
        if self._health_cache is not None and time.monotonic() - self._health_time < 0.75:
            return self._health_cache
        _sdk, driver = self._sdk()
        result = self._await(driver.call_tool("health_report", json.dumps({"skip": ["screen_capture_capability"]})))
        if result.is_error:
            raise DesktopUnavailable("Cua 无法提供桌面健康状态")
        self._health_cache = tool_payload(result)
        self._health_time = time.monotonic()
        return self._health_cache

    def _check(self, hint: dict[str, Any], run_id: str, *, mutate=False):
        state = self.state()
        if state["state"] != "ready":
            raise DesktopUnavailable(state["blocked_reason"])
        if hint.get("os") not in (None, "", state["os"]) or hint.get("session_key") not in (None, "", "console"):
            raise DesktopUnavailable("PC 系统或桌面会话目标不匹配")
        if hint.get("session_generation") != state["session_generation"]:
            raise DesktopUnavailable("桌面会话已变化，旧任务引用失效，请重新创建任务")
        if run_id in self._cancelled or run_id.split("::", 1)[0] in self._cancelled:
            raise DesktopUnavailable("任务已取消")
        if mutate and (not run_id or hint.get("desktop_lease") != run_id.split("::", 1)[0]):
            raise DesktopUnavailable("桌面输入缺少任务独占租约")

    def _select_window(self, windows, pid, preferred=None):
        candidates = [window for window in windows if isinstance(window, dict)
                      and window.get("pid", pid) == pid and window.get("layer") in (None, 0)
                      and isinstance(window.get("window_id"), int) and not isinstance(window["window_id"], bool)
                      and window["window_id"] > 0]
        if preferred is not None:
            if not isinstance(preferred, int) or isinstance(preferred, bool) or preferred <= 0:
                raise DesktopUnavailable("window_id 必须为正整数")
            return next((window for window in candidates if window["window_id"] == preferred), None)
        visible = [window for window in candidates if window.get("is_on_screen") is True
                   and not window.get("minimized") and window.get("on_current_space") is not False]
        eligible = []
        for window in visible:
            bounds = window.get("bounds") if isinstance(window.get("bounds"), dict) else {}
            dimensions = (bounds.get("width"), bounds.get("height"))
            if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) and value >= 64 for value in dimensions):
                eligible.append(window)
        if len(eligible) == 1:
            return eligible[0]
        ranked = [window for window in eligible if isinstance(window.get("z_index"), int)
                  and not isinstance(window["z_index"], bool)]
        if ranked and len(ranked) == len(eligible):
            # Cua's z_index increases towards the front; missing ranks are ambiguous.
            first = max(window["z_index"] for window in ranked)
            frontmost = [window for window in ranked if window["z_index"] == first]
            if len(frontmost) == 1:
                return frontmost[0]
        if eligible:
            raise DesktopUnavailable("被测应用有多个无法唯一选择的窗口，请指定 window_id", data={
                "window_candidates": [window["window_id"] for window in eligible], "pid": pid,
            })
        return None

    def _resolve_target(self, sdk, driver, hint, run_id):
        package = str(hint.get("target_package") or "")
        os_name = self.state()["os"]
        application_target(package, os_name)
        apps = self._await(driver.list_apps(sdk.ListAppsInput()))
        matching = [app for app in apps.apps if app.running and app.pid > 0
                    and application_matches(app, package, os_name)]
        bound = self._targets.get(run_id)
        if bound and bound["package"] == package:
            if hint.get("window_id") is not None and hint["window_id"] != bound["target"]["window_id"]:
                raise DesktopUnavailable("指定窗口与本任务绑定窗口不一致，请重新执行 launch_app")
            if not any(app.pid == bound["target"]["pid"] for app in matching):
                self._targets.pop(run_id, None)
                self._clear_focus(run_id)
                raise DesktopUnavailable("被测应用进程已变化，请重新启动并绑定窗口")
            return bound["target"]
        if len(matching) != 1:
            raise DesktopUnavailable("尚未唯一确认被测应用进程，请先执行 launch_app")
        pid = matching[0].pid
        listed = self._await(driver.list_windows(sdk.ListWindowsInput(pid=pid, on_screen_only=False)))
        window = self._select_window([public_record(window) for window in listed.windows], pid, hint.get("window_id"))
        if window is None:
            raise DesktopUnavailable("被测应用尚无指定窗口，请先执行 launch_app")
        target = {"kind": "window", "pid": pid, "window_id": window["window_id"]}
        self._targets[run_id] = {"package": package, "target": target}
        self._clear_focus(run_id)
        return target

    def _capture_window(self, driver, run_id, target, max_dimension):
        result = self._await(driver.call_tool("get_window_state", json.dumps({
            "pid": target["pid"], "window_id": target["window_id"], "session": run_id,
            "include_accessibility_tree": False, "include_screenshot": True, "display_only": False,
            "max_image_dimension": max_dimension,
        })))
        if result.is_error or not result.images:
            raise DesktopUnavailable("Cua 指定窗口截图失败；不会回退为整屏截图")
        data = tool_payload(result)
        if data.get("pid") != target["pid"] or data.get("window_id") != target["window_id"]:
            raise DesktopUnavailable("Cua 截图窗口与被测窗口不一致")
        capture = data.get("capture_id")
        if not isinstance(capture, str) or not capture or data.get("screenshot_frame_valid") is False:
            raise DesktopUnavailable("Cua 未返回有效窗口捕获引用，请重新观察")
        image = result.images[0]
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(base64.b64decode(image.data_base64))) as captured:
            width, height = captured.size
        if data.get("screenshot_width") != width or data.get("screenshot_height") != height:
            raise DesktopUnavailable("Cua 窗口图像尺寸与捕获元数据不一致")
        if width <= 0 or height <= 0:
            raise DesktopUnavailable("Cua 窗口截图为空")
        bounds = data.get("window_bounds") or {}
        size = (bounds.get("width"), bounds.get("height"))
        if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in size):
            raise DesktopUnavailable("Cua 未确认窗口尺寸，请重新观察")
        return {"capture_id": capture, "snapshot_id": str(data.get("snapshot_id") or ""),
                "width": width, "height": height, "image_base64": image.data_base64,
                "image_mime": image.mime_type, "target": dict(target), "window_size": size,
                "window_bounds": bounds, "screenshot_scale": data.get("screenshot_scale")}

    def observe(self, hint: dict[str, Any], run_id: str, compress_ratio: float = 1.0) -> dict[str, Any]:
        return self._submit(self._observe, hint, run_id, compress_ratio)

    def _observe(self, hint, run_id, compress_ratio):
        self._observations.pop(run_id, None)
        self._check(hint, run_id)
        if not math.isfinite(compress_ratio) or not 1.0 <= compress_ratio <= 10.0:
            raise DesktopUnavailable("PC 压缩比必须在 1 到 10 之间")
        sdk, driver = self._session(run_id or "mino-observe")
        target = self._resolve_target(sdk, driver, hint, run_id)
        obs = self._capture_window(driver, run_id, target, 0)
        native_width, native_height = obs["width"], obs["height"]
        if compress_ratio > 1.0:
            self._check(hint, run_id)
            obs = self._capture_window(driver, run_id, target, max(1, round(max(native_width, native_height) / compress_ratio)))
        self._check(hint, run_id)
        obs.update(session_generation=hint["session_generation"], capture_scope="window", pid=target["pid"],
                   window_id=target["window_id"], native_width=native_width, native_height=native_height,
                   compress_ratio=compress_ratio)
        self._observations[run_id] = obs
        return dict(obs)

    def action(self, capability: str, params: dict[str, Any], hint: dict[str, Any], run_id: str):
        return self._submit(self._action, capability, params, hint, run_id,
                            timeout=25 if capability == "launch_app" else 15)

    def _launch_failure(self, message, result, hint, obs, *, stage, package, pid=0, diagnostics=None):
        payload = tool_payload(result)
        evidence = action_evidence(result)
        if not evidence.get("effect"):
            evidence["effect"] = "partial" if payload.get("request_accepted") else "unverifiable"
        code = str(getattr(result, "error_code", None) or payload.get("code") or payload.get("error") or stage)[:80]
        evidence["error"] = {"code": code}
        return DesktopUnavailable(message, data={
            "cua_action": evidence, "capture_id": (obs or {}).get("capture_id", ""),
            "session_generation": hint["session_generation"],
            "launch": {**(diagnostics or {}), "stage": stage, "target_package": package, "pid": pid},
        })

    def _active_apps(self, sdk, driver, os_name):
        apps = self._await(driver.list_apps(sdk.ListAppsInput()))
        active = [app for app in apps.apps if app.active]
        if len(active) != 1 or not (active[0].bundle_id or active[0].launch_path):
            raise DesktopUnavailable("Cua 未唯一确认当前前台应用，请重新观察", data={
                "foreground_source": "cua", "foreground_error": "identity_unavailable",
            })
        return active

    def _launch_app(self, sdk, driver, params, hint, run_id, obs, os_name):
        package = str(hint.get("target_package") or "")
        if not package or str(params.get("package") or package) != package:
            raise DesktopUnavailable("请配置被测 PC 应用标识，不能切换到其他应用")
        diagnostics = {"launched": False, "activated": False, "foreground_verified": False,
                       "foreground_source": "not_required"}
        stage, pid, result = "launch_app", 0, None
        try:
            existing = self._targets.get(run_id)
            launched = {}
            apps = self._await(driver.list_apps(sdk.ListAppsInput()))
            if existing and existing["package"] == package and any(
                app.running and app.pid == existing["target"]["pid"]
                and application_matches(app, package, os_name) for app in apps.apps
            ):
                pid = existing["target"]["pid"]
            else:
                existing = None
                result = self._await(driver.call_tool("launch_app", json.dumps(application_target(package, os_name))))
                if result.is_error:
                    raise DesktopUnavailable("Cua 无法启动被测应用，请查看启动诊断")
                launched = tool_payload(result)
                stage = "process_not_ready"
                value = launched.get("pid")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise DesktopUnavailable("Cua 未确认被测应用进程，请回看实际状态")
                pid = value
            diagnostics["launched"] = True
            self._check(hint, run_id, mutate=True)
            target_params = {"pid": pid}
            stage = "window_not_ready"
            windows = launched.get("windows") if isinstance(launched.get("windows"), list) else []
            preferred = params.get("window_id", hint.get("window_id"))
            if hint.get("window_id") is not None and preferred != hint["window_id"]:
                raise DesktopUnavailable("启动请求与任务指定的 window_id 不一致")
            if preferred is None and existing:
                preferred = existing["target"]["window_id"]
            deadline = time.monotonic() + 3.0
            while True:
                window = self._select_window(windows, pid, preferred)
                if window is not None:
                    break
                if time.monotonic() >= deadline:
                    raise DesktopUnavailable("被测 PC 应用尚无指定的可绑定窗口，请回看实际状态")
                self._check(hint, run_id, mutate=True)
                listed = self._await(driver.list_windows(sdk.ListWindowsInput(pid=pid, on_screen_only=False)))
                windows = [public_record(window) for window in listed.windows]
                self._await(asyncio.sleep(0.1))
            target_params["window_id"] = window["window_id"]
            diagnostics["window_id"] = window["window_id"]
            diagnostics["window_bounds"] = {
                key: value for key, value in (window.get("bounds") or {}).items()
                if key in ("x", "y", "width", "height") and isinstance(value, (int, float))
            }
            self._check(hint, run_id, mutate=True)
            diagnostics["window_verified"] = True
            target = {"kind": "window", **target_params}
            self._targets[run_id] = {"package": package, "target": target}
            self._clear_focus(run_id)
            return {
                "execution": {"execution_started": "no" if existing else "yes", "phase": "completed",
                              "source": "desktop.cua.v1"},
                "cua_action": {"effect": "confirmed", "route": "reuse_running_app" if existing else "launch_app",
                               "evidence": [{"kind": "window_identity", "detail_omitted": False}]},
                "capture_id": (obs or {}).get("capture_id", ""), "session_generation": hint["session_generation"],
                "target": target, "capture_scope": "window",
                "launch": {**diagnostics, "stage": "window_ready", "target_package": package, **target_params},
            }
        except DesktopUnavailable as exc:
            diagnostics.update({key: exc.data[key] for key in ("foreground_pid", "foreground_error") if key in exc.data})
            if isinstance(exc.data.get("window_candidates"), list):
                diagnostics["window_candidates"] = exc.data["window_candidates"][:32]
            raise self._launch_failure(str(exc), result, hint, obs, stage=stage, package=package,
                                       pid=pid, diagnostics=diagnostics) from exc
        except Exception as exc:
            diagnostics["error_type"] = type(exc).__name__
            raise self._launch_failure("Cua 启动或激活被测应用失败，请查看分段诊断", result, hint, obs,
                                       stage=stage, package=package, pid=pid, diagnostics=diagnostics) from exc

    def _action(self, capability, params, hint, run_id):
        if capability == "kill_app":
            capability = "close_app"
        self._check(hint, run_id, mutate=capability not in ("get_foreground_app", "close_app"))
        os_name = self.state()["os"]
        sdk, driver = self._session(run_id)
        if capability == "close_app":
            package = str(params.get("package") or hint.get("target_package") or "").strip()
            active = self._active_apps(sdk, driver, os_name)
            targets = [a for a in active if not package or application_matches(a, package, os_name)]
            if not targets:
                return {"execution": {"execution_started": "yes", "phase": "completed", "source": "desktop.cua.v1"},
                        "summary": "close_app: 目标应用未在前台", "session_generation": hint.get("session_generation")}
            pid = int(getattr(targets[0], "pid", 0) or 0)
            if pid <= 0:
                raise DesktopUnavailable("close_app 无法解析被测进程")
            result = self._await(driver.call_tool("terminate_app", json.dumps({"pid": pid, "session": run_id})))
            if getattr(result, "is_error", False):
                raise DesktopUnavailable("Cua 未能终止被测应用")
            self._clear_focus(run_id)
            return {"execution": {"execution_started": "yes", "phase": "completed", "source": "desktop.cua.v1"},
                    "summary": f"close_app pid={pid}", "session_generation": hint.get("session_generation")}
        if capability == "get_foreground_app":
            active = self._active_apps(sdk, driver, os_name)
            package = str(hint.get("target_package") or "")
            return {"apps": [{**public_record(a), "active": True} for a in active],
                    "target_active": bool(package and any(application_matches(a, package, os_name) for a in active)),
                    "foreground_source": "cua",
                    "foreground_pid": int(getattr(active[0], "pid", 0) or 0) if active else 0,
                    "foreground_package": str(active[0].bundle_id or active[0].launch_path or "") if active else ""}
        if capability == "exec_script":
            try:
                timeout_ms = int(params.get("timeout_ms") or 30000)
            except (TypeError, ValueError):
                timeout_ms = 30000
            if os_name == "macos":
                from mino_scout.platform.mac.actions import run_command
            else:
                from mino_scout.platform.win.actions import run_command
            outcome = run_command(str(params.get("script") or ""), timeout_ms=timeout_ms)
            return {"execution": {"execution_started": "yes", "phase": "completed", "source": "desktop.cua.v1"},
                    "summary": "已执行脚本", "stdout": outcome.get("stdout") or "",
                    "session_generation": hint.get("session_generation")}
        if capability == "set_clipboard":
            text = str(params.get("text") or "")
            if not text:
                raise DesktopUnavailable("set_clipboard 缺 text")
            if os_name == "macos":
                from mino_scout.platform.mac.actions import write_clipboard
            else:
                from mino_scout.platform.win.actions import write_clipboard
            write_clipboard(self, sdk, driver, run_id, text)
            return {"execution": {"execution_started": "yes", "phase": "completed", "source": "desktop.cua.v1"},
                    "summary": f"剪贴板写入 {len(text)} 字", "session_generation": hint.get("session_generation")}
        obs = self._observations.pop(run_id, None)
        if capability != "input_text":
            self._clear_focus(run_id)
        if capability not in {"launch_app", "install", "install_apk", "install_app", "hierarchy"} and (not obs or obs["session_generation"] != hint["session_generation"]):
            raise DesktopUnavailable("桌面动作需要重新截图，旧捕获引用已消费或失效")
        if capability in {"install", "install_apk", "install_app"}:
            path = str(params.get("path") or "")
            if os_name == "macos":
                from mino_scout.platform.mac.actions import install as install_app
            else:
                from mino_scout.platform.win.actions import install as install_app
            summary = install_app(path)
            return {"execution": {"execution_started": "yes", "phase": "completed", "source": "desktop.cua.v1"},
                    "summary": summary, "session_generation": hint.get("session_generation")}
        if capability == "launch_app":
            if os_name == "macos":
                from mino_scout.platform.mac.actions import launch
            else:
                from mino_scout.platform.win.actions import launch
            return launch(self, sdk, driver, params, hint, run_id, obs)
        if capability == "hierarchy":
            if os_name == "macos":
                from mino_scout.platform.mac.actions import hierarchy as read_tree
            else:
                from mino_scout.platform.win.actions import hierarchy as read_tree
            return read_tree(self, hint, run_id)
        bound = self._resolve_target(sdk, driver, hint, run_id)
        if obs["target"] != bound:
            raise DesktopUnavailable("截图与当前被测窗口不一致，请重新观察")
        target = sdk.ActionTarget.WINDOW(pid=bound["pid"], window_id=bound["window_id"])
        self._check(hint, run_id, mutate=True)
        if capability in ("tap_xy", "tap_element"):
            if os_name == "macos":
                from mino_scout.platform.mac.actions import tap as tap_at
            else:
                from mino_scout.platform.win.actions import tap as tap_at
            result = tap_at(self, sdk, driver, params, run_id, obs, bound)
        elif capability in ("swipe_direction", "swipe_element_to_element", "long_press_element"):
            if os_name == "macos":
                from mino_scout.platform.mac.actions import gesture
            else:
                from mino_scout.platform.win.actions import gesture
            result = gesture(self, sdk, driver, capability, params, run_id, obs, bound)
        elif capability == "input_text":
            if str(params.get("target_mode") or "current_focus") != "current_focus":
                raise DesktopUnavailable("Desktop 输入仅支持 current_focus")
            mode = str(params.get("mode") or "replace")
            if mode not in ("replace", "append"):
                raise DesktopUnavailable("Desktop 输入 mode 必须是 replace 或 append")
            self._require_focus(run_id, params, obs)
            try:
                self._check(hint, run_id, mutate=True)
                if os_name == "macos":
                    from mino_scout.platform.mac.actions import focus_chord
                else:
                    from mino_scout.platform.win.actions import focus_chord
                key, modifiers = focus_chord(mode)
                selected = self._await(driver.call_tool("press_key", json.dumps({
                    "target": bound, "session": run_id, "delivery_mode": "foreground",
                    "key": key, "modifiers": modifiers,
                })))
                select_evidence = action_evidence(selected)
                if selected.is_error or select_evidence.get("effect") in ("refused", "partial", "suspected_noop"):
                    raise DesktopUnavailable("Cua 未确认输入选中或定位动作；未发送输入正文")
                self._check(hint, run_id, mutate=True)
                result = self._await(driver.call_tool("type_text", json.dumps({
                    "target": bound, "session": run_id, "delivery_mode": "foreground", "text": str(params.get("text") or ""),
                })))
            finally:
                self._clear_focus(run_id)
        elif capability == "press_key":
            if os_name == "macos":
                from mino_scout.platform.mac.actions import key_input as host_key
            else:
                from mino_scout.platform.win.actions import key_input as host_key
            key, modifiers = host_key(params)
            result = self._await(driver.call_tool("press_key", json.dumps({
                "target": bound, "session": run_id, "delivery_mode": "foreground", "key": key,
                "modifiers": modifiers,
            })))
        else:
            raise DesktopUnavailable("Desktop 未实现这项能力")
        evidence = action_evidence(result)
        if not evidence.get("effect"):
            evidence["effect"] = "unverifiable"
        if capability in ("tap_xy", "tap_element"):
            self._clear_focus(run_id)
            ref = str(params.get("focus_ref") or "")
            if ref.startswith(f"focus:{run_id}:") and ref.rsplit(":", 1)[-1].isdigit() and not getattr(result, "is_error", False) and evidence.get("effect") in ("confirmed", "unverifiable"):
                self._focus[run_id] = {"ref": ref, "target": dict(bound), "window_size": obs["window_size"], "created_at": time.monotonic(),
                                       "x": float(params["x"]), "y": float(params["y"])}
        elif capability in ("press_key", "launch_app"):
            self._clear_focus(run_id)
        if getattr(result, "is_error", False):
            evidence["effect"] = "refused"
        # This receipt is emitted only after the actual driver call returned.
        # Timeout/exception paths retain unknown; an aggregate PASS never invents it.
        return {"execution": {"execution_started": "yes", "phase": "completed", "source": "desktop.cua.v1"},
                "cua_action": evidence, "capture_id": obs["capture_id"], "snapshot_id": obs["snapshot_id"],
                "session_generation": hint["session_generation"], "target": dict(bound), "capture_scope": "window"}

    def cancel(self, run_id: str):
        # Cancellation prevents any newly admitted input, including delayed retries.
        with self._lock:
            for rid in list(self._observations):
                if rid == run_id or rid.startswith(run_id + "::"):
                    self._observations.pop(rid, None)
                    self._cancelled.add(rid)
            for rid in list(self._targets):
                if rid == run_id or rid.startswith(run_id + "::"):
                    self._targets.pop(rid, None)
            self._cancelled.add(run_id)
            self._clear_focus(run_id)
            if not self._disabled:
                self._pool.submit(self._end_run, run_id)

    def _end_run(self, run_id: str):
        with self._lock:
            for rid in list(self._targets):
                if rid == run_id or rid.startswith(run_id + "::"):
                    self._targets.pop(rid, None)
        if self._driver is None:
            return
        for session in list(self._sessions):
            if session == run_id or session.startswith(run_id + "::"):
                try:
                    self._await(self._driver.end_session(self._sdk_module.EndSessionInput(session=session)))
                finally:
                    self._sessions.discard(session)

    def shutdown(self):
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._closed = True
            self._disabled = True
            self._observations.clear()
            self._targets.clear()
            self._clear_focus()
            closing = self._pool.submit(self._close_sdk)
        try:
            closing.result(timeout=5)
        except Exception:
            pass  # A stuck native lane is never restarted or reused.
        finally:
            self._pool.shutdown(wait=False)

    def _close_sdk(self):
        if self._driver is not None:
            self._await(self._driver.shutdown())
            self._sessions.clear()
            self._driver = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None


_runtime: DesktopRuntime | None = None
_runtime_lock = threading.Lock()


def get_desktop_runtime() -> DesktopRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = DesktopRuntime()
        return _runtime


class DesktopExecutor:
    """macOS / Windows 原生执行器。会话在本模块，动作在各自的 platform 里。

    `id` 就是宿主平台（`mac` / `win`），不再是历史名 `desktop`。
    """

    provides = list(host_caps())

    def __init__(self):
        import platform as _system
        from mino_scout.runtime.platform_contract import host_os_from_system
        self.id = host_os_from_system(_system.system())

    def probe(self):
        state = get_desktop_runtime().state()
        return state["state"] == "ready", state["blocked_reason"]

    def supports(self, capability_id, low_level=None):
        del low_level
        return capability_id in self.provides

    def execute(self, event, ctx):
        import time

        from mino_scout.executors.base import make_event_result, now_iso
        from mino_scout.schemas import EventStatus
        from mino_scout.tools.keep_alive.log import SLog

        started, tick = now_iso(), time.monotonic()
        status, error, data = EventStatus.PASS, "", {}
        try:
            if ctx.device.platform not in {"mac", "win"}:
                raise DesktopUnavailable("不是 macOS / Windows 原生设备")
            data = get_desktop_runtime().action(event.capability_id, dict(event.params or {}), ctx.device.extra, ctx.run_id)
            effect = (data.get("cua_action") or {}).get("effect")
            if effect == "refused":
                status, error = EventStatus.BLOCKED, "桌面驱动拒绝操作，请查看 Session Log 中的动作证据"
            elif effect in ("suspected_noop", "partial"):
                status, error = EventStatus.FAIL, f"桌面动作效果：{effect}，需要重新观察"
            data["verification_required"] = effect in ("unverifiable", "partial", "suspected_noop")
        except DesktopUnavailable as exc:
            status, error = EventStatus.BLOCKED, str(exc)
            data = exc.data
        except Exception as exc:
            SLog.e("DesktopExecutor", f"run={ctx.run_id} cap={event.capability_id} error={type(exc).__name__}")
            status, error = EventStatus.FAIL, "Cua 桌面执行失败，请查看节点日志并重新观察"
        if event.capability_id == "get_foreground_app":
            summary = "前台应用匹配被测应用" if data.get("target_active") is True else "前台应用未匹配被测应用"
        elif event.capability_id == "input_text":
            summary = "已写入当前焦点；输入效果待下一张截图确认"
        elif event.capability_id == "set_clipboard":
            summary = str(data.get("summary") or "已写入剪贴板")
        else:
            summary = str(data.get("summary") or "") or "桌面动作已投递；业务结果待下一张截图确认"
        return make_event_result(event, status=status, executor_used=self.id, started_at=started,
                                 elapsed_ms=int((time.monotonic() - tick) * 1000),
                                 summary=error or summary, error=error, raw_response=data)
