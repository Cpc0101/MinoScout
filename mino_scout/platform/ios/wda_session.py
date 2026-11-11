"""WDA HTTP 会话。iOS 不走 adb，也不使用 Appium 客户端库。

每台设备单独解析 WDA 地址：配置覆盖 → CoreDevice 隧道（局域网）→ USB iproxy。
签名团队和 Runner bundle 仍来自环境，不写死某台设备。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from mino_scout.platform.ios.wda_reach import (
    candidate_urls,
    default_wda_url,
    probe_url,
    resolve_wda_url,
)
from mino_scout.tools.keep_alive.log import SLog

TAG = "Wda"


class WdaError(RuntimeError):
    pass


def wda_base_url() -> str:
    return default_wda_url()


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
_LINKS: dict[str, dict[str, Any]] = {}
_TUNNEL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TUNNEL_TTL_SEC = 20.0


def client_for(udid: str) -> WdaClient:
    """Return a WDA client bound to a **reachable** per-device URL."""
    key = str(udid or "").strip()
    if not key:
        raise WdaError("iOS 设备缺少 udid")
    link = describe_device(key) or _LINKS.get(key) or {"udid": key, "hardware_udid": key}
    try:
        base = resolve_wda_url(link, start_forward=True)
    except Exception as exc:
        raise WdaError(str(exc)) from exc
    client = _CLIENTS.get(key)
    if client is None or client.base != base:
        client = WdaClient(key, base_url=base)
        _CLIENTS[key] = client
    _LINKS[key] = dict(link)
    return client


def device_wda_ready(link: dict[str, Any]) -> bool:
    """True if any candidate URL for this device answers /status."""
    try:
        if resolve_wda_url(link, start_forward=True):
            return True
    except Exception:
        pass
    return any(probe_url(url) for url in candidate_urls(link))


_ATTACHED = frozenset({"connected", "available", "paired"})
# 有实际通路才可控。仅配对、无 transport 的是 Xcode 历史名单，装包/驱动都会失败。
_LIVE_TRANSPORT = frozenset({"wired", "localnetwork", "usb"})


def list_attached_devices() -> list[dict[str, Any]]:
    """用 devicectl 列出 iOS 设备，**逐台**判定能不能被本机驱动。

    装包/卸载不经过 WDA，所以不把「本机 WDA 是否监听」当成所有设备的共同状态。
    但「已配对」也不等于可控：没有 `transportType`、或 `tunnelState=unavailable`
    的条目只是历史配对残留，会上报成假在线、下发必挂。
    """
    rows = _devicectl_devices()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_link(row)
        if not item:
            continue
        if item.get("state") == "connected":
            item = _enrich_tunnel(item)
        out.append(item)
        _LINKS[str(item["udid"])] = item
        hw = str(item.get("hardware_udid") or "").strip()
        if hw:
            _LINKS[hw] = item
    return out


def _enrich_tunnel(link: dict[str, Any]) -> dict[str, Any]:
    """``list devices`` often omits tunnelIP until the CoreDevice tunnel is up.

    Pull ``device info details`` for connected phones so LAN WDA can use
    ``http://[tunnelIP]:8100`` without usbmux.
    """
    if str(link.get("tunnel_ip") or "").strip():
        return link
    udid = str(link.get("udid") or "").strip()
    if not udid:
        return link
    cached = _TUNNEL_CACHE.get(udid)
    now = time.monotonic()
    if cached and now - cached[0] < _TUNNEL_TTL_SEC:
        merged = dict(link)
        merged.update(cached[1])
        return merged
    try:
        proc = subprocess.run(
            ["xcrun", "devicectl", "device", "info", "details", "--device", udid, "--json-output", "-"],
            capture_output=True, text=True, timeout=12,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        SLog.w(TAG, f"details {udid} 失败: {exc}")
        return link
    if proc.returncode != 0:
        return link
    try:
        payload = json.loads(proc.stdout[proc.stdout.find("{"):] if proc.stdout else "{}")
    except json.JSONDecodeError:
        return link
    result = payload.get("result") if isinstance(payload, dict) else {}
    if not isinstance(result, dict):
        return link
    # details result *is* the device object
    enriched = _row_to_link(result)
    if not enriched:
        return link
    patch = {
        key: enriched[key]
        for key in ("tunnel_ip", "tunnel_state", "transport", "hardware_udid", "hostnames", "os_version")
        if enriched.get(key)
    }
    _TUNNEL_CACHE[udid] = (now, patch)
    merged = dict(link)
    merged.update(patch)
    return merged


def describe_device(udid: str) -> dict[str, Any] | None:
    """Fresh link info for one udid (core id or hardware udid)."""
    wanted = str(udid or "").strip().lower()
    if not wanted:
        return None
    for row in list_attached_devices():
        if str(row.get("udid") or "").lower() == wanted:
            return row
        if str(row.get("hardware_udid") or "").lower() == wanted:
            return row
    cached = _LINKS.get(str(udid or "").strip())
    return dict(cached) if cached else None


def _row_to_link(row: dict[str, Any]) -> dict[str, Any] | None:
    udid = str(row.get("identifier") or "").strip()
    if not udid:
        return None
    props = row.get("deviceProperties") if isinstance(row.get("deviceProperties"), dict) else {}
    hardware = row.get("hardwareProperties") if isinstance(row.get("hardwareProperties"), dict) else {}
    conn = row.get("connectionProperties") if isinstance(row.get("connectionProperties"), dict) else {}
    hostnames: list[str] = []
    for key in ("localHostnames", "potentialHostnames"):
        raw = conn.get(key)
        if isinstance(raw, list):
            hostnames.extend(str(item).strip() for item in raw if str(item).strip())
    seen: set[str] = set()
    hosts: list[str] = []
    for host in hostnames:
        if host not in seen:
            seen.add(host)
            hosts.append(host)
    return {
        "udid": udid,
        "core_id": udid,
        "hardware_udid": str(hardware.get("udid") or "").strip(),
        "name": str(props.get("name") or hardware.get("marketingName") or ""),
        "state": _device_state(conn),
        "os_version": str(props.get("osVersionNumber") or ""),
        "developer_mode": str(props.get("developerModeStatus") or ""),
        "transport": str(conn.get("transportType") or ""),
        "pairing": str(conn.get("pairingState") or ""),
        "tunnel_ip": str(conn.get("tunnelIPAddress") or "").strip(),
        "tunnel_state": str(conn.get("tunnelState") or "").strip(),
        "hostnames": hosts,
    }


def _device_state(conn: dict[str, Any]) -> str:
    """能不能被 devicectl 驱动。

    - 必须已配对；
    - 必须有当前通路（`transportType`：wired / localNetwork / …）；
    - `tunnelState=unavailable` 表示 CoreDevice 已找不到这台机，不可控；
    - 空闲时 `tunnelState=disconnected` 很常见（命令下发时才建隧道），不算离线。
    """
    paired = str(conn.get("pairingState") or "").strip().lower()
    if paired not in _ATTACHED:
        return "disconnected"
    transport = str(conn.get("transportType") or "").strip().lower()
    if transport not in _LIVE_TRANSPORT:
        return "disconnected"
    tunnel = str(conn.get("tunnelState") or "").strip().lower()
    if tunnel == "unavailable":
        return "disconnected"
    return "connected"


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
