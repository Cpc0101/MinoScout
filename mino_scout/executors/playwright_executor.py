# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Web 执行通道：Playwright，和 AdbExecutor 平级。禁止 page.evaluate 改界面。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from mino_scout.tools.keep_alive.log import SLog

from mino_scout.schemas import EventResult, EventStatus, PlanEvent
from mino_scout.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from mino_scout.platform.web.playwright_hub import get_hub, goto_url, headed_from_hint, pick_goto_url
from mino_scout.platform.web.playwright_context import resolve_locator_scope
from mino_scout.platform.web.playwright_coords import resolve_viewport_xy, to_viewport_xy
from mino_scout.platform.web.playwright_locators import (
    css_selector_from_params,
    exact_visible_pattern,
    locator_from_params,
    unique_exact_text,
)
from mino_scout.platform.web.viewports import DEFAULT_WEB_RESOLUTION, web_viewport
from mino_scout.platform.web.web_focus import locate_editable_at
from mino_scout.platform.web.web_keyboard import normalize_key, normalize_key_value
from mino_scout.platform.web.web_url import (
    parse_http_url,
    resolve_uri,
    same_entry_url,
    validate_page_url,
)

TAG = "PlaywrightExecutor"
_DEFAULT_WEB_VIEWPORT = web_viewport(DEFAULT_WEB_RESOLUTION)

_CSRF_NAME = re.compile(r"csrf|xsrf", re.I)
_AUTH_TOKEN_NAME = re.compile(
    r"^(access_token|refresh_token|id_token|auth_token|authorization)$",
    re.I,
)


def _host_of(url: str) -> str:
    try:
        parsed, _ = parse_http_url(str(url or ""))
    except ValueError:
        return ""
    return str(parsed.hostname or "").lower().rstrip(".")


def _mouse_drag(page: Any, start: tuple[float, float], end: tuple[float, float], *,
                duration_ms: int = 500, steps: int = 0) -> None:
    """Drag with monotonic pacing and release the mouse on every exit path."""
    if type(duration_ms) is not int or not 0 <= duration_ms <= 120_000:
        raise ValueError("drag_duration_invalid")
    if type(steps) is not int or not 0 <= steps <= 500:
        raise ValueError("drag_steps_invalid")
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    page.mouse.move(sx, sy)
    page.mouse.down()
    try:
        started = time.monotonic()
        if steps == 0:
            if duration_ms:
                time.sleep(duration_ms / 1000.0)
            page.mouse.move(ex, ey)
        else:
            for index in range(1, steps + 1):
                ratio = index / steps
                remaining = duration_ms / 1000.0 - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining / (steps - index + 1))
                page.mouse.move(sx + (ex - sx) * ratio, sy + (ey - sy) * ratio)
    except BaseException:
        try:
            page.mouse.up()
        except BaseException:
            # Preserve cancellation and the original drag failure.
            pass
        raise
    page.mouse.up()


def _hosts_related(left: str, right: str) -> bool:
    a = str(left or "").lower().rstrip(".")
    b = str(right or "").lower().rstrip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _cookie_for_host(domain: str, host: str) -> bool:
    d = str(domain or "").lstrip(".").lower().rstrip(".")
    return _hosts_related(d, host)


def _web_auth_evidence(name: str, value: str) -> str:
    """当前站点上的 JWT 或明确令牌名才算凭据。普通 session/sid/csrf 不算。"""
    key = str(name or "")
    raw = str(value or "")
    if not raw or _CSRF_NAME.search(key):
        return ""
    parts = raw.split(".")
    if len(parts) == 3 and parts[0].startswith("eyJ") and all(parts) and len(raw) >= 20:
        return "jwt"
    if _AUTH_TOKEN_NAME.fullmatch(key) and len(raw) >= 24:
        return "auth_token"
    return ""

_SUPPORTED_CAPS: set[str] = {
    "launch_app",
    "close_app",
    "kill_app",
    "press_key",
    "key_down",
    "key_up",
    "tap_element",
    "tap_xy",
    "exec_script",
    "long_press_element",
    "input_text",
    "swipe_direction",
    "swipe_element_to_element",
    "get_foreground_app",
    "read_web_auth",
    "wait_screen_ready",
    "switch_tab",
    "open_tab",
    "upload_file",
    "reload_page",
    "set_clipboard",
    "network_expect_register",
    "network_expect_wait",
    "network_expect_cancel",
    "read_browser_errors",
    "read_web_environment",
    "read_clipboard",
    "assert_clipboard",
    "focus_element",
    "check_focused",
    "read_focused",
    "forward_page",
    "navigate_page",
    "read_url",
    "drag_control",
    "set_geolocation",
    "set_web_permission",
    "set_page_time",
}


def _web_identity(event: PlanEvent, ctx: ExecutorContext) -> tuple[str, str, str]:
    """Read the same explicit identity from event params or device attribution."""
    from mino_scout.platform.web.playwright_hub import normalize_web_identity

    params = event.params or {}
    extra = ctx.device.extra or {}
    nested = extra.get("_web_identity")
    sources = (params, nested if isinstance(nested, dict) else {}, extra)
    actor = context_id = page_id = ""
    for source in sources:
        if not isinstance(source, dict):
            continue
        actor = actor or str(source.get("actor") or "")
        context_id = context_id or str(source.get("context_id") or source.get("context") or "")
        page_id = page_id or str(source.get("page_id") or source.get("page") or "")
    return normalize_web_identity(actor, context_id, page_id)


def _identity_kwargs(identity: tuple[str, str, str]) -> dict[str, str]:
    actor, context_id, page_id = identity
    return {"actor": actor, "context_id": context_id, "page_id": page_id}


def _clipboard_evidence(value: str) -> dict[str, Any]:
    raw = str(value or "").encode("utf-8")
    return {"length": len(str(value or "")), "sha256": hashlib.sha256(raw).hexdigest()}


def _input_summary_snippet(text: str, *, max_len: int = 40) -> str:
    """结果摘要展示用，避免 24 字截断把 gmail.com 切成 gmail.co 误导上游。"""
    t = str(text or "").strip()
    if len(t) <= max_len:
        return t
    if "@" in t:
        local, domain = t.split("@", 1)
        head = local[:14] if len(local) > 14 else local
        tail = domain if len(domain) <= 16 else f"…{domain[-12:]}"
        return f"{head}@{tail}"
    return f"{t[: max_len - 1]}…"


def _name_from_params(params: dict) -> str:
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    for key in ("selector_text", "description", "label", "text"):
        val = params.get(key) or target.get(key if key != "selector_text" else "text")
        s = str(val or "").strip().strip("「」\"'")
        if s:
            return s
    return str(target.get("text") or "").strip()


def _anchor_name(params: dict) -> str:
    """DOM 锚点。不读 input 的 text，那是要输入的内容。"""
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    for key in ("text", "content_desc", "label"):
        s = str(target.get(key) or "").strip().strip("「」\"'")
        if s:
            return s
    return str(params.get("selector_text") or "").strip()


def _login_field_from_params(params: dict | None) -> str:
    """Nexus Web 渠道：input_text.params.field / login_field（安卓 adb 路径可忽略）。"""
    p = params or {}
    for key in ("login_field", "field"):
        raw = str(p.get(key) or "").strip().lower()
        if raw in ("email", "login_email"):
            return "email"
        if raw == "phone":
            return "phone"
        if raw in ("sms_code", "验证码", "otp"):
            return "sms_code"
        if raw in ("password", "密码"):
            return "password"
    return ""


