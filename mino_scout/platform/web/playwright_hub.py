# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""进程内 Playwright 会话：和 adb 一样由 MiniOrangeServer 调起，Chromium 是子进程。

Playwright 的 sync API 必须在创建它的线程里用。CaseRunner 每个 sn 一条 worker
线程，因此 Hub 按线程保存 Playwright / Browser，按 sn 保存当前 Context/Page。
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from mino_scout.platform.web.playwright_observability import (
    WebObservability,
    apply_context_options,
    context_kwargs,
    validate_context_options,
)
from mino_scout.platform.web.viewports import (
    DEFAULT_WEB_RESOLUTION,
    normalize_web_resolution,
    web_viewport,
)
from mino_scout.platform.web.web_url import normalize_goto_url, validate_page_url
from mino_scout.tools.keep_alive.log import SLog

TAG = "PlaywrightHub"

# Backward-compatible globals now describe the configured default viewport.
_DEFAULT_VIEWPORT = web_viewport(DEFAULT_WEB_RESOLUTION)
VIEWPORT_W = int(_DEFAULT_VIEWPORT["width"])
VIEWPORT_H = int(_DEFAULT_VIEWPORT["height"])

_probe_lock = threading.Lock()
_probe_cache: tuple[float, str, dict] = (0.0, "", {})
_PROBE_TTL_SEC = 30.0


def browsers_dir() -> Path:
    if getattr(sys,"frozen",False): return Path(sys.executable).resolve().parent / "ms-playwright"
    from mino_scout.config import config_dir
    return config_dir() / "bin" / "ms-playwright"


def apply_browsers_path() -> Path:
    """Point Playwright at the onedir copy (or Studio-installed) Chromium."""
    existing = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if existing:
        return Path(existing)
    dest = browsers_dir()
    try:
        if dest.is_dir() and any(dest.iterdir()):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(dest)
            return dest
    except OSError:
        pass
    return dest


def web_slot_sn(scout_id: str) -> str:
    """Per-node Playwright sn: `web` + alphanumeric scout_id. Fixed for that install."""
    sid = "".join(c for c in str(scout_id or "").lower() if c.isalnum())
    if not sid: raise ValueError("web_node_identity_required")
    return f"web{sid}"


def is_web_slot(sn: str = "", platform: str = "") -> bool:
    s = str(sn or "")
    valid = s.startswith("web") and len(s)>3 and s[3:].isascii() and s[3:].isalnum() and s==s.lower()
    return valid and (not platform or platform=="web")


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def normalize_web_identity(
    actor: str = "default",
    context_id: str = "",
    page_id: str = "",
) -> tuple[str, str, str]:
    """Validate the optional actor/context/page identity used by multi-account runs.

    Calling the browser API without an explicit identity keeps the legacy
    ``sn::run_id`` session key.  Scripts that use ``case.init(actor=...)`` get a
    stable, deterministic identity tuple instead.
    """
    values = [str(actor or "").strip(), str(context_id or "").strip(), str(page_id or "").strip()]
    if not any(values):
        return "", "", ""
    actor_v = values[0] or "default"
    for name, value in (("actor", actor_v), ("context_id", values[1] or actor_v), ("page_id", values[2] or "main")):
        if not _IDENTITY_RE.fullmatch(value):
            raise ValueError(f"web_identity_invalid:{name}")
    return actor_v, values[1] or actor_v, values[2] or "main"


def _session_key(
    sn: str,
    run_id: str = "",
    *,
    actor: str = "",
    context_id: str = "",
    page_id: str = "",
) -> str:
    base = str(sn or "").strip() or "web"
    rid = str(run_id or "").strip()
    legacy = f"{base}::{rid}" if rid else base
    actor_v, context_v, _page_v = normalize_web_identity(actor, context_id, page_id)
    if not actor_v:
        return legacy
    # A page selects an active tab; it does not own another BrowserContext.
    # All tabs for one actor/context lease share cookies and storage.
    return f"{legacy}::actor={actor_v}::context={context_v}"


