"""Exact observations and bounded polling; no generated-model fallback."""
from __future__ import annotations

import time


def control_selector(app, control):
    from mino_scout.runtime.script_exec import ScriptAbort
    page_key, control_id = app._split_control(control)
    page = app._pages.get(page_key) or {}
    controls = [row for row in page.get("controls", []) if isinstance(row, dict) and row.get("id") == control_id]
    if len(controls) != 1:
        raise ScriptAbort("控件未定义", reason_code="control_unknown", status="blocked")
    from mino_scout.runtime.script_exec import _CELLS
    cell = (controls[0].get("locator_spec") or {}).get(_CELLS[app._platform]) or {}
    selector = cell.get("selector") or {}
    return selector


def control_nodes(app, control, nodes):
    from mino_scout.runtime.script_exec import ScriptAbort
    selector = control_selector(app, control)
    key = {"text": "text", "resource_id": "resource_id", "content_desc": "content_desc", "class": "class",
           "name": "content_desc", "accessibility_id": "content_desc", "label": "text", "title": "text", "role": "role"}.get(selector.get("kind"))
    value = selector.get("value") or selector.get("name")
    if key is None or not value:
        raise ScriptAbort("该定位尚无可用于确定性断言的节点属性，请使用原生文字/ID定位或显式 OCR 文字断言",
                          reason_code="observation_insufficient", status="blocked")
    found = [n for n in nodes if str(n.get(key) or "") == str(value)]
    if selector.get("kind") == "role" and selector.get("name"):
        found = [n for n in found if selector["name"] in {n.get("text"), n.get("content_desc")}]
    return found


def read_condition(app, kind, subject, control, ocr, region):
    from mino_scout.runtime.script_exec import ScriptAbort
    if ocr:
        if kind != "text" or control:
            raise ScriptAbort("OCR 只验证指定区域的文字，不验证原生控件状态", reason_code="observation_insufficient", status="blocked")
        data = app.ocr(region=region).data
        nodes = data.get("nodes", [])
        matched = [n for n in nodes if n["text"] == subject]
        actual = {"matched": [n["text"] for n in matched], "observed": [n["text"] for n in nodes]}
        return bool(matched), actual, not any(n["confidence"] < .8 for n in nodes)
    data, nodes, complete = {}, [], False
    if control:
        selector = control_selector(app, control)
        if app._platform == "web" and selector.get("kind") in {"css", "test_id"}:
            from mino_scout.platform.web.script import observe_control
            observation = observe_control(app, selector)
            nodes = observation["nodes"]
            complete = True
            if kind == "count":
                return observation["count"], {"count": observation["count"]}, True
            if observation["count"] != 1:
                return (False, {"matches": observation["count"]}, observation["count"] == 0) if kind == "state" and subject == "visible" else (None, {"matches": observation["count"]}, False)
    if not data and not (control and app._platform == "web" and selector.get("kind") in {"css", "test_id"}):
        data = app.hierarchy().data
        nodes = data.get("nodes", [])
        complete = not data.get("truncated", True)
        if control:
            if not data.get("structured"):
                return None, {"reason": "structured_nodes_missing"}, False
            nodes = control_nodes(app, control, nodes)
    if kind == "count":
        return len(nodes), {"count": len(nodes)}, complete
    if kind == "state" and subject == "visible":
        visible = any(n.get("visible") is True for n in nodes)
        known = visible or complete and all(type(n.get("visible")) is bool for n in nodes)
        return visible if known else None, {"visible": visible if known else None, "matches": len(nodes)}, known
    if kind in {"state", "value"}:
        if len(nodes) != 1:
            return None, {"matches": len(nodes)}, False
        node = nodes[0]
        if kind == "value" and node.get("sensitive"):
            return None, {"reason": "secret_value_unreadable"}, False
        field = subject if kind == "state" else "value"
        if field == "value" and "value" not in node and node.get("editable"):
            field = "text"
        known = field in node and (field != "checked" or node.get("checked_known", True))
        return node.get(field), {field: node.get(field)}, known and complete
    texts = [str(n.get(k) or "") for n in nodes for k in ("text", "content_desc") if n.get(k)]
    if not nodes and not control:
        texts = str(data.get("text") or "").splitlines()
    if control and not subject:
        visible = [n for n in nodes if n.get("visible") is not False]
        return bool(visible), {"matches": len(nodes)}, complete or bool(visible)
    matched = [s for s in texts if s == subject]
    # Positive exact text can be established in a partial tree; absence cannot.
    return bool(matched), {"matched": matched, "observed": list(dict.fromkeys(texts))[:32]}, complete or bool(matched)


def verify(app, kind, subject, *, control="", wanted=True, timeout_ms=0, ocr=False, region=None):
    from mino_scout.runtime.script_exec import ActionResult, ScriptAbort
    if type(timeout_ms) is not int or not 0 <= timeout_ms <= 60000:
        raise ScriptAbort("timeout_ms 必须是 0..60000", reason_code="assertion_invalid", status="blocked")
    if kind == "state" and (not control or subject not in {"visible", "enabled", "checked", "selected", "focused"}):
        raise ScriptAbort("状态断言需要控件和受支持的状态", reason_code="assertion_invalid", status="blocked")
    if kind == "count" and (not control or type(wanted) is not int or wanted < 0):
        raise ScriptAbort("数量断言需要控件和非负整数", reason_code="assertion_invalid", status="blocked")
    if kind in {"state", "text"} and type(wanted) is not bool or kind == "text" and not subject and not control:
        raise ScriptAbort("文字/状态断言需要明确目标和布尔预期", reason_code="assertion_invalid", status="blocked")
    deadline = time.monotonic() + timeout_ms / 1000
    expected = {"control": control, "text" if kind == "text" else "state": subject, "expected": wanted}
    while True:
        app._guard()
        value, actual, sufficient = read_condition(app, kind, subject, control, ocr, region)
        passed = sufficient and type(value) is type(wanted) and value == wanted
        if passed or time.monotonic() >= deadline:
            status = "pass" if passed else "fail" if sufficient else "unknown"
            reason = "" if passed else "assertion_failed" if sufficient else "observation_insufficient"
            app.case._assert("ocr_text" if ocr else kind, status, expected=expected, actual=actual,
                             observation_id=app._observation_id, artifact_id=app._artifact_id, app=app.key,
                             reason_code=reason, message="" if passed else "业务预期未满足" if sufficient else "当前观察不足以判定预期")
            if not passed:
                raise app.case._latched_error
            return ActionResult(ok=True, status="pass", summary="业务预期已核验", data=actual)
        # Poll only observations; a final mismatch registers exactly one assertion.
        app.case.scout.wait(min(200, max(1, int((deadline - time.monotonic()) * 1000))))
