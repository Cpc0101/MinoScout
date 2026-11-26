"""Scout 进程里完成的脚本动作。不进各个设备渠道。"""
from __future__ import annotations

import time


def prep(case, number: int, source_ref: str = ""):
    from mino_scout.runtime.script_exec import _Scope

    return _Scope(case, number if isinstance(number, str) else int(number), "prep", source_ref=source_ref)


def do(case, number: int, source_ref: str = ""):
    from mino_scout.runtime.script_exec import _Scope

    return _Scope(case, number if isinstance(number, str) else int(number), "do", source_ref=source_ref)


def check(case, number: int, source_ref: str = ""):
    from mino_scout.runtime.script_exec import _Scope

    return _Scope(case, number if isinstance(number, str) else int(number), "check", source_ref=source_ref)


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
    case._frame_refs = []
    while len(frames) < 40:
        case._guard()
        shot_started = time.perf_counter()
        result = case.screenshot()
        frames.append({"index": len(frames), "ok": result.ok, "width": result.data.get("width", 0), "height": result.data.get("height", 0)})
        case._frames.append(case._shot)
        case._frame_refs.append({"observation_id": case._observation_id, "artifact_id": case._artifact_id})
        if time.perf_counter() >= deadline:
            break
        remain = interval / 1000 - (time.perf_counter() - shot_started)
        if remain > 0:
            time.sleep(remain)
    return {"frames": frames, "elapsed_ms": int((time.perf_counter() - started) * 1000)}


def on_page(case, page_key: str):
    from mino_scout.runtime.script_exec import (
        ActionResult, ScriptAbort, _EXECUTOR, _anchors_match, _locator_nodes, _visible_text,
    )

    case._guard()
    page = case._pages.get(str(page_key or "").strip())
    if not isinstance(page, dict):
        case._report("on_page", ActionResult(ok=False, summary="control_unknown", status="fail"), "script", 0)
        raise ScriptAbort("control_unknown", reason_code="control_unknown")
    observed = case.hierarchy()
    fields = {"nodes": observed.data.get("nodes", []), "text": observed.data.get("text", "")}
    # 结构化锚点用这一屏刚读到节点，不再另采一份。
    page = {**page, "_live_nodes": _locator_nodes(fields)}
    matched = _anchors_match(page, _visible_text(fields))
    case.case._assert("on_page", "pass" if matched else "fail", expected=page_key,
                     actual={"matched": matched, "observed": _visible_text(fields)[:1000]},
                     observation_id=case._observation_id, app=case.key, reason_code="" if matched else "page_mismatch")
    if not matched:
        raise ScriptAbort("页面锚点未对上", reason_code="page_mismatch")
    case._page_key = str(page.get("page_key") or page_key)
    result = ActionResult(ok=True, summary=f"已在 {case._page_key}", status="pass", data={})
    case._report("on_page", result, _EXECUTOR.get(case._platform, "core"), int(fields.get("elapsed_ms") or 0))
    return result


def note(app, *messages: str, **fields) -> None:
    """记一条证据。expect 用它标出这次判定用的是哪一种依据。"""
    app.case.nexus.note(*messages, **fields)


def expect(app, *, page: str = "", prompt: str = "", verdict: str = "pass", allow_model: bool = False) -> str:
    """Page identity and business expectations are separate assertions.

    Only an inconclusive text observation may fall back to a fresh image.
    """
    from mino_scout.runtime.script_exec import ScriptAbort

    app._guard()
    text = str(prompt or "").strip()
    key = str(page or "").strip()
    if text and not allow_model:
        error = ScriptAbort("自然语言预期需要显式 allow_model=True 和 MODEL_POLICY；固定文字、状态、数量、值请使用确定性断言",
                            reason_code="model_policy_denied", status="blocked")
        app.case._assert("policy", "unknown", expected=text, message=str(error), reason_code=error.reason_code)
        raise error
    if key:
        app.on_page(key)
        if not text:
            return "on_page"
    if not text and not key:
        raise ScriptAbort("expect 必须给页面或业务预期", reason_code="assertion_missing", status="blocked")
    if text:
        observed = screen_text(app)
        try:
            answer = app.case.scout.llm(
                "只根据界面文字判定预期。成立只回答 yes，明确不成立只回答 no，证据不足只回答 unknown。不要解释。\n"
                f"预期：{text}", text=observed,
            ) if observed else "unknown"
        except ScriptAbort as exc:
            app.case._assert("llm", "unknown", expected=text, observation_id=app._observation_id,
                             app=app.key, message=str(exc), reason_code=exc.reason_code)
            raise ScriptAbort(str(exc), reason_code=exc.reason_code, status="blocked", details=exc.details) from exc
        verdict_text = str(answer or "").strip().lower()
        if verdict_text in {"yes", "true", "是", "成立", "no", "false", "否", "不成立"}:
            passed = verdict_text in {"yes", "true", "是", "成立"}
            app.case._assert("llm", "pass" if passed else "fail", expected=text,
                             actual=verdict_text, observation_id=app._observation_id, app=app.key)
            if not passed:
                raise ScriptAbort("界面文字不满足预期", reason_code="assertion_failed")
            return "llm"
    app.vlm(text or f"当前是否在 {key}", verdict=verdict)
    note(app, f"预期按截图判定：{text or key}", checker="vlm")
    return "vlm"


def screen_text(app) -> str:
    """当前这一屏的可读文字，给文本判定用。"""
    data = app.hierarchy().data or {}
    lines: list[str] = []
    for node in data.get("nodes") or []:
        for field in ("text", "content_desc"):
            value = str(node.get(field) or "").strip()
            if value and value not in lines:
                lines.append(value)
    if not lines:
        lines.append(str(data.get("text") or ""))
    return "\n".join(lines)[:4000]


def fail(case, message: str) -> None:
    from mino_scout.runtime.script_exec import ScriptAbort

    text = str(message or "脚本判定失败")
    case._callback("script.callback", {"op": "fail", "message": text}, 15)
    raise ScriptAbort(text, reason_code="script_failed")