def _browser_key(sn: str, resolution: str = DEFAULT_WEB_RESOLUTION) -> str:
    """同一 Web 设备的不同分辨率不能复用带固定 window-size 的浏览器。"""
    return f"{_session_key(sn)}::{normalize_web_resolution(resolution)}"


def looks_like_url(value: str) -> bool:
    return bool(normalize_goto_url(value))


_GOTO_WAIT_STATES = frozenset({"commit", "domcontentloaded", "load", "networkidle"})


def goto_options_from_params(params: dict[str, Any] | None) -> tuple[str, int]:
    p = dict(params or {})
    wait_until = str(p.get("wait_until") or p.get("load_state") or "domcontentloaded").strip().lower()
    if wait_until not in _GOTO_WAIT_STATES:
        raise ValueError("web_goto_wait_invalid")
    raw_timeout = p.get("timeout_ms", p.get("goto_timeout_ms", 60_000))
    if type(raw_timeout) is not int or not 5_000 <= raw_timeout <= 120_000:
        raise ValueError("web_goto_timeout_invalid")
    return wait_until, raw_timeout


def goto_url(page: Any, url: str, *, params: dict[str, Any] | None = None) -> None:
    wait_until, timeout = goto_options_from_params(params)
    target = validate_page_url(url)
    page.goto(target, wait_until=wait_until, timeout=timeout)


def pick_goto_url(*candidates: Any) -> str:
    for raw in candidates:
        url = normalize_goto_url("" if raw is None else str(raw))
        if url:
            return url
    return ""


