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


def view(row):
    return {key: row[key] for key in ("request_id", "state", "created_at", "started_at", "finished_at", "cancel_requested")} | {
        "result": json.loads(row["result"]), "timeout_sec": json.loads(row["body"])["timeout_sec"]}


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
        store._append(db, attempt, "debug/queued", {"request_id": request, "code_hash": hashlib.sha256(code.encode()).hexdigest(), "script_path": path})
        store._publish(db, task, attempt)
        return view(db.execute("SELECT * FROM debug_commands WHERE attempt_id=? AND request_id=?", (attempt, request)).fetchone())


def current(store, task, attempt, request_id=""):
    with store._db(task) as db:
        _table(db)
        row = db.execute("SELECT * FROM debug_commands WHERE attempt_id=?" + (" AND request_id=?" if request_id else " ORDER BY created_at DESC LIMIT 1"),
                         (attempt, identifier(request_id)) if request_id else (attempt,)).fetchone()
        return view(row) if row else None


def take(store, task, attempt):
    with store._db(task) as db:
        _table(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM debug_commands WHERE attempt_id=? AND state='queued' ORDER BY created_at LIMIT 1", (attempt,)).fetchone()
        if not row:
            return None
        db.execute("UPDATE debug_commands SET state='running',started_at=? WHERE attempt_id=? AND request_id=?", (time.time(), attempt, row["request_id"]))
        return {**json.loads(row["body"]), "request_id": row["request_id"]}


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
            db.execute("UPDATE debug_commands SET cancel_requested=1 WHERE attempt_id=? AND request_id=?", (attempt, request))
            store._append(db, attempt, "debug/cancel_requested", {"request_id": request})
            store._publish(db, task, attempt)
    return current(store, task, attempt, request)
