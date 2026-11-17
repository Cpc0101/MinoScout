"""Bind business values to selector templates without keeping stale UI handles."""
from __future__ import annotations
from copy import deepcopy
import re

_TOKEN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_.]*)\}\}")


def _lookup(values, path):
    value = values
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"缺少定位变量：{path}")
        value = value[part]
    if type(value) not in (str, int, float, bool) or value == "":
        raise ValueError(f"定位变量不是明确的业务值：{path}")
    return str(value)


def _css_value(value):
    # Hex escapes work in both CSS identifiers and quoted attribute values.
    return "".join(char if char.isascii() and (char.isalpha() or char in "_-") or char.isascii() and char.isdigit() and index > 0
                   else f"\\{ord(char):x} " for index, char in enumerate(value))


def bind_cell(cell, values):
    from mino_scout.server_comm.protocol import validate_runtime_data
    values = validate_runtime_data(values)
    def fill(item, css=False):
        if isinstance(item, dict):
            is_css = item.get("kind") == "css"
            return {key: fill(value, is_css and key == "value" or key in {"scope_css", "frame_selector", "shadow_host_css"} or css and key in {"value", "scope"}) for key, value in item.items()}
        if isinstance(item, list):
            return [fill(value, css) for value in item]
        if isinstance(item, str):
            value = _TOKEN.sub(lambda match: _css_value(_lookup(values, match[1])) if css else _lookup(values, match[1]), item)
            if "{{" in value or "}}" in value:
                raise ValueError("定位模板包含未解析变量")
            return value
        return deepcopy(item)
    return fill(deepcopy(cell))
