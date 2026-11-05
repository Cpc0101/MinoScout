"""Official CLI authorization. Credentials stay in its account-scoped local store."""
from __future__ import annotations
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from mino_scout.plugins.state import plugin_dir
from mino_scout.plugins.host_exec import _base_env, _safe_entry
from mino_scout.plugins.install import acquire_plugin

_guard = threading.RLock()
_jobs: dict[str, dict] = {}
_ACTIVE = {"starting", "creating_app", "authorizing", "verifying"}

def root() -> Path:
    return plugin_dir("cli", "feishu") / ".authorization"

def _env(home: Path) -> dict[str, str]:
    env = _base_env()
    env["LARKSUITE_CLI_CONFIG_DIR"] = str(home)
    return env

def _read(home: Path) -> dict:
    try:
        return json.loads((home / "mino-status.json").read_text())
    except (OSError, ValueError):
        return {"state": "idle"}

def _save(home: Path, data: dict) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = home / ".mino-status.tmp"
    tmp.write_text(json.dumps(data))
    tmp.chmod(0o600)
    tmp.replace(home / "mino-status.json")

def ready(identity: str = "") -> bool:
    """CLI 可用性。用户态扫码或 bot 应用凭据任一已验证即可。"""
    from mino_scout.plugins.state import _item, installed
    if _item("cli", "feishu").get("import_blocked") or not installed("cli", "feishu"):
        return False
    home = root()
    if not (home / "config.json").is_file():
        return False
    if _read(home).get("state") == "authorized":
        return True
    # bot 身份不经过用户扫码状态文件；用 auth status 验证应用凭据是否已生效。
    if identity in ("", "bot"):
        try:
            info = run_json(["auth", "status", "--json", "--verify"], home=home)
        except Exception:
            return False
        return info.get("identity") in ("bot", "user") and info.get("verified") is True
    return False

def _url(line: str) -> str:
    for value in re.findall(r'https://[^\s<>"\x1b]+', line):
        value = value.rstrip(").,")
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if not parsed.username and not parsed.password and (
            host == "feishu.cn" or host.endswith(".feishu.cn")
            or host == "larksuite.com" or host.endswith(".larksuite.com")
        ) and ("?" in value):
            return value
    return ""

