"""Web 用例脚本里的定位和页面动作。"""
from __future__ import annotations

from typing import Any


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
    case.touched = True
    capability = "tap_element" if kind == "tap" else "input_text"
    case._begin_action(capability, {**params, **({"text": text} if kind != "tap" else {})})
    import time
    started = time.perf_counter()
    try:
        loc = locator_from_params(page, params, name=str(params.get("selector_text") or ""))
        if loc is None:
            action = ActionResult(ok=False, summary="没有点到控件", status="fail", data={})
            case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
            raise ScriptAbort("没有点到控件", reason_code="script_failed")
        if kind == "tap":
            loc.click(timeout=5000)
            summary = "已点击"
        else:
            loc.fill(text, timeout=5000)
            summary = f"输入 {len(text)} 字"
        action = ActionResult(ok=True, summary=summary, status="pass", data={})
    except ScriptAbort:
        raise
    except Exception as exc:
        action = ActionResult(ok=False, summary="页面动作失败", status="fail", data={})
        case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
        error = ScriptAbort(f"{type(exc).__name__}", reason_code="write_outcome_unknown", status="blocked",
                            details={"resource_isolated": True})
        case.case._latched_error = case.case._latched_error or error
        raise error from exc
    case._report(capability, action, "playwright", int((time.perf_counter() - started) * 1000))
    return action
