"""Authentication preparation and URI entry on the Scout browser lane."""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from mino_scout.platform.web.web_url import normalize_goto_url, origin, resolve_uri, same_entry_url


def _abort(message, code):
    from mino_scout.runtime.script_exec import ScriptAbort
    return ScriptAbort(message, reason_code=code, status="blocked")


def _account(app, account):
    if app._platform != "web":
        raise _abort("认证快照仅支持 Web", "web_auth_platform_invalid")
    accounts = getattr(app._case, "_accounts_by_slot", {})
    if account is None or accounts.get(getattr(account, "slot", "")) is not account:
        raise _abort("请先通过 Nexus 租用账号", "web_auth_account_not_leased")
    if account.platform != "web" or account.app_id != app._case.app_id or account.project_id == "":
        raise _abort("账号不属于当前 Web 应用", "web_auth_account_scope_mismatch")
    if origin(account.target_package) != origin(app._url):
        raise _abort("账号与应用环境不一致", "web_auth_account_scope_mismatch")
    return {"slot": account.slot, "account_id": account.id}


def _identity(account, field):
    if not isinstance(field, str) or field not in {"username", "email", "phone"}:
        raise _abort("identity_field 必须是 username/email/phone", "web_auth_identity_invalid")
    value = str(getattr(account, field, "") or "")
    if not value:
        raise _abort("账号缺少所选身份字段", "web_auth_identity_invalid")
    return value


def _verify_identity(app, control, value, timeout_ms, *, ready=False):
    """Exact identity match; preparation checks never count as business assertions."""
    from mino_scout.platform.web.script import observe_control
    if not isinstance(control, str) or not control:
        raise _abort("需要指定显示账号身份的控件", "web_auth_identity_control_required")
    from mino_scout.runtime.deterministic import control_selector
    selector = control_selector(app, control)
    if selector.get("kind") not in {"css", "test_id", "role"}:
        raise _abort("身份校验需要确定性 Web 定位", "web_auth_identity_control_invalid")
    end = time.monotonic() + timeout_ms / 1000
    while True:
        app._guard()
        observation = observe_control(app, selector)
        nodes = observation.get("nodes") or []
        if observation.get("count") == 1 and len(nodes) == 1 and nodes[0].get("visible") and (ready or
            str(nodes[0].get("text") or "").strip() == value or str(nodes[0].get("value") or "") == value):
            return
        if time.monotonic() >= end:
            if ready:
                raise _abort("目标页控件未就绪", "web_entry_not_ready")
            raise _abort("恢复后的账号身份未通过校验", "web_auth_identity_mismatch")
        from mino_scout.platform.web.playwright_hub import get_hub
        identity = app.identity
        get_hub().current_page(
            app._sn, run_id=app.run_id, actor=identity.get("actor", ""),
            context_id=identity.get("context_id", ""), page_id=identity.get("page_id", ""),
        ).wait_for_timeout(100)


def _timeout(value):
    if type(value) is not int or not 1 <= value <= 120000:
        raise _abort("timeout_ms 必须为 1–120000", "web_auth_timeout_invalid")
    return value


def _report(app, operation, started, data):
    from mino_scout.runtime.script_exec import ActionResult
    result = ActionResult(ok=True, status="pass", summary="认证准备已完成", data=data)
    app._report(operation, result, "playwright", int((time.monotonic() - started) * 1000))
    return result