def headed_from_env() -> bool:
    """仅 probe / 无 EXECUTE 时的本机缺省。跑用例时 headed 由 Nexus device_hint 决定。"""
    raw = str(os.environ.get("MINIORANGE_PLAYWRIGHT_HEADED", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def headed_from_hint(hint: dict | None, *, default_headed: bool = False) -> bool:
    """读 EXECUTE.device_hint：headless=true → 无头；headed=false → 无头。缺省无头。"""
    extra = hint if isinstance(hint, dict) else {}
    if "headless" in extra:
        return not bool(extra.get("headless"))
    if "headed" in extra:
        return bool(extra.get("headed"))
    return default_headed


# probe_playwright() 的"可用"状态名。上游用 "available" 而不是 "connected" ——
# 语义是"包和 Chromium 都在"，不是"此刻正驱动着浏览器"。
# 导成常量，避免调用方各写一遍字面量再写错（已经踩过一次）。
PROBE_OK_STATE = "available"

_QUIET_INSTALLED = False


def is_playwright_closed_noise(message: str, *, extra: str = "", exc_name: str = "") -> bool:
    """Playwright 关 page/browser 时 asyncio 会甩 TargetClosedError，不是业务故障。"""
    if exc_name == "TargetClosedError":
        return True
    text = f"{message} {extra}"
    if "TargetClosedError" in text or "Target page, context or browser has been closed" in text:
        return True
    if "Task was destroyed but it is pending" in message and (
        "playwright" in extra or "Connection.run" in extra
    ):
        return True
    return False


class _PlaywrightClosedFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        extra_parts: list[str] = []
        if record.args:
            extra_parts.append(str(record.args))
        if record.exc_text:
            extra_parts.append(record.exc_text)
        exc_name = ""
        if record.exc_info and record.exc_info[1] is not None:
            extra_parts.append(repr(record.exc_info[1]))
            extra_parts.append("".join(traceback.format_exception(*record.exc_info)))
            exc_name = type(record.exc_info[1]).__name__
        extra = " ".join(extra_parts)
        return not is_playwright_closed_noise(record.getMessage(), extra=extra, exc_name=exc_name)


def install_playwright_closed_quiet() -> None:
    """进程级只滤 Playwright 关连接的噪音，不影响 transport 的 ConnectionClosedError。"""
    global _QUIET_INSTALLED
    if _QUIET_INSTALLED:
        return
    _QUIET_INSTALLED = True
    logging.getLogger("asyncio").addFilter(_PlaywrightClosedFilter())


def probe_playwright() -> tuple[str, dict]:
    """不长期占浏览器：只确认 Python 包和 Chromium 可执行文件在。"""
    global _probe_cache
    install_playwright_closed_quiet()
    apply_browsers_path()
    now = time.time()
    with _probe_lock:
        ts, state, meta = _probe_cache
        if state and now - ts < _PROBE_TTL_SEC:
            return state, dict(meta)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result = ("disconnected", {"reason": "未安装 playwright：pip install playwright && playwright install chromium"})
        with _probe_lock:
            _probe_cache = (now, result[0], result[1])
        return result
    pw = None
    try:
        pw = sync_playwright().start()
        exe = str(getattr(pw.chromium, "executable_path", "") or "")
        if not exe or not os.path.isfile(exe):
            result = (
                "disconnected",
                {"reason": "未安装 Chromium：在 Server 环境执行 playwright install chromium"},
            )
        else:
            result = ("available", {"browser": "chromium", "path": exe, "headed": headed_from_env()})
    except Exception as exc:
        result = ("disconnected", {"reason": str(exc)[:240]})
    finally:
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
    with _probe_lock:
        _probe_cache = (now, result[0], dict(result[1]))
    return result


class PlaywrightHub:
    def __init__(self) -> None:
        install_playwright_closed_quiet()
        self._local = threading.local()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        # 同一条 run 超时后 +1。旧线程晚到的写入对不上这个数，不能盖住新线程的页面。
        self._run_tokens: dict[str, int] = {}
        # key_down is intentionally cross-event state.  It belongs to the
        # actor/context lease and is released before that context is closed.
        self._pressed_keys: dict[str, set[str]] = {}

    def _playwright(self):
        pw = getattr(self._local, "pw", None)
        if pw is None:
            from playwright.sync_api import sync_playwright

            install_playwright_closed_quiet()
            pw = sync_playwright().start()
            self._quiet_thread_loop()
            self._local.pw = pw
            self._local.browsers = {}
            SLog.i(TAG, f"playwright started thread={threading.get_ident()}")
        return pw

    @staticmethod
    def _quiet_thread_loop() -> None:
        """能挂到正在跑的 loop 就挂；挂不到就靠 asyncio logger filter。"""
        import asyncio

        def handler(loop, context):
            exc = context.get("exception")
            name = type(exc).__name__ if exc is not None else ""
            msg = str(context.get("message") or "")
            extra = f"{msg} {exc!r}"
            if is_playwright_closed_noise(msg, extra=extra, exc_name=name):
                return
            loop.default_exception_handler(context)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.set_exception_handler(handler)

    def _browser(
        self,
        sn: str,
        *,
        headed: Optional[bool] = None,
        web_resolution: str = DEFAULT_WEB_RESOLUTION,
    ):
        resolution = normalize_web_resolution(web_resolution)
        viewport = web_viewport(resolution)
        key = _browser_key(sn, resolution)
        head = headed_from_env() if headed is None else bool(headed)
        browsers: dict = getattr(self._local, "browsers", None) or {}
        modes: dict = getattr(self._local, "browser_headed", None) or {}
        browser = browsers.get(key)
        if browser is not None:
            if modes.get(key) != head:
                browser.close()
                if browser.is_connected():
                    raise RuntimeError("browser_close_unconfirmed")
                browsers.pop(key, None)
                browser = None
            else:
                try:
                    if browser.is_connected():
                        return browser
                except Exception:
                    pass
        pw = self._playwright()
        launch_args = [
            "--disable-dev-shm-usage",
            "--force-device-scale-factor=1",
        ]
        if head:
            launch_args.extend(
                [
                    f"--window-size={viewport['width']},{viewport['height']}",
                    "--window-position=80,40",
                ]
            )
        browser = pw.chromium.launch(
            headless=not head,
            args=launch_args,
        )
        browsers[key] = browser
        modes[key] = head
        self._local.browsers = browsers
        self._local.browser_headed = modes
        SLog.i(TAG, f"chromium launched sn={key} headed={head}")
        return browser

    def open_case(
        self,
        sn: str,
        *,
        run_id: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
        base_url: str = "",
        headed: Optional[bool] = None,
        goto_params: dict[str, Any] | None = None,
        web_resolution: str = DEFAULT_WEB_RESOLUTION,
        credential: dict[str, Any] | None = None,
        web_options: dict[str, Any] | None = None,
        evidence_sink=None,
    ) -> Any:
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        key = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        resolution = normalize_web_resolution(web_resolution)
        viewport = web_viewport(resolution)
        web_options = validate_context_options(web_options)
        # A fresh context replaces only this actor/context. Other actors in the
        # same run stay alive and retain their independent authentication.
        self.close_case(sn, run_id=run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        browser = self._browser(_session_key(sn), headed=headed, web_resolution=resolution)
        browser_context_options = context_kwargs(web_options)
        context = browser.new_context(
            viewport=viewport,
            screen=viewport,
            device_scale_factor=1,
            ignore_https_errors=True,
            **browser_context_options,
            **({"storage_state": credential["storage_state"]} if credential else {}),
        )
        try:
            page = context.new_page()
            # Clock and listeners must exist before authentication injection or
            # the first application request, otherwise early evidence is lost.
            apply_context_options(context, page, web_options)
            observability = WebObservability(
                page,
                {"actor": actor_v or "default", "context_id": context_v or "default", "page_id": page_v or "main"},
                dict(web_options or {}),
                evidence_sink=evidence_sink,
            )
            url = normalize_goto_url(base_url)
            if credential and credential.get("session_storage"):
                # Chromium script exists only for the first navigation. Later logout,
                # refresh and token rotation must not resurrect the saved baseline.
                import json
                cdp = context.new_cdp_session(page)
                script = "const saved = " + json.dumps(credential["session_storage"], ensure_ascii=True) + ";" + \
                    "if (saved[location.origin]) { for (const [k,v] of Object.entries(saved[location.origin])) sessionStorage.setItem(k,v); }"
                identifier = cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": "(() => {" + script + "})()"})["identifier"]
                try:
                    if url:
                        goto_url(page, url, params=goto_params)
                finally:
                    try:
                        cdp.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})
                    finally:
                        cdp.detach()
            elif url:
                goto_url(page, url, params=goto_params)
        except BaseException as exc:
            if 'observability' in locals():
                observability.record_navigation_error(exc, base_url)
            try:
                context.close()
            except Exception:
                pass
            if credential and isinstance(exc, Exception):
                raise RuntimeError("web_auth_restore_failed") from None
            raise
        self._publish_session(
            key,
            {
                "context": context,
                "page": page,
                "base_url": url,
                "web_resolution": resolution,
                "sn": str(sn or ""),
                "run_id": str(run_id or ""),
                "actor": actor_v,
                "context_id": context_v,
                "page_id": page_v,
                "web_options": dict(web_options or {}),
                "observability": observability,
            },
            run_id=str(run_id or ""),
        )
        SLog.i(TAG, f"case context ready sn={key} resolution={resolution} url={url or 'about:blank'}")
        return page

    def note_lane(self, run_id: str) -> None:
        """本线程开始执行这条 run 时记下代次。超时换线程后再记一次新的。"""
        rid = str(run_id or "")
        with self._lock:
            token = int(self._run_tokens.get(rid, 0))
        self._local.lane_run = rid
        self._local.lane_token = token

    def retire_lane(self, run_id: str) -> None:
        """这条 run 的页面作废。旧线程稍后回来也不能把页面写回去。"""
        self._bump_runs([str(run_id or "")])

    def retire_owner(self, owner: int) -> None:
        """这条线程上打开的页面都作废。同一条 shard 上的其它 run 也不能再用它的 page。"""
        ident = int(owner or 0)
        if not ident:
            return
        with self._lock:
            run_ids = []
            for key, row in list(self._sessions.items()):
                if int(row.get("owner_thread") or 0) != ident:
                    continue
                run_ids.append(str(row.get("run_id") or ""))
                self._pressed_keys.pop(key, None)
                self._sessions.pop(key, None)
        self._bump_runs(run_ids)

    def _bump_runs(self, run_ids: list[str]) -> None:
        with self._lock:
            for rid in run_ids:
                rid = str(rid or "")
                self._run_tokens[rid] = int(self._run_tokens.get(rid, 0)) + 1
                if not rid:
                    self._sessions.clear()
                    self._pressed_keys.clear()
                    continue
                for key in [k for k, row in self._sessions.items()
                            if str(row.get("run_id") or "") == rid]:
                    self._pressed_keys.pop(key, None)
                    self._sessions.pop(key, None)

    def release_this_lane(self) -> bool:
        """只关本线程启动的浏览器。别的线程上的页面不动。"""
        return self.shutdown_thread()

    def _lane_open(self, run_id: str) -> bool:
        rid = str(run_id or "")
        local_run = str(getattr(self._local, "lane_run", "") or "")
        local_token = getattr(self._local, "lane_token", None)
        if local_token is None or local_run != rid:
            return True
        with self._lock:
            return int(self._run_tokens.get(rid, 0)) == int(local_token)

    def _replace_page(self, key: str, page: Any, *, run_id: str) -> None:
        if not self._lane_open(run_id):
            return
        token = int(getattr(self._local, "lane_token", 0) or 0)
        with self._lock:
            row = self._sessions.get(key)
            if not row:
                return
            if int(row.get("lane_token") or 0) != token:
                return
            row["page"] = page
            row["owner_thread"] = threading.get_ident()

    def _publish_session(self, key: str, row: dict[str, Any], *, run_id: str) -> None:
        rid = str(run_id or "")
        if not self._lane_open(rid):
            SLog.w(TAG, f"丢弃过期页面写入 sn={key}")
            return
        row["owner_thread"] = threading.get_ident()
        row["lane_token"] = int(getattr(self._local, "lane_token", 0) or 0)
        with self._lock:
            if int(self._run_tokens.get(rid, 0)) != int(row["lane_token"]):
                return
            self._sessions[key] = row

    def current_page(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> Any:
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        key = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        rid = str(run_id or "")
        with self._lock:
            row = dict(self._sessions.get(key) or {})
            token = int(self._run_tokens.get(rid, 0))
        if not row:
            return None
        if "lane_token" in row and int(row.get("lane_token") or 0) != token:
            return None
        return row.get("page")

    def keyboard_down(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        key: str,
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> None:
        """Press and retain one key for the actor/context lease."""
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        session = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        with self._lock:
            row = dict(self._sessions.get(session) or {})
            held = set(self._pressed_keys.get(session) or set())
        page = row.get("page")
        if page is None:
            raise RuntimeError("web_session_missing")
        if key in held:
            raise RuntimeError("web_key_already_down")
        page.keyboard.down(key)
        with self._lock:
            self._pressed_keys.setdefault(session, set()).add(key)

    def keyboard_up(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        key: str,
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> None:
        """Release a key that this lease explicitly pressed."""
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        session = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        with self._lock:
            row = dict(self._sessions.get(session) or {})
            held = set(self._pressed_keys.get(session) or set())
        if key not in held:
            raise RuntimeError("web_key_not_down")
        page = row.get("page")
        if page is None:
            raise RuntimeError("web_session_missing")
        page.keyboard.up(key)
        with self._lock:
            values = self._pressed_keys.get(session)
            if values is not None:
                values.discard(key)
                if not values:
                    self._pressed_keys.pop(session, None)

    def release_pressed_keys(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> list[str]:
        """Best-effort release for every key held by this actor/context lease."""
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        session = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        with self._lock:
            keys = set(self._pressed_keys.pop(session, set()) or set())
            row = dict(self._sessions.get(session) or {})
        return self._release_key_set(row.get("page"), keys)

    @staticmethod
    def _release_key_set(page: Any, keys: set[str]) -> list[str]:
        failures: list[str] = []
        if page is None:
            return sorted(keys)
        for key in sorted(keys):
            try:
                page.keyboard.up(key)
            except Exception:
                failures.append(key)
        return failures

    def current_observability(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> WebObservability | None:
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        key = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        with self._lock:
            row = self._sessions.get(key) or {}
        obs = row.get("observability")
        page = row.get("page")
        if obs is not None and page is not None:
            obs.attach_page(page)
            obs.page = page
        return obs

    def current_url(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> str:
        page = self.current_page(
            sn,
            run_id=run_id,
            actor=actor,
            context_id=context_id,
            page_id=page_id,
        )
        if page is None:
            return ""
        try:
            return str(page.url or "")
        except Exception:
            return ""

    def _clip_from_params(self, params: dict[str, Any] | None) -> dict[str, float] | None:
        p = dict(params or {})
        clip = p.get("clip")
        if isinstance(clip, dict) and all(k in clip for k in ("x", "y", "width", "height")):
            return {
                "x": float(clip["x"]),
                "y": float(clip["y"]),
                "width": float(clip["width"]),
                "height": float(clip["height"]),
            }
        region = p.get("region")
        if isinstance(region, (list, tuple)) and len(region) >= 4:
            x1, y1, x2, y2 = [float(region[i]) for i in range(4)]
            return {"x": x1, "y": y1, "width": max(1.0, x2 - x1), "height": max(1.0, y2 - y1)}
        return None

    def screenshot_png(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        timeout_ms: int = 8_000,
        base_url: str = "",
        headed: Optional[bool] = None,
        screenshot_params: dict[str, Any] | None = None,
        web_resolution: str = DEFAULT_WEB_RESOLUTION,
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> bytes:
        page = self.current_page(
            sn,
            run_id=run_id,
            actor=actor,
            context_id=context_id,
            page_id=page_id,
        )
        if page is None:
            page = self.open_case(
                sn,
                run_id=run_id,
                actor=actor,
                context_id=context_id,
                page_id=page_id,
                base_url=base_url,
                headed=headed,
                web_resolution=web_resolution,
            )
        if page is None:
            raise RuntimeError("playwright 当前没有打开的页面")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=500)
        except Exception:
            pass
        p = dict(screenshot_params or {})
        cap_ms = max(2000, min(int(p.get("timeout_ms") or timeout_ms or 8_000), 30_000))
        # Web 默认截取完整文档；显式 full_page=False 可退化为当前视口。
        full_page = bool(p["full_page"] if "full_page" in p else p.get("fullPage", True))
        clip = self._clip_from_params(p)
        kwargs: dict[str, Any] = {
            "type": "png",
            "full_page": full_page,
            "scale": "css",
            "animations": "disabled",
            "caret": "hide",
            "timeout": cap_ms,
        }
        from mino_scout.runtime.secret_redaction import has_sensitive_inputs
        kwargs["mask"] = [page.locator('input[type="password"]')]
        if has_sensitive_inputs(run_id):
            kwargs["mask"].append(page.locator('input, textarea, [contenteditable="true"]'))
        if clip and not full_page:
            kwargs["clip"] = clip
        return page.screenshot(**kwargs)

    def list_tabs(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> list[dict[str, Any]]:
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        key = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        with self._lock:
            row = self._sessions.get(key) or {}
        ctx = row.get("context")
        active = row.get("page")
        if ctx is None:
            return []
        out: list[dict[str, Any]] = []
        try:
            pages = list(ctx.pages)
        except Exception:
            pages = [active] if active else []
        for i, pg in enumerate(pages):
            if pg is None:
                continue
            try:
                out.append(
                    {
                        "index": i,
                        "url": str(pg.url or ""),
                        "title": str(pg.title() or ""),
                        "active": pg is active,
                    }
                )
            except Exception:
                out.append({"index": i, "url": "", "title": "", "active": pg is active})
        return out

    def switch_tab(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        tab_index: int | None = None,
        url_contains: str = "",
        title_contains: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> Any:
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        key = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        with self._lock:
            row = self._sessions.get(key) or {}
        ctx = row.get("context")
        if ctx is None:
            raise RuntimeError("无浏览器上下文")
        pages = list(ctx.pages)
        target = None
        if tab_index is not None:
            idx = int(tab_index)
            if 0 <= idx < len(pages):
                target = pages[idx]
        needle_url = str(url_contains or "").strip()
        needle_title = str(title_contains or "").strip()
        if target is None and (needle_url or needle_title):
            for pg in pages:
                try:
                    if needle_url and needle_url not in str(pg.url or ""):
                        continue
                    if needle_title and needle_title not in str(pg.title() or ""):
                        continue
                    target = pg
                    break
                except Exception:
                    continue
        if target is None:
            raise RuntimeError("未找到目标 Tab")
        # Keyboard state is page-scoped in Playwright. Release it on the outgoing
        # page before changing the active tab.
        self.release_pressed_keys(
            sn, run_id=run_id, actor=actor_v, context_id=context_v, page_id=page_v
        )
        try:
            target.bring_to_front()
        except Exception:
            pass
        self._replace_page(key, target, run_id=str(run_id or ""))
        self.current_observability(
            sn, run_id=run_id, actor=actor_v, context_id=context_v, page_id=page_v
        )
        return target

    def open_tab(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        url: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> Any:
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        key = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        with self._lock:
            row = self._sessions.get(key) or {}
        ctx = row.get("context")
        if ctx is None:
            raise RuntimeError("无浏览器上下文")
        self.release_pressed_keys(
            sn, run_id=run_id, actor=actor_v, context_id=context_v, page_id=page_v
        )
        raw_dest = str(url or "").strip()
        dest = normalize_goto_url(raw_dest)
        if raw_dest and not dest:
            raise ValueError("web_uri_invalid")
        page = ctx.new_page()
        try:
            if dest:
                goto_url(page, dest)
        except BaseException:
            try:
                page.close()
            except Exception:
                pass
            raise
        self._replace_page(key, page, run_id=str(run_id or ""))
        self.current_observability(
            sn, run_id=run_id, actor=actor_v, context_id=context_v, page_id=page_v
        )
        return page

    def a11y_text(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        max_chars: int = 4000,
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> str:
        page = self.current_page(
            sn,
            run_id=run_id,
            actor=actor,
            context_id=context_id,
            page_id=page_id,
        )
        if page is None:
            return ""
        try:
            snap = page.locator("body").aria_snapshot()
        except Exception:
            try:
                snap = page.inner_text("body")
            except Exception as exc:
                return f"(a11y 失败: {exc})"
        text = str(snap or "").strip()
        if len(text) > max_chars:
            return text[:max_chars]
        return text

    def _session_keys_for_sn(self, sn: str) -> list[str]:
        prefix = _session_key(sn) + "::"
        bare = _session_key(sn)
        with self._lock:
            keys = list(self._sessions.keys())
        out = [k for k in keys if k == bare or k.startswith(prefix)]
        return out

    def _close_keys(self, keys: list[str]) -> bool:
        with self._lock:
            rows = [(key, self._sessions.pop(key, None)) for key in keys]
        closed = True
        for session, row in rows:
            if not row:
                with self._lock:
                    self._pressed_keys.pop(session, None)
                continue
            page = row.get("page")
            ctx = row.get("context")
            obs = row.get("observability")
            with self._lock:
                held = set(self._pressed_keys.pop(session, set()) or set())
            self._release_key_set(page, held)
            if obs is not None:
                obs.flush_summary()
            for obj in (page, ctx):
                if obj is None:
                    continue
                try:
                    obj.close()
                except Exception:
                    closed = False
        return closed

    def close_case(
        self,
        sn: str = "",
        *,
        run_id: str = "",
        actor: str = "",
        context_id: str = "",
        page_id: str = "",
    ) -> bool:
        """Close exactly one actor/context/page session."""
        actor_v, context_v, page_v = normalize_web_identity(actor, context_id, page_id)
        key = _session_key(sn, run_id, actor=actor_v, context_id=context_v, page_id=page_v)
        return self._close_keys([key])

    def close_run(self, sn: str, run_id: str = "", *, shutdown_browser: bool = False) -> None:
        """Close every actor/context/page of this application and run."""
        rid = str(run_id or "")
        with self._lock:
            keys = [key for key, row in self._sessions.items()
                    if str(row.get("sn") or "") == str(sn or "")
                    and str(row.get("run_id") or "") == rid]
        self._close_keys(keys)
        if not shutdown_browser:
            return
        if self._session_keys_for_sn(sn):
            return
        sn_key = _session_key(sn)
        browsers: dict = getattr(self._local, "browsers", None) or {}
        modes: dict = getattr(self._local, "browser_headed", None) or {}
        self._local.browsers = browsers
        self._local.browser_headed = modes
        keys = [k for k in browsers if k == sn_key or k.startswith(sn_key + "::")]
        for key in keys:
            browser = browsers.get(key)
            if browser is None:
                continue
            try:
                browser.close()
                if not browser.is_connected():
                    browsers.pop(key, None)
                    modes.pop(key, None)
            except Exception:
                # Retain the handle for final shutdown/review; losing it would
                # make a later cleanup falsely report that no browser remains.
                pass

    def release_run(self, run_id: str) -> bool:
        """任务 cancel / 结束：关闭该 run_id 下所有会话并返回关闭回执。"""
        rid = str(run_id or "").strip()
        if not rid:
            return True
        with self._lock:
            keys = [key for key, row in self._sessions.items()
                    if str(row.get("run_id") or "") == rid]
        return self._close_keys(keys)

    def shutdown_thread(self) -> bool:
        closed = True
        with self._lock:
            keys = [key for key, row in self._sessions.items()
                    if int(row.get("owner_thread") or 0) == threading.get_ident()]
        closed = self._close_keys(keys) and closed
        browsers: dict = getattr(self._local, "browsers", None) or {}
        remaining = {}
        for sn, browser in list(browsers.items()):
            try:
                if browser.is_connected():
                    browser.close()
                if browser.is_connected():
                    remaining[sn] = browser
                    closed = False
            except Exception:
                remaining[sn] = browser
                closed = False
        self._local.browsers = remaining
        self._local.browser_headed = {key: value for key, value in
            (getattr(self._local, "browser_headed", None) or {}).items() if key in remaining}
        pw = getattr(self._local, "pw", None)
        if pw is not None:
            try:
                pw.stop()
                self._local.pw = None
            except Exception:
                closed = False
            SLog.i(TAG, f"playwright stopped thread={threading.get_ident()}")
        return closed


_HUB: Optional[PlaywrightHub] = None
_HUB_LOCK = threading.Lock()


def get_hub() -> PlaywrightHub:
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = PlaywrightHub()
        return _HUB
