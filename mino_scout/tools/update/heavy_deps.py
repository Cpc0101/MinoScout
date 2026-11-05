"""runtime+app 装完后的后台依赖：Chromium 层 + ADB Keyboard。"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from mino_scout.config import config_dir
from mino_scout.tools.update.install_plan import parse_layers_txt, plan_heavy_deps
from mino_scout.tools.keep_alive.log import SLog

TAG = "HeavyDeps"

_lock = threading.Lock()
_running = False
_last_result: dict[str, Any] | None = None


def _installed() -> dict[str, str] | None:
    path = config_dir() / "bin" / "layers.txt"
    if not path.is_file():
        return None
    try:
        return parse_layers_txt(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def install_heavy_deps(*, manifest_url: str = "", adb_hook=None) -> dict[str, Any]:
    from mino_scout.tools.update.self_update import (
        apply_plan_step,
        fetch_json,
        manifest_url_from_config,
        pick_manifest_item,
    )

    from mino_scout.config import development

    prefix = config_dir()
    if development():
        from mino_scout.plugins.components import chromium, chromium_ready

        if chromium_ready():
            SLog.i(TAG, "Chromium 已就绪，跳过下载")
            plan = {"mode": "up-to-date", "reason": "source_scout"}
        else:
            SLog.i(TAG, "安装与当前源码 Scout 匹配的 Chromium")
            plan = {"mode": "installed", "reason": "source_scout", "detail": chromium("install")}
    else:
        bin_dir = prefix / "bin"
        url = str(manifest_url or manifest_url_from_config()).strip()
        manifest = fetch_json(url)
        item = pick_manifest_item(manifest)
        installed = _installed()
        plan = plan_heavy_deps(item, installed, bin_dir=bin_dir if bin_dir.is_dir() else None)

        if plan.get("mode") == "up-to-date":
            SLog.i(TAG, "Chromium 层已就绪，跳过下载")
        else:
            for step in plan.get("steps") or []:
                SLog.i(TAG, f"安装 {step.get('layer') or 'layer'} …")
                from mino_scout.service import pid_alive, read_pid

                apply_plan_step(step, inplace=pid_alive(read_pid()))

    adb_ok = False
    adb_err = ""
    try:
        if adb_hook is not None:
            adb_hook()
        adb_ok = True
    except Exception as exc:
        adb_err = str(exc)
        SLog.w(TAG, f"ADB Keyboard 准备: {exc}")

    out = {
        "ok": True,
        "browser": plan,
        "adb_keyboard": {"ok": adb_ok, "error": adb_err},
    }
    return out


def schedule_heavy_deps(*, manifest_url: str = "", adb_hook=None) -> None:
    """进程内只跑一轮，不阻塞 REGISTER。"""
    global _running, _last_result
    with _lock:
        if _running:
            return
        _running = True

    def _worker() -> None:
        global _last_result, _running
        try:
            SLog.i(TAG, "开始后台安装 Chromium / ADB 依赖…")
            _last_result = install_heavy_deps(manifest_url=manifest_url, adb_hook=adb_hook)
            SLog.i(TAG, "后台依赖安装完成")
        except Exception as exc:
            SLog.e(TAG, f"后台依赖安装失败: {exc}")
            _last_result = {"ok": False, "error": str(exc)}
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_worker, name="mino-scout-heavy-deps", daemon=True).start()


def last_heavy_deps_result() -> dict[str, Any] | None:
    return _last_result
