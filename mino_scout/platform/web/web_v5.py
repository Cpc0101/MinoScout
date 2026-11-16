"""Strict Web contract. Four leased processes, one browser/context per run.

No legacy resolver or executor fallback is reachable from this module.
"""
from __future__ import annotations

import base64
import multiprocessing as mp
import threading
import time
import uuid
from dataclasses import asdict

from mino_scout.runtime.secret_redaction import register_inputs, remember, sanitize
from mino_scout.platform.web.viewports import DEFAULT_WEB_RESOLUTION, normalize_web_resolution, web_viewport

CONTRACTS = {"web_v5": 1, "locator_contract": "web.locator.v1", "predicate_facts_contract": "web.atomic.v1"}
READS = frozenset({"probe", "read_web_target_facts", "screenshot"})
CLEANUP = frozenset({"cancel_run", "close_web_v5"})
WRITES = frozenset({"open_web_v5", "tap_target", "input_target"})


def response(started="no", phase="resolve", reason="", **data):
    return {"execution": {"execution_started": started, "phase": phase, "reason_code": reason}, **data}


def validate_locator(spec: dict) -> None:
    if not isinstance(spec, dict) or set(spec) != {"playwright"}:
        raise ValueError("invalid_locator")
    config = spec["playwright"]
    if config.get("scope") != {"kind": "page", "page_id": "main"}:
        raise ValueError("unsupported_scope")
    if config.get("contract") != "web.locator.v1" or config.get("cardinality") not in {"one", "set"}:
        raise ValueError("invalid_locator")
    spec = config.get("selector") or {}
    kind = spec.get("kind")
    allowed = {"kind", "scope", "value", "name", "exact"}
    if kind not in {"test_id", "role", "label", "css"} or set(spec) - allowed:
        raise ValueError("invalid_locator")
    if not isinstance(spec.get("value"), str) or not spec["value"].strip():
        raise ValueError("invalid_locator")
    if kind in {"role", "label"} and spec.get("exact") is not True:
        raise ValueError("invalid_locator")
    if kind == "role" and not isinstance(spec.get("name"), str):
        raise ValueError("invalid_locator")
    if kind == "css" and any(x in spec["value"].lower() for x in
                             ("xpath=", "text=", ">>", "nth", ":first", ":last", "javascript:", ":has")):
        raise ValueError("invalid_locator")


def _locator(page, spec):
    validate_locator(spec)
    spec = spec["playwright"]["selector"]
    kind, value = spec["kind"], spec["value"]
    if kind == "test_id":
        return page.get_by_test_id(value)
    if kind == "role":
        return page.get_by_role(value, name=spec["name"], exact=True)
    if kind == "label":
        return page.get_by_label(value, exact=True)
    return page.locator(value)


