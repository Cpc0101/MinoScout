"""跑 def run(case)。设备动词走本机渠道。账号和视觉判断回调 Nexus。

验证码用本节点 Gmail，文本模型用本节点保存的密钥。不安装渠道程序，不读能力目录。YAML 只解析。
"""
from __future__ import annotations

import ast
import difflib
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from mino_scout.executors.base import DeviceRef, ExecutorContext
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
    def __init__(self, message: str, *, reason_code: str):
        self.reason_code = reason_code
        super().__init__(message)


class ActionResult:
    def __init__(self, *, ok: bool, summary: str, status: str, data: dict[str, Any] | None = None):
        self.ok = bool(ok)
        self.summary = str(summary or "")
        self.status = str(status or "")
        self.data = dict(data or {})

    def __bool__(self) -> bool:
        return self.ok


class _Scope:
    def __init__(self, case: "Case", number: "int | str", role: str):
        self._case = case
        self._number = number
        self._role = role

    def _body(self, **extra: Any) -> dict[str, Any]:
        key = "title" if isinstance(self._number, str) else "number"
        return {"op": "step", "role": self._role, key: self._number, **extra}

    def __enter__(self) -> "Case":
        self._case._guard()
        self._case._callback("script.callback", self._body(action="enter"), 15)
        self._case._step = self._number
        self._case._role = self._role
        return self._case

    def __exit__(self, exc_type, exc, tb) -> bool:
        failed = exc_type is not None
        try:
            self._case._callback("script.callback", self._body(action="leave", failed=failed), 15)
        except Exception:
            if exc_type is None:
                raise
        finally:
            self._case._step = None
            self._case._role = ""
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
        return _run_locked(core, payload, root, deadline=deadline, stop=stop)


def _run_locked(core, payload, root: Path, *, deadline: float, stop) -> dict[str, Any]:
    case = None
    try:
        if str(payload.platform) == "web":
            from mino_scout.platform.web.playwright_hub import get_hub
            get_hub().note_lane(str(payload.run_id or ""))
        case = Case(core, payload, root, deadline=deadline, stop=stop)
        if getattr(payload, "revision", ""):
            case._report("checkout", ActionResult(ok=True, summary=f"脚本仓库提交 {payload.revision}", status="pass"), "scout", 0)
        _execute_hooks(root, payload.script_path, case)
        return {"status": "pass", "summary": "脚本执行完成", "error": "", "reason_code": "", "touched": bool(case.touched)}
    except ScriptHalt as exc:
        return _failure(exc, case)
    except ScriptAbort as exc:
        return _failure(exc, case)
    except Exception as exc:
        return _failure(ScriptAbort(f"{type(exc).__name__}: {exc}", reason_code="script_failed"), case)


def _failure(exc: BaseException, case: Case | None) -> dict[str, Any]:
    return {
        "status": "fail",
        "summary": str(exc)[:300],
        "error": str(exc)[:400],
        "reason_code": str(getattr(exc, "reason_code", "") or "script_failed"),
        "touched": bool(case and case.touched),
    }


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

    __slots__ = ("key", "app_id", "platform", "package", "url")

    def __init__(self, key: str, platform: str, package: str, url: str, app_id: str = ""):
        self.key = key
        self.app_id = app_id
        self.platform = platform
        self.package = package
        self.url = url


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
            self._apps[str(key)] = AppRef(str(key), str(row.get("platform") or ""), package, str(row.get("url") or ""), str(row.get("app_id") or ""))

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

    def prep(self, number: int) -> _Scope:
        from mino_scout.platform.scout.run import prep

        return prep(self._case, number)

    def do(self, number: int) -> _Scope:
        from mino_scout.platform.scout.run import do

        return do(self._case, number)

    def check(self, number: int) -> _Scope:
        from mino_scout.platform.scout.run import check

        return check(self._case, number)

    def account(self) -> SimpleNamespace:
        from mino_scout.server_comm.callbacks import account

        return account(self._case)

    def note(self, text: str, **fields: Any) -> None:
        from mino_scout.server_comm.callbacks import note

        return note(self._case, text, **fields)

    def fail(self, message: str) -> None:
        from mino_scout.platform.scout.run import fail

        return fail(self._case, message)