class PlaywrightExecutor:
    id = "playwright"

    def __init__(self) -> None:
        self._focus_actions: dict[
            tuple[str, str, str, str, str], tuple[Any, list[tuple[Any, str, Any]]]
        ] = {}

    def _forget_focus(self, key: tuple[str, str, str, str, str]) -> None:
        old = self._focus_actions.pop(key, None)
        if old is not None:
            for _, _, document in old[1]:
                try:
                    document.dispose()
                except Exception:
                    pass

    @staticmethod
    def _focused_element(page):
        frame = page.main_frame
        frames = []
        for _ in range(32):
            frames.append(frame)
            active = frame.evaluate_handle("""() => {
                let element = document.activeElement;
                while (element && element.shadowRoot && element.shadowRoot.activeElement) {
                    element = element.shadowRoot.activeElement;
                }
                return element;
            }""")
            element = active.as_element()
            if element is None:
                active.dispose()
                return None, frames
            try:
                child = element.content_frame()
            except Exception:
                element.dispose()
                raise
            if child is None:
                return element, frames
            element.dispose()
            frame = child
        raise RuntimeError("focus_frame_depth_exceeded")

    def _remember_focus_page(self, key, page) -> None:
        documents = []
        element = None
        try:
            element, frames = self._focused_element(page)
            for frame in frames:
                documents.append((frame, frame.url, frame.evaluate_handle("document")))
            self._focus_actions[key] = (page, documents)
        except Exception:
            for _, _, document in documents:
                try:
                    document.dispose()
                except Exception:
                    pass
        finally:
            if element is not None:
                try:
                    element.dispose()
                except Exception:
                    pass

    def _focus_page_error(self, key, page) -> str:
        record = self._focus_actions.get(key)
        if record is None:
            return "focus_unavailable: page_context_missing"
        if record[0] is not page:
            return "focus_unavailable: page_changed"
        for frame, url, document in record[1]:
            try:
                if frame.is_detached() or frame.url != url or not document.evaluate("saved => saved === document"):
                    return "focus_unavailable: document_changed"
            except Exception:
                return "focus_unavailable: document_check_failed"
        return ""

    # 与 Nexus 能力目录同词表：只声明"我能执行哪些事件"。
    # Web 端按名字点/填，不先 VLM locate —— 所以 cost 比 VLM 路径低。
    provides = tuple(sorted(_SUPPORTED_CAPS | {"screenshot", "hierarchy"}))

    def probe(self) -> tuple[bool, str]:
        from mino_scout.platform.web.playwright_hub import PROBE_OK_STATE, probe_playwright

        # 注意：probe_playwright() 成功时返回 "available"（= 包和 Chromium 都在，
        # 但没有长期占着浏览器），**不是 "connected"**。写成 connected 会让
        # playwright 永远被上报为不可用 —— 这个坑踩过一次。
        state, detail = probe_playwright()
        if state == PROBE_OK_STATE:
            return True, ""
        return False, str(detail.get("reason") or "playwright 不可用")

    def supports(self, capability_id: str, low_level=None) -> bool:
        del low_level
        return capability_id in _SUPPORTED_CAPS

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        """Run one event and clear this lease's transient keys on every non-pass exit."""
        try:
            result = self._execute(event, ctx)
        except BaseException:
            if ctx.device.is_web:
                try:
                    identity = _web_identity(event, ctx)
                    get_hub().release_pressed_keys(
                        str(ctx.device.sn or ""),
                        run_id=str(ctx.run_id or ""),
                        **_identity_kwargs(identity),
                    )
                except BaseException:
                    pass
            raise
        if ctx.device.is_web and result.status != EventStatus.PASS:
            try:
                identity = _web_identity(event, ctx)
                get_hub().release_pressed_keys(
                    str(ctx.device.sn or ""),
                    run_id=str(ctx.run_id or ""),
                    **_identity_kwargs(identity),
                )
            except BaseException:
                pass
        return result

    def _execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        if cap == "kill_app":
            cap = "close_app"
        sn = str(ctx.device.sn or "")
        run_id = str(ctx.run_id or "")
        identity = _web_identity(event, ctx)
        id_kwargs = _identity_kwargs(identity)
        focus_key = (sn, run_id, *identity)
        if not ctx.device.is_web:
            return make_event_result(
                event, status=EventStatus.DECLINED, executor_used=self.id,
                started_at=started_at, elapsed_ms=0,
                summary="playwright 只服务 web 槽，让位给真机通道",
            )
        if cap not in ("tap_element", "input_text", "wait_screen_ready",
                       "get_foreground_app", "read_web_auth"):
            self._forget_focus(focus_key)
        hub = get_hub()
        headed = headed_from_hint(ctx.device.extra)
        web_resolution = str(ctx.device.extra.get("web_resolution") or DEFAULT_WEB_RESOLUTION)
        web_options = ctx.device.extra.get("web_options")
        web_options = dict(web_options) if isinstance(web_options, dict) else {}
        try:
            if cap == "launch_app":
                p = event.params or {}
                url = pick_goto_url(
                    p.get("url"),
                    ctx.device.extra.get("target_package"),
                    p.get("package"),
                )
                page = hub.current_page(sn, run_id=run_id, **id_kwargs)
                if page is None or p.get("fresh_context"):
                    page = hub.open_case(
                        sn,
                        run_id=run_id,
                        base_url=url,
                        headed=headed,
                        goto_params=p,
                        web_resolution=web_resolution,
                        credential=p.get("credential"),
                        web_options=web_options,
                        evidence_sink=ctx.shared.get("web_observer_sink"),
                        **id_kwargs,
                    )
                elif url:
                    goto_url(page, url, params=p)
                return self._ok(event, started_at, t0, f"打开 {url or page.url}", execution_started="yes")
            if cap == "close_app":
                p = event.params or {}
                if p.get("shutdown_browser") or p.get("shutdown"):
                    hub.close_run(sn, run_id, shutdown_browser=True)
                    return self._ok(event, started_at, t0, "关闭本任务 Chromium", execution_started="yes")
                hub.close_case(sn, run_id=run_id, **id_kwargs)
                return self._ok(event, started_at, t0, "关闭当前页面会话", execution_started="yes")
            if cap == "read_web_auth":
                return self._read_web_auth(
                    event, hub.current_page(sn, run_id=run_id, **id_kwargs), started_at, t0
                )
            page = hub.current_page(sn, run_id=run_id, **id_kwargs)
            if page is None:
                page = hub.open_case(
                    sn,
                    run_id=run_id,
                    base_url=pick_goto_url(ctx.device.extra.get("target_package")),
                    headed=headed,
                    web_resolution=web_resolution,
                    web_options=web_options,
                    evidence_sink=ctx.shared.get("web_observer_sink"),
                    **id_kwargs,
                )
            if cap == "get_foreground_app":
                return self._get_foreground_app(
                    event, hub, sn, run_id, started_at, t0, identity=identity
                )
            if cap == "wait_screen_ready":
                return self._wait_screen_ready(event, page, started_at, t0)
            if cap == "switch_tab":
                return self._switch_tab(
                    event, hub, sn, run_id, started_at, t0, identity=identity
                )
            if cap == "open_tab":
                return self._open_tab(
                    event, hub, sn, run_id, started_at, t0, identity=identity
                )
            if cap == "upload_file":
                return self._upload_file(event, page, started_at, t0)
            if cap == "set_clipboard":
                p = event.params or {}
                if "text" not in p:
                    return self._fail(event, started_at, t0, "set_clipboard 缺 text")
                text = str(p.get("text") or "")
                try:
                    page.evaluate("(value) => navigator.clipboard.writeText(value)", text)
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"剪贴板写入失败: {exc}", execution_started="yes")
                evidence = _clipboard_evidence(text)
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary=f"剪贴板写入 {evidence['length']} 字",
                    raw_response={"clipboard": evidence, "execution": {
                        "execution_started": "yes", "phase": "completed", "source": "playwright.native.v1"}},
                )
            if cap == "read_clipboard":
                try:
                    text = str(page.evaluate("() => navigator.clipboard.readText()") or "")
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"剪贴板读取失败: {exc}", execution_started="no")
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary="已读取浏览器剪贴板（内容不落证据）",
                    raw_response={"clipboard": _clipboard_evidence(text), "execution": {
                        "execution_started": "no", "phase": "completed", "source": "playwright.native.v1"}},
                )
            if cap == "assert_clipboard":
                expected = str((event.params or {}).get("expected_text") or "")
                exact = bool((event.params or {}).get("exact", True))
                try:
                    actual = str(page.evaluate("() => navigator.clipboard.readText()") or "")
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"剪贴板读取失败: {exc}", execution_started="no")
                matched = actual == expected if exact else expected in actual
                evidence = {"expected": _clipboard_evidence(expected), "actual": _clipboard_evidence(actual),
                            "exact": exact, "matched": matched}
                if not matched:
                    return make_event_result(
                        event, status=EventStatus.FAIL, executor_used=self.id,
                        started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                        summary="剪贴板内容与预期不一致", error="clipboard_mismatch",
                        raw_response={"clipboard": evidence, "execution": {
                            "execution_started": "no", "phase": "executor", "source": "playwright.native.v1"}},
                    )
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary="剪贴板内容符合预期",
                    raw_response={"clipboard": evidence, "execution": {
                        "execution_started": "no", "phase": "completed", "source": "playwright.native.v1"}},
                )
            if cap in {"network_expect_register", "network_expect_wait", "network_expect_cancel"}:
                obs = hub.current_observability(sn, run_id=run_id, **id_kwargs)
                if obs is None:
                    return self._fail(event, started_at, t0, "当前页面没有请求观测器", execution_started="no")
                p = event.params or {}
                try:
                    if cap == "network_expect_register":
                        expectation = obs.register(**{key: value for key, value in p.items()
                                                       if key != "_web_identity"})
                        data = {"expectation": expectation.describe()}
                        summary = "已注册网络响应期望"
                    elif cap == "network_expect_wait":
                        data = obs.wait(str(p.get("expectation_id") or ""),
                                        timeout_ms=int(p.get("timeout_ms") or 10_000),
                                        guard=ctx.shared.get("network_guard"))
                        summary = f"匹配响应 {data.get('method', '')} {data.get('path', '')} {data.get('status', '')}"
                    else:
                        removed = obs.cancel(str(p.get("expectation_id") or ""))
                        data = {"cancelled": removed, "expectation_id": str(p.get("expectation_id") or "")}
                        summary = "已取消网络响应期望" if removed else "网络响应期望已不存在"
                except Exception as exc:
                    reason = str(getattr(exc, "reason_code", "") or str(exc).split(" ", 1)[0])
                    if not reason.startswith("network_") and reason not in {"cancelled", "script_timeout"}:
                        reason = "network_observation_failed"
                    blocked = reason in {"network_expectation_ambiguous", "network_observation_overflow",
                                         "network_capture_fields_invalid", "cancelled", "script_timeout"}
                    return make_event_result(
                        event, status=EventStatus.BLOCKED if blocked else EventStatus.FAIL,
                        executor_used=self.id, started_at=started_at,
                        elapsed_ms=int((time.time() - t0) * 1000), summary=f"网络响应期望失败: {exc}",
                        raw_response={"reason_code": reason, "execution": {"execution_started": "no",
                                      "phase": "observer", "reason_code": reason}},
                    )
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary=summary,
                    raw_response={**data, "execution": {
                        "execution_started": "no", "phase": "completed", "source": "playwright.native.v1"}},
                )
            if cap in {"read_browser_errors", "read_web_environment"}:
                obs = hub.current_observability(sn, run_id=run_id, **id_kwargs)
                if obs is None:
                    return self._fail(event, started_at, t0, "当前页面没有请求观测器", execution_started="no")
                try:
                    if cap == "read_browser_errors":
                        data = obs.browser_errors(clear=bool((event.params or {}).get("clear", False)))
                        summary = f"读取浏览器错误 {data.get('count', 0)} 条"
                    else:
                        data = obs.environment()
                        observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
                        summary = f"浏览器环境 {observed.get('language') or '未读取'} / {observed.get('timezone_id') or '未读取'}"
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"读取 Web 证据失败: {exc}", execution_started="no")
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary=summary,
                    raw_response={**data, "execution": {
                        "execution_started": "no", "phase": "completed", "source": "playwright.native.v1"}},
                )
            if cap == "focus_element":
                try:
                    loc = locator_from_params(page, event.params or {})
                    if loc is None:
                        return self._fail(event, started_at, t0, "焦点控件未找到", execution_started="no")
                    loc.focus(timeout=4_000)
                    return self._ok(event, started_at, t0, "控件已获得焦点", execution_started="yes")
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"控件聚焦失败: {exc}", execution_started="yes")
            if cap == "check_focused":
                target = actual = None
                try:
                    loc = locator_from_params(page, event.params or {})
                    if loc is None:
                        return self._fail(event, started_at, t0, "焦点控件未找到", execution_started="no")
                    actual, frames = self._focused_element(page)
                    target = loc.element_handle(timeout=4_000)
                    target_frame = getattr(loc, "_frame", None) or getattr(loc, "owner_frame", None)
                    actual_frame = frames[-1] if frames else None
                    if target is None or actual is None:
                        return self._fail(event, started_at, t0, "无法取得焦点元素身份", execution_started="no")
                    if target_frame is None or actual_frame is None or target_frame != actual_frame:
                        return self._fail(event, started_at, t0, "目标控件不在当前焦点 Frame", execution_started="no")
                    focused = bool(target.evaluate(
                        "(target, actual) => target === actual", actual
                    ))
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"读取焦点失败: {exc}", execution_started="no")
                finally:
                    for handle in (target, actual):
                        if handle is not None:
                            try:
                                handle.dispose()
                            except Exception:
                                pass
                if not focused:
                    return self._fail(event, started_at, t0, "目标控件没有焦点", execution_started="no")
                return self._ok(event, started_at, t0, "目标控件持有焦点", execution_started="no")
            if cap == "read_focused":
                try:
                    element, _ = self._focused_element(page)
                    if element is None:
                        return self._fail(event, started_at, t0, "当前没有焦点控件", execution_started="no")
                    facts = element.evaluate(
                        "el => ({tag:el.tagName?.toLowerCase() || '', id:el.id || '', name:el.getAttribute?.('name') || '',"
                        "type:el.getAttribute?.('type') || '', role:el.getAttribute?.('role') || '',"
                        "value_length:String(el.value ?? el.textContent ?? '').length})"
                    )
                    element.dispose()
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"读取焦点失败: {exc}", execution_started="no")
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary=f"焦点 {facts.get('tag', '')}#{facts.get('id', '')}",
                    raw_response={"focused": facts, "execution": {
                        "execution_started": "no", "phase": "completed", "source": "playwright.native.v1"}},
                )
            if cap == "read_url":
                return self._read_url(event, page, started_at, t0)
            if cap in {"forward_page", "navigate_page"}:
                p = event.params or {}
                timeout = p.get("timeout_ms", 15_000)
                if type(timeout) is not int or not 1 <= timeout <= 120_000:
                    return self._fail(
                        event, started_at, t0,
                        "timeout_ms 必须是 1..120000 的整数",
                        execution_started="no",
                    )
                try:
                    current = validate_page_url(str(page.url or ""))
                except ValueError as exc:
                    return self._fail(
                        event, started_at, t0, f"当前页面 URL 无效: {exc}",
                        execution_started="no",
                    )
                target = current
                if cap == "navigate_page":
                    raw = str(p.get("uri") or p.get("url") or "").strip()
                    if not raw:
                        return self._fail(event, started_at, t0, f"{cap} 缺 uri", execution_started="no")
                    try:
                        target = resolve_uri(current, raw)
                    except ValueError as exc:
                        return self._fail(
                            event, started_at, t0, f"导航 URI 无效: {exc}",
                            execution_started="no",
                        )
                navigated = {"main_frame": False}
                if cap == "forward_page":
                    def _on_main_frame_navigation(frame: Any) -> None:
                        if frame == page.main_frame:
                            navigated["main_frame"] = True
                    page.on("framenavigated", _on_main_frame_navigation)
                try:
                    if cap == "forward_page":
                        page.go_forward(wait_until="domcontentloaded", timeout=timeout)
                    else:
                        page.goto(target, wait_until="domcontentloaded", timeout=timeout)
                    actual = validate_page_url(str(page.url or ""))
                    if cap == "forward_page" and not navigated["main_frame"] and actual == current:
                        return self._fail(
                            event, started_at, t0, "没有可前进的页面历史",
                            error="web_forward_noop", execution_started="yes",
                        )
                    return make_event_result(
                        event, status=EventStatus.PASS, executor_used=self.id,
                        started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                        summary=f"已导航 {actual[:120]}",
                        raw_response={"page_url": actual, "requested_url": target,
                                      "instrument_simulated": False,
                                      "load_state": "domcontentloaded", "execution": {
                                          "execution_started": "yes", "phase": "completed",
                                          "source": "playwright.native.v1"}},
                    )
                except Exception as exc:
                    obs = hub.current_observability(sn, run_id=run_id, **id_kwargs)
                    if obs is not None:
                        obs.record_navigation_error(exc, target)
                    return self._fail(event, started_at, t0, f"页面导航失败: {exc}", execution_started="yes")
                finally:
                    if cap == "forward_page":
                        try:
                            page.remove_listener("framenavigated", _on_main_frame_navigation)
                        except Exception:
                            pass
            if cap == "drag_control":
                return self._drag_control(event, page, started_at, t0)
            if cap == "set_geolocation":
                p = event.params or {}
                try:
                    latitude = float(p.get("latitude"))
                    longitude = float(p.get("longitude"))
                    accuracy = float(p.get("accuracy", 0))
                    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or accuracy < 0:
                        raise ValueError("范围内无效")
                    page.context.set_geolocation({"latitude": latitude, "longitude": longitude,
                                                  "accuracy": accuracy})
                except (TypeError, ValueError) as exc:
                    return self._fail(event, started_at, t0, f"地理位置无效: {exc}", execution_started="no")
                return self._ok(event, started_at, t0, f"位置 {latitude},{longitude}", execution_started="yes")
            if cap == "set_web_permission":
                p = event.params or {}
                name = str(p.get("permission") or p.get("name") or "").strip()
                state = str(p.get("state") or "").strip().lower()
                origin = str(p.get("origin") or "").strip()
                if name not in {"geolocation", "notifications", "clipboard-read", "clipboard-write"}:
                    return self._fail(event, started_at, t0, "不支持的浏览器权限", execution_started="no")
                try:
                    if state == "granted":
                        kwargs = {"origin": origin} if origin else {}
                        page.context.grant_permissions([name], **kwargs)
                    elif state in {"prompt", "default"}:
                        kwargs = {"origin": origin} if origin else {}
                        page.context.clear_permissions(**kwargs)
                    elif state == "denied":
                        return make_event_result(
                            event, status=EventStatus.BLOCKED, executor_used=self.id,
                            started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                            summary="当前 Chromium 通道不能可靠强制 denied，请重建 context 或在配置中声明",
                            error="web_permission_denied_unsupported",
                            raw_response={"permission": name, "requested_state": state,
                                          "execution": {"execution_started": "no", "phase": "preflight",
                                                        "reason_code": "web_permission_denied_unsupported",
                                                        "source": "playwright.native.v1"}},
                        )
                    else:
                        return self._fail(event, started_at, t0, "权限状态必须是 granted/denied/prompt",
                                          execution_started="no")
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"设置权限失败: {exc}", execution_started="yes")
                return self._ok(event, started_at, t0, f"权限 {name}={state}", execution_started="yes")
            if cap == "set_page_time":
                p = event.params or {}
                try:
                    action = str(p.get("action") or "set_fixed").strip()
                    if action == "fast_forward":
                        page.clock.fast_forward(int(p.get("milliseconds") or 0))
                    elif action == "run_for":
                        page.clock.run_for(int(p.get("milliseconds") or 0))
                    elif action == "pause":
                        page.clock.pause_at(int(p.get("epoch_ms")))
                    elif action == "resume":
                        page.clock.resume()
                    else:
                        page.clock.set_fixed_time(int(p.get("epoch_ms")))
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"页面时钟操作失败: {exc}", execution_started="yes")
                return self._ok(event, started_at, t0, f"页面时钟 {action}", execution_started="yes")
            if cap == "reload_page":
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15_000)
                    return self._ok(event, started_at, t0, "已刷新当前页面", execution_started="yes")
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"刷新页面失败: {exc}")
            if cap == "tap_xy":
                try:
                    x, y = resolve_viewport_xy(event.params or {}, page)
                except ValueError:
                    return self._fail(event, started_at, t0, "tap_xy 缺坐标")
                page.mouse.click(x, y)
                return self._ok(event, started_at, t0, f"点击坐标 ({x},{y})", execution_started="yes")
            if cap == "exec_script":
                script = str((event.params or {}).get("script") or "").strip()
                if not script:
                    return self._fail(event, started_at, t0, "exec_script 缺 script")
                try:
                    timeout = int((event.params or {}).get("timeout_ms") or 30000)
                except (TypeError, ValueError):
                    timeout = 30000
                try:
                    page.set_default_timeout(max(1000, min(timeout, 60000)))
                except Exception as exc:
                    result = self._fail(event, started_at, t0, "页面脚本准备失败", execution_started="no")
                    result.raw_response["executor_failure"] = _script_failure(exc, phase="preflight")
                    return result
                try:
                    value = page.evaluate("(source) => eval(source)", script)
                except Exception as exc:
                    result = self._write_unknown(event, started_at, t0, RuntimeError("页面脚本执行异常"))
                    result.raw_response["executor_failure"] = _script_failure(exc, phase="evaluate")
                    return result
                text = "" if value is None else str(value)[:2000]
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                    elapsed_ms=int((time.time() - t0) * 1000), summary="已执行页面脚本",
                    raw_response={"stdout": text, "execution": {"execution_started": "yes", "phase": "completed", "source": "playwright.native.v1"}},
                )
            if cap == "tap_element":
                self._forget_focus(focus_key)
                result = self._tap(event, page, started_at, t0)
                if result.status == EventStatus.PASS:
                    self._remember_focus_page(focus_key, page)
                return result
            if cap == "long_press_element":
                return self._long_press(event, page, started_at, t0)
            if cap == "input_text":
                params = event.params or {}
                if params.get("target_mode") == "current_focus" and params.get("point_policy") != "node":
                    focus_error = self._focus_page_error(focus_key, page)
                    if focus_error:
                        self._forget_focus(focus_key)
                        return self._fail(event, started_at, t0, focus_error, execution_started="no")
                result = self._input(event, ctx, page, started_at, t0)
                self._forget_focus(focus_key)
                return result
            if cap in {"press_key", "key_down", "key_up"}:
                p = event.params or {}
                raw_key = p.get("key") or p.get("keycode")
                if cap in {"key_down", "key_up"} and raw_key is None:
                    return self._fail(event, started_at, t0, f"{cap} 缺 key", execution_started="no")
                key = str(raw_key or "Escape")
                low = key.lower()
                if cap == "press_key" and low in ("back", "browser_back") and p.get("browser_back", True):
                    try:
                        page.go_back(wait_until="domcontentloaded", timeout=10_000)
                        return self._ok(event, started_at, t0, "浏览器后退", execution_started="yes")
                    except Exception as exc:
                        return self._fail(event, started_at, t0, f"浏览器后退失败: {exc}")
                try:
                    mapped = (
                        normalize_key(key)
                        if cap in {"key_down", "key_up"}
                        else normalize_key_value(key, allow_combo=True)
                    )
                except ValueError as exc:
                    return self._fail(event, started_at, t0, f"按键无效: {exc}", execution_started="no")
                try:
                    if cap == "key_down":
                        hub.keyboard_down(sn, run_id=run_id, key=mapped, **id_kwargs)
                    elif cap == "key_up":
                        hub.keyboard_up(sn, run_id=run_id, key=mapped, **id_kwargs)
                    else:
                        page.keyboard.press(mapped)
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"按键失败: {exc}", execution_started="no")
                action = "按下" if cap == "key_down" else "抬起" if cap == "key_up" else "按键"
                return self._ok(event, started_at, t0, f"{action} {mapped}", execution_started="yes")
            if cap == "swipe_direction":
                p = event.params or {}
                direction = str(p.get("direction") or "down").strip().lower()
                if "direction" in p and direction not in {"up", "down", "left", "right"}:
                    return self._fail(event, started_at, t0, "direction 必须是 up/down/left/right",
                                      execution_started="no")
                duration = p.get("duration_ms", 500)
                steps = p.get("steps", 12)
                if type(duration) is not int or not 0 <= duration <= 120_000:
                    return self._fail(event, started_at, t0, "duration_ms 必须是 0..120000 的整数",
                                      execution_started="no")
                if type(steps) is not int or not 0 <= steps <= 500:
                    return self._fail(event, started_at, t0, "steps 必须是 0..500 的整数",
                                      execution_started="no")
                box = page.viewport_size or _DEFAULT_WEB_VIEWPORT
                w, h = int(box["width"]), int(box["height"])
                fx, fy, tx, ty = p.get("from_x"), p.get("from_y"), p.get("to_x"), p.get("to_y")
                try:
                    if None not in (fx, fy, tx, ty):
                        x1, y1 = to_viewport_xy(fx, fy, page)
                        x2, y2 = to_viewport_xy(tx, ty, page)
                        delta = (x1, y1, x2, y2)
                        summary = f"滑动 ({fx},{fy})→({tx},{ty})"
                    else:
                        cx, cy = w // 2, h // 2
                        delta = {
                            "up": (cx, int(h * 0.75), cx, int(h * 0.25)),
                            "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
                            "left": (int(w * 0.75), cy, int(w * 0.25), cy),
                            "right": (int(w * 0.25), cy, int(w * 0.75), cy),
                        }[direction]
                        summary = f"滑动 {direction}"
                    _mouse_drag(page, (delta[0], delta[1]), (delta[2], delta[3]),
                                duration_ms=duration, steps=steps)
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"滑动失败: {exc}", execution_started="yes")
                return self._ok(event, started_at, t0, summary, execution_started="yes")
            if cap == "swipe_element_to_element":
                p = event.params or {}
                if None in (p.get("from_x"), p.get("from_y"), p.get("to_x"), p.get("to_y")):
                    return self._fail(event, started_at, t0, "拖拽需要 from_x/from_y/to_x/to_y",
                                      execution_started="no")
                duration = p.get("duration_ms", 400)
                steps = p.get("steps", 12)
                if type(duration) is not int or not 0 <= duration <= 120_000:
                    return self._fail(event, started_at, t0, "duration_ms 必须是 0..120000 的整数",
                                      execution_started="no")
                if type(steps) is not int or not 0 <= steps <= 500:
                    return self._fail(event, started_at, t0, "steps 必须是 0..500 的整数",
                                      execution_started="no")
                try:
                    x1, y1 = to_viewport_xy(p.get("from_x"), p.get("from_y"), page)
                    x2, y2 = to_viewport_xy(p.get("to_x"), p.get("to_y"), page)
                    _mouse_drag(page, (x1, y1), (x2, y2), duration_ms=duration, steps=steps)
                except Exception as exc:
                    return self._fail(event, started_at, t0, f"拖拽失败: {exc}", execution_started="yes")
                return self._ok(event, started_at, t0, f"拖拽 ({x1},{y1})→({x2},{y2})", execution_started="yes")
            return self._fail(event, started_at, t0, f"PlaywrightExecutor 不处理 {cap}")
        except Exception as exc:
            SLog.e(TAG, f"execute exception cap={cap} sn={sn}: {exc}")
            if cap in {"tap_element", "long_press_element", "input_text", "press_key", "swipe_direction",
                       "swipe_element_to_element", "set_clipboard", "focus_element", "drag_control",
                       "forward_page", "navigate_page", "set_geolocation", "set_web_permission", "set_page_time"}:
                return make_event_result(event, status=EventStatus.BLOCKED, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time()-t0)*1000),
                    summary="设备写动作结果不确定，请先观察确认", error=f"exception: {exc}",
                       raw_response={"execution_started": True, "error_kind": "write_outcome_unknown"},
                   )
            return self._fail(event, started_at, t0, f"exception: {exc}")

    def _auth_body(
        self,
        *,
        session: str,
        evidence: str = "",
        host: str = "",
        expected_host: str = "",
        cookie_count: int = 0,
        storage_keys: int = 0,
        reason: str = "",
    ) -> str:
        return json.dumps(
            {
                "session": session,
                "evidence": evidence,
                "host": host,
                "expected_host": expected_host,
                "cookie_count": cookie_count,
                "storage_keys": storage_keys,
                "reason": reason,
            },
            ensure_ascii=False,
        )

    def _read_web_auth(self, event, page, started_at: str, t0: float) -> EventResult:
        """只读当前被测网页域名上的 Cookie / 本地存储。别的站点的令牌不算已登录。不返回值。"""
        if page is None:
            return self._ok(event, started_at, t0, self._auth_body(session="guest", reason="no_page"), execution_started="no")
        try:
            page_url = str(page.url or "")
        except Exception:
            page_url = ""
        page_host = _host_of(page_url)
        params = event.params or {}
        expect_host = ""
        for key in ("url", "origin", "package"):
            raw = str(params.get(key) or "").strip()
            if raw.startswith("http://") or raw.startswith("https://"):
                expect_host = _host_of(raw)
                break
        if expect_host and page_host and not _hosts_related(page_host, expect_host):
            return self._ok(
                event,
                started_at,
                t0,
                self._auth_body(
                    session="guest",
                    host=page_host,
                    expected_host=expect_host,
                    reason="page_not_app",
                ),
                execution_started="no",
            )
        try:
            cookies = page.context.cookies([page_url]) if page_url else []
        except Exception:
            try:
                cookies = page.context.cookies()
            except Exception:
                cookies = []
        cookies = [
            item
            for item in (cookies or [])
            if isinstance(item, dict) and _cookie_for_host(str(item.get("domain") or ""), page_host)
        ]
        evidence = ""
        for item in cookies or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            hit = _web_auth_evidence(name, value)
            if hit == "jwt" or (hit and not evidence):
                evidence = hit
            if evidence == "jwt":
                break
        storage = {
            "localJwt": False,
            "localToken": False,
            "sessionJwt": False,
            "sessionToken": False,
            "localCount": 0,
            "sessionCount": 0,
        }
        try:
            raw = page.evaluate(
                """() => {
                  const csrf = /csrf|xsrf/i;
                  const authName = /^(access_token|refresh_token|id_token|auth_token|authorization)$/i;
                  const jwt = (v) => {
                    if (!v || v.length < 20) return false;
                    const p = String(v).split('.');
                    return p.length === 3 && p[0].indexOf('eyJ') === 0 && p[1] && p[2];
                  };
                  const scan = (store) => {
                    let jwtHit = false;
                    let tokenHit = false;
                    for (let i = 0; i < store.length; i++) {
                      const k = store.key(i) || '';
                      const v = store.getItem(k) || '';
                      if (!v || csrf.test(k)) continue;
                      if (jwt(v)) jwtHit = true;
                      else if (authName.test(k) && v.length >= 24) tokenHit = true;
                    }
                    return { jwtHit, tokenHit, count: store.length };
                  };
                  const local = scan(localStorage);
                  const sess = scan(sessionStorage);
                  return {
                    localJwt: local.jwtHit,
                    localToken: local.tokenHit,
                    sessionJwt: sess.jwtHit,
                    sessionToken: sess.tokenHit,
                    localCount: local.count,
                    sessionCount: sess.count,
                  };
                }"""
            )
            if isinstance(raw, dict):
                storage = raw
        except Exception:
            storage = {
                "localJwt": False,
                "localToken": False,
                "sessionJwt": False,
                "sessionToken": False,
                "localCount": 0,
                "sessionCount": 0,
            }
        if storage.get("localJwt") or storage.get("sessionJwt"):
            evidence = "jwt"
        elif not evidence and (storage.get("localToken") or storage.get("sessionToken")):
            evidence = "auth_token"
        local_n = int(storage.get("localCount") or 0)
        session_n = int(storage.get("sessionCount") or 0)
        # 只有当前页域名上的 JWT / 明确令牌名才是已登录。空存储、无关 cookie、对不上的站点都是 guest。
        associated = bool(evidence) and bool(page_host) and (
            not expect_host or _hosts_related(page_host, expect_host)
        )
        return self._ok(
            event,
            started_at,
            t0,
            self._auth_body(
                session="logged_in" if associated else "guest",
                evidence=evidence if associated else "",
                host=page_host,
                expected_host=expect_host,
                cookie_count=len(cookies or []),
                storage_keys=local_n + session_n,
                reason="associated" if associated else "no_credential",
            ),
            execution_started="no",
        )

    def _get_foreground_app(
        self,
        event: PlanEvent,
        hub: Any,
        sn: str,
        run_id: str,
        started_at: str,
        t0: float,
        *,
        identity: tuple[str, str, str] = ("", "", ""),
    ) -> EventResult:
        url = hub.current_url(sn, run_id=run_id, **_identity_kwargs(identity))
        if not url:
            return self._fail(event, started_at, t0, "无打开页面，无法读取当前 URL")
        expect = pick_goto_url(
            (event.params or {}).get("url"),
            (event.params or {}).get("package"),
        )
        match = None
        if expect:
            try:
                match = same_entry_url(url, expect)
            except ValueError as exc:
                return self._fail(
                    event, started_at, t0, f"URL 比较失败: {exc}",
                    execution_started="no",
                )
        summary = url[:120]
        if expect:
            summary = f"{summary}（目标{'命中' if match else '未命中'}）"
        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=summary,
            raw_response={
                "page_url": url,
                "package": url,
                "platform": "web",
                "expected_url": expect,
                "match": match,
            },
        )

    def _switch_tab(
        self,
        event: PlanEvent,
        hub: Any,
        sn: str,
        run_id: str,
        started_at: str,
        t0: float,
        *,
        identity: tuple[str, str, str] = ("", "", ""),
    ) -> EventResult:
        p = event.params or {}
        try:
            page = hub.switch_tab(
                sn,
                run_id=run_id,
                tab_index=p.get("tab_index") if p.get("tab_index") is not None else None,
                url_contains=str(p.get("url_contains") or p.get("url") or ""),
                title_contains=str(p.get("title_contains") or p.get("title") or ""),
                **_identity_kwargs(identity),
            )
            return self._ok(event, started_at, t0, f"切换 Tab {str(page.url or '')[:80]}", execution_started="yes")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"切换 Tab 失败: {exc}")

    def _open_tab(
        self,
        event: PlanEvent,
        hub: Any,
        sn: str,
        run_id: str,
        started_at: str,
        t0: float,
        *,
        identity: tuple[str, str, str] = ("", "", ""),
    ) -> EventResult:
        p = event.params or {}
        try:
            page = hub.open_tab(
                sn,
                run_id=run_id,
                url=str(p.get("url") or pick_goto_url(p.get("package")) or ""),
                **_identity_kwargs(identity),
            )
            return self._ok(event, started_at, t0, f"新开 Tab {str(page.url or '')[:80]}", execution_started="yes")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"新开 Tab 失败: {exc}")

    def _upload_file(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        p = event.params or {}
        paths = p.get("file_paths") or p.get("files")
        if not paths:
            one = str(p.get("file_path") or p.get("path") or "").strip()
            paths = [one] if one else []
        if isinstance(paths, str):
            paths = [paths]
        files = [str(x).strip() for x in paths if str(x).strip()]
        if not files:
            return self._fail(event, started_at, t0, "upload_file 需要 file_path 或 file_paths")
        loc = locator_from_params(page, p)
        if loc is None and p.get("require_unique"):
            return self._fail(event, started_at, t0, "动态上传控件未找到", execution_started="no")
        if loc is None:
            css = css_selector_from_params(p)
            if css:
                loc = resolve_locator_scope(page, p).locator(css).first
            else:
                loc = resolve_locator_scope(page, p).locator("input[type='file']").first
        try:
            loc.set_input_files(files)
            return self._ok(event, started_at, t0, f"上传 {len(files)} 个文件", execution_started="yes")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"上传失败: {exc}")

    def _read_url(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        actual = str(page.url or "")
        expected = str((event.params or {}).get("expected_uri") or (event.params or {}).get("uri") or "")
        try:
            actual = validate_page_url(actual)
        except ValueError as exc:
            return self._fail(
                event, started_at, t0, f"当前页面 URL 无效: {exc}",
                execution_started="no",
            )
        if expected:
            try:
                expected = resolve_uri(actual, expected)
                matched = same_entry_url(actual, expected)
            except ValueError as exc:
                return self._fail(event, started_at, t0, f"期望 URI 无效: {exc}",
                                  execution_started="no")
            if not matched:
                return make_event_result(
                    event, status=EventStatus.FAIL, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary=f"URL 未命中 {actual[:120]}", error="url_mismatch",
                    raw_response={"page_url": actual, "expected_url": expected, "match": False,
                                  "execution": {"execution_started": "no", "phase": "executor",
                                                "source": "playwright.native.v1"}},
                )
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id,
                started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                summary=f"URL 命中 {actual[:120]}",
                raw_response={"page_url": actual, "expected_url": expected, "match": True,
                              "execution": {"execution_started": "no", "phase": "completed",
                                            "source": "playwright.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id,
            started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
            summary=f"URL {actual[:120]}",
            raw_response={"page_url": actual, "execution": {
                "execution_started": "no", "phase": "completed", "source": "playwright.native.v1"}},
        )

    def _drag_control(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        p = event.params or {}
        source_p = p.get("source_target") if isinstance(p.get("source_target"), dict) else {}
        target_p = p.get("target_target") if isinstance(p.get("target_target"), dict) else {}
        duration = p.get("duration_ms", 500)
        if type(duration) is not int or not 0 <= duration <= 120_000:
            return self._fail(event, started_at, t0, "duration_ms 必须是 0..120000 的整数",
                              execution_started="no")
        steps_value = p.get("steps")
        if steps_value is None:
            steps = min(60, max(8, duration // 20)) if duration else 0
        else:
            steps = steps_value
        if type(steps) is not int or not 0 <= steps <= 500:
            return self._fail(event, started_at, t0, "steps 必须是 0..500 的整数",
                              execution_started="no")
        try:
            source = locator_from_params(page, {**p, **source_p, "require_unique": True})
            target = locator_from_params(page, {**p, **target_p, "require_unique": True})
            if source is None or target is None:
                return self._fail(event, started_at, t0, "拖拽源或目标控件未找到", execution_started="no")
            mode = str(p.get("mode") or "mouse").lower()
            if mode == "html5":
                source.drag_to(target, timeout=max(1_000, duration or 4_000))
                return self._ok(event, started_at, t0, "HTML5 拖拽已执行", execution_started="yes")
            if mode != "mouse":
                return self._fail(event, started_at, t0, "拖拽 mode 必须是 mouse 或 html5", execution_started="no")
            source.scroll_into_view_if_needed(timeout=4_000)
            target.scroll_into_view_if_needed(timeout=4_000)
            src_box = source.bounding_box()
            dst_box = target.bounding_box()
            if not src_box or not dst_box:
                return self._fail(event, started_at, t0, "拖拽控件没有可操作区域", execution_started="no")
            sx = float(src_box["x"]) + float(src_box["width"]) * float(p.get("source_x_ratio", 0.5))
            sy = float(src_box["y"]) + float(src_box["height"]) * float(p.get("source_y_ratio", 0.5))
            tx = float(dst_box["x"]) + float(dst_box["width"]) * float(p.get("target_x_ratio", 0.5))
            ty = float(dst_box["y"]) + float(dst_box["height"]) * float(p.get("target_y_ratio", 0.5))
            _mouse_drag(page, (sx, sy), (tx, ty), duration_ms=duration, steps=steps)
            return self._ok(event, started_at, t0,
                            f"控件拖拽 ({sx:.0f},{sy:.0f})→({tx:.0f},{ty:.0f})，{duration}ms",
                            execution_started="yes")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"控件拖拽失败: {exc}", execution_started="yes")

    def _wait_screen_ready(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        p = event.params or {}
        scope = resolve_locator_scope(page, p)
        timeout = max(1000, min(int(p.get("timeout_ms") or 20_000), 120_000))
        sel = css_selector_from_params(p) or str(p.get("wait_selector") or "").strip()
        try:
            if p.get("require_unique"):
                loc = locator_from_params(page, p)
                if loc is None:
                    return self._fail(event, started_at, t0, "动态对象未找到", execution_started="no")
                loc.wait_for(state="visible", timeout=timeout)
                return self._ok(event, started_at, t0, "动态对象已出现", execution_started="no")
            if sel:
                scope.locator(sel).first.wait_for(state="visible", timeout=timeout)
                return self._ok(event, started_at, t0, f"已出现 {sel[:48]}", execution_started="no")
            state = str(p.get("load_state") or p.get("wait_until") or "domcontentloaded").strip()
            if state not in ("commit", "domcontentloaded", "load", "networkidle"):
                state = "domcontentloaded"
            page.wait_for_load_state(state, timeout=timeout)
            return self._ok(event, started_at, t0, f"页面 {state}", execution_started="no")
        except Exception as exc:
            return self._fail(event, started_at, t0, f"等待就绪超时: {exc}", execution_started="no")

    def _fallback_xy(self, params, page):
        """Nexus 已经算好的点。锚点没命中时直接执行，不再替 Nexus 做策略判断。"""
        try:
            return resolve_viewport_xy(params, page)
        except ValueError:
            return None

    def _tap(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        policy = str(params.get("point_policy") or "").strip().lower()
        if policy == "node":
            anchored = self._tap_node(event, page, started_at, t0, params)
            if anchored is not None:
                return anchored
            xy = self._fallback_xy(params, page)
            if xy is None:
                return self._fail(event, started_at, t0, "DOM 点击锚点未命中且未给坐标")
            x, y = xy
            page.mouse.click(x, y)
            self._settle_page(page)
            return self._ok(event, started_at, t0, f"点击 ({x},{y})[锚点未命中，回落坐标]", execution_started="yes")
        name = _name_from_params(params)
        try:
            x, y = resolve_viewport_xy(params, page)
        except ValueError:
            return self._fail(
                event,
                started_at,
                t0,
                f"Web 点击需要坐标 x/y「{name[:40]}」" if name else "Web 点击需要坐标 x/y",
            )
        page.mouse.click(x, y)
        self._settle_page(page)
        label = f"「{name[:40]}」({x},{y})" if name else f"({x},{y})"
        return self._ok(event, started_at, t0, f"点击 {label}", execution_started="yes")

    def _tap_node(self, event, page, started_at, t0, params):
        """锚点命中则点击。没命中返回 None，由调用方按 Nexus 给的坐标执行。"""
        if params.get("node_ref"):
            handle = None
            try:
                handle = page.evaluate_handle("""p => {
                    const state=window.__minoObservation;
                    if (!state || state.snapshot!==p.snapshot_id) throw new Error('stale_observation');
                    const entry=state.elements.get(p.node_ref);
                    if (!entry || !entry.el.isConnected || entry.el.disabled || entry.el.getAttribute('aria-disabled')==='true') throw new Error('stale_node_ref');
                    return entry.el;
                }""", {"node_ref": params["node_ref"], "snapshot_id": params.get("snapshot_id")})
                element = handle.as_element()
                if element is None or not element.is_visible() or not element.is_enabled():
                    raise ValueError("stale_node_ref")
                # Trial uses Playwright actionability without sending the click.
                element.click(trial=True, timeout=2000)
            except Exception as exc:
                return self._fail(
                    event, started_at, t0, f"观察节点预检失败: {type(exc).__name__}",
                    execution_started="no",
                )
            finally:
                if handle is not None:
                    try:
                        handle.dispose()
                    except Exception:
                        pass
            try:
                element.click(timeout=4000)
                self._settle_page(page)
                return self._ok(event, started_at, t0, "点击当前观察节点", execution_started="yes")
            except Exception as exc:
                return make_event_result(
                    event, status=EventStatus.BLOCKED, executor_used=self.id,
                    started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                    summary="节点点击结果不确定，先观察确认", error=str(exc),
                    raw_response={"execution": {
                        "execution_started": "unknown", "phase": "executor",
                        "reason_code": "write_outcome_unknown", "source": "playwright.native.v1",
                    }, "execution_control": {
                        "stop_run": True, "reason_code": "write_outcome_unknown",
                        "resource_isolated": True, "stop_confirmed": False,
                    }},
                )
        name = _anchor_name(params)
        if not name:
            return None
        loc, count = unique_exact_text(page, name)
        if count > 1:
            return self._fail(
                event, started_at, t0, f"文案「{name[:40]}」命中 {count} 处，无法唯一点击",
                execution_started="no",
            )
        if loc is None:
            return None
        try:
            loc.click(trial=True, timeout=2000)
        except Exception as exc:
            return self._fail(
                event, started_at, t0, f"文案「{name[:40]}」预检失败: {type(exc).__name__}",
                execution_started="no",
            )
        try:
            loc.click(timeout=4000)
            self._settle_page(page)
            return self._ok(event, started_at, t0, f"点击「{name[:40]}」", execution_started="yes")
        except Exception as exc:
            return make_event_result(
                event, status=EventStatus.BLOCKED, executor_used=self.id,
                started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
                summary="文案点击结果不确定，先观察确认", error=str(exc),
                raw_response={"execution": {
                    "execution_started": "unknown", "phase": "executor",
                    "reason_code": "write_outcome_unknown", "source": "playwright.native.v1",
                }, "execution_control": {
                    "stop_run": True, "reason_code": "write_outcome_unknown",
                    "resource_isolated": True, "stop_confirmed": False,
                }},
            )

    def _long_press(self, event: PlanEvent, page, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        if str(params.get("point_policy") or "").strip().lower() == "node":
            name = _anchor_name(params)
            box = None
            if name:
                loc, hits = unique_exact_text(page, name)
                if hits > 1:
                    return self._fail(event, started_at, t0, f"文案「{name[:40]}」命中 {hits} 处，无法唯一长按")
                if loc is not None:
                    try:
                        box = loc.bounding_box()
                    except Exception:
                        box = None
            if not box:
                xy = self._fallback_xy(params, page)
                if xy is None:
                    return self._fail(event, started_at, t0, "DOM 长按锚点未命中且未给坐标")
                x, y = xy
                page.mouse.move(x, y)
                page.mouse.down()
                time.sleep(0.8)
                page.mouse.up()
                return self._ok(event, started_at, t0, f"长按 ({x},{y})[锚点未命中，回落坐标]", execution_started="yes")
            x = int(box["x"] + box["width"] / 2)
            y = int(box["y"] + box["height"] / 2)
            page.mouse.move(x, y)
            page.mouse.down()
            time.sleep(0.8)
            page.mouse.up()
            return self._ok(event, started_at, t0, f"长按「{name[:40]}」", execution_started="yes")
        name = _name_from_params(params)
        try:
            x, y = resolve_viewport_xy(event.params or {}, page)
        except ValueError:
            return self._fail(
                event,
                started_at,
                t0,
                f"Web 长按需要坐标 x/y「{name[:40]}」" if name else "Web 长按需要坐标 x/y",
            )
        page.mouse.move(x, y)
        page.mouse.down()
        time.sleep(0.8)
        page.mouse.up()
        return self._ok(event, started_at, t0, f"长按 ({x},{y})", execution_started="yes")

    def _input(self, event: PlanEvent, ctx: ExecutorContext, page, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        text = str(params.get("text") or "")
        login_field = _login_field_from_params(params)
        from mino_scout.runtime.secret_redaction import remember
        if login_field in {"password", "otp"} or params.get("sensitive"):
            remember(text)
            event.params["sensitive"] = True
        tag = login_field or "textbox"
        policy = str(params.get("point_policy") or "").strip().lower()
        if policy == "node":
            name = _anchor_name(params)
            loc = None
            pattern = exact_visible_pattern(name)
            if pattern is not None:
                try:
                    for cand in (
                        page.get_by_role("textbox", name=pattern),
                        page.get_by_label(pattern),
                        page.get_by_placeholder(pattern),
                    ):
                        if int(cand.count()) == 1:
                            loc = cand
                            break
                except Exception:
                    loc = None
            if loc is not None:
                try:
                    loc.wait_for(state="visible", timeout=2000)
                    if not loc.is_enabled():
                        raise ValueError("not_enabled")
                    if loc.evaluate("e => e.type === 'password'"):
                        remember(text)
                        event.params["sensitive"] = True
                except Exception as exc:
                    return self._fail(
                        event, started_at, t0, f"输入({tag})「{name[:40]}」预检失败: {type(exc).__name__}",
                        execution_started="no",
                    )
                try:
                    loc.fill(text)
                    return self._ok(event, started_at, t0, f"输入({tag})「{name[:40]}」", execution_started="yes")
                except Exception as exc:
                    return self._write_unknown(event, started_at, t0, exc)
        focus_ref = str(params.get("focus_ref") or "")
        if str(params.get("target_mode") or "") == "current_focus":
            if not ctx.run_id or not focus_ref.startswith(f"focus:{ctx.run_id}:") or not focus_ref.rsplit(":", 1)[-1].isdigit():
                return self._fail(
                    event, started_at, t0,
                    "contract_unsupported: current_focus 缺少有效 focus_ref", execution_started="no",
                )
            return self._input_focused(event, page, started_at, t0, text, tag, params)
        try:
            x, y = resolve_viewport_xy(params, page)
        except ValueError:
            return self._fail(event, started_at, t0, f"Web 输入({tag}) 需要坐标 x/y", execution_started="no")
        target = locate_editable_at(
            page, x, y, field=login_field, allow_near=policy != "coordinate"
        )
        if target is None:
            return self._fail(
                event,
                started_at,
                t0,
                f"输入({tag})@({x},{y}) {_input_summary_snippet(text)}（焦点未确认）",
                execution_started="no",
            )
        if target.evaluate("e => e.type === 'password'"):
            remember(text)
            event.params["sensitive"] = True
        landed, write_started = self._commit_input_value(
            page, target, text, x=x, y=y, field=login_field
        )
        if landed:
            return self._ok(
                event,
                started_at,
                t0,
                f"输入({tag})@({x},{y}) {_input_summary_snippet(text)}",
                execution_started="yes",
            )
        if write_started:
            return self._write_unknown(event, started_at, t0, RuntimeError("input_verification_failed"))
        return self._fail(
            event,
            started_at,
            t0,
            f"输入({tag})@({x},{y}) {_input_summary_snippet(text)}（焦点未确认）",
            execution_started="no",
        )

    def _input_focused(self, event, page, started_at, t0, text: str, tag: str, params: dict) -> EventResult:
        """写入当前焦点。不点击、不按坐标再找输入框。"""
        mode = str(params.get("mode") or "replace").strip().lower()
        target = None
        try:
            target, _ = self._focused_element(page)
            if target is not None and target.evaluate("e => e.type === 'password'"):
                from mino_scout.runtime.secret_redaction import remember
                remember(text)
                event.params["sensitive"] = True
            reason = "no_active_element" if target is None else target.evaluate("""element => {
                if (element === document.body || element === document.documentElement) {
                    if (!element.isContentEditable) return 'no_active_element';
                }
                if (element.matches(':disabled') || element.readOnly || element.closest('[inert]')) {
                    return 'not_editable';
                }
                if (element.isContentEditable || element.tagName === 'TEXTAREA') return '';
                if (element.tagName === 'INPUT' &&
                    ['text', 'search', 'email', 'url', 'tel', 'password', 'number'].includes(element.type)) return '';
                return 'not_editable';
            }""")
        except Exception:
            reason = "active_element_check_failed"
        finally:
            if target is not None:
                try:
                    target.dispose()
                except Exception:
                    pass
        if reason:
            return self._fail(event, started_at, t0, f"focus_unavailable: {reason}", execution_started="no")
        try:
            if mode != "append":
                page.keyboard.press("ControlOrMeta+A")
            page.keyboard.insert_text(text)
        except Exception as exc:
            return self._write_unknown(event, started_at, t0, exc)
        return self._ok(event, started_at, t0, f"输入({tag}) 已写入当前焦点", execution_started="yes")

    @staticmethod
    def _read_editable_value(target) -> str:
        try:
            raw = target.evaluate(
                """el => {
                  if (!el || !el.tagName) return '';
                  const tag = el.tagName.toLowerCase();
                  if (tag === 'input' || tag === 'textarea') return String(el.value || '');
                  return String(el.innerText || el.textContent || '').trim();
                }"""
            )
        except Exception:
            return ""
        return str(raw or "")

    @staticmethod
    def _value_has_expected(got: str, expected: str) -> bool:
        exp = str(expected or "").strip()
        val = str(got or "").strip()
        if not exp or not val:
            return False
        return exp in val

    def _commit_input_value(
        self,
        page,
        target,
        text: str,
        *,
        x: int,
        y: int,
        field: str,
    ) -> tuple[bool, bool]:
        """把字写进定位到的控件本身。成功只认框里包含完整文本。"""
        expected = str(text or "")
        from mino_scout.runtime.secret_redaction import remember
        try:
            password_field = bool(target.evaluate("e => e.type === 'password'"))
        except Exception:
            password_field = False
        if field in {"password", "otp"} or password_field:
            remember(expected)
        if not expected.strip():
            return False, False
        write_started = False
        try:
            write_started = True
            target.fill(expected, timeout=2000)
        except Exception:
            try:
                target.click(timeout=2000, force=True)
            except Exception:
                try:
                    target.focus()
                except Exception:
                    return False, write_started
            try:
                page.keyboard.press("Control+a")
            except Exception:
                pass
            try:
                page.keyboard.press("Meta+a")
            except Exception:
                pass
            try:
                page.keyboard.press("Backspace")
            except Exception:
                pass
            try:
                page.keyboard.type(expected, delay=15)
            except Exception:
                return False, write_started
        self._settle_page(page, ms=80)
        if self._value_has_expected(self._read_editable_value(target), expected):
            return True, write_started
        fresh = locate_editable_at(page, x, y, field=field)
        if fresh is None:
            return False, write_started
        return self._value_has_expected(self._read_editable_value(fresh), expected), write_started

    def _write_unknown(self, event, started_at, t0, exc: Exception) -> EventResult:
        return make_event_result(
            event, status=EventStatus.BLOCKED, executor_used=self.id,
            started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
            summary="写动作结果未核验，已停止后备执行并保留资源隔离", error=str(exc),
            raw_response={"sensitive": bool(event.params.get("sensitive")), "execution": {
                "execution_started": "unknown", "phase": "executor",
                "reason_code": "write_outcome_unknown", "source": "playwright.native.v1",
            }, "execution_control": {
                "stop_run": True, "reason_code": "write_outcome_unknown",
                "resource_isolated": True, "stop_confirmed": False,
            }},
        )

    @staticmethod
    def _settle_page(page, ms: int = 300) -> None:
        """DOM 变更后短等，避免下一帧截图卡在动画/布局抖动上。"""
        try:
            page.wait_for_timeout(max(0, int(ms)))
        except Exception:
            pass

    def _ok(self, event, started_at, t0, summary: str, *, execution_started: str = "unknown") -> EventResult:
        if event.params.get("sensitive"):
            summary = "敏感输入已完成（内容已脱敏）"
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id,
            started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
            summary=summary,
            raw_response={"sensitive": bool(event.params.get("sensitive")), "execution": {
                "execution_started": execution_started, "phase": "completed", "source": "playwright.native.v1"}},
        )

    def _fail(self, event, started_at, t0, msg: str, *, execution_started: str = "unknown",
              error: str = "") -> EventResult:
        if event.params.get("sensitive"):
            msg = "敏感输入失败（详细内容已脱敏）"
        execution = {
            "execution_started": execution_started,
            "phase": "preflight" if execution_started == "no" else "executor",
            "source": "playwright.native.v1",
        }
        if error:
            execution["reason_code"] = error
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id,
            started_at=started_at, elapsed_ms=int((time.time() - t0) * 1000),
            summary=msg, error=error or msg,
            raw_response={"sensitive": bool(event.params.get("sensitive")), "execution": execution},
        )


def _script_failure(exc: Exception, *, phase: str) -> dict:
    """Persist bounded error categories, never JavaScript source or page contents."""
    message = str(exc)
    category = "executor_error"
    for name in ("SyntaxError", "ReferenceError", "TypeError", "RangeError"):
        if name + ":" in message:
            category = name
            break
    else:
        if "TargetClosedError" in type(exc).__name__ or "Target page, context or browser has been closed" in message:
            category = "target_closed"
        elif "TimeoutError" in type(exc).__name__:
            category = "timeout"
        elif "Execution context was destroyed" in message:
            category = "context_destroyed"
    return {"contract": "executor_failure.v1", "phase": phase, "category": category}
