"""Versioned authoring milestones backed by the existing Scout evidence ledger."""
from __future__ import annotations

import ast
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from mino_scout.runtime import debug_commands
from mino_scout.runtime.script_check import check_repo

STAGES = ("draft", "implemented", "path_validated")


def save(store, task, attempt, command, files, hashes, workspace_hash, work):
    path, stage = command["script_path"], command["stage"]
    if path not in files or not path.startswith("tests/") or not path.endswith(".py"):
        raise ValueError("checkpoint_case_path_invalid")
    findings = []
    if stage != "draft":
        tree = ast.parse(files[path], filename=path)
        from mino_scout.runtime.script_source import source_binding
        if source_binding(tree) is None and str(Path(path).with_suffix(".yaml")) not in files:
            raise ValueError("checkpoint_source_required")
        run = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"), None)
        if run is None or not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                 and n.func.attr == "check" for n in ast.walk(run)):
            raise ValueError("checkpoint_complete_case_required")
        # Static checks see the exact captured version, not files being edited.
        with tempfile.TemporaryDirectory(prefix="checkpoint-", dir=work) as directory:
            snapshot = Path(directory)
            for name, data in files.items():
                target = snapshot / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            findings = [asdict(h) for h in check_repo(snapshot, require_cases=True)
                        if not h.path.startswith("tests/") or h.path in {path, str(Path(path).with_suffix(".yaml"))}]
        if any(h["severity"] != "info" for h in findings):
            return {"status": "failed", "reason_code": "checkpoint_static_check_failed", "findings": findings[:100]}
    evidence = None
    if stage == "path_validated":
        evidence = debug_commands.current(store, task, attempt, command["evidence_request_id"])
        result = (evidence or {}).get("result", {})
        if (not evidence or evidence["state"] != "completed" or result.get("status") != "completed"
                or result.get("script_path") != path or result.get("workspace_hash") != workspace_hash
                or result.get("last_action_seq", 0) < result.get("first_action_seq", 1)
                or result.get("assertions", 0) < 1
                or result.get("write_stopped")):
            raise ValueError("checkpoint_matching_execution_required")
    receipt = {"checkpoint_id": command["request_id"], "task_id": task, "session_id": attempt,
               "script_path": path, "stage": stage, "workspace_hash": workspace_hash,
               "script_hash": hashes[path], "evidence_request_id": command["evidence_request_id"],
               "findings": findings[:100]}
    if evidence:
        receipt["first_action_seq"] = evidence["result"]["first_action_seq"]
        receipt["last_action_seq"] = evidence["result"]["last_action_seq"]
    return {"status": "completed", "checkpoint": receipt}


def progress(store, task, attempt):
    with store._db(task) as db:
        debug_commands._table(db)
        rows = db.execute("SELECT result FROM debug_commands WHERE attempt_id=? AND state='completed' ORDER BY created_at", (attempt,)).fetchall()
    latest = {}
    for row in rows:
        receipt = json.loads(row["result"]).get("checkpoint")
        if receipt:
            latest[receipt["script_path"]] = receipt
    current = store.metadata(task, attempt).get("workspace_hash", "")
    items = [{**item, "stale": item["workspace_hash"] != current} for item in latest.values()]
    return {"unit": "case_path", "items": items,
            "counts": {stage: sum(not i["stale"] and STAGES.index(i["stage"]) >= STAGES.index(stage) for i in items) for stage in STAGES},
            "stale": sum(i["stale"] for i in items)}


def get(store, task, attempt, request):
    row = debug_commands.current(store, task, attempt, request)
    receipt = (row or {}).get("result", {}).get("checkpoint")
    if not receipt:
        raise ValueError("debug_checkpoint_not_found")
    return receipt
