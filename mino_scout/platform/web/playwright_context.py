"""Playwright 作用域：iframe 链、Shadow host、多 Tab 的 page 解析。"""
from __future__ import annotations

from typing import Any


def frame_chain_from_params(params: dict[str, Any] | None) -> list[str]:
    p = dict(params or {})
    raw = p.get("frame_path") or p.get("iframe_path")
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    one = str(
        p.get("frame_selector")
        or p.get("iframe_selector")
        or p.get("iframe")
        or ""
    ).strip()
    return [one] if one else []


def shadow_host_from_params(params: dict[str, Any] | None) -> str:
    p = dict(params or {})
    return str(p.get("shadow_host_css") or p.get("shadow_host") or "").strip()


def resolve_locator_scope(page: Any, params: dict[str, Any] | None) -> Any:
    """返回可作 locator / get_by_role 的根：Page、Frame、FrameLocator 或 Locator。"""
    scope: Any = page
    for sel in frame_chain_from_params(params):
        scope = scope.frame_locator(sel)
    frame_name = str((params or {}).get("frame_name") or "").strip()
    if frame_name and not frame_chain_from_params(params):
        fr = page.frame(name=frame_name)
        if fr is not None:
            scope = fr
    host = shadow_host_from_params(params)
    if host:
        try:
            loc = scope.locator(host) if hasattr(scope, "locator") else page.locator(host)
            if (params or {}).get("require_unique") and int(loc.count()) == 0:
                from mino_scout.runtime.script_exec import ScriptAbort
                raise ScriptAbort("动态对象作用域未找到", reason_code="locator_missing", status="blocked")
            if loc.count() > 0:
                if (params or {}).get("require_unique"):
                    if int(loc.count()) != 1:
                        from mino_scout.runtime.script_exec import ScriptAbort
                        raise ScriptAbort("动态对象作用域不唯一", reason_code="locator_ambiguous", status="blocked")
                    scope = loc
                else:
                    scope = loc.first
        except Exception:
            if (params or {}).get("require_unique"):
                raise
    container = str((params or {}).get("scope_css") or "").strip()
    if container:
        scope = scope.locator(container)
        if (params or {}).get("require_unique") and int(scope.count()) != 1:
            from mino_scout.runtime.script_exec import ScriptAbort
            raise ScriptAbort("动态对象作用域不唯一", reason_code="locator_ambiguous", status="blocked")
    return scope


def resolve_aria_root(page: Any, params: dict[str, Any] | None) -> Any:
    """aria_snapshot / DOM 采集根：默认主 frame 的 body。"""
    scope = resolve_locator_scope(page, params)
    if hasattr(scope, "locator"):
        return scope.locator("body")
    return page.locator("body")