def run_json(args: list[str], *, home: Path | None = None, cwd: Path | None = None) -> dict:
    home = home or root()
    entry = _safe_entry(home.parent, "invoke")
    try:
        result = subprocess.run([str(entry), *args], cwd=str(cwd or home.parent), env=_env(home),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise RuntimeError("飞书 CLI 未返回有效结果，请检查安装与网络后重试") from None
    if not isinstance(payload, dict):
        raise RuntimeError("飞书 CLI 返回格式异常，请检查安装版本")
    if result.returncode or payload.get("ok") is False:
        raise RuntimeError("飞书 CLI 调用失败，请检查授权是否过期、应用权限及文档访问权限")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise RuntimeError("飞书 CLI 返回格式异常，请检查安装版本")
    return data

def check() -> dict:
    data = run_json(["auth", "status", "--json", "--verify"])
    if data.get("identity") not in ("bot", "user") or data.get("verified") is not True:
        raise RuntimeError("飞书授权尚未通过，请重新扫码授权")
    return {"ok": True, "summary": "飞书 CLI 授权可用"}

def _worker(home: Path, job: dict, lock) -> None:
    def update(**values):
        with _guard:
            job["public"].update(values)
            _save(home, job["public"])
    try:
        entry = _safe_entry(home.parent, "invoke")
        steps = []
        if (home / "config.json").is_file():
            update(state="verifying", authorization_url="", summary="正在核对已有授权，无需重复扫码")
            try:
                existing = run_json(["auth", "status", "--json", "--verify"], home=home)
            except RuntimeError:
                existing = {}
            if job["cancel"].is_set():
                raise RuntimeError("cancelled")
            if existing.get("identity") == "user" and existing.get("verified") is True:
                update(state="authorized", authorization_url="", summary="已有登录授权验证通过", warning="登录已验证；具体功能仍以已授予权限为准。")
                return
        if not (home / "config.json").exists():
            steps.append(("creating_app", ["config", "init", "--new"]))
        steps.append(("authorizing", ["auth", "login", "--scope", "docx:document:readonly"]))
        login_warning = False
        for stage, args in steps:
            if job["cancel"].is_set():
                raise RuntimeError("cancelled")
            update(state=stage, authorization_url="", summary="扫码创建应用" if stage == "creating_app" else "扫码授权 CLI 访问")
            proc = subprocess.Popen([str(entry), *args], cwd=home.parent, env=_env(home),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            job["process"] = proc
            if job["cancel"].is_set():
                proc.kill()
            timer = threading.Timer(600, lambda: proc.kill() if proc.poll() is None else None)
            timer.start()
            try:
                for line in proc.stdout:
                    url = _url(line)
                    if url:
                        update(authorization_url=url)
                    if job["cancel"].is_set() and proc.poll() is None:
                        proc.kill()
                code = proc.wait()
            finally:
                timer.cancel()
                proc.stdout.close()
                job["process"] = None
            if job["cancel"].is_set():
                raise RuntimeError("cancelled")
            update(last_stage=stage, exit_code=code)
            if code:
                if stage == "authorizing":
                    # CLI may persist a valid login then fail its requested-scope check.
                    # The verified identity below, never the exit code alone, decides success.
                    login_warning = True
                else:
                    raise RuntimeError("应用创建未完成。已保存的应用会保留，请继续授权。")
        update(state="verifying", authorization_url="", summary="正在检查实际授权结果")
        status = run_json(["auth", "status", "--json", "--verify"], home=home)
        if status.get("identity") != "user" or status.get("verified") is not True:
            raise RuntimeError("应用已保存，但用户授权尚未通过，请继续授权")
        update(state="authorized", authorization_url="", warning=("登录验证已通过，但 CLI 授权命令未正常完成；部分权限可能未授予，具体功能仍需核对权限。" if login_warning else ""), summary="已授权，无需手填 App ID 或 Secret")
    except Exception as exc:
        cancelled = job["cancel"].is_set()
        update(state="cancelled" if cancelled else "failed", authorization_url="",
               summary="已取消授权，已创建的应用会保留" if cancelled else (
                   str(exc) if isinstance(exc, RuntimeError) else "授权流程未完成，请检查 CLI 安装与网络后重试"))
    finally:
        lock.release()
        if job["public"].get("state") == "authorized" and root() == home:
            from mino_scout.plugins import feishu_messages
            feishu_messages.reconcile()

def _store_authorized(home: Path) -> dict:
    """CLI 已验证时把授权写入状态文件。只放在返回值里的话，下一次查询会读到旧的 idle。"""
    payload = {"state": "authorized", "summary": "已授权，可直接使用", "authorization_url": ""}
    _save(home, payload)
    job = _jobs.get(str(home.resolve()))
    if job is not None and job["public"].get("state") not in _ACTIVE:
        job["public"].update(payload)
    return dict(payload)


def action(name: str) -> dict:
    home = root()
    key = str(home.resolve())
    result: dict | None = None
    reconcile = False
    with _guard:
        job = _jobs.get(key)
        active = bool(job and job["public"]["state"] in _ACTIVE)
        if name == "auth_status":
            result = dict(job["public"] if job else _read(home))
            if not job and result.get("state") in _ACTIVE:
                result.update(state="interrupted", authorization_url="", summary="节点重启中断了授权；继续时将复用已保存的应用")
            elif not active and result.get("state") in (None, "", "idle") and ready():
                result = _store_authorized(home)
                reconcile = True
        elif name == "auth_cancel":
            if active:
                job["cancel"].set()
                proc = job.get("process")
                if proc and proc.poll() is None:
                    proc.kill()
            result = {"state": "cancelling" if active else "idle", "summary": "正在取消" if active else "没有进行中的授权"}
        elif name != "auth_start":
            raise ValueError("不支持的授权操作")
        elif active:
            result = dict(job["public"])
        elif ready():
            stored = _read(home)
            if stored.get("state") == "authorized":
                result = dict(stored)
            else:
                result = _store_authorized(home)
                reconcile = True
        else:
            _safe_entry(home.parent, "invoke")
            lock = acquire_plugin("cli", "feishu")
            job = {"public": {"state": "starting", "summary": "正在准备飞书授权", "authorization_url": ""},
                   "cancel": threading.Event(), "process": None}
            try:
                _save(home, job["public"])
                _jobs[key] = job
                threading.Thread(target=_worker, args=(home, job, lock), daemon=True).start()
            except Exception:
                lock.release()
                raise
            result = dict(job["public"])
    if reconcile:
        from mino_scout.plugins import feishu_messages
        feishu_messages.reconcile()
    return result or {}


def forget_uninstalled() -> None:
    """Called under the plugin install lock after successful removal."""
    with _guard:
        _jobs.pop(str(root().resolve()), None)
