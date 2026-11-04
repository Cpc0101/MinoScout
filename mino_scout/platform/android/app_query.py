# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""从 dumpsys 读安装包版本、当前前台应用。给 adb 通道和前置检查共用。"""
from __future__ import annotations

import re
from typing import Any, Optional

_VERSION_NAME_RE = re.compile(r"versionName=([^\s]+)")
_VERSION_CODE_RE = re.compile(r"versionCode[=:](\d+)")
_CONSTRAINT_RE = re.compile(
    r"(?:客户端版本|应用版本|app版本|版本号|版本)\s*(≥|>=|≤|<=|>|<|=)?\s*v?(\d+(?:\.\d+){0,3})",
    re.I,
)
_FOREGROUND_RE = re.compile(
    r"(?:topResumedActivity|mResumedActivity)=\S+\s+\S+\s+([\w.]+)/([^\s}]+)"
)
_FOCUS_RE = re.compile(
    r"(?:mCurrentFocus|mFocusedApp)=\S+\s+\S+\s+([\w.]+)/([^\s}]+)"
)
_OP_ALIASES = {
    "≥": ">=",
    "≤": "<=",
    "=>": ">=",
    "=<": "<=",
}

FOREGROUND_SHELL = (
    "(dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' ; "
    "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp') || true"
)


def version_dump_shell(package: str) -> str:
    pkg = str(package or "").strip()
    return f"dumpsys package {pkg}"


def parse_package_version(raw: str) -> dict[str, Any]:
    text = raw or ""
    names = _VERSION_NAME_RE.findall(text)
    codes = _VERSION_CODE_RE.findall(text)
    version_name = ""
    for name in names:
        cleaned = str(name or "").strip().strip("'\"")
        if cleaned and cleaned.lower() not in ("null", "none"):
            version_name = cleaned
            break
    version_code = int(codes[0]) if codes else None
    return {
        "version_name": version_name,
        "version_code": version_code,
    }


def parse_foreground(raw: str) -> dict[str, Any]:
    text = raw or ""
    m = _FOREGROUND_RE.search(text) or _FOCUS_RE.search(text)
    if not m:
        return {"package": "", "activity": ""}
    pkg = str(m.group(1) or "").strip()
    act = str(m.group(2) or "").strip()
    return {"package": pkg, "activity": f"{pkg}/{act}" if pkg and act else act}


def version_tuple(value: str) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", str(value or ""))]
    return tuple(parts[:4]) if parts else (0,)


def compare_version(actual: str, op: str, expected: str) -> bool:
    a = version_tuple(actual)
    b = version_tuple(expected)
    n = max(len(a), len(b), 1)
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    op = _OP_ALIASES.get(str(op or "").strip(), str(op or "").strip() or ">=")
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == "=":
        return a == b
    return a >= b


def parse_version_constraint(text: str) -> Optional[dict[str, str]]:
    m = _CONSTRAINT_RE.search(text or "")
    if not m:
        return None
    op = _OP_ALIASES.get(str(m.group(1) or "").strip(), str(m.group(1) or "").strip()) or ">="
    expected = str(m.group(2) or "").strip()
    if not expected:
        return None
    return {"op": op, "expected": expected}


def looks_like_version_line(text: str) -> bool:
    t = text or ""
    if _CONSTRAINT_RE.search(t):
        return True
    return bool(re.search(r"客户端版本|应用版本|app版本|versionName", t, re.I))


def looks_like_foreground_line(text: str) -> bool:
    t = text or ""
    if re.search(r"当前已打开|已打开\s*(造好物|.+)?\s*(App|APP|应用)", t):
        return True
    return bool(re.search(r"前台(是|为|应用)|应用在前台|目标应用已打开", t))
