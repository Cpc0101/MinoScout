"""Web 执行：视口坐标解析（千分比 0–1000 或像素），不依赖 DOM 定位。"""
from __future__ import annotations

from typing import Any

from mino_scout.platform.web.viewports import DEFAULT_WEB_RESOLUTION, web_viewport


_DEFAULT_WEB_VIEWPORT = web_viewport(DEFAULT_WEB_RESOLUTION)


def viewport_wh(page: Any) -> tuple[int, int]:
    box = page.viewport_size or _DEFAULT_WEB_VIEWPORT
    return int(box["width"]), int(box["height"])


def to_viewport_xy(x: Any, y: Any, page: Any) -> tuple[int, int]:
    w, h = viewport_wh(page)
    xi, yi = int(x), int(y)
    if 0 <= xi <= 1000 and 0 <= yi <= 1000 and w > 1000:
        xi = int(round(xi / 1000.0 * w))
        yi = int(round(yi / 1000.0 * h))
        return max(0, min(w - 1, xi)), max(0, min(h - 1, yi))
    if xi < 0 or yi < 0 or xi >= w or yi >= h:
        raise ValueError(f"坐标超出视口 ({xi},{yi})，视口 {w}x{h}")
    return xi, yi


def resolve_viewport_xy(params: dict[str, Any] | None, page: Any) -> tuple[int, int]:
    """从 params.x/y 或 fallback_xy 解析为视口像素坐标。"""
    p = dict(params or {})
    if p.get("x") is not None and p.get("y") is not None:
        return to_viewport_xy(p["x"], p["y"], page)
    raw = p.get("fallback_xy")
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return to_viewport_xy(raw[0], raw[1], page)
    raise ValueError("缺坐标 x/y 或 fallback_xy")
