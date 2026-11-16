"""Web 截图。"""
from __future__ import annotations

import base64
import os
import tempfile
import time
from typing import Any

from mino_scout.platform.web.playwright_hub import get_hub, headed_from_hint, pick_goto_url
from mino_scout.platform.web.viewports import DEFAULT_WEB_RESOLUTION
from mino_scout.schemas import CapturedScreen
from mino_scout.screen import compress_web_png


def capture(device, *, timeout_sec: float = 15.0, compress_ratio: float = 2.0,
            screenshot_params: dict[str, Any] | None = None, **_ignored) -> CapturedScreen:
    started = time.time()
    url = pick_goto_url(device.extra.get("target_package"), device.extra.get("url"))
    try:
        png_bytes = get_hub().screenshot_png(
            str(device.sn or ""),
            run_id=str(device.extra.get("run_id") or ""),
            timeout_ms=int(timeout_sec * 1000),
            base_url=url,
            headed=headed_from_hint(dict(device.extra or {})),
            screenshot_params=screenshot_params,
            web_resolution=str(device.extra.get("web_resolution") or DEFAULT_WEB_RESOLUTION),
        )
    except Exception as exc:
        return CapturedScreen(
            ok=False, source="playwright", error=f"playwright screenshot failed: {exc}",
            elapsed_ms=int((time.time() - started) * 1000),
        )
    elapsed_ms = int((time.time() - started) * 1000)
    if not png_bytes:
        return CapturedScreen(ok=False, source="playwright", error="empty screenshot", elapsed_ms=elapsed_ms)
    out, mime, width, height = compress_web_png(png_bytes, compress_ratio)
    suffix = ".jpg" if mime == "image/jpeg" else ".png"
    fd, path = tempfile.mkstemp(prefix="screen_web_", suffix=suffix)
    try:
        os.write(fd, out)
    finally:
        os.close(fd)
    return CapturedScreen(
        ok=True, source="playwright", path=path,
        image_base64=base64.b64encode(out).decode("ascii"), image_mime=mime,
        width=width, height=height, elapsed_ms=elapsed_ms,
    )
