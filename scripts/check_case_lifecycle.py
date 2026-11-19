#!/usr/bin/env python3
"""Offline runtime smoke in disposable workspaces; no device or network calls."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mino_scout.runtime import debug_commands, debug_session, script_exec
from mino_scout.runtime.records import RecordStore, Recorder
from mino_scout.runtime.workspaces import source_snapshot
from mino_scout.server_comm import protocol as P


def wait_for(read, ready):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        value = read()
        if ready(value):
            return value
        time.sleep(0.02)
    raise AssertionError("Offline runtime did not complete within 10 seconds")


def source(root, body, expectations=("E1",)):
    path = root / "tests/REQ/C1.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.with_suffix(".yaml").write_text("case_id: C1\nsource:\n  revision: fixture\npreconditions: []\nsteps: []\nexpectations:\n" +
        "".join(f"  - id: {ref}\n    text: {ref}\n" for ref in expectations), encoding="utf-8")
    return "tests/REQ/C1.py"


def record(store, kind, path):
    ident = kind + str(time.time_ns())
    payload = P.ScriptRun(run_id=ident, task_id=ident, session_id=ident, attempt_id=ident,
        owner_user_id="fixture", case_id="C1", sn="offline-phone", platform="android",
        repo_url="https://example.invalid/repo", branch="", revision="", script_path=path,
        execution_kind=kind)
    payload.device_hint = {"env": "test", "app_key": "sample"}
    meta = {key: getattr(payload, key) for key in ("run_id", "session_id", "case_id", "sn", "platform", "owner_user_id", "execution_kind")}
    store.create(ident, ident, meta, ident)
    store.start(ident, ident)
    core = SimpleNamespace(node_id="offline", _recording=Recorder(store, ident, ident))
    return core, payload


def notes(store, payload):
    return [e["payload"]["text"] for e in store.events(payload.task_id, payload.attempt_id, limit=200)["items"]
            if e["type"] == "script/note" and "text" in e["payload"]]


def runtime_smoke(base):
    root = base / "authoring"
    root.mkdir()
    (root / "config.yaml").write_text("apps:\n  sample:\n    platform: android\n    package: com.example.app\n", encoding="utf-8")
    (root / "pretest.py").write_text("def pretest(case):\n    case.nexus.note('project_pre')\n", encoding="utf-8")
    (root / "posttest.py").write_text("def posttest(case):\n    case.nexus.note('project_post')\n", encoding="utf-8")
    good = """def pretest(case):
    case.nexus.note('case_pre')
def run(case):
    with case.nexus.check('expected', source_ref='E1'):
        case.nexus.assert_equal(1, 1)
def posttest(case):
    case.nexus.note('case_post')
