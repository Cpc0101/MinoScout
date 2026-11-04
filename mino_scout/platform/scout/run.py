"""Scout 进程里完成的脚本动作。不进各个设备渠道。"""
from __future__ import annotations

import time


def prep(case, number: int):
    from mino_scout.runtime.script_exec import _Scope

    return _Scope(case, number if isinstance(number, str) else int(number), "prep")


def do(case, number: int):
    from mino_scout.runtime.script_exec import _Scope

    return _Scope(case, number if isinstance(number, str) else int(number), "do")


def check(case, number: int):
    from mino_scout.runtime.script_exec import _Scope

    return _Scope(case, number if isinstance(number, str) else int(number), "check")


def wait(case, ms: int):
    from mino_scout.runtime.script_exec import ActionResult

    case._guard()
    duration = max(0, int(ms))
    end = time.monotonic() + duration / 1000
    while True:
        case._guard()
        remain = end - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(0.1, remain))
    result = ActionResult(ok=True, summary=f"等待 {duration} ms", status="pass", data={})
    case._report("wait", result, "core", duration)
    return result


def sample(case, duration_ms: int, interval_ms: int = 300) -> dict:
    duration = max(0, min(int(duration_ms), 30_000))
    interval = max(200, min(int(interval_ms), 5_000))
    started = time.perf_counter()
    deadline = started + duration / 1000
    frames: list[dict] = []
    case._frames = []
    while len(frames) < 40:
        case._guard()
        shot_started = time.perf_counter()
        result = case.screenshot()
        frames.append({"index": len(frames), "ok": result.ok, "width": result.data.get("width", 0), "height": result.data.get("height", 0)})
        case._frames.append(case._shot)
        if time.perf_counter() >= deadline:
            break
        remain = interval / 1000 - (time.perf_counter() - shot_started)
        if remain > 0:
            time.sleep(remain)
    return {"frames": frames, "elapsed_ms": int((time.perf_counter() - started) * 1000)}


def on_page(case, page_key: str):
    from mino_scout.runtime.script_exec import ActionResult, ScriptAbort, _EXECUTOR, _anchors_match, _visible_text

    case._guard()
    page = case._pages.get(str(page_key or "").strip())
    if not isinstance(page, dict):
        case._report("on_page", ActionResult(ok=False, summary="control_unknown", status="fail"), "script", 0)
        raise ScriptAbort("control_unknown", reason_code="control_unknown")
    case._require_channel()
    case.touched = True
    status, fields = case.core._hierarchy_fields(case._device_ref(), run_id=case.run_id)
    if str(getattr(status, "value", status)) != "pass":
        raise ScriptAbort(str(fields.get("error") or "没有读到界面结构"), reason_code="script_failed")
    if not _anchors_match(page, _visible_text(fields)):
        raise ScriptAbort("页面锚点未对上", reason_code="page_mismatch")
    case._page_key = str(page.get("page_key") or page_key)
    result = ActionResult(ok=True, summary=f"已在 {case._page_key}", status="pass", data={})
    case._report("on_page", result, _EXECUTOR.get(case._platform, "core"), int(fields.get("elapsed_ms") or 0))
    return result


def fail(case, message: str) -> None:
    from mino_scout.runtime.script_exec import ScriptAbort

    text = str(message or "脚本判定失败")
    case._callback("script.callback", {"op": "fail", "message": text}, 15)
    raise ScriptAbort(text, reason_code="script_failed")
