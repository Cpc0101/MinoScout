# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""adb 版 EXEC_SCRIPT 脚本库。

命令形状沿用历史契约（params `{script, language, timeout_ms}`、script_id
语义、回传 `{status, stdout, stderr, message}`），但脚本实现**全部用 adb 原语**，
不 import 任何设备侧代理模块。

language 支持：
  - dsl   ：JSON {"steps":[{op...}]}，逐步映射成 adb 命令
  - shell ：直接作为 adb shell 命令执行（可多行，按行执行）
  - js    ：需要设备侧代理的 claw.* API，adb 无对应 → 返回 not_supported

DSL op：
  wake / sleep{ms} / key{key} / open_app_details{package} / foreground{expect} / shell{cmd}
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Tuple

from mino_scout.tools.keep_alive.log import SLog
# 复用命令级执行器的 adb 调用与 keycode 映射（adb_command 不在模块顶层 import 本模块，无循环）
from mino_scout.platform.android.adb_command import _adb_shell, _KEYEVENT_MAP, _ok, _err

TAG = "AdbScript"

# ---------------- 内建脚本（adb 原语重写，script_id 与历史命令层同名） ----------------
_BUILTIN: Dict[str, Dict[str, Any]] = {}


def _register(script_id: str, *, language: str = "shell"):
    def deco(fn: Callable[[dict], str]):
        _BUILTIN[script_id] = {"language": language, "builder": fn}
        return fn
    return deco


def _pkg(vars: dict) -> str:
    pkg = str((vars or {}).get("package") or (vars or {}).get("pkg") or "").strip()
    if not pkg:
        raise ValueError("script requires vars.package")
    return pkg


@_register("open_settings", language="shell")
def _b_open_settings(vars: dict) -> str:
    return "am start -a android.settings.SETTINGS"


@_register("launch_package", language="shell")
def _b_launch_package(vars: dict) -> str:
    return f"monkey -p {_pkg(vars)} -c android.intent.category.LAUNCHER 1"


@_register("open_app_settings", language="dsl")
def _b_open_app_settings(vars: dict) -> str:
    pkg = _pkg(vars)
    return json.dumps({
        "steps": [
            {"op": "wake"},
            {"op": "sleep", "ms": 500},
            {"op": "open_app_details", "package": pkg},
            {"op": "sleep", "ms": 600},
            {"op": "foreground", "expect": "com.android.settings"},
        ]
    })


@_register("open_app_settings_dsl", language="dsl")
def _b_open_app_settings_dsl(vars: dict) -> str:
    return _b_open_app_settings(vars)


@_register("home", language="dsl")
def _b_home(vars: dict) -> str:
    return json.dumps({"steps": [{"op": "key", "key": "home"}]})


@_register("shell_raw", language="shell")
def _b_shell_raw(vars: dict) -> str:
    cmd = str((vars or {}).get("command") or (vars or {}).get("cmd") or "").strip()
    if not cmd:
        raise ValueError("shell_raw requires vars.command")
    return cmd


def list_script_ids() -> List[str]:
    return sorted(_BUILTIN.keys())


# ---------------- 解析（语义与历史命令层一致） ----------------

def _flatten_params(params: Dict[str, Any] | None) -> Dict[str, Any]:
    """摊平 {capability_id, params:{...}} 嵌套。"""
    p = dict(params or {})
    inner = p.get("params")
    if isinstance(inner, dict):
        merged = {k: v for k, v in p.items() if k not in ("params", "capability_id", "event_kind")}
        merged.update(inner)
        return merged
    return p


def resolve_adb_script(
    *,
    script: str = "",
    script_id: str = "",
    language: str = "",
    script_vars: Dict[str, Any] | None = None,
) -> Tuple[str, str]:
    """返回 (body, language)。内联 script 优先；否则查 adb 内建脚本。未知 script_id 抛 ValueError。"""
    if script and str(script).strip():
        return str(script).strip(), (language or "dsl").strip().lower()
    sid = (script_id or "").strip()
    if not sid:
        raise ValueError("adb exec_script requires script or script_id")
    entry = _BUILTIN.get(sid)
    if not entry:
        raise ValueError(f"unknown adb script_id: {sid}")
    body = entry["builder"](script_vars or {})
    return str(body).strip(), entry["language"]


