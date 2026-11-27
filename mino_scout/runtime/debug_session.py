"""One Scout worker, one retained device context, many recorded fragments."""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import quote

from mino_scout.runtime import debug_commands as commands
from mino_scout.runtime import script_exec as runtime
from mino_scout.runtime.records import scrub
from mino_scout.runtime.workspaces import authoring_workspace, attempt_workspace, source_snapshot


class SnapshotImports(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Compile the captured local bytes, with their original workspace filenames."""
    def __init__(self, root, files):
        self.root, self.files = root, files

    def find_spec(self, fullname, path=None, target=None):
        stem = fullname.replace(".", "/")
        name = stem + "/__init__.py" if stem + "/__init__.py" in self.files else stem + ".py"
        if name in self.files:
            return importlib.util.spec_from_file_location(fullname, self.root / name, loader=self,
                submodule_search_locations=[str(self.root / stem)] if name.endswith("/__init__.py") else None)
        if any(key.startswith(stem + "/") for key in self.files):
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(self.root / stem)]
            return spec
        # Namespace packages must not fall through to newly edited local bytes.
        fallback = importlib.machinery.PathFinder.find_spec(fullname, path)
        locations = ([fallback.origin] if fallback and fallback.origin else [])
        if fallback and fallback.submodule_search_locations:
            locations.extend(fallback.submodule_search_locations)
        if any(Path(location).absolute().is_relative_to(self.root) for location in locations):
            raise ImportError("debug_module_not_in_snapshot:" + fullname)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = Path(module.__spec__.origin).relative_to(self.root).as_posix()
        exec(compile(self.files[name], str(self.root / name), "exec"), module.__dict__)


def pages_from_snapshot(files):
    pages = {}
    for name, data in files.items():
        parts = Path(name).parts
        if parts[0] != "pages" or Path(name).suffix not in {".yaml", ".yml"}:
            continue
        if len(parts) < 3:
            raise ValueError("page_layout_invalid")
        page = runtime.parse_yaml(data.decode("utf-8"))
        if not isinstance(page, dict):
            raise ValueError("page_invalid")
        pages.setdefault(parts[1], {})[str(page.get("page_key") or Path(name).stem)] = page
    return pages


class FragmentStop:
    def __init__(self, stop, store, task, attempt, request):
        self.stop, self.store, self.task, self.attempt, self.request = stop, store, task, attempt, request
        self.checked, self.cancelled = 0.0, False

    def is_set(self):
        if self.stop.is_set():
            return True
        now = time.monotonic()
        if now - self.checked >= 0.05:
            row = commands.current(self.store, self.task, self.attempt, self.request)
            self.cancelled = bool(row and row["cancel_requested"])
            self.checked = now
        return self.cancelled


def run(core, payload, store, stop):
    root = authoring_workspace(payload.workspace, store.root)
    work = attempt_workspace(store, payload.task_id, payload.attempt_id)
    output = work / "output"
    if output.is_symlink() or not output.resolve().is_relative_to(work.resolve()):
        raise ValueError("debug_output_workspace_invalid")
    output.mkdir(exist_ok=True, mode=0o700)
    os.chdir(output)
    recorder = core._recording
    case = runtime.Case(core, payload, root, deadline=0, stop=stop, debug_source="RUN_PURPOSE = 'authoring'\n")
    namespace = {"case": case, "__name__": "__mino_debug__"}
    if not case.app_key or case.app_key not in case.config._apps:
        raise ValueError("debug_app_key_missing")
    namespace["app"] = case.init(getattr(case.config, case.app_key))
    if payload.platform == "web":
        from mino_scout.platform.web.playwright_hub import get_hub
        get_hub().note_lane(payload.run_id)
    store.update_metadata(payload.task_id, payload.attempt_id, {
        "execution_kind": "debug", "run_purpose": "authoring", "workspace": str(root),
        "execution_workspace": str(work), "output_workspace": str(output), "debug_state": "ready"})
    config_hash = hashlib.sha256((root / "config.yaml").read_bytes()).hexdigest()
    while not stop.is_set() and not recorder.cancelled():
        command = commands.take(store, payload.task_id, payload.attempt_id)
        if not command:
            stop.wait(0.1)
            continue
        request = command["request_id"]
        fragment_output = output / quote(request, safe="-_.")
        if fragment_output.is_symlink() or not fragment_output.resolve().is_relative_to(output.resolve()):
            raise ValueError("debug_output_workspace_invalid")
        fragment_output.mkdir(exist_ok=True, mode=0o700)
        os.chdir(fragment_output)
        case.output_dir = fragment_output
        case.stop = FragmentStop(stop, store, payload.task_id, payload.attempt_id, request)
        case.deadline = time.monotonic() + max(0, command["started_at"] + command["timeout_sec"] - time.time())
        case._latched_error = None
        case._failure_role = case._failure_ref = case._failure_title = ""
        case._source_ref = case._step_id = case._role = ""
        case._step = None
        assertion_start, action_start = len(case._assertions), case._seq
        hooks = []
        old_trace = sys.gettrace()
        outcome = {"status": "completed", "reason_code": "", "request_id": request,
                   "script_path": command["script_path"], "phase": "snapshot", "execution_started": "no"}
        try:
            case._guard()
            files, hashes, workspace_hash = source_snapshot(root)
            if command["workspace_hash"] and workspace_hash != command["workspace_hash"]:
                raise ValueError("workspace_version_conflict")
            if hashes.get("config.yaml") != config_hash:
                raise ValueError("debug_config_changed_restart_required")
            outcome.update(workspace_hash=workspace_hash)
            store.update_metadata(payload.task_id, payload.attempt_id, {"workspace_hash": workspace_hash})
            if command["kind"] == "checkpoint":
                from mino_scout.runtime.debug_checkpoints import save
                if command["stage"] == "path_validated" and case._debug_write_error:
                    raise case._debug_write_error
                outcome["phase"] = "static_check"
                outcome.update(save(store, payload.task_id, payload.attempt_id, command, files, hashes, workspace_hash, work))
                continue
            outcome["phase"] = "load_code"
            case._all_pages = pages_from_snapshot(files)
            for app in case._apps.values():
                app._pages = case._all_pages.get(app.key, {})
                app._bound_controls.clear()
                app._shot = None
                app._observation_id = app._artifact_id = ""
            path = command["script_path"]
            if path:
                from mino_scout.runtime.script_repo import locate_script
                from mino_scout.runtime.script_source import source_sidecar_path
                path = locate_script(root, "", path)
                code = files[path].decode("utf-8")
                outcome["phase"] = "source"
                case._bind_source(root / path, source=code, sidecar=files.get(source_sidecar_path(path)))
            else:
                path, code = ".mino-debug-" + request + ".py", command["code"]
                case._source_snapshot, case._source_clauses, case._source_roles = {}, {}, {}
                case._source_hash, case._covered = "", set()
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            # Every local import uses this fragment's captured bytes, never stale pyc.
            local = {name: module for name, module in list(sys.modules.items())
                     if getattr(module, "__file__", None) and Path(module.__file__).resolve().is_relative_to(root)}
            for name in local:
                sys.modules.pop(name, None)
            hooks = [runtime._BlockImports(root), SnapshotImports(root, files)]
            sys.meta_path[:0] = hooks
            for name, value in list(namespace.items()):
                module_name = getattr(value, "__module__", "")
                if module_name in local and hasattr(value, "__name__"):
                    namespace[name] = getattr(importlib.import_module(module_name), value.__name__)
                elif getattr(value, "__name__", "") in local:
                    namespace[name] = importlib.import_module(value.__name__)
                elif type(value).__module__ in local:
                    del namespace[name]  # Instances retain old class code; require explicit reconstruction.
            from mino_scout.runtime.model_policy import ModelGate
            case._model_gate = ModelGate(case, root / path, source=code)
            outcome.update(workspace_hash=workspace_hash, code_hash=code_hash)
            fragment = {"request_id": request, "workspace_hash": workspace_hash, "file_hashes": hashes,
                        "source_snapshot": case._source_snapshot, "source_hash": case._source_hash,
                        "code_hash": code_hash, "script_path": command["script_path"], "first_action_seq": action_start + 1,
                        "started_at": command["started_at"], "timeout_sec": command["timeout_sec"],
                        "deadline_at": command["started_at"] + command["timeout_sec"], "output_workspace": str(fragment_output)}
            recorder.append("debug/started", fragment)
            store.update_metadata(payload.task_id, payload.attempt_id, {
                "debug_state": "executing", "workspace_hash": workspace_hash,
                "debug_last_command": commands.current(store, payload.task_id, payload.attempt_id, request),
                "output_workspace": str(fragment_output), "execution_started": "yes"})
            def trace(frame, event, arg):
                if event == "line" and frame.f_code.co_filename.startswith(str(root)):
                    case._guard()
                return trace
            sys.settrace(trace)
            outcome["phase"] = "execution"
            outcome["execution_started"] = "yes"
            if command["kind"] == "reset":
                # Reset is a recorded device operation, not a way around unknown writes.
                case._otp_requests.clear()
                app = namespace["app"]
                app.close()
                app.launch()
                namespace = {"case": case, "app": app, "__name__": "__mino_debug__"}
            else:
                if command["script_path"]:
                    from mino_scout.runtime.case_data import CaseData
                    case._otp_requests.clear()
                    case.data = CaseData(case, getattr(payload, "inputs", {}))
                    runtime._execute_hooks(root, path, case, source_files=files)
                    runtime._validate_case(case, assertion_start)
                    outcome.update(case_lifecycle_completed=True, source_coverage=case._coverage(), source_hash=case._source_hash)
                else:
                    tree = ast.parse(code, filename=str(root / path))
                    exec(compile(tree, str(root / path), "exec"), namespace)
                    if any(isinstance(node, ast.FunctionDef) and node.name == "run" for node in tree.body):
                        namespace["run"](case)
            case._guard()
            if case._latched_error:
                raise case._latched_error
            outcome.update(workspace_hash=workspace_hash, code_hash=code_hash, phase="completed")
        except BaseException as exc:
            reason = str(getattr(exc, "reason_code", "") or (str(exc) if isinstance(exc, ValueError) else "script_failed"))
            if isinstance(exc, runtime.ScriptHalt):
                reason = "cancelled" if case.stop.is_set() else "script_timeout"
            outcome.update(status="cancelled" if reason == "cancelled" else "blocked" if getattr(exc, "status", "") == "blocked" else "failed", reason_code=reason,
                           summary=str(scrub(str(exc)))[:2000], traceback=str(scrub(traceback.format_exc()))[-8000:])
            details = getattr(exc, "details", {})
            if details.get("phase"):
                outcome["phase"] = details["phase"]
            if details.get("action_execution_started"):
                outcome["action_execution_started"] = details["action_execution_started"]
            if details.get("execution_started") in {"no", "yes", "unknown"}:
                outcome["execution_started"] = details["execution_started"]
            error = case._latched_error or exc
            if getattr(error, "reason_code", "") == "write_outcome_unknown" or getattr(error, "details", {}).get("resource_isolated"):
                case._debug_write_error = error
                outcome["execution_started"] = "unknown"
        finally:
            sys.settrace(old_trace)
            for hook in hooks:
                if hook in sys.meta_path:
                    sys.meta_path.remove(hook)
            case.deadline = 0
            case.stop = stop
            assertions = case._assertions[assertion_start:]
            outcome.update(validation_status="assertions_passed" if outcome["status"] == "completed" and assertions else "observed" if outcome["status"] == "completed" else "failed",
                           first_action_seq=action_start + 1, last_action_seq=case._seq, assertions=len(assertions),
                           write_stopped=case._debug_write_error is not None)
            recorder.append("debug/finished", outcome)
            receipt = commands.finish(store, payload.task_id, payload.attempt_id, request, outcome)
            store.update_metadata(payload.task_id, payload.attempt_id, {
                "debug_state": "write_stopped" if case._debug_write_error else "ready", "debug_last_command": receipt})
    return {"status": "cancelled", "reason_code": "debug_session_closed", "summary": "调试会话已结束；不产生正式测试结论",
            "execution_started": "yes" if case.touched else "no", "resource_isolated": bool(case._debug_write_error)}
