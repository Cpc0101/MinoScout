"""WDA HTTP 会话。iOS 不走 adb，也不使用 Appium 客户端库。

本机 WDA 地址、签名团队和 Runner bundle 来自环境，不写死某台设备。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any

from mino_scout.tools.keep_alive.log import SLog

TAG = "Wda"


class WdaError(RuntimeError):
    pass


def wda_base_url() -> str:
    return str(os.environ.get("MINO_WDA_URL") or "http://127.0.0.1:8100").rstrip("/")


def _signing_capabilities() -> dict[str, Any]:
    caps: dict[str, Any] = {}
    org = str(os.environ.get("MINO_IOS_XCODE_ORG_ID") or "").strip()
    bundle = str(os.environ.get("MINO_IOS_WDA_BUNDLE_ID") or "").strip()
    if org:
        caps["appium:xcodeOrgId"] = org
    if bundle:
        caps["appium:updatedWDABundleId"] = bundle
    return caps


class WdaClient:
    def __init__(self, udid: str, base_url: str = ""):
        self.udid = str(udid or "").strip()
        self.base = str(base_url or wda_base_url()).rstrip("/")
        self.session_id = ""

    def reachable(self) -> bool:
        try:
            self._json("GET", "/status")
            return True
        except WdaError:
            return False

    def ensure(self) -> str:
        if self.session_id:
            return self.session_id
        if not self.udid:
            raise WdaError("iOS 设备缺少 udid")
        caps = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            "appium:udid": self.udid,
            "appium:noReset": True,
            "appium:usePrebuiltWDA": True,
            **_signing_capabilities(),
        }
        body = self._json("POST", "/session", {"capabilities": {"alwaysMatch": caps}})
        value = body.get("value") if isinstance(body, dict) else {}
        sid = ""
        if isinstance(value, dict):
            sid = str(value.get("sessionId") or "")
        sid = sid or str((body or {}).get("sessionId") or "")
        if not sid:
            raise WdaError("WDA 没有返回 session")
        self.session_id = sid
        return sid

    def screenshot(self) -> str:
        body = self._json("GET", self._path("/screenshot"))
        value = body.get("value") if isinstance(body, dict) else ""
        if not isinstance(value, str) or not value:
            raise WdaError("WDA 截图为空")
        return value

    def source(self) -> str:
        body = self._json("GET", self._path("/source"))
        value = body.get("value") if isinstance(body, dict) else ""
        if not isinstance(value, str) or not value:
            raise WdaError("WDA 没有返回界面结构")
        return value

    def tap_xy(self, x: int, y: int) -> None:
        self._json("POST", self._path("/wda/tap"), {"x": int(x), "y": int(y)})

    def tap_target(self, target: str) -> None:
        element = self._find(target)
        self._json("POST", self._path(f"/element/{element}/click"), {})

    def activate(self, bundle_id: str) -> None:
        self._json("POST", self._path("/wda/apps/activate"), {"bundleId": bundle_id})

    def terminate(self, bundle_id: str) -> None:
        self._json("POST", self._path("/wda/apps/terminate"), {"bundleId": bundle_id})

    def keys(self, text: str) -> None:
        self._json("POST", self._path("/wda/keys"), {"value": list(str(text or ""))})

    def window_size(self) -> tuple[int, int]:
        body = self._json("GET", self._path("/window/size"))
        value = body.get("value") if isinstance(body, dict) else {}
        if not isinstance(value, dict) or "width" not in value or "height" not in value:
            raise WdaError("WDA 没有返回窗口尺寸")
        return int(value["width"]), int(value["height"])

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self._json("POST", self._path("/wda/dragfromtoforduration"), {
            "fromX": int(x1), "fromY": int(y1), "toX": int(x2), "toY": int(y2), "duration": float(duration),
        })

    def touch_and_hold(self, x: int, y: int, duration: float = 0.6) -> None:
        self._json("POST", self._path("/wda/touchAndHold"), {"x": int(x), "y": int(y), "duration": float(duration)})

    def press_button(self, name: str) -> None:
        self._json("POST", self._path("/wda/pressButton"), {"name": name})

    def active_app(self) -> dict[str, Any]:
        body = self._json("GET", self._path("/wda/activeAppInfo"))
        value = body.get("value") if isinstance(body, dict) else {}
        if not isinstance(value, dict) or not value.get("bundleId"):
            raise WdaError("WDA 没有返回前台应用")
        return value

    def set_clipboard(self, text: str) -> None:
        content = base64.b64encode(str(text or "").encode()).decode()
        self._json("POST", self._path("/wda/setPasteboard"), {"content": content, "contentType": "plaintext"})

    def element_center(self, target: str) -> tuple[int, int]:
        element = self._find(target)
        body = self._json("GET", self._path(f"/element/{element}/rect"))
        value = body.get("value") if isinstance(body, dict) else {}
        if not isinstance(value, dict) or "x" not in value:
            raise WdaError("WDA 没有返回控件位置")
        return int(value["x"] + value.get("width", 0) / 2), int(value["y"] + value.get("height", 0) / 2)

    def _find(self, target: str) -> str:
        text = str(target or "").strip()
        if not text:
            raise WdaError("没有要点的目标")
        strategies = (
            ("accessibility id", text),
            ("name", text),
            ("predicate string", f'name == "{text}" OR label == "{text}"'),
        )
        last = "没有找到元素"
        for using, value in strategies:
            try:
                body = self._json("POST", self._path("/element"), {"using": using, "value": value})
            except WdaError as exc:
                last = str(exc)
                continue
            found = body.get("value") if isinstance(body, dict) else {}
            if isinstance(found, dict):
                element = str(found.get("ELEMENT") or found.get("element-6066-11e4-a52e-4f735466cecf") or "")
                if element:
                    return element
        raise WdaError(last)

    def _path(self, suffix: str) -> str:
        return f"/session/{self.ensure()}{suffix}"

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise WdaError(f"WDA {method} {path} 失败：{detail or exc.code}") from exc
        except urllib.error.URLError as exc:
            raise WdaError(f"WDA 连不上 {self.base}") from exc
        if not raw:
            return {}
        parsed = json.loads(raw.decode())
        if not isinstance(parsed, dict):
            raise WdaError("WDA 返回的不是 JSON 对象")
        value = parsed.get("value")
        if isinstance(value, dict) and value.get("error"):
            raise WdaError(str(value.get("message") or value.get("error")))
        return parsed


_CLIENTS: dict[str, WdaClient] = {}


def client_for(udid: str) -> WdaClient:
    key = str(udid or "").strip()
    client = _CLIENTS.get(key)
    if client is None:
        client = WdaClient(key)
        _CLIENTS[key] = client
    return client


_ATTACHED = frozenset({"connected", "available", "paired"})


def list_attached_devices() -> list[dict[str, str]]:
    """用 devicectl 列出 iOS 设备，**逐台**判定连通性。

    上游版本把「本机 WDA 是否监听」当成所有设备的共同状态，于是 WDA 没起来时
    所有设备一起变 disconnected；装包/卸载根本不经过 WDA，不该被它连坐。
    这里只看每台设备自己的 `connectionProperties`。
    """
    rows = _devicectl_devices()
    out: list[dict[str, str]] = []
    for row in rows:
        udid = str(row.get("identifier") or "").strip()
        if not udid:
            continue
        props = row.get("deviceProperties") if isinstance(row.get("deviceProperties"), dict) else {}
        hardware = row.get("hardwareProperties") if isinstance(row.get("hardwareProperties"), dict) else {}
        conn = row.get("connectionProperties") if isinstance(row.get("connectionProperties"), dict) else {}
        out.append({
            "udid": udid,
            "name": str(props.get("name") or hardware.get("marketingName") or ""),
            "state": _device_state(conn),
            "os_version": str(props.get("osVersionNumber") or ""),
            "developer_mode": str(props.get("developerModeStatus") or ""),
            "transport": str(conn.get("transportType") or ""),
            "pairing": str(conn.get("pairingState") or ""),
        })
    return out


def _device_state(conn: dict[str, Any]) -> str:
    """能不能被 devicectl 驱动，只看配对状态。

    `tunnelState` 在设备空闲时本来就是 disconnected —— devicectl 执行命令时才建立
    隧道。把它当成连通性会得到「刚看一眼是断开、用之前又变连上」的假象。
    """
    paired = str(conn.get("pairingState") or "").strip().lower()
    return "connected" if paired in _ATTACHED or paired == "paired" else "disconnected"


def _devicectl_devices() -> list[dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["xcrun", "devicectl", "list", "devices", "--json-output", "-"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        SLog.w(TAG, f"devicectl list devices 失败: {exc}")
        return []
    if proc.returncode != 0:
        SLog.w(TAG, f"devicectl list devices 退出 {proc.returncode}")
        return []
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []
    result = payload.get("result") if isinstance(payload, dict) else {}
    rows = result.get("devices") if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def device_online(udid: str) -> tuple[bool, str]:
    """这台 udid 现在能不能被 devicectl 驱动。装包前用它，不看 WDA。"""
    devices = list_attached_devices()
    if not devices:
        return False, "devicectl 没有列出任何 iOS 设备"
    for row in devices:
        if row["udid"] == udid or row["udid"].lower() == str(udid or "").lower():
            return row["state"] == "connected", row["state"]
    return False, f"devicectl 里没有 {udid}"


def install_app(udid: str, bundle_path: str, *, timeout_sec: float = 600.0) -> dict[str, Any]:
    """`devicectl` 自己解包：`.ipa` 和 `.app` 都直传，不需要先解压。"""
    payload = _devicectl_device(udid, ["install", "app", str(bundle_path)], timeout_sec=timeout_sec,
                                timeout_flag=True)
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    return {
        "summary": _join(result.get("bundleIdentifier"), result.get("installedApplicationURL"))
        or "已安装",
        "bundle_id": str(result.get("bundleIdentifier") or ""),
        "install_url": str(result.get("installedApplicationURL") or ""),
    }


def uninstall_app(udid: str, bundle_id: str, *, timeout_sec: float = 120.0) -> dict[str, Any]:
    payload = _devicectl_device(udid, ["uninstall", "app", str(bundle_id)], timeout_sec=timeout_sec)
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    return {"summary": f"已卸载 {bundle_id}", "bundle_id": str(bundle_id),
            "uninstalled": bool(result.get("uninstalled") is not False)}


def installed_apps(udid: str) -> list[dict[str, Any]]:
    payload = _devicectl_device(udid, ["info", "apps"], timeout_sec=30.0)
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    rows = result.get("apps") if isinstance(result, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _join(*parts: Any) -> str:
    return " ".join(str(part) for part in parts if part)


def _devicectl_device(udid: str, args: list[str], *, timeout_sec: float, timeout_flag: bool = False) -> dict[str, Any]:
    """跑一条 `devicectl device ...`，按 JSON 判定成败。

    退出码不可靠（实测装包失败仍可能返 0），所以只看 `info.outcome` 与 `error`。
    """
    if not udid:
        raise WdaError("缺少设备 udid")
    command = ["xcrun", "devicectl", "device", *args, "--device", udid, "--json-output", "-"]
    if timeout_flag:
        command += ["--timeout", str(max(1, int(timeout_sec)))]
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=max(30.0, float(timeout_sec) + 15.0))
    except FileNotFoundError as exc:
        raise WdaError("本机没有 xcrun devicectl（需要 Xcode 命令行工具）") from exc
    except subprocess.TimeoutExpired as exc:
        raise WdaError(f"devicectl {' '.join(args)} 超过 {timeout_sec:.0f}s 未返回") from exc
    except OSError as exc:
        raise WdaError(f"devicectl 启动失败：{exc}") from exc
    payload = _parse_devicectl(proc.stdout)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if error:
        raise WdaError(_devicectl_error(error))
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    outcome = str(info.get("outcome") or "").strip().lower()
    if outcome and outcome not in {"success", "succeeded"}:
        raise WdaError(f"devicectl {' '.join(args)} 结果 {outcome}")
    if not payload and proc.returncode != 0:
        raise WdaError((proc.stderr or proc.stdout or f"devicectl 退出 {proc.returncode}")[:300])
    return payload


def _parse_devicectl(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            return {}
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _devicectl_error(error: dict[str, Any]) -> str:
    """把 devicectl 的嵌套错误摊平成一句能看的原文。

    `NSLocalizedDescription` 顶层往往只有「Failed to install the app on the device.」，
    真正原因在 `NSUnderlyingError>…>NSLocalizedRecoverySuggestion` 里
    （例如 Beta 描述文件缺 entitlement、签名不匹配）。
    """
    parts: list[str] = []
    node: Any = error
    for _ in range(6):
        if not isinstance(node, dict):
            break
        info = node.get("userInfo") if isinstance(node.get("userInfo"), dict) else {}
        for key in ("NSLocalizedFailureReason", "NSLocalizedRecoverySuggestion", "NSLocalizedDescription"):
            value = info.get(key)
            if isinstance(value, dict):
                value = value.get("string")
            text = str(value or "").strip()
            if text and text not in parts:
                # 顶层那句「Failed to install…」没有信息量，单独出现的放最后
                if len(text) > 40 or key != "NSLocalizedDescription" or not parts:
                    parts.append(text)
        node = info.get("NSUnderlyingError")
        node = node.get("error") if isinstance(node, dict) and isinstance(node.get("error"), dict) else None
    detail = "；".join(parts[:3]) or "内层错误"
    code = error.get("code")
    domain = str(error.get("domain") or "")
    suffix = f" [{domain} {code}]" if domain or code else ""
    return f"{detail}{suffix}"[:400]


def app_version(udid: str, bundle_id: str) -> dict[str, str]:
    if not udid or not bundle_id:
        raise WdaError("读版本缺少 udid 或 bundle id")
    for row in installed_apps(udid):
        ident = str(row.get("bundleIdentifier") or row.get("bundleId") or "")
        if ident != bundle_id:
            continue
        version = str(row.get("version") or row.get("shortVersion") or "")
        build = str(row.get("bundleVersion") or "")
        if not version and not build:
            raise WdaError(f"已安装 {bundle_id}，但没有版本号")
        return {"bundle_id": ident, "version": version or build, "build": build}
    raise WdaError(f"设备上没有 {bundle_id}")
