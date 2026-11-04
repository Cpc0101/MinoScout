"""屏幕采集。

port(scout): 源自 MiniOrangeServer `server/services/regression/screen.py`（578 行）。

四种通道都已实现：adb / ios_wda / playwright / macOS·Windows 原生。按设备平台分派，
**不做多通道回退** —— 选哪条通道是 Nexus 的事。

与上游的差异：
  - `CapturedScreen` 移到 `mino_scout.schemas`（要进 RESULT 载荷，改成 pydantic）。
    上游的 `image_path` 字段在本仓叫 `path`。
  - `capture_screen(ctx: RunContext, ...)` 的入口签名改成 `capture(device)`
    —— Scout 没有 RunContext（那是 Nexus 的）。
  - Web 压缩比上游从 `system_settings_service.get_ai_web_compress_ratio()` 读（Nexus 设置），
    本仓改为由 `OBSERVE.compress_ratio` 下发（协议 §4.4）。
  - **不做缩略图。** 上游在 router 里 `make_thumb`；拆分后缩略图归 Nexus
    （它才服务 UI）。Scout 回原图。
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from io import BytesIO
from typing import Any, Optional

from mino_scout.executors.base import DeviceRef
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.schemas import CapturedScreen

TAG = "ScreenCapture"

# serial → 最近一次成功截图的像素尺寸。input 坐标空间若与截图不一致（wm override），
# AdbExecutor 用它来缩放 tap，避免点到屏幕外。
_LAST_CAPTURE_SIZE: dict[str, tuple[int, int]] = {}


def last_capture_size(serial: str) -> tuple[int, int]:
    return _LAST_CAPTURE_SIZE.get(str(serial or "")) or (0, 0)


def remember_capture_size(serial: str, width: int, height: int) -> None:
    if serial and width > 0 and height > 0:
        _LAST_CAPTURE_SIZE[str(serial)] = (int(width), int(height))


def peek_png_size(data: bytes) -> tuple[int, int]:
    """PNG 文件头里取 width/height。失败返回 (0,0)。

    port: 上游 `screen.py::_peek_png_size`，逐字一致。
    """
    try:
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return 0, 0
        # IHDR length=13 starts at byte 8; width @16-19, height @20-23
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h
    except Exception:
        return 0, 0


# ---------- 空白帧检测 ----------


def shot_is_blank(shot: Any, *, white_threshold: float = 244.0, std_threshold: float = 14.0) -> bool:
    """检测全白/全黑过渡帧。

    port(scout): 源自上游 `shared/screenshot/regression_capture.py::shot_is_blank`，
    判据逐条一致（只把入参从 PIL Image 放宽到"能被 np.asarray 吃下的东西"）。

    为什么必须有：真机息屏时 `adb exec-out screencap` **照样返回一张合法 PNG**，
    只是全黑。不检测就会把黑屏当有效帧回给 Nexus，VLM 拿着黑图做决策。
    （在 vivo V2528A 上实测过：息屏截图 mean≈0，亮屏后 mean≈161。）
    """
    if shot is None:
        return True
    try:
        import numpy as np

        arr = np.asarray(shot)
        if arr.size == 0:
            return True
        gray = arr.mean(axis=2) if arr.ndim == 3 else arr.astype(float)
        mean = float(gray.mean())
        std = float(gray.std())
        if mean >= white_threshold and std <= std_threshold:
            return True
        if mean <= 18.0 and std <= std_threshold:
            return True
        return False
    except Exception:
        return False


def png_is_blank(png_bytes: bytes) -> bool:
    """PNG 字节版。缺 numpy/pillow 时保守返回 False（不误判成空白）。"""
    try:
        from PIL import Image

        return shot_is_blank(Image.open(BytesIO(png_bytes)).convert("RGB"))
    except Exception:
        return False


def compress_web_png(png_bytes: bytes, ratio: float) -> tuple[bytes, str, int, int]:
    """按压缩比缩小截图。返回 (bytes, mime, orig_w, orig_h)。

    port: 上游 `screen.py::_compress_web_png`，逻辑逐条对齐。

    **width/height 返回的始终是原图尺寸** —— 坐标体系按真实视口，不受压缩影响。
    这一点写进了协议 §4.4，改动会破坏 Nexus 的千分比坐标换算。
    """
    orig_w, orig_h = peek_png_size(png_bytes)
    if ratio <= 1.0 or not png_bytes:
        return png_bytes, "image/png", orig_w, orig_h
    try:
        from PIL import Image

        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        orig_w, orig_h = img.size
        preview_w = max(1, round(orig_w / ratio))
        preview_h = max(1, round(orig_h / ratio))
        if preview_w < orig_w or preview_h < orig_h:
            img = img.resize((preview_w, preview_h), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue(), "image/jpeg", orig_w, orig_h
    except Exception as exc:
        SLog.w(TAG, f"web screenshot compress failed: {exc}")
        return png_bytes, "image/png", orig_w, orig_h


# ---------- 对外入口 ----------


def _platform_capture(device):
    name = str(getattr(device, "platform", "") or "")
    if name == "android":
        from mino_scout.platform.android.screen import capture as take
    elif name == "ios":
        from mino_scout.platform.ios.screen import capture as take
    elif getattr(device, "is_web", False) or name == "web":
        from mino_scout.platform.web.screen import capture as take
    elif name == "mac":
        from mino_scout.platform.mac.actions import capture as take
    elif name == "win":
        from mino_scout.platform.win.actions import capture as take
    else:
        take = None
    return take


def capture(
    device: DeviceRef,
    *,
    timeout_sec: float = 15.0,
    compress_ratio: float = 2.0,
    allow_blank: bool = False,
    screenshot_params: dict[str, Any] | None = None,
) -> CapturedScreen:
    """按设备平台调用该平台自己的截图。"""
    started = time.time()
    take = _platform_capture(device)
    if take is None:
        shot = CapturedScreen(ok=False, source="screen", error="设备平台没有实现截图通道")
    else:
        shot = take(device, timeout_sec=timeout_sec, compress_ratio=compress_ratio, screenshot_params=screenshot_params)

    if shot.has_image():
        if device.platform in {"mac", "win"} or allow_blank or not png_is_blank(base64.b64decode(shot.image_base64)):
            SLog.i(
                TAG,
                f"capture ok via {shot.source} {shot.width}x{shot.height} "
                f"mime={shot.image_mime} bytes={len(shot.image_base64)} {shot.elapsed_ms}ms",
            )
            return shot
        shot.remote_detail = {**dict(shot.remote_detail or {}), "blank": True}
        shot.error = "截到空白帧（设备可能息屏）"
        SLog.w(TAG, shot.error)
        return shot

    if not shot.elapsed_ms:
        shot.elapsed_ms = int((time.time() - started) * 1000)
    SLog.w(TAG, f"capture via {shot.source} 失败: {shot.error}")
    return shot




def cleanup(shot: Optional[CapturedScreen]) -> None:
    """删掉落盘的原图。"""
    if shot is None or not shot.path:
        return
    try:
        os.remove(shot.path)
    except OSError:
        pass
