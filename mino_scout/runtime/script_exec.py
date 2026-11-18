"""跑 def run(case)。设备动词走本机渠道。账号和视觉判断回调 Nexus。

验证码用本节点 Gmail，文本模型用本节点保存的密钥。不安装渠道程序，不读能力目录。YAML 只解析。
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from mino_scout.executors.base import DeviceRef, ExecutorContext
from mino_scout.platform.web.viewports import DEFAULT_WEB_RESOLUTION, normalize_web_resolution
from mino_scout.schemas import PlanEvent

_CELLS = {"android": "uiautomator", "ios": "xcuitest", "web": "playwright", "mac": "ax", "win": "uia"}
_EXECUTOR = {"android": "adb", "ios": "ios_wda", "web": "playwright", "mac": "mac", "win": "win"}
_ITEM = {"android": "adb", "ios": "appium", "web": "chromium", "mac": "mac", "win": "win"}
_BANNED = {"adbutils", "uiautomator2", "playwright", "facebook", "facebook_wda", "wda", "appium", "selenium", "subprocess"}
_HIDDEN = {"otp", "password", "text", "image_base64", "image", "secret", "token"}
_RUN_LOCK = threading.Lock()


class ScriptHalt(BaseException):
    def __init__(self, message: str, *, reason_code: str):
        self.reason_code = reason_code
        super().__init__(message)


class ScriptAbort(Exception):
    def __init__(self, message: str, *, reason_code: str, status: str = "fail", details: dict | None = None):
        self.reason_code = reason_code
        self.status = status
        self.details = dict(details or {})
        super().__init__(message)


def _public_names(obj: Any) -> list[str]:
    """这个对象上能用的公开方法名，报错时给出来。"""
    names: list[str] = []
    for name in dir(type(obj)):
        if name.startswith("_") or name in {"core", "nexus", "scout"}:
            continue
        attr = getattr(obj, name, None)
        if callable(attr):
            names.append(name)
    return sorted(set(names))


def _unknown(owner: str, name: str, available: list[str], hint: str = "") -> ScriptAbort:
    """写错属性名时给出相近的名字和可用列表，而不是干巴巴一句报错。"""
    close = difflib.get_close_matches(str(name or ""), list(available or []), n=3, cutoff=0.4)
    bits = [f"{owner} 上没有 {name}"]
    if close:
        bits.append("最相近的是 " + "、".join(close))
    if available:
        bits.append("可用：" + "、".join(sorted(set(available))))
    text = "；".join(bits)
    if hint:
        text += hint
    return ScriptAbort(text, reason_code="method_unknown")


class ActionResult:
    def __init__(self, *, ok: bool, summary: str, status: str, data: dict[str, Any] | None = None):
        self.ok = bool(ok)
        self.summary = str(summary or "")
        self.status = str(status or "")
        self.data = dict(data or {})

    def __bool__(self) -> bool:
        return self.ok


class _Scope:
    def __init__(self, case: "Case", number: "int | str", role: str, source_ref: str = ""):
        self._case = case
        self._number = number
        self._role = role
        self._source_ref = str(source_ref or "")

    def _body(self, **extra: Any) -> dict[str, Any]:
        key = "title" if isinstance(self._number, str) else "number"
        return {"op": "step", "role": self._role, key: self._number,
                "source_ref": self._source_ref, "step_id": self._step_id, **extra}

    def __enter__(self) -> "Case":
        self._case._guard()
        if self._source_ref and self._case._source_clauses and self._source_ref not in self._case._source_clauses:
            raise ScriptAbort("source_ref 不在用例原文中", reason_code="source_ref_unknown", status="blocked")
        if self._source_ref in self._case._source_roles and self._case._source_roles[self._source_ref] != self._role:
            raise ScriptAbort("source_ref 的原文类型与步骤不符", reason_code="source_ref_role_mismatch", status="blocked")
        self._previous = (self._case._step, self._case._role, self._case._source_ref, self._case._step_id)
        self._start_assertions = len(self._case._assertions)
        self._start_error = self._case._latched_error
        self._step_id = "step_" + uuid.uuid4().hex
        self._case._step = self._number
        self._case._role = self._role
        self._case._source_ref = self._source_ref
        self._case._step_id = self._step_id
        self._case._callback("script.callback", self._body(action="enter"), 15)
        return self._case

    def __exit__(self, exc_type, exc, tb) -> bool:
        latched = self._case._latched_error is not self._start_error
        empty = not self._case._debug and exc_type is None and not latched and self._role == "check" and len(self._case._assertions) == self._start_assertions
        assertions = self._case._assertions[self._start_assertions:]
        failed = exc_type is not None or empty or latched or any(row["status"] != "pass" for row in assertions)
        if failed:
            self._case._failure_role = self._case._failure_role or self._role
            self._case._failure_ref = self._case._failure_ref or self._source_ref
            self._case._failure_title = self._case._failure_title or str(self._number)
        if exc_type is not None and isinstance(exc, Exception):
            self._case._latched_error = self._case._latched_error or (
                exc if isinstance(exc, ScriptAbort) else ScriptAbort(str(exc), reason_code="script_failed"))
        try:
            error = self._case._latched_error if latched else exc
            self._case._callback("script.callback", self._body(action="leave", failed=failed,
                status="blocked" if empty or any(row["status"] == "unknown" for row in assertions) else
                       str(getattr(error, "status", "fail")) if failed else "pass",
                reason_code="check_empty" if empty else str(getattr(error, "reason_code", "") or "")), 15)
            if not failed and self._source_ref:
                self._case._covered.add(self._source_ref)
            if empty:
                error = ScriptAbort("check 中没有执行有效断言", reason_code="check_empty", status="blocked")
                self._case._latched_error = self._case._latched_error or error
                raise error
        except Exception:
            if exc_type is None:
                raise
        finally:
            self._case._step, self._case._role, self._case._source_ref, self._case._step_id = self._previous
        return False


class _BlockImports:
    """只拦住脚本仓库自己写的 import。Scout 执行时的渠道导入照常放行。"""

    def __init__(self, root: Path):
        self._root = Path(root).resolve()

    def find_spec(self, fullname, path, target=None):
        name = str(fullname or "")
        top = name.split(".", 1)[0]
        if top not in _BANNED and name not in _BANNED:
            return None
        if not _issued_by_script(self._root):
            return None
        raise ScriptAbort("脚本不能连接 adb、WDA、Playwright 或 Appium", reason_code="script_forbidden")


def _issued_by_script(root: Path) -> bool:
    """import 语句所在文件在脚本仓库里，才算脚本自己在连设备。"""
    hook = Path(__file__).resolve()
    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename or ""
        if _outside_import_machinery(filename) and not _is_hook(filename, hook):
            try:
                return Path(filename).resolve().is_relative_to(root)
            except (OSError, ValueError):
                return False
        frame = frame.f_back
    return False


def _is_hook(filename: str, hook: Path) -> bool:
    try:
        return Path(filename).resolve() == hook
    except OSError:
        return False


def _outside_import_machinery(filename: str) -> bool:
    if filename.startswith("<frozen importlib"):
        return False
    if "importlib._bootstrap" in filename:
        return False
    normalized = filename.replace("\\", "/")
    return "/importlib/" not in f"/{normalized}" and not normalized.endswith("/importlib")


def run(core, payload, root: Path, *, deadline: float, stop) -> dict[str, Any]:
    """执行一份已经落盘的脚本包。异常留在返回值里。"""
    with _RUN_LOCK:
        from mino_scout.runtime.model_policy import declarations
        import ast
        purpose = "unclassified"
        try:
            purpose, _ = declarations(ast.parse((root / payload.script_path).read_text(encoding="utf-8")))
        except (ValueError, SyntaxError, OSError):
            pass
        recorder = getattr(core, "_recording", None)
        if recorder:
            recorder.store.update_metadata(recorder.task_id, recorder.attempt_id, {"run_purpose": purpose})
        started = time.time()
        result = _run_locked(core, payload, root, deadline=deadline, stop=stop)
        from datetime import datetime, timezone
        result.update(run_purpose=purpose, started_at=datetime.fromtimestamp(started, timezone.utc).isoformat(),
                      finished_at=datetime.now(timezone.utc).isoformat())
        return result


def _run_locked(core, payload, root: Path, *, deadline: float, stop) -> dict[str, Any]:
    case = None
    try:
        origin = getattr(payload, "debug_origin", {})
        if origin:
            from mino_scout.runtime.workspaces import source_snapshot
            _, hashes, digest = source_snapshot(root)
            matched = (payload.script_path == origin["script_path"] and digest == origin["workspace_hash"]
                       and hashes.get(payload.script_path) == origin["script_hash"])
            recorder = getattr(core, "_recording", None)
            if recorder:
                recorder.append("debug/origin", {**origin, "revision": payload.revision, "matched": matched, "actual_workspace_hash": digest})
                recorder.store.update_metadata(recorder.task_id, recorder.attempt_id,
                    {"debug_origin": origin, "debug_origin_matched": matched})
            if not matched:
                raise ScriptAbort("正式源码与已验证调试版本不一致", reason_code="debug_version_mismatch", status="blocked")
        if str(payload.platform) == "web":
            from mino_scout.platform.web.playwright_hub import get_hub
            get_hub().note_lane(str(payload.run_id or ""))
        case = Case(core, payload, root, deadline=deadline, stop=stop)
        if getattr(payload, "revision", ""):
            case._report("checkout", ActionResult(ok=True, summary=f"脚本仓库提交 {payload.revision}", status="pass"), "scout", 0)
        _execute_hooks(root, payload.script_path, case)
        case._guard()
        if case._latched_error is not None:
            raise case._latched_error
        if not case._assertions:
            raise ScriptAbort("脚本没有有效断言；观察记录已保留", reason_code="assertion_missing", status="blocked")
        missing = sorted(set(case._source_clauses) - case._covered)
        if missing:
            raise ScriptAbort("原文条目未覆盖：" + ", ".join(missing), reason_code="source_coverage_missing", status="blocked")
        return _report_outcome(case, {"status": "pass", "summary": "脚本执行完成且断言通过", "error": "", "reason_code": "", "touched": bool(case.touched),
                "execution_started": "yes" if case.touched else "no", "source_coverage": case._coverage(), **case.data.snapshot()})
    except ScriptHalt as exc:
        return _failure(exc, case)
    except ScriptAbort as exc:
        return _failure(exc, case)
    except Exception as exc:
        return _failure(ScriptAbort(f"{type(exc).__name__}: {exc}", reason_code="script_failed"), case)


def _failure(exc: BaseException, case: Case | None) -> dict[str, Any]:
    details = getattr(exc, "details", {}) if isinstance(getattr(exc, "details", {}), dict) else {}
    explicit_started = str(details.get("execution_started") or "")
    execution_started = explicit_started if explicit_started in {"yes", "no", "unknown"} else (
        "yes" if case and case.touched else "no"
    )
    return _report_outcome(case, {
        "status": str(getattr(exc, "status", "") or ("cancelled" if getattr(exc, "reason_code", "") == "cancelled" else "fail")),
        "summary": str(exc)[:300],
        "error": str(exc)[:400],
        "reason_code": str(getattr(exc, "reason_code", "") or "script_failed"),
        "touched": execution_started != "no",
        "execution_started": execution_started,
        "failure_stage": str(details.get("phase") or ((case._failure_role or case._role) if case else "admission")),
        "failure_source_ref": case._failure_ref if case else "",
        "failure_title": case._failure_title if case else "",
        "resource_isolated": bool(details.get("resource_isolated")),
        "source_coverage": case._coverage() if case else {},
        **(case.data.snapshot() if case and hasattr(case, "data") else {}),
    })


def _report_outcome(case, outcome):
    if case is None:
        return outcome
    from mino_scout.runtime.business_report import build_report
    assertions = case._assertions
    counts = {"total": len(assertions), "pass": sum(r["status"] == "pass" for r in assertions),
              "fail": sum(r["status"] == "fail" for r in assertions), "unknown": sum(r["status"] not in {"pass", "fail"} for r in assertions)}
    # The durable path builds the final report from full events at RecordStore.finish.
    # Legacy synchronous nodes still return a bounded business projection.
    meta = {"source_snapshot": case._source_snapshot, "source_hash": case._source_hash,
            "run_purpose": case._model_gate.run_purpose, "source_context": case._source_context}
    outcome.update(assertions=counts, test_report=build_report(meta, outcome, [(0, "assertion/result", r) for r in assertions]))
    usage = outcome["test_report"]["model_usage"]
    gate = case._model_gate
    usage.update(calls=gate.calls, llm_calls=sum(count for key, count in gate.by_rule.items() if key[0] == "llm"),
                 vlm_calls=sum(count for key, count in gate.by_rule.items() if key[0] == "vlm"), elapsed_ms=int(gate.elapsed_ms),
                 total_tokens=0 if not gate.calls else None, usage_complete=not bool(gate.calls))
    return outcome


def _execute_hooks(root: Path, script_path: str, case: "Case") -> None:
    inserted = str(root) not in sys.path
    if inserted:
        sys.path.insert(0, str(root))
    blocker = _BlockImports(root)
    sys.meta_path.insert(0, blocker)
    loaded: list[str] = []
    try:
        project_pre = _load_hook(root / "pretest.py", "pretest", "mino_project_pretest", loaded)
        project_post = _load_hook(root / "posttest.py", "posttest", "mino_project_posttest", loaded)
        module = _load(root / script_path, "mino_case_run", loaded)
        run_fn = getattr(module, "run", None)
        if not callable(run_fn):
            raise ScriptAbort("用例没有 run(case)", reason_code="script_failed")
        case_pre = getattr(module, "pretest", None)
        case_post = getattr(module, "posttest", None)
        run_error: BaseException | None = None
        try:
            if callable(project_pre):
                project_pre(case)
            if callable(case_pre):
                case_pre(case)
            run_fn(case)
        except BaseException as exc:
            run_error = exc
            raise
        finally:
            post_error: BaseException | None = None
            try:
                if callable(case_post):
                    case_post(case)
                if callable(project_post):
                    project_post(case)
            except BaseException as exc:
                post_error = exc
            if post_error is not None and run_error is None:
                raise post_error
    finally:
        if blocker in sys.meta_path:
            sys.meta_path.remove(blocker)
        if inserted:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
        for name in loaded:
            sys.modules.pop(name, None)
        for name in [key for key in sys.modules if key in {"scripts", "flows"} or key.startswith(("scripts.", "flows."))]:
            sys.modules.pop(name, None)


class AppRef:
    """case.config.<KEY> 的值。只表示 config.yaml 里的一个应用，不能自己构造。"""

    __slots__ = ("key", "app_id", "platform", "package", "url", "otp_code")

    def __init__(self, key: str, platform: str, package: str, url: str, app_id: str = "",
                 otp_code: str = ""):
        self.key = key
        self.app_id = app_id
        self.platform = platform
        self.package = package
        self.url = url
        self.otp_code = otp_code


def _otp_code_of(row: dict) -> str:
    """兼容旧脚本仓的 otp.code；新项目验证码策略由 Nexus 项目环境下发。"""
    otp = row.get("otp")
    if not isinstance(otp, dict):
        return ""
    return str(otp.get("code") or "").strip()


def _load_apps(root: Path) -> tuple[dict | None, str]:
    """读脚本仓库根的 config.yaml。返回（apps, 失败原因）。"""
    path = root / "config.yaml"
    if not path.is_file():
        return None, "仓库根目录没有 config.yaml"
    try:
        doc = parse_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"config.yaml 解析失败：{exc}"
    apps = doc.get("apps") if isinstance(doc, dict) else None
    if not isinstance(apps, dict):
        return None, "config.yaml 顶层要有 apps"
    return apps, ""


class _Config:
    """config.yaml 的 apps。行里有 envs 时按本次运行的环境取一份。"""

    def __init__(self, apps: Any, env: str, problem: str = ""):
        self._apps: dict[str, AppRef] = {}
        self._missing_env: dict[str, str] = {}
        self._problem = problem
        if not isinstance(apps, dict):
            return
        for key, row in apps.items():
            if not isinstance(row, dict):
                continue
            envs = row.get("envs")
            if isinstance(envs, dict):
                picked = envs.get(env)
                if not isinstance(picked, dict):
                    self._missing_env[str(key)] = env
                    continue
                row = {**{k: v for k, v in row.items() if k != "envs"}, **picked}
            package = str(row.get("package") or row.get("bundle_id") or row.get("bundle") or "")
            self._apps[str(key)] = AppRef(
                str(key),
                str(row.get("platform") or ""),
                package,
                str(row.get("url") or ""),
                str(row.get("app_id") or ""),
                _otp_code_of(row),
            )

    def __getattr__(self, name: str) -> AppRef:
        if name.startswith("_"):
            raise AttributeError(name)
        if self._problem:
            raise ScriptAbort(self._problem, reason_code="config_missing")
        ref = self._apps.get(name)
        if ref is None:
            if name in self._missing_env:
                raise ScriptAbort(f"config.yaml 里 {name} 没有 env={self._missing_env[name] or '(未指定)'} 的配置", reason_code="config_missing")
            raise ScriptAbort(f"config.yaml 的 apps 里没有 {name}", reason_code="config_missing")
        return ref


class _Scout:
    """本机能力。不绑定设备，不需要实例化。"""

    def __init__(self, case: "Case"):
        self._case = case

    def wait(self, ms: int) -> ActionResult:
        from mino_scout.platform.scout.run import wait

        return wait(self._case, ms)

    def otp(self, account) -> str:
        """取验证码：账号自带码 → Nexus 登录策略 → 旧脚本仓固定码 → 人工。"""
        from mino_scout.server_comm.callbacks import otp

        return otp(self._case, account)

    def otp_mail(self) -> str:
        from mino_scout.server_comm.callbacks import otp_mail

        return otp_mail(self._case)

    def llm(self, prompt: str, text: str = "", schema: dict | None = None) -> Any:
        from mino_scout.server_comm.callbacks import llm

        return llm(self._case, prompt, text=text, schema=schema)


class _Nexus:
    """写入 Nexus 的能力。不绑定设备，不需要实例化。"""

    def __init__(self, case: "Case"):
        self._case = case

    def prep(self, number: int | str, source_ref: str = "") -> _Scope:
        from mino_scout.platform.scout.run import prep

        return prep(self._case, number, source_ref=source_ref)

    def do(self, number: int | str, source_ref: str = "") -> _Scope:
        from mino_scout.platform.scout.run import do

        return do(self._case, number, source_ref=source_ref)

    def check(self, number: int | str, source_ref: str = "") -> _Scope:
        from mino_scout.platform.scout.run import check

        return check(self._case, number, source_ref=source_ref)

    def account(self, requirements: dict | None = None, prefer: list | None = None, data_need: str | None = None, data_requirements: list | None = None) -> SimpleNamespace:
        from mino_scout.server_comm.callbacks import account

        return account(self._case, requirements=requirements, prefer=prefer, data_need=data_need, data_requirements=data_requirements)

    def assert_equal(self, actual: Any, expected: Any, message: str = "") -> None:
        self._case._guard()
        passed = actual == expected
        self._case._assert("equal", "pass" if passed else "fail", expected=expected, actual=actual, message=message)
        if not passed:
            raise self._case._latched_error

    def note(self, text: str, **fields: Any) -> None:
        from mino_scout.server_comm.callbacks import note

        return note(self._case, text, **fields)

    def fail(self, message: str) -> None:
        from mino_scout.platform.scout.run import fail

        return fail(self._case, message)


class Case:
    """一次运行的共享上下文。设备动作在 case.init(...) 返回的 App 上。"""

    def __init__(self, core, payload, root: Path, *, deadline: float, stop, debug_source: str | None = None):
        self.core = core
        self.workspace = root
        self.output_dir = Path.cwd()
        self._debug = debug_source is not None
        self._debug_write_error = None
        self.run_id = str(payload.run_id)
        self.session_id = str(payload.session_id)
        self._source_context = dict(getattr(payload, "source_context", {}) or {})
        self._sn = str(payload.sn)
        self._platform = str(payload.platform)
        self._hint = dict(getattr(payload, "device_hint", None) or {})
        self._all_pages = _load_pages(root)
        self.env = str(self._hint.get("env") or "")
        self.app_id = str(self._hint.get("app_id") or "")
        self.app_key = str(self._hint.get("app_key") or "")
        apps, problem = _load_apps(root)
        self.config = _Config(apps, self.env, problem)
        self.scout = _Scout(self)
        self.nexus = _Nexus(self)
        self.deadline = float(deadline or 0)
        self.stop = stop
        self.touched = False
        self._seq = 0
        self._step: int | None = None
        self._role = ""
        self._started = time.time()
        self._mail_to = ""
        self._apps: dict[str, App] = {}
        self._recorder = getattr(core, "_recording", None)
        self._source_ref = ""
        self._step_id = ""
        self._assertions: list[dict] = []
        self._latched_error: ScriptAbort | None = None
        self._failure_role = ""
        self._failure_ref = ""
        self._failure_title = ""
        self._covered: set[str] = set()
        from mino_scout.runtime.case_data import CaseData
        self.data = CaseData(self, getattr(payload, "inputs", {}))
        sidecar = (root / payload.script_path).with_suffix(".yaml")
        from mino_scout.runtime.script_source import load_source
        self._guard()
        try:
            external = None if self._debug else load_source(root / payload.script_path, core.node_id)
        except ValueError as exc:
            raise ScriptAbort(str(exc), reason_code="source_read_failed", status="blocked") from None
        except Exception:
            raise ScriptAbort("用例来源读取失败，请检查来源配置、飞书授权及访问权限", reason_code="source_read_failed", status="blocked") from None
        self._guard()
        self._source_snapshot = {} if self._debug else external if external is not None else parse_yaml(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
        if not isinstance(self._source_snapshot, dict):
            raise ScriptAbort("用例原文快照不是对象", reason_code="source_invalid", status="blocked")
        if self._source_snapshot:
            source = self._source_snapshot.get("source")
            if str(self._source_snapshot.get("case_id") or "") != (root / payload.script_path).stem:
                raise ScriptAbort("原文快照 case_id 与脚本不一致", reason_code="source_case_mismatch", status="blocked")
            if not isinstance(source, dict) or not str(source.get("revision") or "").strip():
                raise ScriptAbort("原文快照缺固定版本", reason_code="source_revision_missing", status="blocked")
            seen_ids = set()
            for group in ("preconditions", "steps", "expectations"):
                rows = self._source_snapshot.get(group, [])
                if not isinstance(rows, list) or group == "expectations" and not rows:
                    raise ScriptAbort(f"原文快照 {group} 无效", reason_code="source_invalid", status="blocked")
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip() or not str(row.get("text") or "").strip():
                        raise ScriptAbort("原文快照条目缺 id/text", reason_code="source_invalid", status="blocked")
                    if row["id"] in seen_ids:
                        raise ScriptAbort("原文快照条目编号重复", reason_code="source_duplicate", status="blocked")
                    seen_ids.add(row["id"])
        self._source_clauses = {str(row["id"]): row for key in ("preconditions", "steps", "expectations")
            for row in (self._source_snapshot.get(key) or []) if isinstance(row, dict) and row.get("id")}
        self._source_roles = {str(row["id"]): role for key, role in (("preconditions", "prep"), ("steps", "do"), ("expectations", "check"))
            for row in (self._source_snapshot.get(key) or []) if isinstance(row, dict) and row.get("id")}
        immutable = {key: self._source_snapshot.get(key, {} if key == "source" else [])
            for key in ("source", "preconditions", "steps", "expectations")}
        if external is not None:
            immutable["source"] = {key: value for key, value in immutable["source"].items() if key != "fetched_at"}
        self._source_hash = hashlib.sha256(json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() if self._source_snapshot else ""
        from mino_scout.runtime.model_policy import ModelGate
        try:
            self._model_gate = ModelGate(self, root / payload.script_path, source=debug_source)
        except ValueError as exc:
            raise ScriptAbort(str(exc), reason_code="model_policy_invalid", status="blocked") from None
        if self._recorder:
            from mino_scout.runtime.records import scrub
            config_doc = parse_yaml((root / "config.yaml").read_text(encoding="utf-8")) if (root / "config.yaml").is_file() else {}
            # Register OTP/password values before any free text is persisted.
            config_snapshot = scrub(config_doc)
            from mino_scout.runtime.workspaces import source_snapshot
            _, files, _ = source_snapshot(root)
            self._recorder.store.update_metadata(self._recorder.task_id, self._recorder.attempt_id,
                {"source_snapshot": self._source_snapshot, "source_hash": self._source_hash, "config_snapshot": config_snapshot,
                 "file_hashes": files, "revision": str(getattr(payload, "revision", "")), "script_path": payload.script_path,
                 "run_purpose": self._model_gate.run_purpose, "model_policy": self._model_gate.policy})

    def _coverage(self):
        return {"expected": len(self._source_clauses), "covered": sorted(self._covered),
                "missing": sorted(set(self._source_clauses) - self._covered)}

    def _assert(self, checker: str, status: str, **evidence):
        row = {"assertion_id": "assert_" + uuid.uuid4().hex, "checker": checker, "status": status,
               "source_ref": self._source_ref, "source_hash": self._source_hash, "step_id": self._step_id,
               "title": str(self._step or ""), "role": self._role,
               "assertion_kind": "page_identity" if checker == "on_page" else "business", **evidence}
        self._assertions.append(row)
        if self._recorder:
            self._recorder.append("assertion/result", row)
        if status != "pass":
            self._latched_error = self._latched_error or ScriptAbort(str(evidence.get("message") or "断言未通过"),
                reason_code=str(evidence.get("reason_code") or "assertion_failed"), status="blocked" if status == "unknown" else "fail")
        return row

    def repo_otp_code(self) -> str:
        """本次运行应用在 config.yaml 里的固定验证码；没有则空串。"""
        key = self.app_key
        if key and key in self.config._apps:
            return str(self.config._apps[key].otp_code or "")
        if self.app_id:
            for ref in self.config._apps.values():
                if ref.app_id and ref.app_id == self.app_id:
                    return str(ref.otp_code or "")
        return ""

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _unknown("case", name, ["config", "env", "app_id", "app_key", "init", "scout", "nexus", "data", "workspace", "output_dir"],
                       "。设备动作写在 app = case.init(case.config.<KEY>) 返回的 app 上")

    def init(self, ref: AppRef, *, web_resolution: str = "") -> "App":
        self._guard()
        if not isinstance(ref, AppRef):
            raise ScriptAbort("case.init 只接受 case.config.<KEY>", reason_code="config_invalid")
        if ref.platform != self._platform:
            raise ScriptAbort(f"应用 {ref.key} 是 {ref.platform}，本次运行是 {self._platform}", reason_code="platform_mismatch")
        resolution = ""
        if ref.platform == "web":
            try:
                resolution = normalize_web_resolution(web_resolution)
            except ValueError as exc:
                raise ScriptAbort(str(exc), reason_code="config_invalid") from None
        elif str(web_resolution or "").strip():
            raise ScriptAbort("web_resolution 只适用于 Web 应用", reason_code="config_invalid")
        app = self._apps.get(ref.key)
        if app is None:
            app = self._apps[ref.key] = App(self, ref, web_resolution=resolution)
        elif app._web_resolution != resolution:
            raise ScriptAbort(
                f"应用 {ref.key} 已按 {app.web_resolution_label or DEFAULT_WEB_RESOLUTION} 初始化，不能在同一用例内切换分辨率",
                reason_code="config_invalid",
            )
        return app

    def _report(self, capability: str, result: ActionResult, executor: str, elapsed: int, image=None,
                app: str = "", extra: dict[str, Any] | None = None) -> None:
        body = {
            "capability_id": capability,
            "status": result.status,
            "summary": result.summary[:300],
            "error": "" if result.ok else result.summary[:300],
            "executor_used": executor,
            "elapsed_ms": int(elapsed),
            "action_seq": self._seq, "source_ref": self._source_ref, "source_hash": self._source_hash,
            "step_id": self._step_id, "data": result.data,
        }
        execution = result.data.get("execution") if isinstance(result.data.get("execution"), dict) else {}
        body["reason_code"] = str(execution.get("reason_code") or result.data.get("reason_code") or "")
        if app:
            body["app"] = app
        if extra:
            body.update(extra)
        if self._step is not None:
            body["case_step"] = self._step
            body["role"] = self._role
        if image is not None:
            if self._recorder:
                body.update(self._recorder.image(str(getattr(image, "image_base64", "") or ""), str(getattr(image, "image_mime", "") or "image/png")))
            else:
                body["image_base64"] = str(getattr(image, "image_base64", "") or "")
                body["image_mime"] = str(getattr(image, "image_mime", "") or "")
            body["width"] = int(getattr(image, "width", 0) or 0)
            body["height"] = int(getattr(image, "height", 0) or 0)
        if self._recorder:
            self._recorder.append("tool/result", body)
        else:
            self._callback("script.session", {"op": "tool_result", "event_type": "tool/result", "event": body}, 20)
        return body

    def _callback(self, capability: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._recorder and capability == "script.callback" and params.get("op") in {"step", "note", "fail"}:
            op = params["op"]
            event_type = ("node/enter" if params.get("action") == "enter" else "node/leave") if op == "step" else "script/note"
            self._recorder.append(event_type, {**params, "source_ref": self._source_ref, "source_hash": self._source_hash, "step_id": self._step_id})
            return {}
        from mino_scout.server_comm.nexus_link import call

        return call(self, capability, params, timeout)

    def _guard(self) -> None:
        if self._recorder and self._recorder.cancelled():
            raise ScriptHalt("运行已取消", reason_code="cancelled")
        if self.stop is not None and self.stop.is_set():
            raise ScriptHalt("脚本运行超时", reason_code="script_timeout")
        if self.deadline and time.monotonic() >= self.deadline:
            raise ScriptHalt("脚本运行超时", reason_code="script_timeout")
        requests = getattr(self.core, "_requests", None)
        if requests is not None and self.run_id and requests.stopped(self.run_id):
            raise ScriptHalt("运行已取消", reason_code="cancelled")


def _shared(name: str) -> property:
    return property(lambda self: getattr(self._case, name), lambda self, value: setattr(self._case, name, value))


class App:
    """被测应用。所有会碰设备的动作都在这里。由 case.init(case.config.<KEY>) 得到。"""

    core = _shared("core")
    run_id = _shared("run_id")
    session_id = _shared("session_id")
    deadline = _shared("deadline")
    stop = _shared("stop")
    touched = _shared("touched")
    _sn = _shared("_sn")
    _platform = _shared("_platform")
    _hint = _shared("_hint")
    _seq = _shared("_seq")
    _started = _shared("_started")
    _mail_to = _shared("_mail_to")

    def __init__(self, case: Case, ref: AppRef, *, web_resolution: str = ""):
        self._case = case
        self.key = ref.key
        self._package = ref.package
        self._url = ref.url
        self._web_resolution = str(web_resolution or "")
        self._pages = case._all_pages.get(ref.key, {})
        self._page_key = ""
        self._shot = None
        self._frames: list[Any] = []
        self._frame_refs: list[dict] = []
        self._observation_id = ""
        self._artifact_id = ""
        self._bound_controls: dict[str, dict] = {}

    @property
    def case(self) -> "Case":
        """这个 app 所属的运行上下文；app.expect 用它写证据。"""
        return self._case

    @property
    def web_resolution(self) -> str:
        """本用例固定的 Web canonical 分辨率；非 Web 应用为空串。"""
        return self._web_resolution

    @property
    def web_resolution_label(self) -> str:
        """运行时证据使用的展示名，例如 1080P、2K、3K/4K。"""
        if not self._web_resolution:
            return ""
        from mino_scout.platform.web.viewports import WEB_VIEWPORTS

        return str(WEB_VIEWPORTS[self._web_resolution]["label"])

    # ---- 启停
    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _unknown("app", name, _public_names(App))

    def launch(self) -> ActionResult:
        params = {"package": self._package, "bundle": self._package, "bundle_id": self._package, "url": self._url}
        if self._web_resolution:
            params["web_resolution"] = self._web_resolution
        return self._device("launch_app", params)

    def close(self) -> ActionResult:
        return self._device("close_app", {"package": self._package, "bundle": self._package, "bundle_id": self._package})

    def kill(self) -> ActionResult:
        return self._device("kill_app", {"package": self._package, "bundle": self._package, "bundle_id": self._package})

    def install(self, path: str) -> ActionResult:
        return self._device("install", {"path": str(path or "")})

    def uninstall(self) -> ActionResult:
        return self._device("uninstall_app", {"package": self._package, "bundle": self._package, "bundle_id": self._package})

    def clear_cache(self) -> ActionResult:
        return self._device("clear_app_cache", {"package": self._package, "bundle": self._package, "bundle_id": self._package})

    def version(self) -> ActionResult:
        return self._device("get_app_version", {"package": self._package, "bundle": self._package, "bundle_id": self._package})

    def foreground(self) -> ActionResult:
        return self._device("get_foreground_app", {})

    # ---- 操作
    def bind(self, control: str, **values: Any) -> str:
        """Resolve selector placeholders from explicit business values."""
        self._guard()
        from mino_scout.runtime.locator_binding import bind_cell
        try:
            cell = bind_cell(self._control_cell(control), values)
        except ValueError as exc:
            raise ScriptAbort(str(exc), reason_code="data_binding_invalid", status="blocked") from None
        cell["require_unique"] = True
        key = "@bound:" + uuid.uuid4().hex
        self._bound_controls[key] = cell
        return key

    def tap(self, control: str, index: Optional[int] = None) -> ActionResult:
        params = self._locate(str(control or ""), "tap", index=index)
        if self._platform == "web":
            return self._web("tap", params)
        return self._device("tap_element", params)

    def tap_xy(self, x: int, y: int) -> ActionResult:
        return self._device("tap_xy", {"x": int(x), "y": int(y)})

    def long_press(self, control: str, duration_ms: int = 1000, index: Optional[int] = None) -> ActionResult:
        params = self._locate(str(control or ""), "long_press", index=index)
        params["duration_ms"] = int(duration_ms)
        return self._device("long_press_element", params)

    def input(self, control: str, text: str, index: Optional[int] = None) -> ActionResult:
        params = self._locate(str(control or ""), "input", index=index)
        if self._platform in {"mac", "win"} and params.get("ocr_observation_id"):
            focus_ref = f"focus:{self.run_id}:{self._seq + 1}"
            self._device("tap_element", {**params, "focus_ref": focus_ref})
            self.screenshot(full_page=False, screenshot_params={"compress_ratio": 1.0})
            params = {"target_mode": "current_focus", "focus_ref": focus_ref, "mode": "replace"}
        params["text"] = str(text or "")
        if self._platform == "web":
            return self._web("input", params, text=str(text or ""))
        return self._device("input_text", params)

    def swipe(self, direction: str) -> ActionResult:
        name = str(direction or "").strip().lower()
        if name not in {"up", "down", "left", "right"}:
            raise ScriptAbort("滑动方向必须是 up、down、left 或 right", reason_code="script_failed")
        return self._device("swipe_direction", {"direction": name})

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> ActionResult:
        return self._device("swipe_element_to_element", {
            "from_x": int(x1), "from_y": int(y1), "to_x": int(x2), "to_y": int(y2),
            "duration_ms": int(duration_ms), "point_policy": "coordinate"})

    def press(self, key: str) -> ActionResult:
        return self._device("press_key", {"key": str(key or "")})

    def back(self) -> ActionResult:
        return self.press("back")

    def set_clipboard(self, text: str) -> ActionResult:
        return self._device("set_clipboard", {"text": str(text or "")})

    def set_ime(self, ime: str = "adbkeyboard") -> ActionResult:
        return self._device("set_input_method", {"ime": str(ime or "adbkeyboard")})

    def read_data(self, key: str = "sim") -> ActionResult:
        return self._device("read_device_data", {"key": str(key or "sim")})

    def exec(self, script: str, timeout_ms: int = 30000, language: str = "") -> ActionResult:
        """在该渠道上执行一段命令。设备层兜底的唯一出口。"""
        import hashlib

        body = str(script or "")
        params: dict[str, Any] = {"script": body, "timeout_ms": int(timeout_ms)}
        if language:
            params["language"] = str(language)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return self._device("exec_script", params, extra={"script_sha256": digest, "script_length": len(body)})

    # ---- Web 专有
    def reload(self) -> ActionResult:
        return self._device("reload_page", {})

    def open_tab(self, url: str = "") -> ActionResult:
        return self._device("open_tab", {"url": str(url or self._url)})

    def switch_tab(self, index: int) -> ActionResult:
        return self._device("switch_tab", {"tab_index": int(index)})

    def upload(self, path: str, control: str = "") -> ActionResult:
        params = self._locate(str(control), "upload") if control else {}
        params["file_path"] = str(path or "")
        return self._device("upload_file", params)

    def wait_ready(self, timeout_ms: int = 20000, control: str = "") -> ActionResult:
        params = self._locate(str(control), "wait_ready") if control else {}
        params["timeout_ms"] = int(timeout_ms)
        return self._device("wait_screen_ready", params)

    # ---- 观察
    def screenshot(self, *, full_page: bool = True,
                   screenshot_params: Optional[dict[str, Any]] = None) -> ActionResult:
        self._guard()
        self._require_channel()
        from mino_scout import screen as SCREEN
        params = dict(screenshot_params or {})
        if self._platform == "web":
            # Web 默认截取完整文档；调用方仍可用 full_page=False 只截当前视口。
            if "full_page" not in params and "fullPage" in params:
                params["full_page"] = bool(params.pop("fullPage"))
            params.setdefault("full_page", bool(full_page))
        ratio = float(params.pop("compress_ratio", 2.0))
        self._begin_action("screenshot", params)
        self.touched = True
        shot = SCREEN.capture(
            self._device_ref(),
            timeout_sec=15,
            compress_ratio=ratio,
            screenshot_params=params,
        )
        self._shot = shot
        self._observation_id = "obs_" + uuid.uuid4().hex
        ok = bool(shot.has_image())
        result = ActionResult(ok=ok, summary="已截图" if ok else (shot.error or "没有截到图"), status="pass" if ok else "fail",
                               data={"width": int(shot.width or 0), "height": int(shot.height or 0)})
        body = self._report("screenshot", result, str(shot.source or _EXECUTOR.get(self._platform) or "screen"), int(shot.elapsed_ms or 0), image=shot if ok else None,
                            extra={"observation_id": self._observation_id, "full_page": bool(params.get("full_page"))})
        self._artifact_id = str(body.get("artifact_id") or "")
        if not ok:
            raise ScriptAbort(result.summary or "没有截到图", reason_code="script_failed")
        return result

    def sample(self, duration_ms: int, interval_ms: int = 300) -> dict[str, Any]:
        from mino_scout.platform.scout.run import sample

        return sample(self, duration_ms, interval_ms)

    def hierarchy(self) -> ActionResult:
        self._guard()
        self._require_channel()
        self._begin_action("hierarchy", {})
        self.touched = True
        status, fields = self.core._hierarchy_fields(self._device_ref(), run_id=self.run_id)
        from mino_scout.runtime.native_nodes import native_nodes
        normalized = native_nodes(fields, self._platform)
        if normalized is not None:
            fields = {**fields, "nodes": normalized}
        text = _visible_text({"nodes": normalized}) if normalized is not None else _visible_text(fields)
        ok = str(getattr(status, "value", status)) == "pass"
        nodes = _locator_nodes(fields)
        raw_nodes = normalized or []
        contract = fields.get("observation") or (fields.get("extra") or {}).get("observation") or {}
        data: dict[str, Any] = {"text": text[:4000], "truncated": bool(contract.get("truncated")) or
                               len(raw_nodes) > 160 or normalized is None and len(text) > 4000 or any(
                                   len(str(node.get(k) or "")) > 160 for node in raw_nodes if isinstance(node, dict)
                                   for k in ("text", "content_desc", "value")),
                               "structured": normalized is not None}
        if nodes:
            data["nodes"] = nodes
        result = ActionResult(ok=ok, summary="已读取界面结构" if ok else str(fields.get("error") or "没有读到界面结构"),
                               status="pass" if ok else "fail", data=data)
        extra = _hierarchy_evidence(fields, nodes)
        self._observation_id = "obs_" + uuid.uuid4().hex
        extra["observation_id"] = self._observation_id
        self._report("hierarchy", result, str(fields.get("source") or "core"), int(fields.get("elapsed_ms") or 0), extra=extra or None)
        if not ok:
            raise ScriptAbort(result.summary or "没有读到界面结构", reason_code="script_failed")
        return result

    def observe(self, control: str = "", *, field: str = "visible", text: str = "") -> ActionResult:
        """Read an exact UI signal for a flow; unknown facts remain unknown, without assertions."""
        from mino_scout.runtime.deterministic import observe
        return observe(self, control, field, text)

    def on_page(self, page_key: str) -> ActionResult:
        from mino_scout.platform.scout.run import on_page

        return on_page(self, page_key)

    def expect(self, page: str = "", prompt: str = "", verdict: str = "pass", *, allow_model: bool = False) -> str:
        """分别验证页面身份和业务预期；文字证据不确定时取新截图判断。

        页面成立不能代替业务预期成立；明确失败直接终止本次断言。
        """
        from mino_scout.platform.scout.run import expect as _expect

        return _expect(self, page=page, prompt=prompt, verdict=verdict, allow_model=allow_model)

    def expect_text(self, text: str, *, control: str = "", present: bool = True, timeout_ms: int = 0,
                    ocr: bool = False, region=None):
        from mino_scout.runtime.deterministic import verify
        return verify(self, "text", text, control=control, wanted=present, timeout_ms=timeout_ms, ocr=ocr, region=region)

    def expect_state(self, control: str, state: str, expected: bool = True, *, timeout_ms: int = 0):
        from mino_scout.runtime.deterministic import verify
        return verify(self, "state", state, control=control, wanted=expected, timeout_ms=timeout_ms)

    def expect_count(self, control: str, expected: int, *, timeout_ms: int = 0):
        from mino_scout.runtime.deterministic import verify
        return verify(self, "count", "", control=control, wanted=expected, timeout_ms=timeout_ms)

    def expect_value(self, control: str, expected, *, timeout_ms: int = 0):
        from mino_scout.runtime.deterministic import verify
        return verify(self, "value", "", control=control, wanted=expected, timeout_ms=timeout_ms)

    def wait_for(self, *, control: str = "", text: str = "", state: str = "", expected: bool = True,
                 timeout_ms: int = 5000, ocr: bool = False, region=None):
        from mino_scout.runtime.deterministic import verify
        return verify(self, "state" if state else "text", state or text, control=control, wanted=expected,
                      timeout_ms=timeout_ms, ocr=ocr, region=region)

    def ocr(self, *, region=None, language: str = "", timeout_ms: int = 10000):
        from mino_scout.platform.ocr import observe
        return observe(self, region=region, language=language, timeout_ms=timeout_ms)

    def vlm(self, prompt: str, verdict: str = "") -> str:
        from mino_scout.server_comm.callbacks import vlm

        return vlm(self, prompt, verdict=verdict)

    def vlm_frame(self, index: int, prompt: str, verdict: str = "") -> str:
        from mino_scout.server_comm.callbacks import vlm_frame

        return vlm_frame(self, index, prompt, verdict=verdict)

    # ---- 内部
    def _locate(self, control: str, kind: str, index: Optional[int] = None) -> dict[str, Any]:
        try:
            params = self._locator_params(control, index=index)
            if params.get("require_unique") and self._platform != "web" and not params.get("ocr_observation_id"):
                from mino_scout.runtime.deterministic import control_nodes
                observation = self.hierarchy().data
                if not observation.get("structured") or observation.get("truncated", True):
                    raise ScriptAbort("当前结构不足以确定动态对象", reason_code="observation_insufficient", status="blocked")
                nodes = control_nodes(self, control, observation.get("nodes", []))
                if len(nodes) != 1:
                    raise ScriptAbort("动态对象有多个候选" if nodes else "动态对象未找到", reason_code="locator_ambiguous" if nodes else "locator_missing", status="blocked")
            return params
        except ScriptAbort as exc:
            exc.details.update(phase="locator", action_execution_started="no")
            if self._case._recorder:
                self._case._recorder.append("locator/result", {"status": "fail", "phase": "locator", "execution_started": "no",
                    "control": control, "capability_id": kind, "reason_code": exc.reason_code, "summary": str(exc),
                    "app": self.key, "action_seq": self._seq + 1, "details": exc.details})
            raise

    def _device(self, capability: str, params: dict[str, Any], extra: dict[str, Any] | None = None) -> ActionResult:
        self._guard()
        executor = self._require_channel()
        if not executor.supports(capability):
            raise ScriptAbort(f"{self._platform} 渠道不支持 {capability}", reason_code="declined")
        from mino_scout.runtime.execution_boundary import ui_write
        self._begin_action(capability, params)
        if ui_write(capability):
            self.touched = True
        event = PlanEvent(seq=self._seq, capability_id=capability, event_kind=capability, params=dict(params),
                           executor_order=[_EXECUTOR[self._platform]], selected_impl={"executor": _EXECUTOR[self._platform]})
        started = time.perf_counter()
        router = getattr(self.core, "router", None)
        if router is not None:
            result = router.dispatch(event, self._device_ref(), run_id=self.run_id, step_idx=self._seq,
                                     shared={"write_stopped": lambda: bool(self._case._latched_error)})
        else:
            result = executor.execute(event, ExecutorContext(device=self._device_ref(), run_id=self.run_id, step_idx=self._seq))
        elapsed = int((time.perf_counter() - started) * 1000)
        status = str(getattr(result.status, "value", result.status) or "fail")
        action = ActionResult(ok=status == "pass", summary=str(result.summary or ""), status=status, data=_public(result.raw_response))
        self._report(capability, action, str(result.executor_used or _EXECUTOR[self._platform]), elapsed, extra=extra)
        if not action.ok:
            raw = action.data
            execution = raw.get("execution") if isinstance(raw.get("execution"), dict) else {}
            control = raw.get("execution_control") if isinstance(raw.get("execution_control"), dict) else {}
            reason = str(execution.get("reason_code") or raw.get("reason_code") or raw.get("error_kind") or "action_failed")
            error = ScriptAbort(action.summary or "设备动作失败", reason_code=reason,
                                status=status if status in {"fail", "blocked", "declined", "skipped"} else "fail",
                                details={**control, "phase": "device", "execution_started": execution.get("execution_started", "unknown")})
            self._case._latched_error = self._case._latched_error or error
            raise error
        return action

    def _begin_action(self, capability: str, params: dict):
        from mino_scout.runtime.execution_boundary import ui_write
        self._seq += 1
        writing = ui_write(capability)
        if writing:
            connection = getattr(self.core, "_record_connection", None)
            if connection is not None and not connection.is_set():
                raise ScriptAbort("节点已断线，暂停新增设备写操作", reason_code="node_disconnected", status="blocked")
            if self._case._debug_write_error:
                raise self._case._debug_write_error
            if self._case._latched_error:
                raise self._case._latched_error
            self._shot = None
            self._observation_id = ""
            self._artifact_id = ""
        safe_params = {key: value for key, value in params.items() if key not in {"text", "script"}}
        if "text" in params:
            safe_params["text_length"] = len(str(params["text"]))
        if "script" in params:
            safe_params["script_sha256"] = hashlib.sha256(str(params["script"]).encode()).hexdigest()
        if self._case._recorder:
            self._case._recorder.append("tool/call", {"capability_id": capability, "params": safe_params,
                "action_seq": self._seq, "effect_class": "write" if writing else "read", "app": self.key,
                "source_ref": self._case._source_ref, "source_hash": self._case._source_hash,
                "step_id": self._case._step_id, "role": self._case._role})

    def _web(self, kind: str, params: dict[str, Any], text: str = "") -> ActionResult:
        from mino_scout.platform.web.script import act

        return act(self, kind, params, text)

    def _locator_params(self, control: str, index: Optional[int] = None) -> dict[str, Any]:
        self._guard()
        cell = self._control_cell(control)
        selector = cell.get("selector") if isinstance(cell.get("selector"), dict) else {}
        return self._cell_params(cell, selector, index)

    def _control_cell(self, control: str) -> dict[str, Any]:
        from copy import deepcopy
        if control in self._bound_controls:
            return deepcopy(self._bound_controls[control])
        page_key, control_id = self._split_control(control)
        page = self._pages.get(page_key)
        if not isinstance(page, dict):
            raise ScriptAbort("control_unknown", reason_code="control_unknown")
        matches = [row for row in page.get("controls") or [] if isinstance(row, dict) and (str(row.get("id") or "") == control_id or str(row.get("label") or "") == control_id)]
        if len(matches) != 1:
            raise ScriptAbort("control_unknown", reason_code="control_unknown")
        spec = matches[0].get("locator_spec") if isinstance(matches[0].get("locator_spec"), dict) else {}
        cell = spec.get(_CELLS[self._platform])
        if not isinstance(cell, dict):
            raise ScriptAbort("locator_missing", reason_code="locator_missing")
        return deepcopy(cell)

    def _cell_params(self, cell, selector, index=None):
        kind = str(selector.get("kind") or "")
        if kind == "ocr_text":
            if index is not None:
                raise ScriptAbort("OCR 定位必须唯一，不支持 index", reason_code="locator_ambiguous", status="blocked")
            from mino_scout.platform.ocr import locate
            return locate(self, selector)
        value = str(selector.get("value") or "")
        name = str(selector.get("name") or "")
        # 一个控件可以同时带多把选择器：按顺序喂给解析器，命中哪把算哪把。
        extra = [s for s in (cell.get("selectors") or []) if isinstance(s, dict) and str(s.get("kind") or "")]
        if not kind or not (value or name):
            if extra:
                kind = str(extra[0].get("kind") or "")
                value = str(extra[0].get("value") or "")
                name = str(extra[0].get("name") or "")
                extra = extra[1:]
            else:
                raise ScriptAbort("locator_missing", reason_code="locator_missing")
        label = name or value
        params: dict[str, Any] = {}
        try:
            if self._platform == "android":
                from mino_scout.platform.android.script import locator_params
                params = locator_params(kind, value, name, label)
            elif self._platform == "ios":
                from mino_scout.platform.ios.script import locator_params
                params = locator_params(kind, value, name, label)
            elif self._platform == "web":
                from mino_scout.platform.web.script import locator_params
                params = locator_params(kind, value, name, label)
            elif self._platform == "mac":
                from mino_scout.platform.mac.script import locator_params
                params = locator_params(selector)
            elif self._platform == "win":
                from mino_scout.platform.win.script import locator_params
                params = locator_params(selector)
        except ValueError as exc:
            raise ScriptAbort("定位参数解析失败", reason_code="locator_invalid", details={"parser_error": str(exc)}) from None
        if not params:
            raise ScriptAbort("locator_missing", reason_code="locator_missing")
        if cell.get("require_unique"):
            if index is not None:
                raise ScriptAbort("动态对象定位不能按首项或序号替代", reason_code="locator_ambiguous", status="blocked")
            if "x" in selector or "y" in selector or extra:
                raise ScriptAbort("动态对象需要明确的单一节点定位", reason_code="data_binding_invalid", status="blocked")
            params["require_unique"] = True
            params["bound_selector"] = dict(selector)
        for key in ("scope_css", "frame_selector", "shadow_host_css"):
            if cell.get(key):
                params[key] = cell[key]
        if extra and isinstance(params.get("target"), dict):
            # 备用选择器挂在 target 上，解析器按 resource_id → text → content_desc 的顺序试。
            fallback = dict(params["target"])
            for row in extra:
                key = str(row.get("kind") or "")
                val = str(row.get("value") or row.get("name") or "")
                if key in ("text", "content_desc", "resource_id", "class") and val:
                    fallback.setdefault(key, val)
            params["target"] = fallback
        if index is not None:
            if self._platform != "android":
                # 其他渠道的执行器还不消费 index；静默忽略会让人以为点到了第 N 个。
                raise ScriptAbort(
                    f"{self._platform} 渠道暂不支持 index 选择第几个控件",
                    reason_code="declined",
                )
            params["index"] = int(index)
        return params

    def _split_control(self, control: str) -> tuple[str, str]:
        text = str(control or "").strip()
        if not text:
            raise ScriptAbort("control_unknown", reason_code="control_unknown")
        if "." in text:
            page_key, control_id = text.split(".", 1)
            if page_key in self._pages and control_id:
                return page_key, control_id
        if not self._page_key:
            raise ScriptAbort("control_unknown", reason_code="control_unknown")
        return self._page_key, text

    def _require_channel(self):
        executor = self.core.executors.get(_EXECUTOR.get(self._platform, ""))
        if executor is None:
            raise ScriptAbort(f"缺 {_ITEM.get(self._platform, '渠道')}", reason_code="channel_missing")
        return executor

    def _device_ref(self) -> DeviceRef:
        hint = dict(self._hint)
        extra = {key: value for key, value in hint.items() if key not in {"password", "adb_serial", "udid", "model"}}
        extra["run_id"] = self.run_id
        if self._package:
            extra["target_package"] = self._package
        if self._url:
            extra["url"] = self._url
        extra["web_resolution"] = self._web_resolution
        serial = str(hint.get("adb_serial") or "")
        udid = str(hint.get("udid") or "")
        if self._platform == "android":
            from mino_scout.platform.android.script import device_serial
            serial = device_serial(self._sn, hint)
        elif self._platform == "ios":
            from mino_scout.platform.ios.script import device_udid
            udid = device_udid(self._sn, hint)
        return DeviceRef(sn=self._sn, platform=self._platform, adb_serial=serial,
                         udid=udid, model=str(hint.get("model") or ""), extra=extra)

    def _report(self, capability: str, result: ActionResult, executor: str, elapsed: int, image=None,
                extra: dict[str, Any] | None = None) -> None:
        return self._case._report(capability, result, executor, elapsed, image=image, app=self.key, extra=extra)

    def _callback(self, capability: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self._case._callback(capability, params, timeout)

    def _guard(self) -> None:
        self._case._guard()


def _load_hook(path: Path, name: str, module_name: str, loaded: list[str]) -> Callable | None:
    if not path.is_file() or not _has_function(path, name):
        return None
    module = _load(path, module_name, loaded)
    fn = getattr(module, name, None)
    return fn if callable(fn) else None


def _has_function(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(node, ast.FunctionDef) and node.name == name for node in tree.body)


def _load(path: Path, module_name: str, loaded: list[str]):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ScriptAbort("脚本无法加载", reason_code="script_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loaded.append(module_name)
    spec.loader.exec_module(module)
    return module


def _load_pages(root: Path) -> dict[str, dict[str, dict]]:
    """pages/<APP_KEY>/<page>.yaml → {APP_KEY: {page_key: 文档}}。"""
    pages: dict[str, dict[str, dict]] = {}
    folder = root / "pages"
    if not folder.is_dir():
        return pages
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in {".yaml", ".yml"} or not path.is_file():
            continue
        parts = path.relative_to(folder).parts
        if len(parts) < 2:
            raise ScriptAbort(f"页面文件要放在 pages/<APP_KEY>/ 下：{path.name}", reason_code="script_failed")
        document = parse_yaml(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ScriptAbort(f"页面文件不是映射：{path.name}", reason_code="script_failed")
        key = str(document.get("page_key") or path.stem)
        pages.setdefault(parts[0], {})[key] = document
    return pages


def _visible_nodes(page: dict) -> list[dict[str, Any]]:
    """这一屏的结构化节点；没有时返回空列表，调用方退回文字锚点。"""
    nodes = page.get("_live_nodes") if isinstance(page.get("_live_nodes"), list) else []
    return [n for n in nodes if isinstance(n, dict)]


def _anchors_match(page: dict, text: str) -> bool:
    """页面锚点命中判定。

    states[].anchors 里的一组全部出现才算在这一页（老规则，保留）。
    另外支持两种更具体的形式，缺参数时再用它们：
      states[].any   —— 任一出现即可
      states[].nodes —— [{kind, value}]，按界面节点精确比对
    """
    states = page.get("states") if isinstance(page.get("states"), list) else []
    checked = False
    for state in states:
        if not isinstance(state, dict):
            continue
        nodes = state.get("nodes") if isinstance(state.get("nodes"), list) else []
        if nodes:
            checked = True
            visible = _visible_nodes(page)
            required = []
            for row in nodes:
                if not isinstance(row, dict):
                    continue
                kind = str(row.get("kind") or "")
                value = str(row.get("value") or "").strip()
                if not value:
                    continue
                key = {"resource_id": "resource_id", "text": "text", "content_desc": "content_desc", "class": "class"}.get(kind)
                if not key:
                    continue
                required.append((key, value))
            if required and all(any(value == str(node.get(key) or "") for node in visible) for key, value in required):
                return True
            if visible:
                continue
        anchors_any = [str(item) for item in (state.get("any") or []) if str(item).strip()]
        if anchors_any:
            checked = True
            if any(anchor in text for anchor in anchors_any):
                return True
            continue
        anchors = [str(item) for item in (state.get("anchors") or []) if str(item).strip()]
        if not anchors:
            continue
        checked = True
        if all(anchor in text for anchor in anchors):
            return True
    if not checked:
        raise ScriptAbort("页面没有锚点", reason_code="page_mismatch")
    return False


_LOCATOR_KEYS = ("resource_id", "text", "content_desc", "class", "role", "clickable", "editable",
                 "enabled", "visible", "checked", "checked_known", "selected", "focused", "value", "sensitive")

def _locator_nodes(fields: dict) -> list[dict[str, Any]]:
    """界面树里写页面定位要用的字段。沿用本次 hierarchy 已经读到的节点，不另采一份。

    没有文案的可点控件（图片按钮、贴图）也留下，带上 class 和 bounds。
    """
    raw = fields.get("nodes")
    if not isinstance(raw, list):
        extra = fields.get("extra") if isinstance(fields.get("extra"), dict) else {}
        raw = extra.get("nodes")
    if not isinstance(raw, list):
        return []
    nodes: list[dict[str, Any]] = []
    for node in raw:
        if not isinstance(node, dict):
            continue
        row: dict[str, Any] = {}
        for key in _LOCATOR_KEYS:
            if key == "value" and (node.get("sensitive") or node.get("secret_value")):
                continue
            value = node.get(key)
            if key == "value" and isinstance(value, str):
                row[key] = value[:160]
            elif isinstance(value, bool):
                row[key] = value
            elif isinstance(value, str) and value.strip():
                row[key] = value.strip()[:160]
            elif key == "value" and type(value) in {int, float}:
                row[key] = value
        bounds = node.get("bounds")
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4 and all(isinstance(n, (int, float)) for n in bounds):
            row["bounds"] = [int(n) for n in bounds]
        labeled = bool(row.get("resource_id") or row.get("text") or row.get("content_desc") or row.get("class"))
        if labeled or row.get("clickable") or row.get("editable"):
            nodes.append(row)
        if len(nodes) >= 160:
            break
    return nodes


def _hierarchy_evidence(fields: dict, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    if nodes:
        evidence["nodes"] = nodes
        return evidence
    raw = str(fields.get("hierarchy_text") or "")
    if not raw:
        extra = fields.get("extra") if isinstance(fields.get("extra"), dict) else {}
        raw = str(extra.get("hierarchy_text") or "")
    if raw.strip():
        evidence["hierarchy_text"] = raw[:8000]
    return evidence


def _visible_text(fields: dict) -> str:
    chunks = [str(fields.get("hierarchy_text") or fields.get("text") or "")]
    nodes = fields.get("nodes")
    if not isinstance(nodes, list):
        extra = fields.get("extra") if isinstance(fields.get("extra"), dict) else {}
        nodes = extra.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                chunks.extend(str(node.get(key) or "") for key in ("text", "content_desc", "label", "name"))
            else:
                chunks.append(str(node))
    return "\n".join(bit for bit in chunks if bit)


def _public(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if str(key) not in _HIDDEN}


def parse_yaml(text: str) -> Any:
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent_text = raw[:len(raw) - len(raw.lstrip(" "))]
        if "\t" in indent_text:
            raise ValueError("页面文件不能用 Tab 缩进")
        lines.append((len(indent_text), stripped))
    if not lines:
        return {}
    value, index = _read_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ValueError("页面文件没有解析完")
    return value


def _read_block(lines, index: int, indent: int):
    if index >= len(lines):
        return None, index
    if lines[index][0] != indent:
        raise ValueError("页面文件缩进不正确")
    if lines[index][1].startswith("- "):
        return _read_list(lines, index, indent)
    return _read_map(lines, index, indent)


def _read_map(lines, index: int, indent: int):
    body: dict[str, Any] = {}
    while index < len(lines) and lines[index][0] == indent and not lines[index][1].startswith("- "):
        key, value_text = _split_key(lines[index][1])
        index += 1
        if value_text == "":
            if index < len(lines) and lines[index][0] > indent:
                body[key], index = _read_block(lines, index, lines[index][0])
            else:
                body[key] = None
        else:
            body[key] = _flow(value_text)
    return body, index


def _read_list(lines, index: int, indent: int):
    body = []
    while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
        content = lines[index][1][2:].strip()
        index += 1
        if not content:
            if index < len(lines) and lines[index][0] > indent:
                item, index = _read_block(lines, index, lines[index][0])
            else:
                item = None
        elif content.startswith(("{", "[", '"', "'")) or ": " not in content and not content.endswith(":"):
            item = _flow(content)
        else:
            key, value_text = _split_key(content)
            child: dict[str, Any] = {}
            if value_text == "":
                if index < len(lines) and lines[index][0] > indent:
                    child[key], index = _read_block(lines, index, lines[index][0])
                else:
                    child[key] = None
            else:
                child[key] = _flow(value_text)
            child_indent = indent + 2
            while index < len(lines) and lines[index][0] == child_indent and not lines[index][1].startswith("- "):
                nested_key, nested_value = _split_key(lines[index][1])
                index += 1
                if nested_value == "":
                    if index < len(lines) and lines[index][0] > child_indent:
                        child[nested_key], index = _read_block(lines, index, lines[index][0])
                    else:
                        child[nested_key] = None
                else:
                    child[nested_key] = _flow(nested_value)
            item = child
        body.append(item)
    return body, index


def _split_key(text: str) -> tuple[str, str]:
    if text.startswith('"') or text.startswith("'"):
        value, offset = _quoted(text, 0)
        rest = text[offset:].strip()
        if not rest.startswith(":"):
            raise ValueError("页面文件的键不正确")
        return str(value), rest[1:].strip()
    key, _, rest = text.partition(":")
    return key.strip(), rest.strip()


def _flow(text: str) -> Any:
    value, offset = _flow_value(text.strip(), 0)
    if offset != len(text.strip()):
        raise ValueError("页面文件的值不正确")
    return value


def _flow_value(text: str, index: int):
    if index >= len(text):
        return None, index
    mark = text[index]
    if mark == "[":
        return _flow_list(text, index + 1)
    if mark == "{":
        return _flow_map(text, index + 1)
    if mark in {'"', "'"}:
        return _quoted(text, index)
    end = index
    while end < len(text) and text[end] not in ",}]":
        end += 1
    return _scalar(text[index:end].strip()), end


def _flow_list(text: str, index: int):
    body = []
    while index < len(text):
        index = _skip(text, index)
        if index < len(text) and text[index] == "]":
            return body, index + 1
        value, index = _flow_value(text, index)
        body.append(value)
        index = _skip(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "]":
            return body, index + 1
        raise ValueError("页面文件的列表不正确")
    raise ValueError("页面文件的列表没有结束")


def _flow_map(text: str, index: int):
    body: dict[str, Any] = {}
    while index < len(text):
        index = _skip(text, index)
        if index < len(text) and text[index] == "}":
            return body, index + 1
        if text[index] in {'"', "'"}:
            key, index = _quoted(text, index)
            key = str(key)
        else:
            end = index
            while end < len(text) and text[end] not in ":}":
                end += 1
            key = text[index:end].strip()
            index = end
        index = _skip(text, index)
        if index >= len(text) or text[index] != ":":
            raise ValueError("页面文件的映射不正确")
        value, index = _flow_value(text, _skip(text, index + 1))
        body[key] = value
        index = _skip(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            return body, index + 1
        raise ValueError("页面文件的映射不正确")
    raise ValueError("页面文件的映射没有结束")


def _quoted(text: str, index: int):
    quote = text[index]
    end = index + 1
    chars = []
    while end < len(text):
        if text[end] == "\\" and end + 1 < len(text):
            chars.append(text[end + 1])
            end += 2
            continue
        if text[end] == quote:
            return "".join(chars), end + 1
        chars.append(text[end])
        end += 1
    raise ValueError("页面文件的引号没有结束")


def _skip(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _scalar(text: str) -> Any:
    if text in {"true", "True"}:
        return True
    if text in {"false", "False"}:
        return False
    if text in {"null", "Null", "~", ""}:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text
