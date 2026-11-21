"""Stable Web script API layered over the existing Playwright capabilities."""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit


def _abort(message: str, reason_code: str, *, status: str = "fail", app=None):
    from mino_scout.runtime.script_exec import ScriptAbort

    error = ScriptAbort(message, reason_code=reason_code, status=status)
    if app is not None and status != "pass":
        app._case._latched_error = app._case._latched_error or error
    return error


def _require_web(app) -> None:
    if app._platform != "web":
        raise _abort("该能力仅支持 Web", "web_capability_platform_invalid", status="blocked", app=app)


def _assert(app, checker: str, status: str, *, expected: Any, actual: Any,
            reason_code: str = "", message: str = "", **extra):
    app._guard()
    row = app._case._assert(
        checker,
        status,
        expected=expected,
        actual=actual,
        app=app.key,
        reason_code=reason_code,
        message=message,
        **extra,
    )
    if status != "pass":
        raise app._case._latched_error
    return row


def _text_evidence(value: str) -> dict[str, Any]:
    raw = value.encode("utf-8")
    return {"length": len(value), "sha256": hashlib.sha256(raw).hexdigest()}


def _url_match(actual: str, expected: str, *, exact: bool) -> bool:
    """Compare a Web location without treating path prefixes as equal.

    An absolute expectation must retain scheme, host, port, path, query and
    fragment. A root-relative expectation compares those latter three parts
    against the actual location. Non-exact matching only tolerates one trailing
    slash on a non-root path; query and fragment differences stay observable.
    """
    if exact:
        return actual == expected
    try:
        got = urlsplit(actual)
        want = urlsplit(expected)
    except ValueError:
        return False
    if not got.scheme or not got.netloc:
        return False
    if want.scheme or want.netloc:
        if (got.scheme.lower(), got.netloc.lower()) != (want.scheme.lower(), want.netloc.lower()):
            return False
    elif not expected.startswith("/"):
        return False
    got_path = got.path or "/"
    want_path = want.path or "/"
    if got_path != "/" and want_path != "/":
        got_path = got_path.rstrip("/")
        want_path = want_path.rstrip("/")
    return (got_path, got.query, got.fragment) == (want_path, want.query, want.fragment)


def _json_field(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


class _NetworkResponseExpectation:
    def __init__(self, response: "_NetworkResponse"):
        self._response = response

    def status(self, expected: int):
        if type(expected) is not int or not 100 <= expected <= 599:
            raise _abort("HTTP 状态码必须是 100..599 的整数", "network_assertion_invalid",
                         status="blocked", app=self._response.app)
        actual = self._response.status
        passed = actual == expected
        return _assert(
            self._response.app,
            "network_status",
            "pass" if passed else "fail",
            expected={"http_status": expected},
            actual={"http_status": actual, "request_id": self._response.request_id},
            reason_code="" if passed else "network_status_mismatch",
            message="" if passed else "HTTP 响应状态与预期不一致",
        )

    def json_field(self, path: str, expected: Any):
        app = self._response.app
        body = self._response.body if isinstance(self._response.body, dict) else {}
        available = body.get("available") is True
        is_json = body.get("format") == "json"
        truncated = body.get("truncated") is True
        if not available or not is_json or truncated:
            reason = "network_body_unavailable" if not available else "network_body_not_json"
            if truncated:
                reason = "network_body_truncated"
            actual = {
                "request_id": self._response.request_id,
                "body": {
                    "available": available,
                    "format": body.get("format", ""),
                    "truncated": truncated,
                    "reason": body.get("reason", ""),
                },
            }
            return _assert(
                app,
                "network_json_field",
                "unknown",
                expected={"path": str(path), "value": expected},
                actual=actual,
                reason_code=reason,
                message="响应体证据不足，无法判定 JSON 字段",
            )
        field = body.get("fields", {}).get(path)
        if field is None:
            return _assert(app, "network_json_field", "unknown",
                           expected={"path": path, "value": expected},
                           actual={"request_id": self._response.request_id},
                           reason_code="network_field_not_captured",
                           message="该字段未声明在 capture_fields 中")
        found, actual_value = field.get("found", False), field.get("value")
        passed = found and actual_value == expected and (
            not isinstance(actual_value, bool) and not isinstance(expected, bool) or type(actual_value) is type(expected))
        return _assert(
            app,
            "network_json_field",
            "pass" if passed else "fail",
            expected={"path": str(path), "value": expected},
            actual={"found": found, "value": actual_value if found else None,
                    "request_id": self._response.request_id},
            reason_code="" if passed else "network_json_field_missing" if not found else "network_json_field_mismatch",
            message="" if passed else "响应 JSON 字段缺失" if not found else "响应 JSON 字段与预期不一致",
        )


class _NetworkResponse:
    def __init__(self, app, data: dict[str, Any]):
        self.app = app
        self.expect = _NetworkResponseExpectation(self)
        self.update(data)

    def update(self, data: dict[str, Any]) -> "_NetworkResponse":
        self.request_id = str(data.get("request_id") or "")
        self.method = str(data.get("method") or "")
        self.path = str(data.get("path") or "")
        try:
            self.status = int(data.get("status"))
        except (TypeError, ValueError):
            self.status = 0
        self.body = dict(data.get("body") or {}) if isinstance(data.get("body"), dict) else {}
        self.correlation = dict(data.get("correlation") or {}) if isinstance(data.get("correlation"), dict) else {}
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "body": dict(self.body),
            "correlation": dict(self.correlation),
        }


