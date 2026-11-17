"""Normalize device-provided iOS XML and desktop accessibility facts."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET


def native_nodes(fields, platform):
    extra = fields.get("extra") or {}
    raw = fields.get("nodes") if isinstance(fields.get("nodes"), list) else extra.get("nodes")
    if isinstance(raw, list):
        return raw
    text = fields.get("hierarchy_text") or extra.get("hierarchy_text") or ""
    if platform == "ios":
        try:
            tree = ET.fromstring(text)
        except (ET.ParseError, TypeError):
            return None
        result = []
        for element in tree.iter():
            attrs = element.attrib
            kind = attrs.get("type", element.tag)
            sensitive = "secure" in kind.lower() or attrs.get("password") == "true"
            node = {"text": attrs.get("label") or attrs.get("name") or "", "content_desc": attrs.get("name", ""),
                    "class": kind, "role": kind, "sensitive": sensitive}
            if not sensitive and "value" in attrs:
                node["value"] = attrs["value"]
            for name in ("enabled", "visible", "selected", "focused"):
                if attrs.get(name) in {"true", "false", "1", "0"}:
                    node[name] = attrs[name] in {"true", "1"}
            try:
                x, y, w, h = (float(attrs[k]) for k in ("x", "y", "width", "height"))
                node["bounds"] = [x, y, x + w, y + h]
            except (KeyError, ValueError):
                pass
            result.append(node)
        return result
    if platform not in {"mac", "win"}:
        return None
    try:
        tree = json.loads(text)
    except (ValueError, TypeError):
        return None
    result, pending = [], [tree]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            kind = str(item.get("role") or item.get("AXRole") or item.get("ControlType") or item.get("type") or "")
            sensitive = bool(item.get("sensitive") or item.get("is_password") or item.get("IsPassword") or "secure" in kind.lower())
            text_value = item.get("text") or item.get("name") or item.get("title") or item.get("AXTitle") or item.get("Name") or ""
            node = {"text": str(text_value), "content_desc": str(item.get("name") or item.get("Name") or text_value),
                    "resource_id": str(item.get("automation_id") or item.get("AutomationId") or item.get("identifier") or ""),
                    "role": kind, "class": kind, "sensitive": sensitive}
            for target, keys in {"enabled": ("enabled", "is_enabled", "IsEnabled", "AXEnabled"),
                                 "checked": ("checked", "is_checked"), "selected": ("selected", "is_selected", "AXSelected"),
                                 "focused": ("focused", "is_focused", "AXFocused"), "visible": ("visible", "is_visible")}.items():
                for key in keys:
                    if type(item.get(key)) is bool:
                        node[target] = item[key]
                        break
            if not sensitive:
                for key in ("value", "AXValue", "Value"):
                    if key in item and type(item[key]) in {str, int, float, bool}:
                        node["value"] = item[key]
                        break
            bounds = item.get("bounds") or item.get("frame") or item.get("rect") or item.get("BoundingRectangle")
            if isinstance(bounds, list) and len(bounds) == 4:
                node["bounds"] = bounds
            elif isinstance(bounds, dict) and all(k in bounds for k in ("x", "y", "width", "height")):
                node["bounds"] = [bounds["x"], bounds["y"], bounds["x"] + bounds["width"], bounds["y"] + bounds["height"]]
            result.append(node)
            for key in ("children", "nodes", "elements", "tree", "accessibility_tree"):
                if key in item:
                    pending.append(item[key])
    return result