class Case:
    """一次运行的共享上下文。设备动作在 case.init(...) 返回的 App 上。"""

    def __init__(self, core, payload, root: Path, *, deadline: float, stop):
        self.core = core
        self.run_id = str(payload.run_id)
        self.session_id = str(payload.session_id)
        self._sn = str(payload.sn)
        self._platform = str(payload.platform)
        self._hint = dict(getattr(payload, "device_hint", None) or {})
        self._all_pages = _load_pages(root)
        self.env = str(self._hint.get("env") or "")
        self.app_id = str(self._hint.get("app_id") or "")
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

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _unknown("case", name, ["config", "env", "app_id", "init", "scout", "nexus"],
                       "。设备动作写在 app = case.init(case.config.<KEY>) 返回的 app 上")

    def init(self, ref: AppRef) -> "App":
        self._guard()
        if not isinstance(ref, AppRef):
            raise ScriptAbort("case.init 只接受 case.config.<KEY>", reason_code="config_invalid")
        if ref.platform != self._platform:
            raise ScriptAbort(f"应用 {ref.key} 是 {ref.platform}，本次运行是 {self._platform}", reason_code="platform_mismatch")
        app = self._apps.get(ref.key)
        if app is None:
            app = self._apps[ref.key] = App(self, ref)
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
        }
        if app:
            body["app"] = app
        if extra:
            body.update(extra)
        if self._step is not None:
            body["case_step"] = self._step
            body["role"] = self._role
        if image is not None:
            body["image_base64"] = str(getattr(image, "image_base64", "") or "")
            body["image_mime"] = str(getattr(image, "image_mime", "") or "")
            body["width"] = int(getattr(image, "width", 0) or 0)
            body["height"] = int(getattr(image, "height", 0) or 0)
        self._callback("script.session", {"op": "tool_result", "event_type": "tool/result", "event": body}, 20)

    def _callback(self, capability: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        from mino_scout.server_comm.nexus_link import call

        return call(self, capability, params, timeout)

    def _guard(self) -> None:
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

    def __init__(self, case: Case, ref: AppRef):
        self._case = case
        self.key = ref.key
        self._package = ref.package
        self._url = ref.url
        self._pages = case._all_pages.get(ref.key, {})
        self._page_key = ""
        self._shot = None
        self._frames: list[Any] = []

    # ---- 启停
    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        raise _unknown("app", name, _public_names(App))

    def launch(self) -> ActionResult:
        return self._device("launch_app", {"package": self._package, "bundle": self._package, "bundle_id": self._package, "url": self._url})

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
    def tap(self, control: str) -> ActionResult:
        params = self._locate(str(control or ""), "tap")
        if self._platform == "web":
            return self._web("tap", params)
        return self._device("tap_element", params)

    def tap_xy(self, x: int, y: int) -> ActionResult:
        return self._device("tap_xy", {"x": int(x), "y": int(y)})

    def long_press(self, control: str, duration_ms: int = 1000) -> ActionResult:
        params = self._locate(str(control or ""), "long_press")
        params["duration_ms"] = int(duration_ms)
        return self._device("long_press_element", params)

    def input(self, control: str, text: str) -> ActionResult:
        params = self._locate(str(control or ""), "input")
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
    def screenshot(self) -> ActionResult:
        self._guard()
        self._require_channel()
        from mino_scout import screen as SCREEN
        self.touched = True
        shot = SCREEN.capture(self._device_ref(), timeout_sec=15, compress_ratio=2.0)
        self._shot = shot
        ok = bool(shot.has_image())
        result = ActionResult(ok=ok, summary="已截图" if ok else (shot.error or "没有截到图"), status="pass" if ok else "fail",
                               data={"width": int(shot.width or 0), "height": int(shot.height or 0)})
        self._report("screenshot", result, str(shot.source or _EXECUTOR.get(self._platform) or "screen"), int(shot.elapsed_ms or 0), image=shot if ok else None)
        if not ok:
            raise ScriptAbort(result.summary or "没有截到图", reason_code="script_failed")
        return result

    def sample(self, duration_ms: int, interval_ms: int = 300) -> dict[str, Any]:
        from mino_scout.platform.scout.run import sample

        return sample(self, duration_ms, interval_ms)

    def hierarchy(self) -> ActionResult:
        self._guard()
        self._require_channel()
        self.touched = True
        status, fields = self.core._hierarchy_fields(self._device_ref(), run_id=self.run_id)
        text = _visible_text(fields)
        ok = str(getattr(status, "value", status)) == "pass"
        nodes = _locator_nodes(fields)
        data: dict[str, Any] = {"text": text[:4000]}
        if nodes:
            data["nodes"] = nodes
        result = ActionResult(ok=ok, summary="已读取界面结构" if ok else str(fields.get("error") or "没有读到界面结构"),
                               status="pass" if ok else "fail", data=data)
        extra = _hierarchy_evidence(fields, nodes)
        self._report("hierarchy", result, str(fields.get("source") or "core"), int(fields.get("elapsed_ms") or 0), extra=extra or None)
        if not ok:
            raise ScriptAbort(result.summary or "没有读到界面结构", reason_code="script_failed")
        return result

    def on_page(self, page_key: str) -> ActionResult:
        from mino_scout.platform.scout.run import on_page

        return on_page(self, page_key)

    def vlm(self, prompt: str, verdict: str = "") -> str:
        from mino_scout.server_comm.callbacks import vlm

        return vlm(self, prompt, verdict=verdict)

    def vlm_frame(self, index: int, prompt: str, verdict: str = "") -> str:
        from mino_scout.server_comm.callbacks import vlm_frame

        return vlm_frame(self, index, prompt, verdict=verdict)

    # ---- 内部
    def _locate(self, control: str, kind: str) -> dict[str, Any]:
        try:
            return self._locator_params(control)
        except ScriptAbort as exc:
            if exc.reason_code in {"locator_missing", "control_unknown"}:
                self._report(kind, ActionResult(ok=False, summary=exc.reason_code, status="fail"), "script", 0)
            raise

    def _device(self, capability: str, params: dict[str, Any], extra: dict[str, Any] | None = None) -> ActionResult:
        self._guard()
        executor = self._require_channel()
        if not executor.supports(capability):
            raise ScriptAbort(f"{self._platform} 渠道不支持 {capability}", reason_code="declined")
        self.touched = True
        self._seq += 1
        event = PlanEvent(seq=self._seq, capability_id=capability, event_kind=capability, params=dict(params),
                           executor_order=[_EXECUTOR[self._platform]], selected_impl={"executor": _EXECUTOR[self._platform]})
        started = time.perf_counter()
        result = executor.execute(event, ExecutorContext(device=self._device_ref(), run_id=self.run_id, step_idx=self._seq))
        elapsed = int((time.perf_counter() - started) * 1000)
        status = str(getattr(result.status, "value", result.status) or "fail")
        action = ActionResult(ok=status == "pass", summary=str(result.summary or ""), status=status, data=_public(result.raw_response))
        self._report(capability, action, str(result.executor_used or _EXECUTOR[self._platform]), elapsed, extra=extra)
        return action

    def _web(self, kind: str, params: dict[str, Any], text: str = "") -> ActionResult:
        from mino_scout.platform.web.script import act

        return act(self, kind, params, text)

    def _locator_params(self, control: str) -> dict[str, Any]:
        self._guard()
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
        selector = cell.get("selector") if isinstance(cell.get("selector"), dict) else {}
        kind = str(selector.get("kind") or "")
        value = str(selector.get("value") or "")
        name = str(selector.get("name") or "")
        if not kind or not (value or name):
            raise ScriptAbort("locator_missing", reason_code="locator_missing")
        label = name or value
        try:
            if self._platform == "android":
                from mino_scout.platform.android.script import locator_params
                return locator_params(kind, value, name, label)
            if self._platform == "ios":
                from mino_scout.platform.ios.script import locator_params
                return locator_params(kind, value, name, label)
            if self._platform == "web":
                from mino_scout.platform.web.script import locator_params
                return locator_params(kind, value, name, label)
            if self._platform == "mac":
                from mino_scout.platform.mac.script import locator_params
                return locator_params(selector)
            if self._platform == "win":
                from mino_scout.platform.win.script import locator_params
                return locator_params(selector)
        except ValueError:
            raise ScriptAbort("locator_missing", reason_code="locator_missing") from None
        raise ScriptAbort("locator_missing", reason_code="locator_missing")

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
        self._case._report(capability, result, executor, elapsed, image=image, app=self.key, extra=extra)

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


def _anchors_match(page: dict, text: str) -> bool:
    states = page.get("states") if isinstance(page.get("states"), list) else []
    checked = False
    for state in states:
        if not isinstance(state, dict):
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


_LOCATOR_KEYS = ("resource_id", "text", "content_desc", "class", "role", "clickable", "editable")


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
            value = node.get(key)
            if isinstance(value, bool):
                row[key] = value
            elif isinstance(value, str) and value.strip():
                row[key] = value.strip()[:160]
        bounds = node.get("bounds")
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4 and all(isinstance(n, (int, float)) for n in bounds):
            row["bounds"] = [int(n) for n in bounds]
        labeled = bool(row.get("resource_id") or row.get("text") or row.get("content_desc"))
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