class _NetworkExpectationContext:
    def __init__(self, app, *, method: str, path: str, url_contains: str,
                 status: int | None, json_fields: dict[str, Any] | None,
                 timeout_ms: int, occurrence: int | None, capture_fields: list[str] | None):
        self.app = app
        self.method = str(method or "").upper()
        self.path = str(path or "")
        self.url_contains = str(url_contains or "")
        self.status_filter = status
        self.json_fields = dict(json_fields or {})
        self.timeout_ms = timeout_ms
        self.occurrence = occurrence
        self.capture_fields = capture_fields
        self.expectation_id = ""
        self.response: _NetworkResponse | None = None
        self._entered = False
        self._exited = False

    def _validate(self) -> None:
        _require_web(self.app)
        if self.method and self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise _abort("method 必须是标准 HTTP 方法", "network_expectation_invalid", status="blocked", app=self.app)
        if self.status_filter is not None and (
            type(self.status_filter) is not int or not 100 <= self.status_filter <= 599
        ):
            raise _abort("status 必须是 100..599 的整数", "network_expectation_invalid", status="blocked", app=self.app)
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= 120_000:
            raise _abort("timeout_ms 必须是 1..120000 的整数", "network_expectation_invalid", status="blocked", app=self.app)
        if self.occurrence is not None and (type(self.occurrence) is not int or not 1 <= self.occurrence <= 100):
            raise _abort("occurrence 必须是 1..100 的整数", "network_expectation_invalid", status="blocked", app=self.app)
        if not any((self.method, self.path, self.url_contains, self.status_filter is not None, self.json_fields)):
            raise _abort("至少提供一个网络响应匹配条件", "network_expectation_invalid", status="blocked", app=self.app)
        if self.capture_fields is not None and (not isinstance(self.capture_fields, list) or
                any(not isinstance(path, str) for path in self.capture_fields)):
            raise _abort("capture_fields 必须是字段路径列表", "network_expectation_invalid", status="blocked", app=self.app)

    def __enter__(self) -> _NetworkResponse:
        if self._entered:
            raise _abort("网络期望不能重复进入", "network_expectation_invalid", status="blocked", app=self.app)
        self._validate()
        self._entered = True
        params = {"action_seq": self.app._seq, "occurrence": self.occurrence}
        if self.capture_fields:
            params["capture_fields"] = self.capture_fields
        if self.method:
            params["method"] = self.method
        if self.path:
            params["path"] = self.path
        if self.url_contains:
            params["url_contains"] = self.url_contains
        if self.status_filter is not None:
            params["status"] = self.status_filter
        if self.json_fields:
            params["json_fields"] = self.json_fields
        result = self.app._device("network_expect_register", params)
        expectation = result.data.get("expectation") if isinstance(result.data, dict) else {}
        self.expectation_id = str((expectation or {}).get("expectation_id") or "")
        if not self.expectation_id:
            raise _abort("网络期望注册未返回 expectation_id", "network_expectation_missing",
                         status="blocked", app=self.app)
        self.response = _NetworkResponse(self.app, {})
        return self.response

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._entered or self._exited:
            return False
        self._exited = True
        try:
            if exc_type is not None:
                try:
                    self.app._device(
                        "network_expect_cancel",
                        {"expectation_id": self.expectation_id},
                        allow_latched=True,
                    )
                except Exception:
                    pass
                return False
            result = self.app._device(
                "network_expect_wait",
                {"expectation_id": self.expectation_id, "timeout_ms": self.timeout_ms},
            )
            self.response.update(result.data if isinstance(result.data, dict) else {})
            return False
        except Exception as error:
            if exc_type is not None:
                return False
            raise error