def launch(app, uri, account, auth, identity_control, identity_field, ready_control, timeout_ms):
    started = time.monotonic()
    app._guard()
    timeout_ms = _timeout(timeout_ms)
    if not isinstance(auth, str) or auth not in {"none", "required"}:
        raise _abort("auth 必须为 none/required", "web_auth_policy_invalid")
    try:
        url = resolve_uri(app._url, uri) if uri else app._url
    except ValueError as exc:
        raise _abort(str(exc), str(exc)) from None
    params = {"package": app._package, "url": url, "timeout_ms": timeout_ms}
    if app._web_resolution:
        params["web_resolution"] = app._web_resolution
    if auth == "none":
        if account is not None:
            raise _abort("传入账号时请显式选择 auth='required'", "web_auth_policy_invalid")
        return app._device("launch_app", params)
    lease = _account(app, account)
    value = _identity(account, identity_field)
    if not identity_control or not ready_control:
        raise _abort("认证启动需要 identity_control 和 ready_control", "web_auth_entry_checks_required")
    # Validate controls before changing browser state.
    app._control_cell(identity_control)
    app._control_cell(ready_control)
    snapshot = app._callback("script.callback", {"op": "web_auth_get", **lease}, 15)
    state = snapshot.get("state")
    if state != "available":
        raise _abort(f"认证快照{ {'missing': '未准备', 'expired': '已过期', 'invalidated': '已失效'}.get(state, '不可用') }", "web_auth_" + str(state or "unavailable"))
    credential = snapshot.get("credential") or {}
    if credential.get("identity_field") != identity_field or credential.get("identity_value") != value:
        raise _abort("认证快照与指定账号身份不一致", "web_auth_identity_mismatch")
    register_secrets(credential)
    params["credential"] = credential
    params["fresh_context"] = True
    app._device("launch_app", params)
    try:
        _verify_identity(app, identity_control, value, timeout_ms)
    except Exception as exc:
        from mino_scout.runtime.script_exec import ScriptAbort
        # Cancellation, locator errors, and transport faults are not proof of expired auth.
        if isinstance(exc, ScriptAbort) and exc.reason_code == "web_auth_identity_mismatch":
            app._callback("script.callback", {"op": "web_auth_invalidate", **lease, "revision": snapshot["revision"]}, 15)
            _report_failure(app, started, snapshot, exc.reason_code)
        raise
    _verify_identity(app, ready_control, "", timeout_ms, ready=True)
    from mino_scout.platform.web.playwright_hub import get_hub
    identity = app.identity
    actual_url = get_hub().current_page(
        app._sn, run_id=app.run_id, actor=identity.get("actor", ""),
        context_id=identity.get("context_id", ""), page_id=identity.get("page_id", ""),
    ).url
    if origin(actual_url) != origin(url):
        raise _abort("目标页跳转到其他环境", "web_uri_origin_mismatch")
    if not same_entry_url(actual_url, url):
        raise _abort("当前 URI 与指定用例入口不一致", "web_entry_uri_mismatch")
    return _report(app, "web_auth_restore", started, {"account_id": account.id, "revision": snapshot["revision"],
        "requested_url": url, "actual_url": actual_url, "identity_verified": True, "ready_control": ready_control})


def _report_failure(app, started, snapshot, reason):
    from mino_scout.runtime.script_exec import ActionResult
    app._report("web_auth_restore", ActionResult(ok=False, status="blocked", summary="认证准备未通过",
        data={"revision": snapshot.get("revision"), "reason_code": reason}), "playwright", int((time.monotonic() - started) * 1000))


def register_secrets(value):
    from mino_scout.runtime.secret_redaction import remember
    def remember_value(secret):
        # Short preference values such as "1" must not corrupt unrelated evidence.
        # The complete credential structure is always opaque at output boundaries.
        if isinstance(secret, str) and len(secret) >= 8:
            remember(secret)
    state = value.get("storage_state") or {}
    for cookie in state.get("cookies") or []:
        remember_value(cookie.get("value"))
    for item in state.get("origins") or []:
        for cell in item.get("localStorage") or []:
            remember_value(cell.get("value"))
    for items in (value.get("session_storage") or {}).values():
        for secret in items.values():
            remember_value(secret)


def save(app, account, identity_control, identity_field, ttl_seconds, timeout_ms):
    started = time.monotonic()
    app._guard()
    lease = _account(app, account)
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 604800:
        raise _abort("ttl_seconds 必须为 60–604800", "web_auth_ttl_invalid")
    value = _identity(account, identity_field)
    _verify_identity(app, identity_control, value, _timeout(timeout_ms))
    from mino_scout.platform.web.playwright_hub import get_hub
    identity = app.identity
    page = get_hub().current_page(
        app._sn, run_id=app.run_id, actor=identity.get("actor", ""),
        context_id=identity.get("context_id", ""), page_id=identity.get("page_id", ""),
    )
    expected_origin = origin(app._url)
    if origin(page.url) != expected_origin:
        raise _abort("当前页面不属于账号环境", "web_auth_origin_mismatch")
    try:
        # Pinned Playwright 1.49 supports cookies/localStorage; IndexedDB is not exported.
        state = page.context.storage_state()
        host = urlsplit(expected_origin).hostname
        state["cookies"] = [c for c in state["cookies"] if host == c["domain"].lstrip(".").lower() or
            (c["domain"].startswith(".") and host.endswith("." + c["domain"].lstrip(".").lower()))]
        state["origins"] = [item for item in state["origins"] if item["origin"] == expected_origin]
        sessions = page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
        credential = {"storage_state": state, "session_storage": {expected_origin: sessions},
            "identity_field": identity_field, "identity_value": value}
        if len(json.dumps(credential, ensure_ascii=True, separators=(",", ":")).encode()) > 196608:
            raise _abort("认证快照超过 192 KiB", "web_auth_snapshot_too_large")
        register_secrets(credential)
    except Exception as exc:
        from mino_scout.runtime.script_exec import ScriptAbort
        if isinstance(exc, ScriptAbort):
            raise
        raise _abort("认证快照读取失败", "web_auth_capture_failed") from None
    _verify_identity(app, identity_control, value, 1)
    result = app._callback("script.callback", {"op": "web_auth_save", **lease, "credential": credential,
        "ttl_seconds": ttl_seconds}, 15)
    return _report(app, "web_auth_save", started, {"account_id": account.id, **result, "identity_verified": True})
