#!/usr/bin/env python3
"""Offline event/correlation/record contracts; never launches a target or browser."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mino_scout.platform.web.playwright_observability import MAX_RESPONSES, MAX_ERRORS, WebObservability


class Events:
    def __init__(self):
        self.callbacks = {}

    def on(self, kind, callback):
        self.callbacks.setdefault(kind, []).append(callback)

    def emit(self, kind, value):
        for callback in self.callbacks.get(kind, []):
            callback(value)


class Page(Events):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.rounds = []
        self.pumped = 0

    def wait_for_timeout(self, ms):
        self.pumped += 1
        if ms and self.rounds:
            self.rounds.pop(0)()


def setup():
    context = Events()
    page = Page(context)
    records = []
    obs = WebObservability(page, {"actor": "a", "context_id": "a", "page_id": "main"}, {},
                           lambda kind, data: records.append((kind, data)))
    return context, page, obs, records


def request(context, page, url="https://example.test/api/orders"):
    value = SimpleNamespace(frame=SimpleNamespace(page=page), method="POST", url=url, failure="net::ERR_FAILED")
    context.emit("request", value)
    return value


def response(context, req, *, status=201, payload=None, headers=None):
    encoded = json.dumps(payload or {"code": "SUCCESS", "token": "must-not-be-recorded"}).encode()
    value = SimpleNamespace(request=req, url=req.url, status=status,
                            headers=headers or {"content-type": "application/json", "content-length": str(len(encoded))},
                            body=lambda: encoded)
    context.emit("response", value)
    context.emit("requestfinished", req)
    return value


def check():
    context, page, obs, records = setup()
    old = request(context, page)
    exp = obs.register(method="POST", path="/api/orders", capture_fields=["code"])
    response(context, old)  # An old in-flight request cannot satisfy a new action.
    response(context, request(context, page, "https://example.test/api/orders-old"))
    foreign = Page(context)
    context.emit("page", foreign)
    response(context, request(context, foreign))
    page.rounds = [lambda: response(context, request(context, page))]
    data = obs.wait(exp.id, timeout_ms=100)
    assert data["status"] == 201 and data["body"]["fields"]["code"]["value"] == "SUCCESS"
    assert "must-not-be-recorded" not in json.dumps(data)
    assert page.pumped >= 2 and exp.id not in obs.expectations
    assert records[-1][0] == "browser/response"

    context, page, obs, _ = setup()
    exp = obs.register(path="/api/orders", occurrence=2)
    page.rounds = [lambda: response(context, request(context, page)),
                   lambda: response(context, request(context, page), status=202)]
    data = obs.wait(exp.id, timeout_ms=100)
    assert data["status"] == 202  # Matches survive separate event-loop rounds.
    assert data["body"]["reason"] == "capture_fields_required"

    exp = obs.register(path="/api/orders")
    response(context, request(context, page))
    response(context, request(context, page))
    try:
        obs.wait(exp.id, timeout_ms=100)
    except RuntimeError as error:
        assert str(error) == "network_expectation_ambiguous"
    else:
        raise AssertionError("ambiguous response accepted")
    assert exp.id not in obs.expectations

    exp = obs.register(path="/never")
    def cancelled():
        raise RuntimeError("cancelled")
    try:
        obs.wait(exp.id, guard=cancelled)
    except RuntimeError as error:
        assert str(error) == "cancelled"
    assert exp.id not in obs.expectations

    for field in ["token", "data.password", "", "data..id"]:
        try:
            obs.register(path="/api/orders", capture_fields=[field])
        except ValueError as error:
            assert str(error) == "network_capture_fields_invalid"
        else:
            raise AssertionError("sensitive or invalid capture accepted")

    forbidden = SimpleNamespace(headers={"content-type": "application/json"},
                                body=lambda: (_ for _ in ()).throw(AssertionError("body must not be read")))
    assert obs._body_evidence(forbidden, ["code"])["reason"] == "content_length_required"
    forbidden.headers["content-length"] = "1000000"
    assert obs._body_evidence(forbidden, ["code"])["reason"] == "body_limit_exceeded"

    context, page, obs, records = setup()
    context.emit("requestfailed", request(context, page))
    assert obs.errors[-1]["message"] == "net::ERR_FAILED"
    assert obs.errors[-1]["kind"] == "request_failed"
    response(context, request(context, page), status=404)
    assert obs.errors[-1]["kind"] == "http_error"
    other = Page(context)
    context.emit("page", other)
    other.emit("console", SimpleNamespace(type="error", text="new tab error", location={}))
    assert records[-1][1]["page_id"] == "tab_2"
    for _ in range(MAX_ERRORS + 1):
        page.emit("pageerror", RuntimeError("synthetic error"))
    errors = obs.browser_errors()
    assert errors["count"] == MAX_ERRORS and errors["truncated"] and errors["dropped_errors"] > 0

    exp = obs.register(path="/api/orders")
    for _ in range(MAX_RESPONSES + 1):
        response(context, request(context, page))
    try:
        obs.wait(exp.id)
    except RuntimeError as error:
        assert str(error) == "network_observation_overflow"
    else:
        raise AssertionError("overflow was hidden")
    assert exp.id not in obs.expectations
    # Exercise the actual executor boundary: runtime identity must not be sent
    # as an unexpected keyword to the observer registration method.
    from mino_scout.executors import playwright_executor as executor_module
    from mino_scout.executors.base import DeviceRef, ExecutorContext
    from mino_scout.schemas import PlanEvent
    context, page, obs, records = setup()
    original_hub = executor_module.get_hub
    executor_module.get_hub = lambda: SimpleNamespace(current_page=lambda *a, **k: page,
                                                       current_observability=lambda *a, **k: obs)
    try:
        executor = executor_module.PlaywrightExecutor()
        ctx = ExecutorContext(device=DeviceRef(sn="webfixture", platform="web"), run_id="fixture")
        event = PlanEvent(seq=1, capability_id="network_expect_register",
                          params={"path": "/api/orders", "_web_identity": {"actor": "a"}})
        result = executor.execute(event, ctx)
        assert result.status.value == "pass", result.summary
        key = result.raw_response["expectation"]["expectation_id"]
        response(context, request(context, page))
        result = executor.execute(PlanEvent(seq=2, capability_id="network_expect_wait",
                                  params={"expectation_id": key}), ctx)
        assert result.status.value == "pass", result.summary
    finally:
        executor_module.get_hub = original_hub

    # Actual Scout persistence in an isolated temporary record store.
    from mino_scout.runtime.records import RecordStore, Recorder
    with tempfile.TemporaryDirectory() as folder:
        store = RecordStore(Path(folder))
        store.create("fixture-task", "fixture-attempt", {}, "fixture-hash")
        recorder = Recorder(store, "fixture-task", "fixture-attempt")
        context, page, obs, _ = setup()
        obs.evidence_sink = recorder.append
        page.emit("pageerror", RuntimeError("first navigation error"))
        exp = obs.register(path="/api/orders", capture_fields=["code"])
        response(context, request(context, page))
        obs.wait(exp.id)
        obs.flush_summary()
        events = store.events("fixture-task", "fixture-attempt")
        encoded = json.dumps(events)
        assert "browser/error" in encoded and "browser/response" in encoded
        assert "browser/observation" in encoded and "must-not-be-recorded" not in encoded
        assert "assertion/result" not in encoded
    print("PASS: event pumping, request start boundary, exact path/page scope, segmented occurrence, ambiguity, cancellation, field allowlist, bounded capture, tab errors, overflow")


if __name__ == "__main__":
    check()
