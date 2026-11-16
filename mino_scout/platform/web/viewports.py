"""Web 分辨率配置与输入规范化。

对外只使用 canonical 值；尺寸只在这里解释，避免调用方各写一份字面量。
"""
from __future__ import annotations

import re
from typing import Any

DEFAULT_WEB_RESOLUTION = "1080p"
WEB_RESOLUTIONS = ("720p", "1080p", "1440p", "2160p")
WEB_VIEWPORTS: dict[str, dict[str, Any]] = {
    "720p": {"label": "720P", "width": 1280, "height": 720},
    "1080p": {"label": "1080P", "width": 1920, "height": 1080},
    "1440p": {"label": "2K", "width": 2560, "height": 1440},
    "2160p": {"label": "3K/4K", "width": 3840, "height": 2160},
}

_ALIASES = {
    "720": "720p",
    "720p": "720p",
    "1280x720": "720p",
    "1080": "1080p",
    "1080p": "1080p",
    "1k": "1080p",
    "1920x1080": "1080p",
    "1440": "1440p",
    "1440p": "1440p",
    "2k": "1440p",
    "2560x1440": "1440p",
    "2160": "2160p",
    "2160p": "2160p",
    "3k": "2160p",
    "4k": "2160p",
    "3k/4k": "2160p",
    "3840x2160": "2160p",
}
_OPTIONS = "720P、1080P（兼容 1K）、2K、3K/4K，或尺寸 1280x720、1920x1080、2560x1440、3840x2160"


def normalize_web_resolution(value: Any = "") -> str:
    """把展示名、canonical 值或尺寸字符串归一化为 canonical 分辨率。"""
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_WEB_RESOLUTION
    key = re.sub(r"\s+", "", raw).lower().replace("×", "x").replace("*", "x")
    resolution = _ALIASES.get(key)
    if resolution:
        return resolution
    raise ValueError(f"不支持的 Web 分辨率: {raw}；可选 {_OPTIONS}")


def web_viewport(value: Any = "") -> dict[str, int]:
    resolution = normalize_web_resolution(value)
    row = WEB_VIEWPORTS[resolution]
    return {"width": int(row["width"]), "height": int(row["height"])}
