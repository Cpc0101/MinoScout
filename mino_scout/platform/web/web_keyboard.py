"""Strict Playwright keyboard token handling shared by runtime and executor."""
from __future__ import annotations

import re


_NAMED_KEYS = {
    "ALT": "Alt",
    "BACKSPACE": "Backspace",
    "CONTROL": "Control",
    "CTRL": "Control",
    "DELETE": "Delete",
    "DEL": "Delete",
    "END": "End",
    "ENTER": "Enter",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "HOME": "Home",
    "META": "Meta",
    "PAGEDOWN": "PageDown",
    "PAGEUP": "PageUp",
    "SHIFT": "Shift",
    "SPACE": "Space",
    "TAB": "Tab",
    "ARROWDOWN": "ArrowDown",
    "DOWN": "ArrowDown",
    "ARROWLEFT": "ArrowLeft",
    "LEFT": "ArrowLeft",
    "ARROWRIGHT": "ArrowRight",
    "RIGHT": "ArrowRight",
    "ARROWUP": "ArrowUp",
    "UP": "ArrowUp",
}
for _index in range(1, 13):
    _NAMED_KEYS[f"F{_index}"] = f"F{_index}"
_MODIFIERS = frozenset({"Alt", "Control", "Meta", "Shift"})
_FUNCTION_KEY = re.compile(r"F(?:[1-9]|1[0-2])")
_PRINTABLE_CONTROL = re.compile(r"[^\x00-\x20\x7f]")


def normalize_key(value: str) -> str:
    """Return a canonical supported single key or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("web_key_invalid")
    raw = value.strip()
    if not raw or "+" in raw:
        raise ValueError("web_key_invalid")
    upper = raw.upper()
    if upper in _NAMED_KEYS:
        return _NAMED_KEYS[upper]
    if _FUNCTION_KEY.fullmatch(upper):
        return upper
    if len(raw) == 1 and _PRINTABLE_CONTROL.fullmatch(raw):
        return raw
    raise ValueError("web_key_invalid")


def normalize_key_value(value: str, *, allow_combo: bool = False) -> str:
    """Validate one key, or a ``Control+a`` style chord when requested."""
    if not isinstance(value, str):
        raise ValueError("web_key_invalid")
    raw = value.strip()
    if not raw:
        raise ValueError("web_key_invalid")
    if not allow_combo or "+" not in raw:
        return normalize_key(raw)
    tokens = raw.split("+")
    if len(tokens) < 2 or any(not token for token in tokens):
        raise ValueError("web_key_invalid")
    normalized = [normalize_key(token) for token in tokens]
    if any(key not in _MODIFIERS for key in normalized[:-1]):
        raise ValueError("web_key_chord_invalid")
    if len(set(normalized[:-1])) != len(normalized[:-1]):
        raise ValueError("web_key_chord_invalid")
    return "+".join(normalized)


def is_modifier(value: str) -> bool:
    return normalize_key(value) in _MODIFIERS
