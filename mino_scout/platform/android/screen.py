"""Android 截图。"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time

from mino_scout.schemas import CapturedScreen


def capture(device, *, timeout_sec: float = 15.0, compress_ratio: float = 1.0, **_ignored) -> CapturedScreen:
    from mino_scout.platform.web.playwright_hub import is_web_slot
    from mino_scout.screen import compress_web_png, remember_capture_size

    adb_serial = str(getattr(device, "adb_serial", "") or "")
    if not adb_serial or is_web_slot(adb_serial):
        return CapturedScreen(ok=False, source="adb", error="invalid adb serial")
    started = time.time()
    try:
        proc = subprocess.run(
            ["adb", "-s", adb_serial, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return CapturedScreen(ok=False, source="adb", error="adb binary not in PATH")
    except subprocess.TimeoutExpired:
        return CapturedScreen(ok=False, source="adb", error=f"adb screencap timeout {timeout_sec}s")
    except Exception as exc:
        return CapturedScreen(ok=False, source="adb", error=f"adb screencap failed: {exc}")
    elapsed_ms = int((time.time() - started) * 1000)
    if proc.returncode != 0 or not proc.stdout:
        return CapturedScreen(
            ok=False, source="adb",
            error=f"adb screencap rc={proc.returncode} stderr={(proc.stderr or b'')[:200]!r}",
            elapsed_ms=elapsed_ms,
        )
    out, mime, width, height = compress_web_png(proc.stdout, compress_ratio)
    suffix = ".jpg" if mime == "image/jpeg" else ".png"
    fd, path = tempfile.mkstemp(prefix=f"screen_{adb_serial}_", suffix=suffix)
    try:
        os.write(fd, out)
    finally:
        os.close(fd)
    remember_capture_size(adb_serial, width, height)
    return CapturedScreen(
        ok=True, source="adb", path=path,
        image_base64=base64.b64encode(out).decode("ascii"), image_mime=mime,
        width=width, height=height, elapsed_ms=elapsed_ms,
    )