"""
    path = source(root, good)
    store = RecordStore(base / "execution")
    core, payload = record(store, "case", path)
    outcome = script_exec.run(core, payload, root, deadline=time.monotonic() + 10, stop=threading.Event())
    assert outcome["status"] == "pass", outcome
    expected_notes = notes(store, payload)
    assert expected_notes == ["project_pre", "case_pre", "case_post", "project_post"], expected_notes
    formal_failures = [
        ("def run(case):\n    with case.nexus.check('empty', source_ref='E1'):\n        pass\n", "blocked", "check_empty"),
        (good.replace("assert_equal(1, 1)", "assert_equal(1, 2)"), "fail", "assertion_failed"),
        ("def run(case):\n    case.nexus.account(data_need='fixture')\n", "blocked", "nexus_rejected"),
    ]
    for body, status, reason in formal_failures:
        source(root, body)
        formal_core, formal_payload = record(store, "case", path)
        formal_core.nexus_call = lambda *args: P.Result(run_id=formal_payload.run_id, step_idx=-1, status=P.EventStatus.FAIL,
                                                       summary="surface_selection_required", error="ValueError")
        result = script_exec.run(formal_core, formal_payload, root, deadline=time.monotonic() + 10, stop=threading.Event())
        assert result["status"] == status and result["reason_code"] == reason, result
        if reason == "nexus_rejected":
            assert result["assertions"]["total"] == 0 and result["test_report"]["attribution"] == "blocked", result
    source(root, good)
    core, payload = record(store, "debug", "debug.py")
    payload.workspace = str(root)
    stop = threading.Event()
    errors = []
    def worker():
        try:
            debug_session.run(core, payload, store, stop)
        except BaseException as exc:
            errors.append(exc)
    old_cwd = Path.cwd()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        wait_for(lambda: store.metadata(payload.task_id, payload.attempt_id), lambda m: m.get("debug_state") == "ready")
        def execute(body, refs=("E1",)):
            source(root, body, refs)
            request = str(time.time_ns())
            debug_commands.submit(store, payload.task_id, payload.attempt_id,
                                  {"client_request_id": request, "script_path": path})
            row = wait_for(lambda: debug_commands.current(store, payload.task_id, payload.attempt_id, request),
                           lambda r: r and r["state"] == "completed")
            return row["result"]
        result = execute(good)
        assert result["status"] == "completed" and result["case_lifecycle_completed"] is True, result
        assert notes(store, payload) == expected_notes
        fragment_id = str(time.time_ns())
        debug_commands.submit(store, payload.task_id, payload.attempt_id,
                              {"client_request_id": fragment_id, "code": "case._otp_requests['prior-fragment'] = {}"})
        wait_for(lambda: debug_commands.current(store, payload.task_id, payload.attempt_id, fragment_id),
                 lambda row: row and row["state"] == "completed")
        result = execute(good.replace("def run(case):", "def run(case):\n    assert not case._otp_requests"))
        assert result["status"] == "completed", "Whole-case replay must clear fragment OTP requests"
        result = execute("def run(case):\n    with case.nexus.check('empty', source_ref='E1'):\n        pass\n")
        assert result["reason_code"] == "check_empty" and result["status"] == "blocked", result
        result = execute(good, ("E1", "E2"))
        assert result["reason_code"] == "source_coverage_missing", result
        bad_post = good.replace("case.nexus.note('case_post')", "case.nexus.note('case_post')\n    raise ValueError('cleanup_failed')")
        result = execute(bad_post)
        assert result["status"] == "failed" and notes(store, payload)[-1] == "project_post", result
        result = execute(bad_post.replace("assert_equal(1, 1)", "assert_equal(1, 2)"))
        assert result["reason_code"] == "assertion_failed", result
        assert notes(store, payload)[-1] == "project_post"
        result = execute(good)
        assert result["status"] == "completed", result
        # A prior milestone without lifecycle verification cannot authorize formal provenance.
        from mino_scout.runtime.debug_checkpoints import get
        old_request = str(time.time_ns())
        debug_commands.submit(store, payload.task_id, payload.attempt_id,
                              {"client_request_id": old_request, "code": "pass"})
        wait_for(lambda: debug_commands.current(store, payload.task_id, payload.attempt_id, old_request),
                 lambda r: r and r["state"] == "completed")
        with store._db(payload.task_id) as db:
            import json
            db.execute("UPDATE debug_commands SET result=? WHERE attempt_id=? AND request_id=?",
                       (json.dumps({"checkpoint": {"stage": "path_validated"}}), payload.attempt_id, old_request))
        try:
            get(store, payload.task_id, payload.attempt_id, old_request)
        except ValueError as exc:
            assert str(exc) == "debug_checkpoint_validation_required"
        else:
            raise AssertionError("Unverified legacy checkpoint accepted")
        core.nexus_call = lambda *args: P.Result(run_id=payload.run_id, step_idx=-1, status=P.EventStatus.FAIL,
                                                summary="surface_selection_required", error="ValueError")
        result = execute("def run(case):\n    case.nexus.account(data_need='fixture')\n")
        assert result["status"] == "blocked" and result["reason_code"] == "nexus_rejected", result
        assert "surface_selection_required" in result["summary"]
    finally:
        stop.set()
        thread.join(timeout=10)
        os.chdir(old_cwd)
    assert not thread.is_alive() and not errors, errors
    # Both known data formats participate in the exact content digest.
    for extension in ("json", "csv"):
        data = root / ("fixture." + extension)
        data.write_text("first", encoding="utf-8")
        before = source_snapshot(root)[2]
        data.write_text("second", encoding="utf-8")
        assert before != source_snapshot(root)[2]
    print("PASS: real debug queue, lifecycle parity, empty check, source coverage, cleanup, failure preservation, callback classification, data digest")


def mail_smoke():
    from mino_scout.plugins.mail_otp import _poll_inbox, _collect_text_parts, extract_otp_from_text
    message = EmailMessage()
    message["To"], message["From"], message["Date"] = "fixture@example.invalid", "sender@example.invalid", formatdate(usegmt=True)
    message.set_content("Your code is 123456")
    class Inbox:
        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b"1"]
            raw = message.as_bytes()
            return "OK", [(b"1 (UID 1 BODY[] {" + str(len(raw)).encode() + b"}", raw), b")"]
    consumed = set()
    def poll():
        return _poll_inbox(Inbox(), to_address="fixture@example.invalid", since_ts=time.time() - 5,
            from_allowlist=[], subject_contains="", seen=set(), in_window=set(), matched=set(),
            refresh=False, guard=lambda mail: None, stats={}, consumed_messages=consumed)
    assert poll() == "123456"
    assert poll() == "", "A consumed message must not satisfy a second OTP request"
    message.set_content("Your code is 654321")
    assert poll() == "654321"
    message.set_content('<html><head><style>.code {color:#123456}</style></head>'
                        '<body><script>var x=987654;</script><p style="color:#123456">'
                        'Your code is <b>654321</b></p></body></html>', subtype="html")
    assert extract_otp_from_text(_collect_text_parts(message)) == "654321"
    assert extract_otp_from_text("Code 654321; repeat 654321") == "654321"
    assert extract_otp_from_text("Reference 123456; code 654321") == ""
    body_with_year = "Your code is 654321; repeat 654321. Copyright 2026"
    assert extract_otp_from_text(body_with_year, subject="654321 is your verification code") == "654321"
    assert extract_otp_from_text(body_with_year) == ""
    assert extract_otp_from_text("Copyright 2026") == ""
    assert extract_otp_from_text("Copyright 2026", subject="Your verification code") == ""
    assert extract_otp_from_text("Your code is 2026") == "2026", "Labeled four-digit OTPs remain supported"
    assert extract_otp_from_text("654321", subject="Your verification code") == "654321"
    assert extract_otp_from_text(body_with_year, subject="123456 is your verification code") == ""
    assert extract_otp_from_text(body_with_year, subject="Codes 123456 and 654321") == ""
    assert extract_otp_from_text("Reference 123456") == ""
    message["Subject"] = "654321 是你的验证码"
    message.set_content(body_with_year)
    assert poll() == "654321", "The IMAP path must decode the subject and pass it to the parser"
    assert poll() == "", "Subject disambiguation must retain consumed-message protection"
    del message["Subject"]
    message.set_content('<html><body><p>Your code is <b>654321</b></p></body></html>', subtype="html")
    message.add_attachment(b"reference 987654", maintype="text", subtype="plain", filename="reference.txt")
    assert extract_otp_from_text(_collect_text_parts(message)) == "654321"

    class SharedInbox:
        def __init__(self):
            self.batches = []

        def uid(self, command, *args):
            if command == "SEARCH":
                return "OK", [b" ".join(str(i).encode() for i in range(1, 82))]
            ids = args[0].split(",")
            self.batches.append(ids)
            rows = []
            for ident in ids:
                item = EmailMessage()
                item["To"] = "fixture@example.invalid" if ident == "1" else "other@example.invalid"
                item["Date"] = formatdate(usegmt=True)
                item.set_content("Your code is 654321")
                raw = item.as_bytes()
                rows.append((f"1 (UID {ident} BODY[] {{{len(raw)}}}".encode(), raw))
            return "OK", rows
    inbox, seen = SharedInbox(), set()
    codes = [_poll_inbox(inbox, to_address="fixture@example.invalid", since_ts=time.time() - 5,
        from_allowlist=[], subject_contains="", seen=seen, in_window=set(), matched=set(),
        refresh=False, guard=lambda mail: None, stats={}) for _ in range(3)]
    assert codes == ["", "", "654321"], codes
    assert max(map(len, inbox.batches)) <= 40
    print("PASS: OTP subject/body confirmation, year rejection, deduplication, MIME/HTML, ambiguity, attachments, pagination")


def checkpoint_progress_smoke():
    import json
    import sqlite3
    from contextlib import contextmanager
    from mino_scout.runtime import debug_checkpoints

    class MemoryStore:
        def __init__(self):
            self.db = sqlite3.connect(":memory:")
            self.db.row_factory = sqlite3.Row

        @contextmanager
        def _db(self, task):
            yield self.db

        def metadata(self, *args):
            return {"workspace_hash": "same"}

    store = MemoryStore()
    try:
        debug_commands._table(store.db)
        for ident, contract, digest in (("legacy", "", "same"), ("valid", "case_lifecycle.v1", "same"),
                                         ("edited", "case_lifecycle.v1", "old")):
            receipt = {"stage": "path_validated", "script_path": f"tests/{ident}.py",
                       "workspace_hash": digest, "validation_contract": contract}
            store.db.execute("INSERT INTO debug_commands(attempt_id,request_id,fingerprint,body,state,created_at,result) "
                             "VALUES(?,?,?,?,?,?,?)", ("a", ident, ident, json.dumps({"timeout_sec": 60}),
                             "completed", 1, json.dumps({"checkpoint": receipt})))
        progress = debug_checkpoints.progress(store, "t", "a")
        assert progress["stale"] == 2 and progress["counts"]["path_validated"] == 1, progress
        assert debug_checkpoints.get(store, "t", "a", "valid")["validation_contract"] == "case_lifecycle.v1"
        try:
            debug_checkpoints.get(store, "t", "a", "legacy")
        except ValueError as exc:
            assert str(exc) == "debug_checkpoint_validation_required"
        else:
            raise AssertionError("Legacy checkpoint must require revalidation")
    finally:
        store.db.close()
    print("PASS: checkpoint progress excludes legacy validation and changed workspace")


def otp_request_smoke():
    from unittest.mock import patch
    from mino_scout.plugins import mail_otp, state
    from mino_scout.server_comm import callbacks
    from mino_scout.runtime.script_exec import _Scout, ScriptAbort

    class Inbox:
        sock = None
        epoch = 7
        next_uid = 501
        messages = {500: "123456"}
        searched = []

        def response(self, field):
            return field, [str(self.epoch if field == "UIDVALIDITY" else self.next_uid).encode()]

        def uid(self, command, *args):
            if command == "SEARCH":
                self.searched.append(args[0])
                # Also emulate servers returning the last old UID for an empty n:* range.
                return "OK", [b" ".join(str(uid).encode() for uid in self.messages)]
            rows = []
            for ident in args[0].split(","):
                msg = EmailMessage()
                msg["To"], msg["Date"] = "fixture@example.invalid", formatdate(usegmt=True)
                msg.set_content("Verification code: " + self.messages[int(ident)])
                raw = msg.as_bytes()
                rows.append((f"1 (UID {ident} BODY[] {{{len(raw)}}}".encode(), raw))
            return "OK", rows

        def logout(self):
            return "BYE", []

    def make_case():
        return SimpleNamespace(core=SimpleNamespace(node_id="offline"), _guard=lambda: None, deadline=0,
                               _otp_requests={}, _otp_messages={}, _mail_to="fixture@example.invalid")

    def rejected(reason, call):
        try:
            call()
        except ScriptAbort as exc:
            assert exc.status == "blocked" and exc.reason_code == reason, (exc.reason_code, reason)
        else:
            raise AssertionError("Expected rejection: " + reason)

    inbox = Inbox()
    account = SimpleNamespace(id="fixture", email="fixture@example.invalid", otp="", otp_policy={"otp_mode": "gmail"})
    case = make_case()
    scout = _Scout(case)
    def configured(plugin, provider, key):
        return "imap.example.invalid" if key == "imap_server" else "inbox@example.invalid"
    with patch.object(state, "plain_value", side_effect=configured), patch.object(state, "secret_value", return_value="fixture-password"), \
         patch.object(mail_otp, "_open_inbox", return_value=inbox) as opens:
        rejected("otp_request_required", lambda: scout.otp(account))
        assert opens.call_count == 0
        token = scout.otp_request(account)
        boundary = case._otp_requests[token]["boundary"]
        assert boundary == {"uid_validity": 7, "min_uid": 501}
        with patch.object(inbox, "response", return_value=("UIDVALIDITY", [None])):
            rejected("otp_environment_unavailable", lambda: scout.otp_request(account))
        token = scout.otp_request(account)
        old_only = mail_otp._poll_inbox(inbox, to_address=account.email, since_ts=time.time() - 5,
            from_allowlist=[], subject_contains="", seen=set(), in_window=set(), matched=set(),
            refresh=False, guard=lambda mail: None, stats={}, min_uid=boundary["min_uid"])
        assert old_only == "", "Previously received, unconsumed mail must be excluded"
        other = SimpleNamespace(id="other", email="other@example.invalid", otp="", otp_policy=account.otp_policy)
        rejected("otp_request_mismatch", lambda: scout.otp(other, request=token))
        rejected("otp_request_invalid", lambda: _Scout(make_case()).otp(account, request=token))
        inbox.messages[501] = "654321"
        with patch("mino_scout.runtime.secret_redaction.remember") as remember:
            assert scout.otp(account, request=token) == "654321"
            remember.assert_called_with("654321")
        assert "UID 501:*" in inbox.searched[-1]
        rejected("otp_request_invalid", lambda: scout.otp(account, request=token))
        inbox.next_uid = 502
        old = scout.otp_request(account)
        token = scout.otp_request(account)
        rejected("otp_request_invalid", lambda: scout.otp(account, request=old))
        case._otp_requests[token]["expires"] = time.monotonic() - 1
        rejected("otp_request_expired", lambda: scout.otp(account, request=token))
        token = scout.otp_request(account)
        inbox.epoch = 8
        rejected("otp_environment_unavailable", lambda: scout.otp(account, request=token))
        assert token not in case._otp_requests
        inbox.epoch = 7
        # Reconnect must re-check UIDVALIDITY rather than accept recycled UIDs.
        token = scout.otp_request(account)
        changed = Inbox()
        changed.epoch = 9
        import imaplib
        with patch.object(mail_otp, "_open_inbox", side_effect=[imaplib.IMAP4.abort("offline fixture"), changed]), \
             patch.object(mail_otp.time, "sleep", return_value=None):
            rejected("otp_environment_unavailable", lambda: scout.otp(account, request=token))
        token = scout.otp_request()
        inbox.messages[502] = "765432"
        with patch("mino_scout.runtime.secret_redaction.remember") as remember:
            assert scout.otp_mail(request=token) == "765432"
            remember.assert_called_with("765432")
        fixed = SimpleNamespace(otp="456789", otp_policy={})
        before = opens.call_count
        assert scout.otp_request(fixed) is None and scout.otp(fixed) == "456789"
        assert opens.call_count == before, "Fixed codes must not access mail"
        interrupted = make_case()
        def cancelled():
            raise ScriptAbort("cancelled fixture", reason_code="cancelled", status="blocked")
        interrupted._guard = cancelled
        rejected("cancelled", lambda: _Scout(interrupted).otp_request(account))
        assert opens.call_count == before, "Cancelled preparation must not connect"
    print("PASS: OTP request boundary, account/session isolation, single use, expiry, mailbox reset/reconnect, fixed-code compatibility and legacy redaction")


def official_endpoint_smoke(base):
    import hashlib
    import json
    from unittest.mock import patch
    from mino_scout.config import nexus_service_identity
    from mino_scout.plugins.state import verify_local_owner
    from mino_scout.server_comm import script_records

    old = "wss://nexus.minocc.top"
    new = "wss://nexus.minocc.top:8443/node"
    config = {"plugin_owner_user_id": "fixture", "plugin_owner_nexus": old,
              "plugin_storage_version": 2}
    assert nexus_service_identity(old) == nexus_service_identity(new)
    for unrelated in ("ws://nexus.minocc.top:8443", "wss://nexus.minocc.top:8444",
                      "wss://other.invalid:8443", "wss://localhost:8443"):
        assert nexus_service_identity(unrelated) != nexus_service_identity(old)
    assert nexus_service_identity("wss://example.invalid") != nexus_service_identity("wss://example.invalid:8443")
    with patch("mino_scout.config.load_config", return_value=config), patch("mino_scout.config.save_config") as save:
        verify_local_owner("fixture", new)
        save.assert_not_called()
        for owner, target in (("other", new), ("fixture", "wss://nexus.minocc.top:8444")):
            try:
                verify_local_owner(owner, target)
            except PermissionError:
                pass
            else:
                raise AssertionError("Unrelated plugin identity was accepted")
        with patch("mino_scout.config.config_dir", return_value=base / "endpoint"):
            core = SimpleNamespace()
            manager = script_records.bind(core, "fixture", new)
            expected = hashlib.sha256(json.dumps([old + "/node", "fixture"]).encode()).hexdigest()
            assert manager.profile == expected
            assert script_records.bind(core, "fixture", old + "/node") is manager
            manager.close()
    print("PASS official endpoint aliases preserve owner, plugin config and evidence paths")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="scout-lifecycle-") as directory:
        runtime_smoke(Path(directory))
        mail_smoke()
        checkpoint_progress_smoke()
        otp_request_smoke()
        official_endpoint_smoke(Path(directory))
