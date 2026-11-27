"""Durable fragment requests in the existing task evidence database."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import time

from mino_scout.runtime.records import identifier, scrub


def _table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS debug_commands (
        attempt_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        body TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
        started_at REAL, finished_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
        result TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(attempt_id,request_id))""")


def view(row, *, now=None):
    now = time.time() if now is None else now
    started, finished = row["started_at"], row["finished_at"]
    timeout = json.loads(row["body"])["timeout_sec"]
    deadline = started + timeout if started is not None else None
    return {key: row[key] for key in ("request_id", "state", "created_at", "started_at", "finished_at", "cancel_requested")} | {
        "result": json.loads(row["result"]), "timeout_sec": timeout,
        "elapsed_ms": int(max(0, (finished if finished is not None else now) - started) * 1000) if started is not None else 0,
        "deadline_at": deadline,
        "remaining_ms": max(0, int((deadline - now) * 1000)) if deadline is not None and row["state"] == "running" else None,
        "overdue": bool(deadline is not None and row["state"] == "running" and now >= deadline),
    }


def projection(db, attempt, *, now=None):
    """Read from the same transaction as the attempt summary, including legacy records."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='debug_commands'").fetchone():
        return None
    row = db.execute("SELECT * FROM debug_commands WHERE attempt_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (attempt,)).fetchone()
    return view(row, now=now) if row else None


def submit(store, task, attempt, params, *, accept_new=True):
    request = identifier(params.get("client_request_id"))
    kind = params.get("kind", "execute")
    code, path = params.get("code", ""), params.get("script_path", "")
    if kind not in {"execute", "reset", "checkpoint"} or not isinstance(code, str) or not isinstance(path, str):
        raise ValueError("debug_command_invalid")
    if kind == "execute" and (bool(code.strip()) == bool(path)):
        raise ValueError("debug_code_or_script_required")
    if len(code.encode()) > 128 * 1024:
        raise ValueError("debug_code_too_large")
    if code:
        ast.parse(code)  # Syntax errors never enter the device queue.
    timeout = params.get("timeout_sec", 60)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
        raise ValueError("debug_timeout_invalid")
    expected = params.get("workspace_hash", "")
    if not isinstance(expected, str) or expected and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
        raise ValueError("workspace_hash_invalid")
    stage, evidence = params.get("stage", "draft"), params.get("evidence_request_id", "")
    if kind == "checkpoint":
        if stage not in {"draft", "implemented", "path_validated"} or not path or code:
            raise ValueError("debug_checkpoint_invalid")
        if stage == "path_validated":
            identifier(evidence)
    body = {"kind": kind, "code": code, "script_path": path, "timeout_sec": timeout, "workspace_hash": expected,
            "stage": stage, "evidence_request_id": evidence}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with store._db(task) as db:
        _table(db)
        db.execute("BEGIN IMMEDIATE")
        row = store._row(db, attempt)
        meta = json.loads(row["metadata"])
        if meta.get("execution_kind") != "debug":
            raise ValueError("debug_session_required")
        previous = db.execute("SELECT * FROM debug_commands WHERE attempt_id=? AND request_id=?", (attempt, request)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("debug_request_conflict")
            return view(previous)
        if not accept_new or row["state"] != "running" or row["cancel_requested"]:
            raise ValueError("debug_session_not_ready")
        if db.execute("SELECT 1 FROM debug_commands WHERE attempt_id=? AND state IN ('queued','running')", (attempt,)).fetchone():
            raise ValueError("debug_session_busy")
        db.execute("INSERT INTO debug_commands(attempt_id,request_id,fingerprint,body,state,created_at) VALUES(?,?,?,?,'queued',?)",
                   (attempt, request, fingerprint, encoded, time.time()))
        store._append(db, attempt, "debug/queued", {"request_id": request, "code_hash": hashlib.sha256(code.encode()).hexdigest(), "script_path": path, "timeout_sec": timeout})
        store._publish(db, task, attempt)
        return view(db.execute("SELECT * FROM debug_commands WHERE attempt_id=? AND request_id=?", (attempt, request)).fetchone())


def current(store, task, attempt, request_id=""):
    with store._db(task) as db:
        _table(db)
        row = db.execute("SELECT * FROM debug_commands WHERE attempt_id=?" + (" AND request_id=?" if request_id else " ORDER BY created_at DESC, rowid DESC LIMIT 1"),
                         (attempt, identifier(request_id)) if request_id else (attempt,)).fetchone()
        return view(row) if row else None


def status(store, task, attempt, request_id=""):
    """One SQLite snapshot for the session, selected receipt and current command."""
    with store._db(task) as db:
        _table(db)
        db.execute("BEGIN")
        summary = store._summary(db, task, store._row(db, attempt))
        if request_id:
            row = db.execute("SELECT * FROM debug_commands WHERE attempt_id=? AND request_id=?", (attempt, identifier(request_id))).fetchone()
            summary["command"] = view(row) if row else None
        return summary


def take(store, task, attempt):
    with store._db(task) as db:
        _table(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM debug_commands WHERE attempt_id=? AND state='queued' ORDER BY created_at LIMIT 1", (attempt,)).fetchone()
        if not row:
            return None
        started = time.time()
        db.execute("UPDATE debug_commands SET state='running',started_at=? WHERE attempt_id=? AND request_id=?", (started, attempt, row["request_id"]))
        store._append(db, attempt, "debug/taken", {"request_id": row["request_id"], "started_at": started,
            "timeout_sec": json.loads(row["body"])["timeout_sec"]})
        store._publish(db, task, attempt)
        return {**json.loads(row["body"]), "request_id": row["request_id"], "started_at": started}


def finish(store, task, attempt, request, result):
    with store._db(task) as db:
        _table(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE debug_commands SET state='completed',finished_at=?,result=? WHERE attempt_id=? AND request_id=? AND state IN ('queued','running')",
                   (time.time(), json.dumps(scrub(result), ensure_ascii=False), attempt, request))
    return current(store, task, attempt, request)


def cancel(store, task, attempt, request):
    with store._db(task) as db:
        _table(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM debug_commands WHERE attempt_id=? AND request_id=?", (attempt, identifier(request))).fetchone()
        if not row:
            raise ValueError("debug_request_missing")
        if row["state"] in {"queued", "running"}:
            if row["state"] == "queued":
                result = {"status": "cancelled", "reason_code": "cancelled", "execution_started": "no", "write_stopped": False}
                db.execute("UPDATE debug_commands SET state='completed',cancel_requested=1,finished_at=?,result=? WHERE attempt_id=? AND request_id=?",
                           (time.time(), json.dumps(result), attempt, request))
                store._append(db, attempt, "debug/finished", {**result, "request_id": request})
            else:
                db.execute("UPDATE debug_commands SET cancel_requested=1 WHERE attempt_id=? AND request_id=?", (attempt, request))
            store._append(db, attempt, "debug/cancel_requested", {"request_id": request})
            store._publish(db, task, attempt)
    return current(store, task, attempt, request)