class _Network:
    def __init__(self, app):
        self.app = app

    def expect_response(self, *, method: str = "", path: str = "", url_contains: str = "",
                        status: int | None = None, json_fields: dict[str, Any] | None = None,
                        timeout_ms: int = 10_000, occurrence: int | None = None,
                        capture_fields: list[str] | None = None) -> _NetworkExpectationContext:
        return _NetworkExpectationContext(
            self.app,
            method=method,
            path=path,
            url_contains=url_contains,
            status=status,
            json_fields=json_fields,
            timeout_ms=timeout_ms,
            occurrence=occurrence,
            capture_fields=capture_fields,
        )

    def errors(self, *, clear: bool = False) -> dict[str, Any]:
        _require_web(self.app)
        result = self.app._device("read_browser_errors", {"clear": bool(clear)})
        return dict(result.data or {})

    def environment(self) -> dict[str, Any]:
        _require_web(self.app)
        result = self.app._device("read_web_environment", {})
        return dict(result.data or {})


class _Clipboard:
    def __init__(self, app):
        self.app = app

    def set(self, text: str):
        _require_web(self.app)
        return self.app._device("set_clipboard", {"text": str(text or "")})

    def read(self):
        _require_web(self.app)
        return self.app._device("read_clipboard", {})

    def expect_text(self, expected: str, *, exact: bool = True):
        _require_web(self.app)
        if not isinstance(expected, str):
            raise _abort("剪贴板预期必须是字符串", "clipboard_assertion_invalid", status="blocked", app=self.app)
        result = self.app._device(
            "assert_clipboard",
            {"expected_text": expected, "exact": bool(exact)},
            allow_failed=True,
        )
        evidence = result.data.get("clipboard") if isinstance(result.data, dict) else {}
        evidence = dict(evidence) if isinstance(evidence, dict) else {}
        matched = evidence.get("matched") is True
        return _assert(
            self.app,
            "clipboard",
            "pass" if matched else "fail",
            expected={"exact": bool(exact), "text": _text_evidence(expected)},
            actual=evidence or {"matched": False, "read": False},
            reason_code="" if matched else "clipboard_mismatch" if evidence else "clipboard_unreadable",
            message="" if matched else "剪贴板内容与预期不一致" if evidence else "无法读取剪贴板内容",
        )


class _Expectation:
    """Callable compatibility facade plus deterministic Web assertions."""

    def __init__(self, app):
        self.app = app

    def __call__(self, page: str = "", prompt: str = "", verdict: str = "pass", *, allow_model: bool = False) -> str:
        from mino_scout.platform.scout.run import expect as _expect

        return _expect(self.app, page=page, prompt=prompt, verdict=verdict, allow_model=allow_model)

    def focused(self, control: str):
        _require_web(self.app)
        params = self.app._locate(str(control or ""), "check_focused")
        result = self.app._device("check_focused", params, allow_failed=True)
        actual = dict(result.data or {})
        explicit_match = actual.get("focused") if "focused" in actual else True
        matched = bool(result.ok and explicit_match is True)
        if not actual:
            actual = {"focused": matched, "summary": result.summary}
        return _assert(
            self.app,
            "focused",
            "pass" if matched else "fail",
            expected={"control": str(control or "")},
            actual=actual or {"control": str(control or ""), "focused": False},
            reason_code="" if matched else "focus_mismatch",
            message="" if matched else "目标控件未获得焦点",
        )

    def url(self, expected: str, *, exact: bool = False):
        _require_web(self.app)
        if not isinstance(expected, str) or not expected:
            raise _abort("URL 预期必须是非空字符串", "url_assertion_invalid", status="blocked", app=self.app)
        result = self.app._device("read_url", {})
        actual = str(result.data.get("page_url") or "") if isinstance(result.data, dict) else ""
        passed = _url_match(actual, expected, exact=exact)
        return _assert(
            self.app,
            "url",
            "pass" if passed else "fail",
            expected={"uri": expected, "exact": bool(exact)},
            actual={"uri": actual},
            reason_code="" if passed else "url_mismatch",
            message="" if passed else "当前页面 URL 与预期不一致",
        )
