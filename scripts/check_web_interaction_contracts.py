#!/usr/bin/env python3
"""Offline Web interaction and routing contracts; no browser is launched."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mino_scout.executors.base import DeviceRef, ExecutorContext, make_event_result
from mino_scout.executors import playwright_executor
from mino_scout.platform.web import playwright_hub
from mino_scout.platform.web.playwright_observability import validate_context_options
from mino_scout.platform.web.web_keyboard import normalize_key, normalize_key_value
from mino_scout.platform.web.web_url import parse_http_url, resolve_uri
from mino_scout.runtime import script_exec
from mino_scout.schemas import EventStatus, PlanEvent


def _event(capability: str, params: dict | None = None, *, seq: int = 1) -> PlanEvent:
    return PlanEvent(seq=seq, capability_id=capability, params=dict(params or {}))


def _ctx(actor: str = "", context: str = "", page: str = "") -> ExecutorContext:
    return ExecutorContext(
        device=DeviceRef(
            sn="webfixture",
            platform="web",
            extra={
                "actor": actor,
                "context_id": context,
                "page_id": page,
                "target_package": "https://example.test/app/",
            },
        ),
        run_id="fixture-run",
    )


def check_drag_release() -> None:
    class Mouse:
        def __init__(self, failure: BaseException | None = None):
            self.failure = failure
            self.calls: list[tuple[str, tuple]] = []

        def move(self, *xy):
            self.calls.append(("move", xy))
            if self.failure is not None and len([c for c in self.calls if c[0] == "move"]) >= 2:
                raise self.failure

        def down(self):
            self.calls.append(("down", ()))

        def up(self):
            self.calls.append(("up", ()))

    for failure in (None, RuntimeError("drag failed"), KeyboardInterrupt("cancelled")):
        mouse = Mouse(failure)
        page = SimpleNamespace(mouse=mouse)
        caught: BaseException | None = None
        try:
            playwright_executor._mouse_drag(
                page, (10, 20), (30, 40), duration_ms=0, steps=2 if failure else 0
            )
        except BaseException as exc:  # cancellation intentionally covers BaseException cleanup
            caught = exc
        if failure is None:
            assert caught is None
        else:
            assert caught is failure
        assert mouse.calls[0] == ("move", (10.0, 20.0))
        assert ("down", ()) in mouse.calls
        assert mouse.calls[-1] == ("up", ()), mouse.calls

    class Page:
        mouse = Mouse()

    try:
        playwright_executor._mouse_drag(Page(), (0, 0), (1, 1), duration_ms=-1)
    except ValueError as exc:
        assert str(exc) == "drag_duration_invalid"
    else:
        raise AssertionError("negative drag duration accepted")
    assert Page.mouse.calls == []


def check_keyboard_cleanup() -> None:
    class Hub:
        def __init__(self):
            self.released: list[dict] = []

        def release_pressed_keys(self, sn, **kwargs):
            self.released.append({"sn": sn, **kwargs})

    executor = playwright_executor.PlaywrightExecutor()
    original_get_hub = playwright_executor.get_hub
    hub = Hub()
    playwright_executor.get_hub = lambda: hub
    try:
        executor._execute = lambda event, ctx: make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=executor.id,
            started_at="fixture",
            elapsed_ms=0,
            summary="failed",
            error="fixture_failure",
        )
        result = executor.execute(_event("key_down", {"key": "Shift"}), _ctx("alice", "ctx-a", "tab-1"))
        assert result.status == EventStatus.FAIL
        assert hub.released[-1] == {
            "sn": "webfixture",
            "run_id": "fixture-run",
            "actor": "alice",
            "context_id": "ctx-a",
            "page_id": "tab-1",
        }

        def explode(event, ctx):
            raise RuntimeError("executor exception")

        executor._execute = explode
        try:
            executor.execute(_event("key_down", {"key": "Control"}), _ctx("bob", "ctx-b", "tab-2"))
        except RuntimeError as exc:
            assert str(exc) == "executor exception"
        else:
            raise AssertionError("keyboard executor exception was swallowed")
        assert hub.released[-1]["actor"] == "bob"
        assert hub.released[-1]["context_id"] == "ctx-b"
    finally:
        playwright_executor.get_hub = original_get_hub

    assert normalize_key("shift") == "Shift"
    assert normalize_key_value("Control+Shift+a", allow_combo=True) == "Control+Shift+a"
    for invalid in ("Control+Control+a", "a+Control", "Control+", "+a"):
        try:
            normalize_key_value(invalid, allow_combo=True)
        except ValueError as exc:
            assert str(exc) in {"web_key_invalid", "web_key_chord_invalid"}
        else:
            raise AssertionError(f"invalid chord accepted: {invalid}")
    for invalid in ("Control+a", "NotAKey", ""):
        try:
            normalize_key(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"non-single key accepted: {invalid}")


def check_url_and_forward() -> None:
    assert parse_http_url("https://example.test:443/app/")[1] == 443
    assert resolve_uri("https://example.test/app/", "../orders?q=1") == "https://example.test/orders?q=1"
    invalid_urls = (
        "https://example.test\\@evil.test/app",
        "https://user@example.test/app",
        "https://example.test/app\n.injected",
        "javascript:alert(1)",
        "file:///tmp/example",
        "https://example.test:99999/app",
        "https://example.test./app",
    )
    for value in invalid_urls:
        try:
            parse_http_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid URL accepted: {value!r}")
    try:
        resolve_uri("https://example.test/app/", "//evil.test/orders")
    except ValueError as exc:
        assert str(exc) == "web_uri_origin_mismatch"
    else:
        raise AssertionError("cross-origin navigation accepted")

    class Page:
        url = "https://example.test/app/orders"

        def __init__(self):
            self.main_frame = object()
            self.listeners: list[tuple[str, object]] = []

        def on(self, name, callback):
            self.listeners.append((name, callback))

        def remove_listener(self, name, callback):
            self.listeners.remove((name, callback))

        def go_forward(self, **kwargs):
            # A no-op is intentionally not a successful navigation.
            return None

    class Hub:
        def __init__(self):
            self.page = Page()
            self.released: list[dict] = []

        def current_page(self, sn, **kwargs):
            return self.page

        def current_observability(self, sn, **kwargs):
            return None

        def release_pressed_keys(self, sn, **kwargs):
            self.released.append({"sn": sn, **kwargs})

    hub = Hub()
    original_get_hub = playwright_executor.get_hub
    playwright_executor.get_hub = lambda: hub
    try:
        result = playwright_executor.PlaywrightExecutor().execute(
            _event("forward_page", {"timeout_ms": 1000}), _ctx("alice", "ctx-a", "main")
        )
    finally:
        playwright_executor.get_hub = original_get_hub
    assert result.status == EventStatus.FAIL
    assert result.error == "web_forward_noop"
    assert result.raw_response["execution"]["reason_code"] == "web_forward_noop"
    assert hub.page.listeners == []
    assert hub.released


def check_clipboard_and_permission() -> None:
    class Page:
        url = "https://example.test/app/"
        main_frame = object()

        def evaluate(self, source):
            assert "clipboard.readText" in source
            return ""

    class Hub:
        def __init__(self):
            self.page = Page()
            self.released: list[dict] = []

        def current_page(self, sn, **kwargs):
            return self.page

        def release_pressed_keys(self, sn, **kwargs):
            self.released.append({"sn": sn, **kwargs})

    hub = Hub()
    original_get_hub = playwright_executor.get_hub
    playwright_executor.get_hub = lambda: hub
    try:
        result = playwright_executor.PlaywrightExecutor().execute(
            _event("assert_clipboard", {"expected_text": "", "exact": True}),
            _ctx("alice", "ctx-a", "main"),
        )
    finally:
        playwright_executor.get_hub = original_get_hub
    assert result.status == EventStatus.PASS
    evidence = result.raw_response["clipboard"]
    assert evidence["expected"]["length"] == 0
    assert evidence["actual"]["length"] == 0
    assert evidence["matched"] is True

    for run_validator in (
        lambda: script_exec._normalize_web_options({"permissions": {"clipboard-read": "denied"}}),
        lambda: validate_context_options({"permissions": {"clipboard-read": "denied"}}),
    ):
        try:
            run_validator()
        except script_exec.ScriptAbort as exc:
            assert exc.status == "blocked"
            assert exc.reason_code == "web_permission_denied_unsupported"
        except ValueError as exc:
            assert str(exc) == "web_permission_denied_unsupported"
        else:
            raise AssertionError("denied permission reached browser context creation")

    app = script_exec.App.__new__(script_exec.App)
    calls = []
    app._case = SimpleNamespace(_platform="web")
    app._device = lambda *args, **kwargs: calls.append((args, kwargs))
    try:
        app.set_permission("clipboard-read", "denied")
    except script_exec.ScriptAbort as exc:
        assert exc.status == "blocked"
        assert exc.reason_code == "web_permission_denied_unsupported"
    else:
        raise AssertionError("runtime denied permission reached the device action")
    assert calls == []


def check_actor_tab_isolation() -> None:
    common = dict(sn="webfixture", run_id="fixture-run", actor="alice", context_id="shared", page_id="main")
    main_key = playwright_hub._session_key(**common)
    tab_key = playwright_hub._session_key(**{**common, "page_id": "secondary"})
    other_actor = playwright_hub._session_key(
        sn="webfixture", run_id="fixture-run", actor="bob", context_id="shared", page_id="main"
    )
    other_context = playwright_hub._session_key(
        sn="webfixture", run_id="fixture-run", actor="alice", context_id="other", page_id="main"
    )
    assert main_key == tab_key
    assert main_key != other_actor
    assert main_key != other_context

    class Page:
        def __init__(self, name):
            self.name = name
            self.url = f"https://example.test/{name}"
            self.front = False

        def bring_to_front(self):
            self.front = True

        def title(self):
            return self.name

    class Context:
        def __init__(self, pages):
            self.pages = pages

    a1, a2, b1 = Page("a1"), Page("a2"), Page("b1")
    hub = playwright_hub.PlaywrightHub.__new__(playwright_hub.PlaywrightHub)
    import threading
    hub._lock = threading.Lock()
    hub._run_tokens = {"fixture-run": 0}
    hub._pressed_keys = {}
    hub._local = SimpleNamespace(lane_run="fixture-run", lane_token=0)
    hub._sessions = {
        main_key: {"context": Context([a1, a2]), "page": a1, "run_id": "fixture-run", "lane_token": 0},
        other_actor: {"context": Context([b1]), "page": b1, "run_id": "fixture-run", "lane_token": 0},
    }
    selected = hub.switch_tab(
        "webfixture",
        run_id="fixture-run",
        tab_index=1,
        actor="alice",
        context_id="shared",
        page_id="secondary",
    )
    assert selected is a2 and a2.front is True
    assert hub._sessions[main_key]["page"] is a2
    assert hub._sessions[other_actor]["page"] is b1


def check() -> None:
    check_drag_release()
    check_keyboard_cleanup()
    check_url_and_forward()
    check_clipboard_and_permission()
    check_actor_tab_isolation()
    print("PASS: Web drag/keyboard cleanup, strict URL/forward, permission blocking, clipboard and actor/tab isolation")


if __name__ == "__main__":
    check()