# ---------------- 执行 ----------------

def run_adb_script(serial: str, params: Dict[str, Any] | None) -> Dict[str, Any]:
    """执行 EXEC_SCRIPT（adb 渠道）。params：script/language/timeout_ms/script_id/script_vars。"""
    if not serial:
        return _err("adb serial 未解析")
    p = _flatten_params(params)
    try:
        body, language = resolve_adb_script(
            script=p.get("script") or "",
            script_id=p.get("script_id") or "",
            language=p.get("language") or "",
            script_vars=p.get("script_vars") or {},
        )
    except ValueError as e:
        return _err(str(e))

    language = (language or "dsl").lower()
    if language == "js":
        return {"status": "not_supported", "message": "adb 渠道不支持 js(claw.*) 脚本",
                "stdout": "", "stderr": "js not supported on adb"}
    if language == "shell":
        return _run_shell_script(serial, body)
    if language == "dsl":
        return _run_dsl_script(serial, body)
    return _err(f"unknown script language: {language}")


def _run_shell_script(serial: str, body: str) -> Dict[str, Any]:
    outs: List[str] = []
    for line in [ln.strip() for ln in body.splitlines() if ln.strip()]:
        rc, out, err = _adb_shell(serial, line)
        if rc != 0:
            return _err(f"shell step failed: {line}", err or out)
        if out:
            outs.append(out)
    return _ok("shell script ok", "\n".join(outs))


def _run_dsl_script(serial: str, body: str) -> Dict[str, Any]:
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        return _err(f"dsl parse error: {e}")
    steps = doc.get("steps") if isinstance(doc, dict) else None
    if not isinstance(steps, list):
        return _err("dsl missing steps[]")

    outs: List[str] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        op = str(step.get("op") or "").lower()
        try:
            res = _exec_dsl_step(serial, op, step)
        except Exception as e:
            return _err(f"dsl step#{i} {op} error: {e}")
        if res is not None:
            ok, msg = res
            if not ok:
                return _err(f"dsl step#{i} {op} failed: {msg}")
            if msg:
                outs.append(msg)
    return _ok("dsl script ok", "\n".join(outs))


def _exec_dsl_step(serial: str, op: str, step: dict) -> Tuple[bool, str] | None:
    if op == "wake":
        rc, out, err = _adb_shell(serial, "input", "keyevent", "224")
        return (rc == 0, err if rc else "")
    if op == "sleep":
        time.sleep(max(0, int(step.get("ms") or 0)) / 1000.0)
        return None
    if op == "key":
        name = str(step.get("key") or "").upper()
        code = _KEYEVENT_MAP.get(name)
        kev = str(code) if code is not None else name
        rc, out, err = _adb_shell(serial, "input", "keyevent", kev)
        return (rc == 0, err if rc else "")
    if op == "open_app_details":
        pkg = str(step.get("package") or "").strip()
        if not pkg:
            return (False, "open_app_details missing package")
        rc, out, err = _adb_shell(
            serial, "am", "start", "-a", "android.settings.APPLICATION_DETAILS_SETTINGS",
            "-d", f"package:{pkg}",
        )
        return (rc == 0 and "error" not in (out + err).lower(), err or out)
    if op == "foreground":
        expect = str(step.get("expect") or "").strip()
        rc, out, err = _adb_shell(serial, "dumpsys", "activity", "activities")
        if rc != 0:
            return (False, err)
        if expect and expect not in out:
            return (False, f"foreground expect {expect} not found")
        return (True, expect)
    if op == "shell":
        cmd = str(step.get("cmd") or step.get("command") or "").strip()
        if not cmd:
            return (False, "shell op missing cmd")
        rc, out, err = _adb_shell(serial, cmd)
        return (rc == 0, out if rc == 0 else err)
    return (False, f"unknown dsl op: {op}")
