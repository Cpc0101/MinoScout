"""Web 页 DOM 快照 → 协议 `accessibility_json` 节点列表（非安卓 UI hierarchy）。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mino_scout.tools.keep_alive.log import SLog
from mino_scout.platform.web.playwright_context import resolve_aria_root
from mino_scout.platform.web.playwright_hub import get_hub
from mino_scout.platform.web.web_focus import evaluate_web_focus, mark_focused_node

TAG = "DomHierarchy"

_JS_COLLECT = Path(__file__).with_name("dom_collect.js").read_text(encoding="utf-8").strip()


@dataclass
class DomDump:
    ok: bool
    nodes: list[dict[str, Any]]
    error: str = ""
    elapsed_ms: int = 0
    web_focus: dict[str, Any] | None = None
    aria_snapshot: str = ""
    tabs: list[dict[str, Any]] | None = None
    observation: dict[str, Any] | None = None


def _aria_snapshot_text(page: Any, params: dict[str, Any] | None, *, max_chars: int) -> str:
    if not params or not params.get("include_aria_snapshot", True):
        return ""
    cap = max(500, min(int(params.get("aria_max_chars") or max_chars), 24_000))
    try:
        root = resolve_aria_root(page, params)
        snap = root.aria_snapshot()
        text = str(snap or "").strip()
    except Exception as exc:
        SLog.d(TAG, f"aria_snapshot skipped: {exc}")
        return ""
    if len(text) > cap:
        return text[:cap]
    return text


def dump_dom_nodes(
    *,
    sn: str,
    run_id: str = "",
    params: dict[str, Any] | None = None,
    actor: str = "",
    context_id: str = "",
    page_id: str = "",
) -> DomDump:
    t0 = time.time()
    hub = get_hub()
    page = hub.current_page(
        str(sn or ""), run_id=str(run_id or ""), actor=str(actor or ""),
        context_id=str(context_id or ""), page_id=str(page_id or ""),
    )
    if page is None:
        return DomDump(
            ok=False,
            nodes=[],
            error=f"web 槽无打开页面 sn={sn} run_id={run_id or '(empty)'}",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    p = dict(params or {})
    web_focus = evaluate_web_focus(page)
    try:
        raw = page.evaluate(_JS_COLLECT)
    except Exception as exc:
        SLog.w(TAG, f"dom evaluate failed sn={sn}: {exc}")
        return DomDump(
            ok=False,
            nodes=[],
            error=f"DOM 采集失败: {exc}",
            elapsed_ms=int((time.time() - t0) * 1000),
        )
    data = raw if isinstance(raw, dict) else {"nodes": raw or []}
    nodes = [n for n in data.get("nodes") or [] if isinstance(n, dict)]
    observation = dict(data.get("observation") or {})
    mark_focused_node(nodes, web_focus)
    aria = _aria_snapshot_text(page, p, max_chars=int(p.get("aria_max_chars") or 6000))
    tabs = hub.list_tabs(
        str(sn or ""), run_id=str(run_id or ""), actor=str(actor or ""),
        context_id=str(context_id or ""), page_id=str(page_id or ""),
    ) if p.get("include_tabs") else None
    try:
        page_url = str(page.url or "").strip()
    except Exception:
        page_url = ""
    if page_url:
        nodes.insert(
            0,
            {
                "resource_id": "__page__",
                "class": "document:html",
                "text": "",
                "content_desc": page_url[:500],
                "url": page_url,
                "page_url": page_url,
                "href": page_url,
                "clickable": False,
                "editable": False,
                "bounds": [0, 0, 0, 0],
                "center": [0, 0],
            },
        )
    elapsed = int((time.time() - t0) * 1000)
    SLog.i(TAG, f"dom dump sn={sn} nodes={len(nodes)} aria={len(aria)} ms={elapsed}")
    return DomDump(
        ok=True,
        nodes=nodes,
        elapsed_ms=elapsed,
        web_focus=web_focus,
        aria_snapshot=aria,
        tabs=tabs,
        observation=observation,
    )
