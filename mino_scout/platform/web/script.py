"""Web 用例脚本里的定位和页面动作。"""
from __future__ import annotations

from typing import Any
import uuid


def observe_control(app, selector):
    """Read exact CSS/test-id facts through the existing browser lane."""
    from mino_scout.platform.web.playwright_hub import get_hub
    from mino_scout.runtime.script_exec import ActionResult, ScriptAbort
    app._guard()
    page = get_hub().current_page(app._sn, run_id=app.run_id)
    if page is None:
        raise ScriptAbort("缺 Chromium 会话", reason_code="channel_missing", status="blocked")
    value = str(selector.get("value") or "")
    try:
        locator = page.locator(value) if selector["kind"] == "css" else page.get_by_test_id(value)
        observation = locator.evaluate_all("""els => {
            const count = els.length;
            if (count !== 1) return {count, nodes:[]};
            const el = els[0], sensitive = el.type === 'password';
            const style = getComputedStyle(el), rect = el.getBoundingClientRect();
            return {count, nodes:[{
                visible: style.visibility !== 'hidden' && style.visibility !== 'collapse' && rect.width > 0 && rect.height > 0,
                enabled: !el.matches(':disabled') && !el.closest('[aria-disabled="true"], [inert]'),
                focused: el.ownerDocument.activeElement === el,
                ...(['true','false'].includes(el.getAttribute('aria-selected')) ? {selected:el.getAttribute('aria-selected') === 'true'} : {}),
                ...(el.type === 'checkbox' || el.type === 'radio' ? {checked:!!el.checked,checked_known:true}
                    : ['true','false'].includes(el.getAttribute('aria-checked')) ? {checked:el.getAttribute('aria-checked') === 'true',checked_known:true} : {}),
                sensitive, text:sensitive ? '' : (el.innerText || el.textContent || ''),
                ...(sensitive || !('value' in el) ? {} : {value:el.value})
            }]};
        }""")
        app._guard()
        app._observation_id, app._artifact_id = "obs_" + uuid.uuid4().hex, ""
        result = ActionResult(ok=True, status="pass", summary="已读取控件属性", data=observation)
        app._report("control_observe", result, "playwright", 0, extra={"observation_id": app._observation_id})
        return result.data
    except ScriptAbort:
        raise
    except Exception:
        raise ScriptAbort("控件属性读取失败", reason_code="observation_insufficient", status="blocked") from None


def locator_params(kind: str, value: str, name: str, label: str) -> dict:
    params: dict[str, Any] = {"point_policy": "node", "target": {"text": label}, "selector_text": label}
    if kind == "css":
        params["css"] = value
    elif kind == "test_id":
        params["test_id"] = value
    elif kind == "role":
        params["selector_text"] = name
        params["target"] = {"text": name}
    return params


def act(case, kind: str, params: dict, text: str = ""):
    from mino_scout.platform.web.playwright_hub import get_hub
    from mino_scout.platform.web.playwright_locators import locator_from_params
    from mino_scout.runtime.script_exec import ActionResult, ScriptAbort

    case._guard()
    case._require_channel()
    page = get_hub().current_page(case._sn, run_id=case.run_id)
    if page is None:
        raise ScriptAbort("缺 chromium", reason_code="channel_missing")
    capability = "tap_element" if kind == "tap" else "input_text"
    case._begin_action(capability, {**params, **({"text": text} if kind != "tap" else {})})
    import time
    started = time.perf_counter()
    try:
        if params.get("coordinate_space") == "viewport_pixels":
            x, y = params["x"], params["y"]
            viewport = page.viewport_size or {}
            if not (0 <= x < viewport.get("width", 0) and 0 <= y < viewport.get("height", 0)):
                raise ScriptAbort("OCR 坐标超出当前视口", reason_code="observation_stale", status="blocked")
            case.touched = True
            page.mouse.click(x, y)
            if kind != "tap":
                page.keyboard.press("ControlOrMeta+A")
                page.keyboard.insert_text(text)
            action = ActionResult(ok=True, summary="已点击 OCR 目标" if kind == "tap" else f"输入 {len(text)} 字", status="pass", data={
                "execution": {"execution_started": "yes", "phase": "completed", "source": "playwright.native.v1"}})
            case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
            return action
        loc = locator_from_params(page, params, name=str(params.get("selector_text") or ""))
        if loc is None:
            action = ActionResult(ok=False, summary="没有点到控件", status="fail", data={
                "execution": {"execution_started": "no", "phase": "preflight", "reason_code": "locator_missing"}})
            case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
            raise ScriptAbort("没有点到控件", reason_code="locator_missing")
        try:
            loc.wait_for(state="visible", timeout=5000)
            if not loc.is_enabled():
                raise ValueError("not_enabled")
            if kind == "tap":
                loc.click(trial=True, timeout=5000)
        except Exception as exc:
            action = ActionResult(ok=False, summary="控件预检失败", status="fail", data={
                "execution": {"execution_started": "no", "phase": "preflight", "reason_code": "preflight_failed"}})
            case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
            raise ScriptAbort(f"控件预检失败: {type(exc).__name__}", reason_code="preflight_failed") from exc
        case.touched = True
        if kind == "tap":
            loc.click(timeout=5000)
            summary = "已点击"
        else:
            loc.fill(text, timeout=5000)
            summary = f"输入 {len(text)} 字"
        action = ActionResult(ok=True, summary=summary, status="pass", data={
            "execution": {"execution_started": "yes", "phase": "completed", "source": "playwright.native.v1"}})
    except ScriptAbort:
        raise
    except Exception as exc:
        action = ActionResult(ok=False, summary="页面动作失败", status="blocked", data={
            "execution": {"execution_started": "unknown", "phase": "executor", "reason_code": "write_outcome_unknown"},
            "execution_control": {"stop_run": True, "reason_code": "write_outcome_unknown",
                                  "resource_isolated": True, "stop_confirmed": False}})
        case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
        error = ScriptAbort(f"{type(exc).__name__}", reason_code="write_outcome_unknown", status="blocked",
                            details={"resource_isolated": True, "execution_started": "unknown"})
        case.case._latched_error = case.case._latched_error or error
        raise error from exc
    case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
    return action
