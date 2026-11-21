"""Bounded Web evidence collected inside the Playwright session.

The recorder observes requests, responses, page errors and the effective browser
environment.  Network timing is correlation with a registered action window; it
does not claim that the action caused a response.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from mino_scout.runtime.secret_redaction import sanitize


MAX_RESPONSES = 250
MAX_ERRORS = 100
MAX_BODY_BYTES = 65_536
_SENSITIVE_KEY = re.compile(
    r"(?:pass(?:word)?|secret|token|authorization|cookie|credential|otp|"
    r"session|api[_-]?key|private[_-]?key)",
    re.I,
)
_SENSITIVE_PARAM = re.compile(r"(?:token|secret|password|otp|key|auth|cookie)", re.I)


def _path(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        return parsed.path or "/"
    except Exception:
        return ""


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            out[name] = "[REDACTED]" if _SENSITIVE_KEY.search(name) else _redact_json(item)
        return out
    if isinstance(value, list):
        return [_redact_json(item) for item in value[:100]]
    if isinstance(value, str):
        return sanitize(value[:2_000])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(str(value)[:500])


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


_MAX_PAGE_CLOCK_EPOCH_MS = 4_102_444_800_000  # 2100-01-01T00:00:00Z


def _fixed_time_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if type(value) is not int and not isinstance(value, str):
        raise ValueError("page_clock.fixed_time 无效")
    if isinstance(value, int):
        epoch_ms = value
    else:
        text = value.strip()
        if not text:
            raise ValueError("page_clock.fixed_time 无效")
        try:
            if re.fullmatch(r"\d+", text):
                epoch_ms = int(text)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                epoch_ms = int(parsed.timestamp() * 1000)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("page_clock.fixed_time 无效") from None
    if not 0 <= epoch_ms <= _MAX_PAGE_CLOCK_EPOCH_MS:
        raise ValueError("page_clock.fixed_time 超出范围")
    return epoch_ms


class NetworkExpectation:
    def __init__(
        self,
        observability: "WebObservability",
        *,
        method: str = "",
        path: str = "",
        url_contains: str = "",
        status: int | None = None,
        json_fields: dict[str, Any] | None = None,
        occurrence: int | None = None,
        action_seq: int = 0,
        capture_fields: list[str] | None = None,
    ) -> None:
        self.observability = observability
        self.id = "web_req_" + uuid.uuid4().hex
        self.method = str(method or "").upper()
        self.path = str(path or "")
        self.url_contains = str(url_contains or "")
        self.status = int(status) if status is not None else None
        self.json_fields = dict(json_fields or {})
        self.capture_fields = list(dict.fromkeys([*(capture_fields or []), *self.json_fields]))
        if len(self.capture_fields) > 32 or any(
            not isinstance(key, str) or not key or len(key) > 200 or
            any(not part or _SENSITIVE_KEY.search(part) for part in key.split('.'))
            for key in self.capture_fields
        ):
            raise ValueError("network_capture_fields_invalid")
        self.occurrence = max(1, int(occurrence)) if occurrence is not None else None
        self.action_seq = max(0, int(action_seq))
        self.registered_mono = time.monotonic()
        self.registered_elapsed_ms = int(
            max(0.0, self.registered_mono - observability.started_mono) * 1000
        )
        self.cancelled = False
        # A response to an earlier in-flight request is outside this action.
        self.registered_request_seq = observability._request_seq
        self.registered_dropped_responses = observability.dropped_responses
        self.registered_page = observability.page

    def matches_meta(self, row: dict[str, Any]) -> bool:
        request = row.get("request") if isinstance(row.get("request"), dict) else {}
        if self.method and request.get("method") != self.method:
            return False
        path = str(request.get("path") or "")
        if self.path and self.path != path:
            return False
        if self.url_contains and self.url_contains not in str(request.get("url_contains") or path):
            return False
        if self.status is not None and int(row.get("status") or 0) != self.status:
            return False
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "expectation_id": self.id,
            "method": self.method,
            "path": self.path,
            "url_contains": self.url_contains,
            "status": self.status,
            "json_fields": {key: sanitize(value) for key, value in self.json_fields.items()},
            "occurrence": self.occurrence,
            "action_seq": self.action_seq,
            "capture_fields": self.capture_fields,
        }


class WebObservability:
    """One recorder per actor/context/page session."""

    def __init__(self, page: Any, identity: dict[str, str], requested_options: dict[str, Any],
                 evidence_sink=None) -> None:
        self.page = page
        self.identity = {key: str(value or "") for key, value in identity.items()}
        self.requested_options = dict(requested_options or {})
        self.started_mono = time.monotonic()
        self.responses = deque(maxlen=MAX_RESPONSES)
        self.errors: list[dict[str, Any]] = []
        self.expectations: dict[str, NetworkExpectation] = {}
        self._response_seq = 0
        self._error_seq = 0
        self._request_seq = 0
        self._requests = OrderedDict()
        self._pages = []
        self._page_labels = {}
        self.dropped_errors = 0
        self.dropped_responses = 0
        self.sink_failures = 0
        self.evidence_sink = evidence_sink
        self._context = page.context
        # Context events include requests from newly opened tabs.
        self._context.on("request", self._on_request)
        self._context.on("response", self._on_response)
        self._context.on("requestfinished", self._on_request_finished)
        self._context.on("requestfailed", self._on_request_failed)
        self._context.on("page", self.attach_page)
        self.attach_page(page)

    def attach_page(self, page) -> None:
        if page in self._pages:
            return
        self._pages.append(page)
        self._page_labels[id(page)] = self.identity.get("page_id", "main") if len(self._pages) == 1 else f"tab_{len(self._pages)}"
        page.on("pageerror", lambda error: self._on_page_error(error, page))
        page.on("console", lambda message: self._on_console(message, page))

    def _emit(self, kind: str, data: dict) -> None:
        if self.evidence_sink is not None:
            try:
                self.evidence_sink(kind, _redact_json(data))
            except Exception:
                self.sink_failures += 1

    def _request_page(self, request):
        try:
            return request.frame.page
        except Exception:
            return None

    def _on_request(self, request) -> None:
        self._request_seq += 1
        self._requests[id(request)] = {"seq": self._request_seq, "request": request,
                                      "page": self._request_page(request), "started_mono": time.monotonic()}
        if len(self._requests) > MAX_RESPONSES * 2:
            self._requests.popitem(last=False)

    def _on_request_finished(self, request) -> None:
        for row in reversed(self.responses):
            if row["request_object"] is request:
                row["finished"] = True
                break
        self._requests.pop(id(request), None)

    def _base(self) -> dict[str, Any]:
        return {
            **self.identity,
            "elapsed_ms": int(max(0, (time.monotonic() - self.started_mono) * 1000)),
        }

    def _add_error(self, kind: str, message: Any, *, url: str = "", details: Any = None, page=None) -> None:
        if len(self.errors) >= MAX_ERRORS:
            self.dropped_errors += 1
            return
        self._error_seq += 1
        row = {
            **self._base(),
            "error_seq": self._error_seq,
            "kind": kind,
            "message": sanitize(str(message or "")[:1_000]),
            "path": _path(url),
            "details": _redact_json(details) if details is not None else None,
            "page_id": self._page_labels.get(id(page), self.identity.get("page_id", "main")),
        }
        self.errors.append(row)
        self._emit("browser/error", row)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            url = str(response.url or request.url or "")
            status = int(response.status or 0)
            self._response_seq += 1
            request_info = self._requests.get(id(request), {})
            if len(self.responses) >= MAX_RESPONSES:
                self.dropped_responses += 1
            row = {
                **self._base(),
                "response_seq": self._response_seq,
                "response": response,
                "request_object": request,
                "request_seq": request_info.get("seq", 0),
                "request_page": request_info.get("page"),
                "request_started_mono": request_info.get("started_mono"),
                "finished": False,
                "status": status,
                "request": {
                    "method": str(request.method or "").upper(),
                    "url_contains": sanitize(url.split("?", 1)[0][:2_000]),
                    "path": _path(url),
                },
            }
            self.responses.append(row)
            if status >= 400:
                self._add_error("http_error", f"HTTP {status}", url=url, page=request_info.get("page"))
        except Exception as exc:
            self._add_error("observer_failure", type(exc).__name__)

    def _on_request_failed(self, request: Any) -> None:
        try:
            failure = request.failure or "request failed"
            self._add_error("request_failed", failure, url=str(request.url or ""), page=self._request_page(request))
            self._requests.pop(id(request), None)
        except Exception as exc:
            self._add_error("observer_failure", type(exc).__name__)

    def _on_page_error(self, error: Any, page=None) -> None:
        self._add_error("page_error", str(error or "page error"), url=str(getattr(error, "url", "") or ""), page=page)

    def _on_console(self, message: Any, page=None) -> None:
        try:
            if str(message.type or "") != "error":
                return
            location = getattr(message, "location", {}) or {}
            self._add_error("console_error", message.text or "console error", url=str(location.get("url") or ""), page=page)
        except Exception as exc:
            self._add_error("observer_failure", type(exc).__name__)

    def record_navigation_error(self, exc: BaseException, url: str = "") -> None:
        self._add_error("navigation_failed", f"{type(exc).__name__}: {exc}", url=url)

    def register(self, **params: Any) -> NetworkExpectation:
        self.page.wait_for_timeout(0)
        expectation = NetworkExpectation(self, **params)
        self.expectations[expectation.id] = expectation
        return expectation

    def cancel(self, expectation_id: str) -> bool:
        expectation = self.expectations.pop(str(expectation_id or ""), None)
        if expectation is None:
            return False
        expectation.cancelled = True
        return True

    def _body_evidence(self, response: Any, fields: list[str]) -> dict[str, Any]:
        if not fields:
            return {"available": False, "reason": "capture_fields_required"}
        try:
            headers = response.headers
            if "json" not in str(headers.get("content-type", "")).lower():
                return {"available": False, "reason": "content_type_not_json"}
            size = headers.get("content-length")
            if size is None or not str(size).isdigit():
                return {"available": False, "reason": "content_length_required"}
            if int(size) > MAX_BODY_BYTES:
                return {"available": False, "reason": "body_limit_exceeded", "truncated": True}
            if str(headers.get("content-encoding", "identity")).lower() not in {"", "identity"}:
                return {"available": False, "reason": "encoded_body_unsupported"}
            body = bytes(response.body() or b"")
        except Exception as exc:
            return {"available": False, "reason": type(exc).__name__}
        bounded = body[:MAX_BODY_BYTES]
        out: dict[str, Any] = {
            "available": True,
            "size": len(body),
            "truncated": len(body) > MAX_BODY_BYTES,
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        try:
            parsed = json.loads(bounded.decode("utf-8"))
        except Exception:
            out["format"] = "binary_or_text"
            return out
        out["format"] = "json"
        # Keep only explicitly allowed fields, never the whole business payload.
        out["fields"] = {path: {"found": found, "value": _redact_json(value) if found else None}
                         for path in fields for found, value in [_json_field(parsed, path)]}
        for path in fields:
            found, value = _json_field(parsed, path)
            if found and (isinstance(value, (dict, list)) or isinstance(value, str) and len(value) > 2000):
                return {"available": False, "reason": "field_scalar_limit", "format": "json"}
        return out

    def _matches_body(self, expectation: NetworkExpectation, body: dict[str, Any]) -> bool:
        if not expectation.json_fields:
            return True
        for path, expected in expectation.json_fields.items():
            field = body.get("fields", {}).get(path, {})
            actual = field.get("value")
            if not field.get("found") or actual != expected or (
                (isinstance(actual, bool) or isinstance(expected, bool)) and type(actual) is not type(expected)
            ):
                return False
        return True

    def wait(self, expectation_id: str, *, timeout_ms: int = 10_000, guard=None) -> dict[str, Any]:
        expectation = self.expectations.get(str(expectation_id or ""))
        if expectation is None or expectation.cancelled:
            raise ValueError("network_expectation_missing")
        deadline = time.monotonic() + max(1, min(int(timeout_ms), 120_000)) / 1000
        scanned: set[int] = set()
        matches = []
        dropped_at_start = expectation.registered_dropped_responses
        try:
            while True:
                if guard is not None:
                    guard()
                if self.dropped_responses > dropped_at_start:
                    raise RuntimeError("network_observation_overflow")
                for row in list(self.responses):
                    index = row["response_seq"]
                    if index in scanned:
                        continue
                    if row["request_seq"] <= expectation.registered_request_seq or row["request_page"] is not expectation.registered_page:
                        scanned.add(index)
                        continue
                    if not expectation.matches_meta(row):
                        scanned.add(index)
                        continue
                    if expectation.capture_fields and not row["finished"]:
                        continue
                    body = self._body_evidence(row["response"], expectation.capture_fields)
                    scanned.add(index)
                    if self._matches_body(expectation, body):
                        matches.append({**row, "body": body})
                occurrence = expectation.occurrence
                if occurrence is None:
                    if len(matches) > 1:
                        raise RuntimeError("network_expectation_ambiguous")
                    if len(matches) == 1:
                        occurrence = 1
                if occurrence is not None and len(matches) >= occurrence:
                    row = matches[occurrence - 1]
                    seen_ms = max(0, int(row.get("elapsed_ms") or 0) - expectation.registered_elapsed_ms)
                    data = {
                        "expectation": expectation.describe(),
                        "matched": True,
                        "request_id": f"{self.identity.get('context_id', 'default')}:{expectation.id}:{row['request_seq']}",
                        "method": row["request"]["method"],
                        "path": row["request"]["path"],
                        "status": row["status"],
                        "body": row["body"],
                        "correlation": {
                            "action_seq": expectation.action_seq,
                            "response_seen_ms_after_register": seen_ms,
                            "window_ms": max(0, int((time.monotonic() - expectation.registered_mono) * 1000)),
                            "statement": "timing_correlation_only",
                        },
                    }
                    if row["request_started_mono"] is not None:
                        data["correlation"]["request_started_ms_after_register"] = max(
                            0, int((row["request_started_mono"] - expectation.registered_mono) * 1000))
                    self._emit("browser/response", {**self._base(), **data})
                    return data
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"network_expectation_timeout method={expectation.method or '*'} "
                        f"path={expectation.path or '*'} occurrence={expectation.occurrence or 'unique'}"
                    )
                # Sync Playwright dispatches callbacks only while its event loop runs.
                self.page.wait_for_timeout(min(50, max(1, int((deadline - time.monotonic()) * 1000))))
        finally:
            self.expectations.pop(expectation.id, None)

    def browser_errors(self, *, clear: bool = False) -> dict[str, Any]:
        rows = list(self.errors)
        if clear:
            self.errors.clear()
        return {"count": len(rows), "truncated": self.dropped_errors > 0, "dropped_errors": self.dropped_errors,
                "dropped_responses": self.dropped_responses, "sink_failures": self.sink_failures,
                "items": _redact_json(rows)}

    def flush_summary(self) -> None:
        self._emit("browser/observation", {**self._base(), "errors_retained": len(self.errors),
                   "dropped_errors": self.dropped_errors, "dropped_responses": self.dropped_responses,
                   "sink_failures": self.sink_failures})

    def environment(self) -> dict[str, Any]:
        observed: dict[str, Any] = {}
        checks = {
            "language": "() => navigator.language || ''",
            "languages": "() => Array.from(navigator.languages || [])",
            "timezone_id": "() => Intl.DateTimeFormat().resolvedOptions().timeZone || ''",
        }
        for key, expression in checks.items():
            try:
                observed[key] = self.page.evaluate(expression)
            except Exception as exc:
                observed[key] = {"error": type(exc).__name__}
        try:
            observed["clipboard_permission"] = self.page.evaluate(
                "async () => { try { return (await navigator.permissions.query({name:'clipboard-read'})).state; } "
                "catch (e) { return 'unsupported'; } }"
            )
        except Exception as exc:
            observed["clipboard_permission"] = {"error": type(exc).__name__}
        try:
            observed["geolocation"] = self.page.evaluate(
                "() => new Promise(resolve => { if (!navigator.geolocation) return resolve('unsupported'); "
                "navigator.geolocation.getCurrentPosition(p => resolve({latitude:p.coords.latitude,"
                "longitude:p.coords.longitude,accuracy:p.coords.accuracy}), e => resolve({error:e.code}),"
                "{timeout:2000,maximumAge:0}); })"
            )
        except Exception as exc:
            observed["geolocation"] = {"error": type(exc).__name__}
        return {"requested": _redact_json(self.requested_options), "observed": _redact_json(observed)}



def validate_context_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Validate options that must fail before a BrowserContext is created."""
    opts = dict(options or {})
    permissions = opts.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            raise ValueError("web_permission_options_invalid")
        allowed = {"geolocation", "notifications", "clipboard-read", "clipboard-write"}
        if set(permissions) - allowed:
            raise ValueError("web_permission_invalid")
        for state in permissions.values():
            state_v = str(state or "").strip().lower()
            if state_v == "denied":
                # Playwright's Chromium API cannot reliably force denied; prompt
                # means no grant and lets the page's real permission behavior run.
                raise ValueError("web_permission_denied_unsupported")
            if state_v not in {"granted", "prompt"}:
                raise ValueError("web_permission_invalid")
    clock = opts.get("page_clock")
    if clock is not None:
        if not isinstance(clock, dict):
            raise ValueError("page_clock_invalid")
        unknown = sorted(set(clock) - {"fixed_time", "pause"})
        if unknown:
            raise ValueError("page_clock_invalid")
        _fixed_time_ms(clock.get("fixed_time"))
        if "pause" in clock and type(clock["pause"]) is not bool:
            raise ValueError("page_clock.pause 必须是布尔值")
    return opts

def apply_context_options(context: Any, page: Any, options: dict[str, Any] | None) -> None:
    """Apply page clock before the first navigation. Context options are passed on creation."""
    clock = (options or {}).get("page_clock")
    if not isinstance(clock, dict):
        return
    fixed = _fixed_time_ms(clock.get("fixed_time"))
    try:
        if fixed is None:
            page.clock.install()
        else:
            page.clock.install(time=fixed)
        if clock.get("pause"):
            target = fixed if fixed is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            page.clock.pause_at(target)
    except Exception as exc:
        raise RuntimeError("page_clock_unsupported") from exc


def context_kwargs(options: dict[str, Any] | None) -> dict[str, Any]:
    opts = dict(options or {})
    out: dict[str, Any] = {}
    if opts.get("locale"):
        out["locale"] = str(opts["locale"])
    if opts.get("timezone_id"):
        out["timezone_id"] = str(opts["timezone_id"])
    if isinstance(opts.get("geolocation"), dict):
        out["geolocation"] = dict(opts["geolocation"])
    permissions = opts.get("permissions") if isinstance(opts.get("permissions"), dict) else {}
    granted = [str(name) for name, state in permissions.items() if state == "granted"]
    if granted:
        out["permissions"] = granted
    return out