def _worker(conn):
    # This worker owns every Playwright object on one thread for its entire life.
    from mino_scout.platform.web.playwright_hub import apply_browsers_path
    apply_browsers_path()
    from playwright.sync_api import sync_playwright
    generation = uuid.uuid4().hex
    browser = context = page = None
    after_step = 0
    with sync_playwright() as pw:
        while True:
            try:
                req = conn.recv()
            except EOFError:
                break
            cap, params = req["capability_id"], req["params"]
            started, phase = "no", "resolve"
            register_inputs(params)
            ms = max(1, int(req["timeout_sec"] * 1000))
            source = {"run_id": req["run_id"], "session_generation": generation, "after_step_idx": after_step}
            try:
                if cap == "open_web_v5":
                    if page is not None:
                        raise ValueError("context_already_open")
                    url = str(params.get("url") or "")
                    if not url.startswith(("http://", "https://")):
                        raise ValueError("invalid_url")
                    resolution = normalize_web_resolution(
                        req.device_hint.get("web_resolution") or params.get("web_resolution")
                    )
                    viewport = web_viewport(resolution)
                    browser = pw.chromium.launch(headless=True, timeout=ms)
                    context = browser.new_context(
                        viewport=viewport,
                        screen=viewport,
                        device_scale_factor=1,
                    )
                    page = context.new_page()
                    started, phase = "yes", "action"
                    conn.send({"boundary": True})
                    page.goto(url, timeout=ms, wait_until="domcontentloaded")
                    after_step = req["step_idx"]
                    data = response(started, "completed", observation={**source, "after_step_idx": after_step})
                elif cap == "screenshot":
                    if page is None:
                        raise ValueError("context_missing")
                    # Mask both configured sensitive controls and actual password fields.
                    masks = [page.locator('input[type="password"]')]
                    for control in params.get("sensitive_controls", []):
                        masks.append(_locator(page, control["locator_spec"]))
                    full_page = bool(params["full_page"] if "full_page" in params else params.get("fullPage", True))
                    shot = page.screenshot(timeout=ms, mask=masks, full_page=full_page)
                    viewport = page.viewport_size or web_viewport(DEFAULT_WEB_RESOLUTION)
                    data = response(phase="completed", observation=source,
                                    image_base64=base64.b64encode(shot).decode(), image_mime="image/png",
                                    width=int(viewport["width"]), height=int(viewport["height"]))
                elif cap in {"tap_target", "input_target", "read_web_target_facts"}:
                    if page is None:
                        raise ValueError("context_missing")
                    control = params.get("control") or {}
                    if not control.get("control_id") or int(control.get("revision", 0)) < 1:
                        raise ValueError("invalid_control")
                    loc = _locator(page, control.get("locator_spec"))
                    if cap in WRITES and control["locator_spec"]["playwright"]["cardinality"] != "one":
                        raise ValueError("invalid_cardinality")
                    if cap in WRITES:
                        resolve_ms = min(ms, int(params.get("timeout_profile", {}).get("resolve_ms", 1000)))
                        try:
                            loc.wait_for(state="visible", timeout=resolve_ms)
                        except Exception:
                            count = loc.count()
                            raise ValueError("target_not_found" if count == 0 else "target_ambiguous" if count > 1 else "target_not_visible")
                    observation_contract = params.get("observation_contract", "")
                    page_ready = False
                    if cap == "read_web_target_facts" and observation_contract:
                        if observation_contract != "single_observation.v1":
                            raise ValueError("observation_contract_unsupported")
                        page_ready = page.evaluate("document.readyState") == "complete"
                        if not page_ready:
                            raise ValueError("page_not_ready")
                    count = loc.count()
                    facts = {"control_id": control["control_id"], "control_revision": control["revision"],
                             "scope_resolved": True, "collection_complete": True, "count": count,
                             "fact_complete": {"count": True}}
                    if observation_contract:
                        facts.update(observation_contract=observation_contract, page_ready=page_ready)
                    if count == 1:
                        # Reject shadow traversal even when Playwright's locator supports it.
                        meta = loc.evaluate("e => ({shadow:e.getRootNode()!==document,password:e.type==='password',tag:e.tagName})", timeout=ms)
                        if meta["shadow"]:
                            raise ValueError("unsupported_scope")
                        sensitive = bool(control.get("sensitive") or meta["password"])
                        facts.update(visible=loc.is_visible(), sensitive=sensitive)
                        facts["fact_complete"]["visible"] = True
                        requested = params.get("requested_facts") or []
                        if "text" in requested:
                            text = "" if sensitive else (loc.text_content(timeout=ms) or "")
                            limit = max(0, min(4096, int(params.get("read_limit", 4096))))
                            facts["text"] = text[:limit]
                            facts["fact_complete"]["text"] = not sensitive and len(text) <= limit
                        if "value" in requested or "secret_value" in requested:
                            if meta["tag"] not in {"INPUT", "TEXTAREA", "SELECT"}:
                                facts["fact_complete"]["value"] = False
                            else:
                                value = loc.input_value(timeout=ms)
                                if sensitive:
                                    remember(value)
                                    facts.update(value="", text="", secret_value={"nonempty": bool(value), "length": len(value)})
                                    facts["fact_complete"].update(value=False, secret_value=True)
                                else:
                                    limit = max(0, min(4096, int(params.get("read_limit", 4096))))
                                    facts["value"] = value[:limit]
                                    facts["fact_complete"]["value"] = len(value) <= limit
                    if cap in WRITES:
                        if count != 1:
                            raise ValueError("target_ambiguous" if count else "target_not_found")
                        if not facts["visible"]:
                            raise ValueError("target_not_visible")
                        payload = params.get("payload") or {}
                        if cap == "input_target" and facts["sensitive"]:
                            remember(payload.get("text"))
                        started, phase = "yes", "action"
                        conn.send({"boundary": True})
                        if cap == "tap_target":
                            loc.click(timeout=min(ms, int(params.get("timeout_profile", {}).get("action_ms", 5000))))
                        else:
                            loc.fill(str(payload.get("text", "")), timeout=min(ms, int(params.get("timeout_profile", {}).get("action_ms", 5000))))
                        after_step = req["step_idx"]
                    data = response(started, "completed", target_facts=facts,
                                    observation={**source, "after_step_idx": after_step})
                else:
                    raise ValueError("unsupported_v5_capability")
                conn.send({"status": "pass", "data": sanitize(data)})
            except Exception as exc:
                reason = "effect_unknown" if started == "yes" else (
                    str(exc) if isinstance(exc, ValueError) else "target_unavailable")
                conn.send({"status": "fail", "data": response(started, phase, reason),
                           "error": sanitize(reason)})
    if context:
        context.close()
    if browser:
        browser.close()


