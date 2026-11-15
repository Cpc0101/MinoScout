"""Short record RPCs and durable, independently executed script attempts.

Worker processes never listen on a socket. Control callbacks use a private pipe
to the authenticated parent's existing outbound Nexus connection.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path

from mino_scout.runtime.records import RecordStore, Recorder, identifier, scrub
from mino_scout.server_comm import protocol as P


def bind(core, owner: str, origin: str):
    """Called only after REGISTERED authentication, never from an RPC owner field."""
    owner = str(owner or "").strip()
    if not owner:
        raise PermissionError("record_owner_mismatch")
    profile = hashlib.sha256(json.dumps([origin, owner]).encode()).hexdigest()
    old = getattr(core, "_script_records", None)
    if old and old.profile == profile:
        return old
    if old:
        old.close()
    from mino_scout.config import config_dir
    manager = ScriptRecords(core, RecordStore(config_dir() / "accounts" / profile / "records"), owner, profile)
    core._script_records = manager
    return manager


def dispatch(core, req):
    from mino_scout.executors.base import make_event_result, now_iso
    from mino_scout.schemas import PlanEvent, EventStatus
    event = PlanEvent(seq=max(req.step_idx, 0), capability_id=req.capability_id)
    try:
        manager = getattr(core, "_script_records", None)
        if manager is None:
            raise PermissionError("record_owner_mismatch")
        data = manager.submit(req) if req.capability_id == "script.submit" else manager.read(req.capability_id, dict(req.params or {}))
        return make_event_result(event, status=EventStatus.PASS, executor_used="scout", started_at=now_iso(), elapsed_ms=0,
                                 summary=str(data.get("summary") or data.get("state") or "ok"), raw_response=data)
    except (OSError, ValueError, PermissionError) as exc:
        code = str(exc)
        if isinstance(exc, FileNotFoundError):
            code = "artifact_not_found" if req.capability_id == "record.artifact" else "record_not_found"
        elif isinstance(exc, PermissionError):
            code = "record_owner_mismatch"
        elif isinstance(exc, OSError):
            code = "record_unavailable"
        return make_event_result(event, status=EventStatus.FAIL, executor_used="scout", started_at=now_iso(), elapsed_ms=0,
                                 summary=code, error=code, raw_response={"reason_code": code})


def run_legacy(core, req):
    """The synchronous compatibility entry shares the same durable worker path."""
    from dataclasses import replace
    from mino_scout.executors.base import make_event_result, now_iso
    from mino_scout.schemas import PlanEvent, EventStatus
    manager = core._script_records
    params = dict(req.params or {})
    params.setdefault("owner_user_id", manager.owner)
    params.setdefault("execution_timeout_sec", float(req.timeout_sec or 600))
    result = dispatch(core, replace(req, capability_id="script.submit", params=params))
    if result.status is not EventStatus.PASS:
        return result
    summary = result.raw_response
    deadline = time.monotonic() + float(params["execution_timeout_sec"]) + 5
    while not summary.get("sealed") and time.monotonic() < deadline:
        time.sleep(0.1)
        summary = manager.store.status(summary["task_id"], summary["attempt_id"])
    if not summary.get("sealed"):
        manager.cancel_run(req.run_id)
        summary = {**summary, "status": "blocked", "reason_code": "script_timeout", "execution_started": "unknown", "resource_isolated": True}
    status = summary["status"] if summary["status"] in {"pass", "fail", "blocked", "declined", "skipped"} else "blocked"
    return make_event_result(PlanEvent(seq=max(0, req.step_idx), capability_id="script_run"), status=EventStatus(status),
        executor_used="script", started_at=now_iso(), elapsed_ms=summary.get("elapsed_ms", 0), summary=summary.get("summary", ""),
        error=summary.get("reason_code", ""), raw_response={**summary,
            "execution": {"execution_started": summary["execution_started"], "reason_code": summary["reason_code"], "phase": summary.get("failure_stage") or "completed"},
            "script_run": {"state": summary["status"], "case_id": summary["case_id"]}})


class ScriptRecords:
    def __init__(self, core, store, owner, profile):
        self.core, self.store, self.owner, self.profile = core, store, owner, profile
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.closed = False
        self.store.recover()

    def _owner(self, supplied):
        if str(supplied or "") != self.owner:
            raise PermissionError("record_owner_mismatch")

    def _resolve(self, params):
        self._owner(params.get("owner_user_id"))
        task_id = identifier(params.get("task_id"))
        attempt_id = str(params.get("attempt_id") or "")
        session_id = str(params.get("session_id") or "")
        if not attempt_id:
            matches = [row for row in self.store.manifest(task_id)["attempts"] if row["session_id"] == session_id]
            if len(matches) != 1:
                raise FileNotFoundError("record_not_found")
            attempt_id = matches[0]["attempt_id"]
        row = self.store.status(task_id, identifier(attempt_id))
        if row["owner_user_id"] != self.owner or (session_id and session_id != row["session_id"]):
            raise PermissionError("record_owner_mismatch")
        return task_id, attempt_id

    def read(self, capability, params):
        self._owner(params.get("owner_user_id"))
        if capability == "record.manifest":
            result = self.store.manifest(identifier(params.get("task_id")))
            if any(row["owner_user_id"] != self.owner for row in result["attempts"]):
                raise PermissionError("record_owner_mismatch")
            return result
        task_id, attempt_id = self._resolve(params)
        if capability == "record.status":
            return self.store.status(task_id, attempt_id)
        if capability == "record.events":
            return self.store.events(task_id, attempt_id, after_seq=params.get("after_seq", 0), limit=params.get("limit", 100), max_bytes=params.get("max_bytes", 524288))
        if capability == "record.artifact":
            return self.store.read_artifact(task_id, attempt_id, params.get("artifact_id"), offset=params.get("offset", 0), length=params.get("length", 262144))
        if capability == "record.cancel":
            result = self.store.cancel(task_id, attempt_id)
            with self.lock:
                job = self.jobs.get(attempt_id)
                if job:
                    job["stop"].set()
            return result
        raise ValueError("record_operation_unknown")

    def submit(self, req):
        from mino_scout.server_comm.script_dispatch import _payload
        payload = _payload(req)
        self._owner(payload.owner_user_id)
        payload.task_id = identifier(payload.task_id or payload.run_id)
        payload.attempt_id = identifier(payload.attempt_id or payload.session_id)
        identifier(payload.session_id)
        timeout = float(payload.execution_timeout_sec)
        if not 1 <= timeout <= 86400:
            raise ValueError("execution_timeout_invalid")
        immutable = {"script": asdict(payload), "device_hint": dict(req.device_hint or {})}
        digest = hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        metadata = {**asdict(payload), "node_id": self.core.node_id,
                    "environment": dict(req.device_hint or {}), "execution_started": "no"}
        with self.lock:
            if self.closed:
                raise ValueError("record_unavailable")
            summary, created = self.store.create(payload.task_id, payload.attempt_id, metadata, digest)
            if not created:
                return summary
            conflicts = [job for job in self.jobs.values() if job["sn"] == payload.sn]
            guarded = getattr(self.core, "_requests", None)
            existing_writes = []
            if guarded:
                with guarded.lock:
                    existing_writes = [row for row in guarded.requests.values() if row.get("worker_active") and row["content"].get("sn") == payload.sn]
            if getattr(self.core, "_update_busy", False) or conflicts and (payload.platform != "web" or len(conflicts) >= 4) or existing_writes:
                return self.store.finish(payload.task_id, payload.attempt_id, {"status": "blocked", "reason_code": "resource_busy",
                    "summary": "目标设备已有执行或节点正在更新", "execution_started": "no", "failure_stage": "admission"})
            ctx = multiprocessing.get_context("spawn")
            stop = ctx.Event()
            connected = ctx.Event()
            connected.set()
            parent_pipe, child_pipe = ctx.Pipe()
            process = ctx.Process(target=_worker, args=(str(self.store.root), asdict(payload), asdict(req), child_pipe, stop, connected), daemon=True,
                                  name="scout-script-" + payload.attempt_id[:16])
            job = {"task_id": payload.task_id, "attempt_id": payload.attempt_id, "run_id": payload.run_id,
                   "sn": payload.sn, "platform": payload.platform, "stop": stop, "connected": connected, "process": process}
            self.jobs[payload.attempt_id] = job
            try:
                process.start()
                child_pipe.close()
                worker = threading.Thread(target=self._monitor, args=(job, parent_pipe, timeout), daemon=True, name="scout-script-monitor")
                job["monitor"] = worker
                worker.start()
            except Exception as exc:
                self.jobs.pop(payload.attempt_id, None)
                parent_pipe.close()
                child_pipe.close()
                return self.store.finish(payload.task_id, payload.attempt_id, {"status": "fail", "reason_code": "worker_start_failed",
                    "summary": type(exc).__name__, "execution_started": "no", "failure_stage": "admission"})
        return summary

    def _monitor(self, job, pipe, timeout):
        process = job["process"]
        deadline = time.monotonic() + timeout
        stopping = None
        outcome = None
        try:
            while process.is_alive():
                if pipe.poll(0.1):
                    try:
                        message = pipe.recv()
                    except EOFError:
                        break
                    if message.get("kind") == "result":
                        outcome = message["outcome"]
                    elif message.get("kind") == "call":
                        ask = getattr(self.core, "nexus_call", None)
                        try:
                            reply = ask(P.Execute(**message["request"]), message["timeout"]) if callable(ask) else None
                            pipe.send({"reply": asdict(reply) if reply is not None else None})
                        except Exception:
                            try:
                                pipe.send({"reply": None})
                            except (BrokenPipeError, OSError):
                                pass
                if self.closed or job["stop"].is_set() or time.monotonic() >= deadline:
                    job["stop"].set()
                    stopping = stopping or time.monotonic()
                    if time.monotonic() - stopping >= 2:
                        _terminate(process)
                        break
            process.join(timeout=2)
            if outcome is None and pipe.poll():
                try:
                    message = pipe.recv()
                except EOFError:
                    message = {}
                if message.get("kind") == "result":
                    outcome = message["outcome"]
            if outcome is None:
                timed_out = time.monotonic() >= deadline
                outcome = {"status": "blocked", "reason_code": "script_timeout" if timed_out else "worker_interrupted",
                           "summary": "执行进程已停止，最后动作需要核验", "execution_started": "unknown",
                           "resource_isolated": True, "failure_stage": "execution"}
                if job["stop"].is_set() and not timed_out:
                    outcome.update(status="cancelled", reason_code="cancelled")
            self.store.finish(job["task_id"], job["attempt_id"], outcome)
        except Exception as exc:
            _terminate(process)
            try:
                self.store.finish(job["task_id"], job["attempt_id"], {"status": "blocked", "reason_code": "record_unavailable",
                    "summary": type(exc).__name__, "execution_started": "unknown", "resource_isolated": True})
            except Exception:
                pass  # Existing durable intent remains nonterminal and is recovered on restart.
        finally:
            pipe.close()
            with self.lock:
                self.jobs.pop(job["attempt_id"], None)

    def active(self):
        with self.lock:
            return [dict(run_id=job["run_id"], sn=job["sn"], platform=job["platform"]) for job in self.jobs.values()]

    def connection(self, connected):
        with self.lock:
            for job in self.jobs.values():
                job["connected"].set() if connected else job["connected"].clear()

    def cancel_run(self, run_id):
        with self.lock:
            for job in self.jobs.values():
                if job["run_id"] == run_id:
                    self.store.cancel(job["task_id"], job["attempt_id"])
                    job["stop"].set()

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                job["stop"].set()


def _terminate(process):
    if not process.is_alive():
        return
    if os.name != "nt":
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)


def _worker(store_root, payload_raw, request_raw, pipe, stop, connected):
    """Spawn entry point: module-level for frozen Windows/macOS launchers."""
    import shutil
    from mino_scout.cli import build_core
    from mino_scout.runtime import script_repo, script_exec
    from mino_scout.server_comm.script_dispatch import _lease, _missing
    if os.name != "nt":
        os.setsid()
    parent = multiprocessing.parent_process()
    def watch_parent():
        while parent is not None and parent.is_alive():
            time.sleep(0.5)
        if parent is not None:
            stop.set()
            time.sleep(2)
            os._exit(70)  # Durable intent is recovered; orphan workers never keep writing.
    threading.Thread(target=watch_parent, daemon=True, name="scout-parent-watch").start()
    store = RecordStore(Path(store_root))
    payload = P.ScriptRun(**payload_raw)
    req = P.Execute(**request_raw)
    payload.device_hint = dict(req.device_hint or {})
    root = None
    outcome = None
    core = None
    try:
        if not store.start(payload.task_id, payload.attempt_id):
            return
        if store.status(payload.task_id, payload.attempt_id)["cancel_requested"] or stop.is_set():
            outcome = {"status": "cancelled", "reason_code": "cancelled", "summary": "开始前已取消", "execution_started": "no"}
            return
        core = build_core()
        core._recording = Recorder(store, payload.task_id, payload.attempt_id)
        core._record_connection = connected

        def call(request, timeout):
            pipe.send({"kind": "call", "request": asdict(request), "timeout": timeout})
            if not pipe.poll(timeout + 2):
                return None
            raw = pipe.recv().get("reply")
            return P.Result(**raw) if raw else None

        core.nexus_call = call
        _lease(core, payload)
        missing = _missing(payload.platform)
        if missing:
            outcome = {"status": "declined", "reason_code": "channel_missing", "summary": f"缺 {missing}",
                       "execution_started": "no", "failure_stage": "admission"}
            return
        root, payload.revision = script_repo.checkout(payload.repo_url, payload.branch, payload.revision)
        payload.script_path = script_repo.locate_script(root, payload.case_id, payload.script_path)
        payload.device_hint = dict(req.device_hint or {})
        outcome = script_exec.run(core, payload, root, deadline=time.monotonic() + payload.execution_timeout_sec, stop=stop)
    except BaseException as exc:
        outcome = {"status": str(getattr(exc, "status", "declined" if isinstance(exc, ValueError) else "fail")),
                   "reason_code": str(getattr(exc, "code", "") or getattr(exc, "reason_code", "") or "worker_failed"),
                   "summary": str(scrub(str(exc)))[:2000], "execution_started": "no", "failure_stage": "admission"}
    finally:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
        if payload.platform == "web":
            try:
                from mino_scout.platform.web.playwright_hub import get_hub
                get_hub().shutdown_thread()
            except Exception:
                pass
        try:
            pipe.send({"kind": "result", "outcome": outcome or {"status": "blocked", "reason_code": "worker_interrupted", "summary": "执行未完成"}})
        finally:
            pipe.close()
