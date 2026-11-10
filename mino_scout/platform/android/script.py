"""Android 用例脚本里的定位。"""
from __future__ import annotations


def locator_params(kind: str, value: str, name: str, label: str) -> dict:
    key = {"text": "text", "content_desc": "content_desc", "resource_id": "resource_id", "role": "text", "class": "class"}.get(kind)
    if not key:
        raise ValueError("locator_missing")
    return {"point_policy": "node", "target": {key: label if kind == "role" else value}}


def device_serial(sn: str, hint: dict) -> str:
    return str(hint.get("adb_serial") or sn or "")
