"""Durable execution evidence. Only summaries leave Scout without a record read request.

Each account has a records root; task ids are directory names, never caller paths.
SQLite transactions atomically publish events, status and the summary outbox.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

TERMINAL = frozenset({"pass", "fail", "blocked", "declined", "skipped", "cancelled", "interrupted"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}\Z")
_SECRETS = frozenset({"password", "passwd", "otp", "otp_code", "token", "api_key", "secret", "secret_value", "authorization", "access_token", "refresh_token", "cookie", "cookies", "credential", "storage_state", "session_storage"})


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def identifier(value: Any) -> str:
    value = str(value or "")
    if not _ID.fullmatch(value) or ".." in value:
        raise ValueError("record_id_invalid")
    return value


def scrub(value: Any) -> Any:
    from mino_scout.runtime.secret_redaction import remember, sanitize
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _SECRETS:
                if isinstance(item, str):
                    remember(item)
                elif isinstance(item, dict):
                    for secret in item.values():
                        if isinstance(secret, str):
                            remember(secret)
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = scrub(item)
        return sanitize(out)
    if isinstance(value, (tuple, list)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        # Git/HTTP URLs must never archive embedded credentials or token queries.
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)([?&](?:token|access_token|api_key|password)=)[^&\s]+", r"\1[REDACTED]", value)
        return sanitize(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(scrub(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


class RecordStore:
    def __init__(self, root: Path, *, legacy_root: Path | None = None):
        self.legacy_root = Path(legacy_root).resolve() if legacy_root else None
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _folder(self, task_id: str) -> Path:
        # Keep wire ids opaque; ':' in legacy session/task ids is not a Windows path character.
        folder = self.root / quote(identifier(task_id), safe="-_.")
        if not folder.exists() and self.legacy_root is not None:
            legacy = self.legacy_root / folder.name
            if (legacy / "records.sqlite3").is_file():
                folder = legacy
        if folder.is_symlink():
            raise ValueError("record_path_invalid")
        return folder

    def _folders(self):
        folders = {p.name: p for p in self.legacy_root.iterdir()} if self.legacy_root and self.legacy_root.is_dir() else {}
        folders.update({p.name: p for p in self.root.iterdir()})
        return sorted(folders.values(), key=lambda p: unquote(p.name))

    def _db(self, task_id: str, *, create: bool = False):
        folder = self._folder(task_id)
        path = folder / "records.sqlite3"
        if not create and not path.is_file():
            raise FileNotFoundError("record_missing")
        if path.is_symlink():
            raise ValueError("record_path_invalid")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(path, timeout=10, factory=_Connection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=10000")
        if create:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                    metadata TEXT NOT NULL, state TEXT NOT NULL, reason_code TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, started_at REAL,
                    finished_at REAL, seq INTEGER NOT NULL DEFAULT 0,
                    summary_version INTEGER NOT NULL DEFAULT 1, acked_version INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, resource_isolated INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    attempt_id TEXT NOT NULL, seq INTEGER NOT NULL, timestamp REAL NOT NULL,
                    type TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(attempt_id,seq)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
                    mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, filename TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    attempt_id TEXT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL
                );
            ''')
        return db

    def existing(self, task_id: str, attempt_id: str, payload_hash: str):
        """Return a durable receipt before revalidating mutable external inputs."""
        try:
            with self._db(task_id) as db:
                row = self._row(db, attempt_id)
                if row["payload_hash"] != payload_hash:
                    raise ValueError("attempt_parameters_conflict")
                return self._summary(db, task_id, row)
        except FileNotFoundError:
            return None

    def create(self, task_id: str, attempt_id: str, metadata: dict, payload_hash: str) -> tuple[dict, bool]:
        identifier(attempt_id)
        with self._db(task_id, create=True) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise ValueError("attempt_parameters_conflict")
                return self._summary(db, task_id, old), False
            db.execute("INSERT INTO attempts(attempt_id,payload_hash,metadata,state,created_at) VALUES(?,?,?,'accepted',?)",
                       (attempt_id, payload_hash, _json(metadata), time.time()))
            self._append(db, attempt_id, "run/accepted", {"task_id": task_id, "attempt_id": attempt_id})
            summary = self._publish(db, task_id, attempt_id)
            return summary, True

    def _row(self, db, attempt_id):
        row = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (identifier(attempt_id),)).fetchone()
        if row is None:
            raise FileNotFoundError("record_missing")
        return row

    def _append(self, db, attempt_id, event_type, data):
        row = self._row(db, attempt_id)
        seq = row["seq"] + 1
        db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (attempt_id, seq, time.time(), event_type, _json(data)))
        db.execute("UPDATE attempts SET seq=?,summary_version=summary_version+1 WHERE attempt_id=?", (seq, attempt_id))
        return seq

    def _summary(self, db, task_id, row):
        meta = json.loads(row["metadata"])
        counts = {"total": 0, "pass": 0, "fail": 0, "unknown": 0}
        for (data,) in db.execute("SELECT data FROM events WHERE attempt_id=? AND type='assertion/result'", (row["attempt_id"],)):
            status = json.loads(data).get("status")
            counts["total"] += 1
            counts[status if status in {"pass", "fail"} else "unknown"] += 1
        state = row["state"]
        status = {"accepted": "queued", "interrupted": "blocked"}.get(state, state)
        public_state = "interrupted" if state == "interrupted" else "completed" if state in TERMINAL else state
        if row["cancel_requested"] and state not in TERMINAL:
            public_state = "cancel_requested"
        debug = {}
        if meta.get("execution_kind") == "debug":
            from mino_scout.runtime.debug_commands import projection
            now = time.time()
            command = projection(db, row["attempt_id"], now=now)
            age = int(max(0, (row["finished_at"] or now) - row["started_at"]) * 1000) if row["started_at"] else 0
            debug = {"session_state": "closed" if state in TERMINAL else "closing" if row["cancel_requested"] else
                "executing" if command and command["state"] == "running" else "queued" if command and command["state"] == "queued" else
                "ready" if state == "running" and meta.get("debug_state") in {"ready", "executing", "write_stopped"} else "starting", "session_age_ms": age, "command": command,
                "active_command": command if command and command["state"] in {"queued", "running"} else None}
            debug["debug_state"] = "write_stopped" if state not in TERMINAL and command and command["result"].get("write_stopped") else debug["session_state"]
        return {"task_id": task_id, "attempt_id": row["attempt_id"],
                **{k: meta.get(k, "") for k in ("run_id", "session_id", "case_id", "node_id", "sn", "platform", "owner_user_id", "revision", "script_path")},
                "state": public_state, "status": status, "reason_code": row["reason_code"],
                "summary": row["summary"], "summary_version": row["summary_version"],
                "last_event_seq": row["seq"], "created_at": _timestamp(row["created_at"]), "started_at": _timestamp(row["started_at"]),
                "finished_at": _timestamp(row["finished_at"]), "sealed": state in TERMINAL,
                "resource_isolated": bool(row["resource_isolated"]), "assertions": counts,
                "detail_availability": "available", "failure_stage": meta.get("failure_stage", ""),
                "elapsed_ms": int(max(0, (row["finished_at"] or time.time()) - row["started_at"]) * 1000) if row["started_at"] else 0,
                "run_purpose": meta.get("run_purpose", "unclassified"),
                **{k: meta[k] for k in ("execution_kind", "workspace", "execution_workspace", "source_workspace", "output_workspace", "debug_state", "debug_last_command", "workspace_hash", "debug_origin", "debug_origin_matched") if k in meta},
                "execution_started": meta.get("execution_started", "unknown" if meta.get("execution_kind") == "debug" else "no"),
                **{key: meta[key] for key in ("execution_stopped", "browser_closed", "scout_process_generation") if key in meta},
                **({"otp_wait": meta["otp_wait"]} if "otp_wait" in meta else {}),
                "data_outputs": meta.get("data_outputs", {}), "data_missing": meta.get("data_missing", []),
                "source_hash": meta.get("source_hash", ""), "source_coverage": meta.get("source_coverage", {}),
                **({"test_report": meta["test_report"]} if "test_report" in meta else {}),
                "cancel_requested": bool(row["cancel_requested"]),
                "sync_state": "acked" if row["acked_version"] >= row["summary_version"] else "pending",
                "record_ref": {"task_id": task_id, "attempt_id": row["attempt_id"], "node_id": meta.get("node_id", "")}, **debug}

    def _publish(self, db, task_id, attempt_id):
        summary = self._summary(db, task_id, self._row(db, attempt_id))
        db.execute("INSERT INTO outbox VALUES(?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET version=excluded.version,payload=excluded.payload",
                   (attempt_id, summary["summary_version"], _json(summary)))
        return summary

    def status(self, task_id, attempt_id):
        with self._db(task_id) as db:
            db.execute("BEGIN")
            return self._summary(db, task_id, self._row(db, attempt_id))

    def metadata(self, task_id, attempt_id):
        with self._db(task_id) as db:
            return json.loads(self._row(db, attempt_id)["metadata"])

    def update_metadata(self, task_id, attempt_id, changes):
        with self._db(task_id) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, attempt_id)
            if row["state"] in TERMINAL:
                raise ValueError("record_sealed")
            meta = json.loads(row["metadata"])
            meta.update(scrub(changes))
            db.execute("UPDATE attempts SET metadata=? WHERE attempt_id=?", (_json(meta), attempt_id))
            self._append(db, attempt_id, "run/snapshot", changes)
            self._publish(db, task_id, attempt_id)

    def append(self, task_id, attempt_id, event_type, data):
        with self._db(task_id) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, attempt_id)
            if row["state"] in TERMINAL:
                raise ValueError("record_sealed")
            if event_type in {"data/bind", "data/missing"}:
                from mino_scout.server_comm.protocol import validate_data_summary
                metadata = json.loads(row["metadata"])
                outputs = dict(metadata.get("data_outputs") or {})
                missing = {item["name"]: item for item in metadata.get("data_missing") or []}
                name = data.get("name")
                if event_type == "data/bind":
                    outputs[name] = data.get("value")
                    missing.pop(name, None)
                else:
                    missing[name] = {key: data[key] for key in ("name", "description", "kind", "attributes") if key in data}
                metadata.update(validate_data_summary({"data_outputs": outputs, "data_missing": list(missing.values())}))
                db.execute("UPDATE attempts SET metadata=? WHERE attempt_id=?", (_json(metadata), attempt_id))
            seq = self._append(db, attempt_id, event_type, data)
            self._publish(db, task_id, attempt_id)
            return seq

    def start(self, task_id, attempt_id):
        with self._db(task_id) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, attempt_id)
            if row["state"] != "accepted":
                return False
            db.execute("UPDATE attempts SET state='running',started_at=? WHERE attempt_id=?", (time.time(), attempt_id))
            self._append(db, attempt_id, "run/started", {})
            self._publish(db, task_id, attempt_id)
            return True

    def finish(self, task_id, attempt_id, outcome):
        state = str(outcome.get("status") or "fail")
        if state not in TERMINAL:
            state = "fail"
        with self._db(task_id) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, attempt_id)
            if row["state"] in TERMINAL:
                return self._summary(db, task_id, row)
            metadata = json.loads(row["metadata"])
            from mino_scout.server_comm.protocol import validate_data_summary
            outcome = {**outcome, **validate_data_summary({**metadata, **outcome})}
            metadata.update({key: outcome[key] for key in ("failure_stage", "execution_started", "source_coverage", "execution_stopped", "browser_closed", "data_outputs", "data_missing") if key in outcome})
            from mino_scout.runtime.business_report import build_report
            events = [(seq, kind, json.loads(data)) for seq, kind, data in db.execute(
                "SELECT seq,type,data FROM events WHERE attempt_id=? ORDER BY seq", (attempt_id,))]
            metadata["test_report"] = scrub(build_report(metadata, outcome, events))
            db.execute("UPDATE attempts SET metadata=? WHERE attempt_id=?", (_json(metadata), attempt_id))
            self._append(db, attempt_id, "run/finished", outcome)
            db.execute("UPDATE attempts SET state=?,reason_code=?,summary=?,finished_at=?,resource_isolated=? WHERE attempt_id=?",
                       (state, str(outcome.get("reason_code") or ""), str(scrub(outcome.get("summary") or ""))[:2000],
                        time.time(), int(bool(outcome.get("resource_isolated"))), attempt_id))
            return self._publish(db, task_id, attempt_id)

    def cancel(self, task_id, attempt_id):
        with self._db(task_id) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, attempt_id)
            if row["state"] not in TERMINAL and not row["cancel_requested"]:
                db.execute("UPDATE attempts SET cancel_requested=1 WHERE attempt_id=?", (attempt_id,))
                self._append(db, attempt_id, "run/cancel_requested", {})
                return self._publish(db, task_id, attempt_id)
            return self._summary(db, task_id, row)

    def events(self, task_id, attempt_id, *, after_seq=0, limit=100, max_bytes=512_000):
        limit = max(1, min(int(limit), 200))
        max_bytes = max(4096, min(int(max_bytes), 1_000_000))
        after_seq = max(0, int(after_seq))
        with self._db(task_id) as db:
            row = self._row(db, attempt_id)
            rows = db.execute("SELECT * FROM events WHERE attempt_id=? AND seq>? ORDER BY seq LIMIT ?", (attempt_id, after_seq, limit))
            items, size, cursor = [], 0, after_seq
            for event in rows:
                item = {"seq": event["seq"], "ts": _timestamp(event["timestamp"]), "type": event["type"], "payload": json.loads(event["data"])}
                used = len(_json(item).encode())
                if used > max_bytes:
                    artifact = self.artifact(task_id, attempt_id, _json(item["payload"]).encode(), "application/json")
                    item["payload"] = {key: item["payload"][key] for key in ("capability_id", "status", "source_ref", "source_hash", "step_id", "observation_id", "assertion_id", "checker", "app", "action_seq") if key in item["payload"]}
                    item["payload"].update(artifact, detail_in_artifact=True)
                    used = len(_json(item).encode())
                if items and size + used > max_bytes:
                    break
                items.append(item)
                size += used
                cursor = event["seq"]
            return {"items": items, "next_seq": cursor, "high_watermark": row["seq"],
                    "has_more": cursor < row["seq"], "sealed": row["state"] in TERMINAL, "record_source": "scout"}

    def manifest(self, task_id):
        with self._db(task_id) as db:
            attempts = [self._summary(db, task_id, row) for row in db.execute("SELECT * FROM attempts ORDER BY created_at")]
            return {"task_id": task_id, "attempts": attempts}

    def artifact(self, task_id, attempt_id, data: bytes, mime: str) -> dict:
        identifier(attempt_id)
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = "a_" + hashlib.sha256((attempt_id + digest).encode()).hexdigest()
        attempt_folder = quote(attempt_id, safe="-_.")
        folder = self._folder(task_id) / "artifacts" / attempt_folder
        if not folder.resolve().is_relative_to(self._folder(task_id).resolve()):
            raise ValueError("record_path_invalid")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = f"artifacts/{attempt_folder}/{artifact_id}"
        path = self._folder(task_id) / filename
        if not path.exists():
            temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            if os.name != "nt":
                fd = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        with self._db(task_id) as db:
            self._row(db, attempt_id)
            db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)", (artifact_id, attempt_id, mime, len(data), digest, filename))
        return {"artifact_id": artifact_id, "mime": mime, "mime_type": mime, "size": len(data), "sha256": digest}

    def read_artifact(self, task_id, attempt_id, artifact_id, *, offset=0, length=262144):
        identifier(artifact_id)
        offset, length = max(0, int(offset)), max(1, min(int(length), 524288))
        with self._db(task_id) as db:
            self._row(db, attempt_id)
            row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND attempt_id=?", (artifact_id, attempt_id)).fetchone()
            if row is None:
                raise FileNotFoundError("artifact_missing")
            path = (self._folder(task_id) / row["filename"]).resolve()
            if not path.is_relative_to(self._folder(task_id).resolve()) or path.is_symlink():
                raise ValueError("record_path_invalid")
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(length)
            return {"artifact_id": artifact_id, "mime_type": row["mime"], "total_size": row["size"], "sha256": row["sha256"],
                    "offset": offset, "bytes_read": len(data), "eof": offset + len(data) >= row["size"],
                    "data_base64": base64.b64encode(data).decode("ascii")}

    def pending(self, limit=32, after=None):
        """Return a bounded rotating batch, including permanently rejected entries.

        The caller can keep the last task/attempt key to prevent one invalid
        summary from starving all later tasks. Unacknowledged entries stay durable.
        """
        limit = max(1, int(limit))
        items, wrapped = [], []
        cursor = tuple(after) if after else None
        for folder in self._folders():
            task_id = unquote(folder.name)
            if not folder.is_dir() or folder.is_symlink() or not _ID.fullmatch(task_id):
                continue
            if not (folder / "records.sqlite3").is_file():
                continue
            with self._db(task_id) as db:
                for row in db.execute("SELECT * FROM outbox ORDER BY attempt_id"):
                    summary = json.loads(row["payload"])
                    if cursor and (task_id, summary["attempt_id"]) <= cursor:
                        if len(wrapped) < limit:
                            wrapped.append(summary)
                        continue
                    items.append(summary)
                    if len(items) >= limit:
                        return items
        return items + wrapped[:limit - len(items)]

    def acknowledge(self, task_id, attempt_id, version):
        with self._db(task_id) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, attempt_id)
            version = int(version)
            if version < 1 or version > row["summary_version"]:
                raise ValueError("summary_ack_invalid")
            db.execute("UPDATE attempts SET acked_version=MAX(acked_version,?) WHERE attempt_id=?", (version, attempt_id))
            db.execute("DELETE FROM outbox WHERE attempt_id=? AND version<=?", (attempt_id, version))

    def recover(self):
        for folder in self._folders():
            task_id = unquote(folder.name)
            if not folder.is_dir() or folder.is_symlink() or not _ID.fullmatch(task_id) or not (folder / "records.sqlite3").is_file():
                continue
            with self._db(task_id) as db:
                ids = [row[0] for row in db.execute("SELECT attempt_id FROM attempts WHERE state NOT IN ('pass','fail','blocked','declined','skipped','cancelled','interrupted')")]
            for attempt_id in ids:
                # An in-flight write cannot be proved to have stopped after a crash.
                with self._db(task_id) as db:
                    calls = db.execute("SELECT type,data FROM events WHERE attempt_id=? AND type IN ('tool/call','tool/result') ORDER BY seq", (attempt_id,)).fetchall()
                active = set()
                for row in calls:
                    data = json.loads(row["data"])
                    key = data.get("action_seq")
                    if row["type"] == "tool/call" and data.get("effect_class") == "write":
                        active.add(key)
                    elif row["type"] == "tool/result":
                        active.discard(key)
                debug = self.metadata(task_id, attempt_id).get("execution_kind") == "debug"
                if debug:
                    from mino_scout.runtime import debug_commands
                    command = debug_commands.current(self, task_id, attempt_id)
                    if command and command["state"] in {"queued", "running"}:
                        debug_commands.finish(self, task_id, attempt_id, command["request_id"], {"status": "interrupted", "reason_code": "scout_restarted"})
                    self.update_metadata(task_id, attempt_id, {"debug_state": "interrupted"})
                self.finish(task_id, attempt_id, {"status": "interrupted", "reason_code": "scout_restarted",
                    "summary": "Scout 已重启，未自动重放；最后动作需核验", "resource_isolated": debug or bool(active),
                    **({"execution_started": "unknown", "execution_stopped": False} if debug else {})})


class Recorder:
    def __init__(self, store: RecordStore, task_id: str, attempt_id: str):
        self.store, self.task_id, self.attempt_id = store, task_id, attempt_id

    def append(self, event_type, data):
        encoded = _json(data).encode()
        if len(encoded) > 262144:
            artifact = self.store.artifact(self.task_id, self.attempt_id, encoded, "application/json")
            data = {key: data[key] for key in ("action_seq", "effect_class", "capability_id", "status", "source_ref", "source_hash", "step_id", "observation_id", "assertion_id", "checker", "app") if key in data}
            data.update(artifact, detail_in_artifact=True)
        return self.store.append(self.task_id, self.attempt_id, event_type, data)

    def image(self, encoded, mime):
        return self.store.artifact(self.task_id, self.attempt_id, base64.b64decode(encoded, validate=True), mime)

    def cancelled(self):
        return self.store.status(self.task_id, self.attempt_id)["cancel_requested"]