class WebV5Runtime:
    def __init__(self, lanes=4):
        self.condition = threading.Condition()
        self.lanes = [{"run": "", "quarantined": False, "lock": threading.Lock(),
                       "kill_lock": threading.Lock(), "children": [],
                       "process": None, "conn": None} for _ in range(lanes)]
        self.closed_runs: set[str] = set()

    def capacity(self):
        self._reap()
        with self.condition:
            return {"lanes_total": len(self.lanes),
                    "lanes_free": sum(not x["run"] and not x["quarantined"] for x in self.lanes),
                    "lanes_quarantined": sum(x["quarantined"] for x in self.lanes)}

    def _lease(self, run, deadline):
        self._reap()
        with self.condition:
            while True:
                if run in self.closed_runs:
                    raise ValueError("run_write_stopped")
                for i, lane in enumerate(self.lanes):
                    if lane["run"] == run:
                        return i, lane
                for i, lane in enumerate(self.lanes):
                    if not lane["run"] and not lane["quarantined"]:
                        lane["run"] = run
                        return i, lane
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("lane_setup_timeout")
                self.condition.wait(remaining)

    def close(self, run, deadline):
        with self.condition:
            self.closed_runs.add(run)
            # A queued run has no lane to kill, but its waiter must still wake.
            self.condition.notify_all()
            lane = next((x for x in self.lanes if x["run"] == run), None)
        if lane is None:
            return True
        if lane["lock"].acquire(timeout=max(0, deadline - time.monotonic())):
            try:
                return self._kill(lane, deadline)
            finally:
                lane["lock"].release()
        else:
            # Kill the owning process even if its dispatch thread is waiting.
            return self._kill(lane, deadline, release=False)

    def _kill(self, lane, deadline, *, release=True):
        if not lane["kill_lock"].acquire(timeout=max(0, deadline - time.monotonic())):
            # Another cleanup owns the process tree. Do not claim it stopped
            # or advertise the lane as free when our deadline has expired.
            return False
        try:
            return self._kill_owned(lane, deadline, release=release)
        finally:
            lane["kill_lock"].release()

    def _kill_owned(self, lane, deadline, *, release):
        import psutil
        process = lane["process"]
        children = list(lane["children"])
        if process and process.is_alive():
            try:
                children = list({p.pid: p for p in children + psutil.Process(process.pid).children(recursive=True)}.values())
            except psutil.NoSuchProcess:
                pass
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if process and process.is_alive():
            process.kill()
            process.join(timeout=max(0, deadline - time.monotonic()))
        _, alive = psutil.wait_procs(children, timeout=max(0, deadline - time.monotonic()))
        stopped = not alive and (not process or not process.is_alive())
        with self.condition:
            lane["children"] = alive
            lane["quarantined"] = not stopped or not release
            if stopped and release:
                if lane["conn"]:
                    lane["conn"].close()
                lane.update(run="", process=None, conn=None)
            self.condition.notify_all()
        return stopped

    def _reap(self):
        # A close racing a dispatch cannot advertise the lane as free until
        # its dispatch lock and entire process tree have both been released.
        for lane in self.lanes:
            if lane["quarantined"] and lane["lock"].acquire(blocking=False):
                try:
                    self._kill(lane, time.monotonic() + .05)
                finally:
                    lane["lock"].release()

    def execute(self, req, *, admit=None):
        deadline = time.monotonic() + max(.001, req.timeout_sec)
        cap = req.capability_id
        if cap in CLEANUP:
            stopped = self.close(req.run_id, deadline)
            return {"status": "pass" if stopped else "blocked", "data": response(phase="cleanup",
                    reason="" if stopped else "lane_quarantined", executor_stopped=stopped)}
        if cap == "probe":
            return {"status": "pass", "data": response(phase="completed", contracts=CONTRACTS, **self.capacity())}
        if cap not in READS | WRITES:
            return {"status": "fail", "data": response(reason="unsupported_v5_capability")}
        if (cap in WRITES and req.step_idx <= 0) or (cap in READS and req.step_idx >= 0):
            return {"status": "fail", "data": response(reason="invalid_wire_index")}
        if req.device_hint.get("headless") is not True:
            return {"status": "fail", "data": response(reason="headless_required")}
        boundary = False
        ipc_sent = False
        lane = None
        try:
            i, lane = self._lease(req.run_id, deadline)
            if not lane["lock"].acquire(blocking=False):
                raise ValueError("lane_request_inflight")
            try:
                with self.condition:
                    if req.run_id in self.closed_runs or lane["run"] != req.run_id or lane["quarantined"]:
                        raise ValueError("run_write_stopped")
                # Queue/lane waiting must not let an expired binding reach a worker.
                reason = admit() if callable(admit) else ""
                if reason:
                    # A fresh, unused lane has no process or possible side effect.
                    # Existing/quarantined workers keep their reservation for cleanup.
                    with self.condition:
                        if (lane["process"] is None and lane["conn"] is None and not lane["children"]
                                and not lane["quarantined"] and lane["run"] == req.run_id):
                            lane["run"] = ""
                            self.condition.notify_all()
                    return {"status": "blocked", "data": response(reason=reason)}
                if lane["process"] is None:
                    ctx = mp.get_context("spawn")
                    parent, child = ctx.Pipe()
                    proc = ctx.Process(target=_worker, args=(child,), daemon=True, name=f"web-v5-{i}")
                    proc.start()
                    child.close()
                    lane.update(process=proc, conn=parent)
                # Process startup can outlive a registration change. Recheck at
                # the final IPC boundary; no request has reached this worker yet.
                reason = admit() if callable(admit) else ""
                if reason:
                    return {"status": "blocked", "data": response(reason=reason)}
                with self.condition:
                    # Linearize cancellation against IPC. Cancellation can arrive
                    # while the process starts or the admission callback runs.
                    if req.run_id in self.closed_runs or lane["run"] != req.run_id or lane["quarantined"]:
                        raise ValueError("run_write_stopped")
                    # A failed send may have delivered bytes; never classify it
                    # as a known pre-write refusal or permit the run to reopen.
                    ipc_sent = True
                    lane["conn"].send(asdict(req))
                while True:
                    if not lane["conn"].poll(max(0, deadline - time.monotonic())):
                        reason = "effect_unknown"
                        with self.condition:
                            self.closed_runs.add(req.run_id)
                            self.condition.notify_all()
                        self._kill(lane, deadline)
                        return {"status": "fail", "data": response("yes" if boundary else "unknown",
                                "action" if boundary else "resolve", reason, lane_id=i)}
                    result = lane["conn"].recv()
                    if result.get("boundary"):
                        boundary = True
                        continue
                    with self.condition:
                        if req.run_id in self.closed_runs:
                            return {"status": "blocked", "data": response("yes" if boundary else "unknown",
                                    "transport", "run_write_stopped", lane_id=i)}
                    result["data"]["lane_id"] = i
                    return result
            except Exception:
                if ipc_sent:
                    with self.condition:
                        self.closed_runs.add(req.run_id)
                        self.condition.notify_all()
                    self._kill(lane, deadline)
                raise
            finally:
                lane["lock"].release()
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "effect_unknown"
            return {"status": "fail", "data": response("yes" if boundary else "unknown" if ipc_sent else "no", "transport", reason)}
