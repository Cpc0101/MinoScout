#!/usr/bin/env python3
"""Offline browser-lane contract smoke; no browser or target is launched."""
from __future__ import annotations
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mino_scout.platform.web import auth, playwright_hub, script
from mino_scout.runtime.script_exec import ActionResult, App, ScriptAbort
from mino_scout.runtime.records import scrub
from mino_scout.runtime.secret_redaction import sanitize


def check():
    base = "https://example.test/app/"
    assert auth.resolve_uri(base, "/orders/123?q=a#detail") == "https://example.test/orders/123?q=a#detail"
    assert auth.resolve_uri(base, "#/orders/123") == base + "#/orders/123"
    for uri in ("https://foreign.test/orders", "//foreign.test/", "javascript:alert(1)", " /bad", "\\foreign.test"):
        try:
            auth.resolve_uri(base, uri)
        except ValueError:
            pass
        else:
            raise AssertionError(uri)
    credential = {"storage_state": {"cookies": [{"domain": "example.test", "name": "session", "value": "fixture-cookie-secret", "path": "/"}],
        "origins": [{"origin": "https://example.test", "localStorage": [{"name": "auth", "value": "fixture-local-secret"}]}]},
        "session_storage": {"https://example.test": {"auth": "fixture-session-secret"}},
        "identity_field": "username", "identity_value": "fixture-user"}
    auth.register_secrets(credential)
    for secret in ("fixture-cookie-secret", "fixture-local-secret", "fixture-session-secret"):
        assert secret not in json.dumps(scrub({"credential": credential}))
        assert secret not in sanitize("exception contained " + secret)
    assert sanitize("session") == "session"  # Cookie names are not registered as secrets.

    calls = []
    class CDP:
        def send(self, name, params):
            calls.append(name)
            if name == "Page.addScriptToEvaluateOnNewDocument":
                assert "sessionStorage.setItem" in params["source"]
                return {"identifier": "fixture"}
        def detach(self): calls.append("detach")
    class Page:
        url = base
        def on(self, event, callback):
            calls.append("on:" + event)
        def goto(self, url, **kwargs):
            calls.append("goto:" + url)
            self.url = url
        def wait_for_timeout(self, ms): pass
        def evaluate(self, code): return {"auth": "fixture-session-secret"}
    class Context:
        def on(self, event, callback): calls.append("on:" + event)
        def new_page(self): calls.append("new_page"); return page
        def new_cdp_session(self, p): return CDP()
        def close(self): calls.append("context_close")
        def storage_state(self): return copy.deepcopy(credential["storage_state"])
    class Browser:
        def new_context(self, **kwargs):
            calls.append("new_context")
            assert kwargs["storage_state"] == credential["storage_state"]
            return context
    page, context = Page(), Context()
    page.context = context
    hub = playwright_hub.PlaywrightHub.__new__(playwright_hub.PlaywrightHub)
    hub.close_case = lambda *a, **k: calls.append("close_case")
    hub._browser = lambda *a, **k: Browser()
    hub._publish_session = lambda *a, **k: calls.append("publish")
    uri = "https://example.test/orders/123#detail"
    assert hub.open_case("webfixture", run_id="fixture", base_url=uri, credential=credential) is page
    assert calls.index("new_context") < calls.index("new_page") < calls.index("Page.addScriptToEvaluateOnNewDocument") < calls.index("goto:" + uri)
    assert calls.index("goto:" + uri) < calls.index("Page.removeScriptToEvaluateOnNewDocument") < calls.index("publish")
    assert "goto:" + base not in calls
    calls.clear()
    original_goto = page.goto
    page.goto = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline navigation failure"))
    try:
        hub.open_case("webfixture", run_id="fixture", base_url=uri, credential=credential)
    except RuntimeError:
        assert "context_close" in calls and "Page.removeScriptToEvaluateOnNewDocument" in calls
        assert "publish" not in calls
    else:
        raise AssertionError("navigation failure was swallowed")
    page.goto = original_goto

    account = SimpleNamespace(slot="default", id="account-a", app_id="app-a", project_id="project-a",
        platform="web", target_package=base, username="fixture-user")
    case = SimpleNamespace(_platform="web", app_id="app-a", _accounts_by_slot={"default": account},
                           _sn="webfixture", run_id="fixture", _latched_error=None)
    app = App.__new__(App)
    app._case, app._url, app._package, app._web_resolution = case, base, base, ""
    app._guard = lambda: None
    app._control_cell = lambda c: {"selector": {"kind": "test_id", "value": c}}
    results, callbacks, devices = [], [], []
    app._report = lambda cap, result, *args: results.append((cap, result.data))
    app._device = lambda cap, params: devices.append((cap, params))
    snapshot = {"state": "available", "revision": "snapshot-1", "credential": credential}
    def callback(cap, params, timeout):
        callbacks.append(params)
        return snapshot if params["op"] == "web_auth_get" else {"state": "available", "revision": "saved"}
    app._callback = callback
    old_observe, old_hub = script.observe_control, playwright_hub.get_hub
    try:
        playwright_hub.get_hub = lambda: SimpleNamespace(current_page=lambda *a, **k: page)
        script.observe_control = lambda *a: {"count": 1, "nodes": [{"visible": True, "text": "fixture-user"}]}
        app.launch("/orders/123#detail", account=account, auth="required", identity_control="identity", ready_control="orders")
        assert devices[-1][1]["fresh_context"] and devices[-1][1]["url"] == uri
        assert results[-1][1]["identity_verified"] and callbacks[-1]["op"] == "web_auth_get"
        app.save_auth(account, identity_control="identity")
        saved = callbacks[-1]
        assert saved["op"] == "web_auth_save" and saved["credential"]["session_storage"] == credential["session_storage"]
        snapshot["state"] = "expired"
        before = len(devices)
        try:
            app.launch("/orders", account=account, auth="required", identity_control="identity", ready_control="orders")
        except ScriptAbort as exc:
            assert exc.reason_code == "web_auth_expired" and len(devices) == before
        else:
            raise AssertionError("expired snapshot was used")
        snapshot["state"] = "available"
        script.observe_control = lambda *a: {"count": 1, "nodes": [{"visible": True, "text": "wrong-user"}]}
        try:
            app.launch("/orders", account=account, auth="required", identity_control="identity", ready_control="orders", timeout_ms=1)
        except ScriptAbort as exc:
            assert exc.reason_code == "web_auth_identity_mismatch"
            assert callbacks[-1]["op"] == "web_auth_invalidate" and callbacks[-1]["revision"] == "snapshot-1"
        else:
            raise AssertionError("wrong identity was accepted")
        script.observe_control = lambda app, selector: {"count": 1 if selector["value"] == "identity" else 0,
            "nodes": [{"visible": True, "text": "fixture-user"}] if selector["value"] == "identity" else []}
        try:
            app.launch("/orders", account=account, auth="required", identity_control="identity", ready_control="orders", timeout_ms=1)
        except ScriptAbort as exc:
            assert exc.reason_code == "web_entry_not_ready" and callbacks[-1]["op"] == "web_auth_get"
        else:
            raise AssertionError("missing target control was accepted")
        script.observe_control = lambda *a: {"count": 1, "nodes": [{"visible": True, "text": "fixture-user"}]}
        page.url = base
        try:
            app.launch("/orders", account=account, auth="required", identity_control="identity", ready_control="orders")
        except ScriptAbort as exc:
            assert exc.reason_code == "web_entry_uri_mismatch" and callbacks[-1]["op"] == "web_auth_get"
        else:
            raise AssertionError("root redirect was accepted as direct entry")
        app.launch("/orders")
        assert "credential" not in devices[-1][1]
        app.launch()
        assert devices[-1][1]["url"] == base  # Backward compatibility.
    finally:
        script.observe_control, playwright_hub.get_hub = old_observe, old_hub

    from mino_scout.runtime import web_api
    assert web_api._url_match("https://example.test/orders/", "/orders", exact=False)
    assert not web_api._url_match("https://example.test/orders-old", "/orders", exact=False)
    assert not web_api._url_match("https://example.test/orders?page=1", "/orders", exact=False)
    assert not web_api._url_match("https://example.test/orders#detail", "/orders", exact=False)
    assert not web_api._url_match("https://other.test/orders", "https://example.test/orders", exact=False)
    assert web_api._url_match("https://example.test/orders?page=1#detail", "https://example.test/orders?page=1#detail", exact=False)
    assert web_api._url_match("https://example.test/orders", "https://example.test/orders", exact=True)
    assert not web_api._url_match("https://example.test/orders/", "https://example.test/orders", exact=True)

    class ProbeCase:
        def __init__(self):
            self.rows, self._latched_error = [], None
        def _assert(self, checker, status, **evidence):
            row = {"checker": checker, "status": status, **evidence}
            self.rows.append(row)
            if status != "pass":
                self._latched_error = ScriptAbort(str(evidence.get("message") or "未通过"),
                    reason_code=str(evidence.get("reason_code") or "assertion_failed"),
                    status="blocked" if status == "unknown" else "fail")
            return row

    class ProbeApp:
        _platform = "web"
        key = "web"
        _seq = 7
        def __init__(self, responses):
            self._case = ProbeCase()
            self.responses = list(responses)
            self.calls = []
        def _guard(self): pass
        def _locate(self, control, capability):
            return {"control": control, "capability": capability}
        def _device(self, capability, params, **kwargs):
            self.calls.append((capability, params, kwargs))
            value = self.responses.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

    response = {
        "request_id": "req-1", "method": "POST", "path": "/api/orders", "status": 201,
        "body": {"available": True, "format": "json", "truncated": False,
                 "fields": {"code": {"found": True, "value": "SUCCESS"},
                            "data.id": {"found": True, "value": "order-1"}}},
        "correlation": {"action_seq": 7, "actor": "applicant"},
    }
    app = ProbeApp([
        ActionResult(ok=True, status="pass", summary="registered", data={"expectation": {"expectation_id": "expect-1"}}),
        ActionResult(ok=True, status="pass", summary="matched", data=response),
    ])
    expectation = web_api._Network(app).expect_response(method="post", path="/api/orders")
    with expectation as captured:
        pass
    assert captured is expectation.response
    assert captured.request_id == "req-1" and captured.status == 201
    captured.expect.status(201)
    captured.expect.json_field("data.id", "order-1")
    assert [row["checker"] for row in app._case.rows] == ["network_status", "network_json_field"]
    assert app.calls[0][1]["method"] == "POST" and app.calls[1][1]["expectation_id"] == "expect-1"

    app = ProbeApp([
        ActionResult(ok=True, status="pass", summary="registered", data={"expectation": {"expectation_id": "expect-2"}}),
        ActionResult(ok=True, status="pass", summary="cancelled", data={}),
    ])
    try:
        with web_api._Network(app).expect_response(path="/api/orders"):
            raise RuntimeError("action failed")
    except RuntimeError:
        pass
    assert app.calls[-1][0] == "network_expect_cancel" and app.calls[-1][2]["allow_latched"] is True

    for body, reason in (
        ({"available": False, "format": "unknown", "truncated": False, "reason": "redacted"}, "network_body_unavailable"),
        ({"available": True, "format": "text", "truncated": False}, "network_body_not_json"),
        ({"available": True, "format": "json", "truncated": True}, "network_body_truncated"),
    ):
        app = ProbeApp([])
        item = web_api._NetworkResponse(app, {**response, "body": body})
        try:
            item.expect.json_field("code", "SUCCESS")
        except ScriptAbort as exc:
            assert exc.reason_code == reason
        else:
            raise AssertionError(reason)
        assert app._case.rows[-1]["status"] == "unknown"

    secret = "https://example.test/invitation/private-token"
    evidence = {"expected": web_api._text_evidence(secret), "actual": web_api._text_evidence(secret),
                "exact": True, "matched": True}
    app = ProbeApp([ActionResult(ok=True, status="pass", summary="matched", data={"clipboard": evidence})])
    web_api._Clipboard(app).expect_text(secret)
    assert app._case.rows[-1]["status"] == "pass"
    assert secret not in json.dumps(app._case.rows, ensure_ascii=False)
    assert app._case.rows[-1]["expected"]["text"]["sha256"] == web_api._text_evidence(secret)["sha256"]
    bad = {"expected": web_api._text_evidence(secret), "actual": web_api._text_evidence(secret + "-old"),
           "exact": True, "matched": False}
    app = ProbeApp([ActionResult(ok=False, status="fail", summary="mismatch", data={"clipboard": bad})])
    try:
        web_api._Clipboard(app).expect_text(secret)
    except ScriptAbort as exc:
        assert exc.reason_code == "clipboard_mismatch"
    else:
        raise AssertionError("clipboard mismatch did not latch")
    assert app.calls[0][2]["allow_failed"] is True
    assert secret not in json.dumps(app._case.rows, ensure_ascii=False)

    app = ProbeApp([ActionResult(ok=True, status="pass", summary="focused", data={})])
    web_api._Expectation(app).focused("form.email")
    assert app._case.rows[-1]["status"] == "pass"
    app = ProbeApp([ActionResult(ok=False, status="fail", summary="目标控件没有焦点", data={})])
    try:
        web_api._Expectation(app).focused("form.email")
    except ScriptAbort as exc:
        assert exc.reason_code == "focus_mismatch"
    else:
        raise AssertionError("focus mismatch did not latch")
    assert app.calls[0][2]["allow_failed"] is True

    clock_app = App.__new__(App)
    clock_case = SimpleNamespace(_platform="web")
    clock_app._case, clock_app._url, clock_app._package, clock_app._platform = clock_case, base, base, "web"
    clock_app._web_resolution = ""
    clock_actions = []
    clock_app._device = lambda cap, params: clock_actions.append((cap, params)) or ActionResult(ok=True, status="pass", summary="clock")
    clock_app.set_page_time(fixed_time="2026-01-02T03:04:05Z")
    clock_app.set_page_time(fast_forward_ms=1000)
    clock_app.set_page_time(run_for_ms=500)
    clock_app.set_page_time(pause_at="2026-01-02T03:04:06Z")
    clock_app.set_page_time(resume=True)
    assert [params["action"] for _, params in clock_actions] == ["set_fixed", "fast_forward", "run_for", "pause", "resume"]

    print("PASS: URI/authentication, URL components, network scope/cancel/unknown-body, clipboard redaction, focus assertions, page clock")


if __name__ == "__main__":
    check()
