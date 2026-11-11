"""Per-device WDA reachability: USB iproxy + CoreDevice LAN tunnel.

UI caps talk HTTP to WDA. Historically Scout only probed ``127.0.0.1:8100``,
which assumes a single USB port-forward. LAN (``localNetwork``) devices expose a
CoreDevice tunnel IPv6; once WDA is listening on the phone, ``http://[tunnel]:port``
works without usbmux. This module resolves candidates per device and optionally
keeps an ``iproxy`` for USB.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from mino_scout.tools.keep_alive.log import SLog

TAG = "WdaReach"

_FORWARDS: dict[str, subprocess.Popen] = {}


def wda_port() -> int:
    try:
        return max(1, min(int(os.environ.get("MINO_WDA_PORT") or 8100), 65535))
    except (TypeError, ValueError):
        return 8100


def default_wda_url() -> str:
    return str(os.environ.get("MINO_WDA_URL") or f"http://127.0.0.1:{wda_port()}").rstrip("/")


def local_forward_port(hardware_udid: str) -> int:
    """Stable per-device localhost port so multi-USB phones do not share 8100."""
    base = 18100
    digest = sum(ord(ch) for ch in str(hardware_udid or ""))
    return base + (digest % 700)


def configured_url(core_id: str, hardware_udid: str) -> str:
    for key in (
        f"MINO_WDA_URL_{hardware_udid}",
        f"MINO_WDA_URL_{core_id}",
        f"MINO_WDA_URL_{hardware_udid.replace('-', '')}",
    ):
        value = str(os.environ.get(key) or "").strip().rstrip("/")
        if value:
            return value
    # Optional map in scout config: {"wda_urls": {"<udid>": "http://..."}}
    try:
        from mino_scout.config import load_config

        raw = load_config().get("wda_urls")
        if isinstance(raw, dict):
            for key in (hardware_udid, core_id, str(hardware_udid or "").lower(), str(core_id or "").lower()):
                value = str(raw.get(key) or "").strip().rstrip("/")
                if value:
                    return value
    except Exception:
        pass
    return ""


def probe_url(url: str, *, timeout_sec: float = 1.5) -> bool:
    target = str(url or "").rstrip("/") + "/status"
    try:
        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return 200 <= int(resp.status) < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return False


def format_tunnel_url(tunnel_ip: str, port: int | None = None) -> str:
    tip = str(tunnel_ip or "").strip()
    if not tip:
        return ""
    port = wda_port() if port is None else int(port)
    if ":" in tip and not tip.startswith("["):
        return f"http://[{tip}]:{port}"
    return f"http://{tip}:{port}"


_USBMUX_CACHE: dict[str, tuple[float, set[str]]] = {}


def usbmux_udids(*, network: bool = False) -> set[str]:
    """UDIDs visible to libimobiledevice (USB or ``idevice_id -n``)."""
    kind = "n" if network else "l"
    cached = _USBMUX_CACHE.get(kind)
    now = time.monotonic()
    if cached and now - cached[0] < 5.0:
        return set(cached[1])
    cmd = ["idevice_id", "-n" if network else "-l"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _USBMUX_CACHE[kind] = (now, set())
        return set()
    if proc.returncode != 0:
        _USBMUX_CACHE[kind] = (now, set())
        return set()
    values = {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}
    _USBMUX_CACHE[kind] = (now, values)
    return set(values)


def ensure_iproxy(hardware_udid: str, *, local_port: int, device_port: int | None = None, network: bool = False) -> str:
    """Start or reuse iproxy; return ``http://127.0.0.1:<local_port>``."""
    udid = str(hardware_udid or "").strip()
    if not udid:
        raise RuntimeError("iproxy 需要 hardware udid")
    device_port = wda_port() if device_port is None else int(device_port)
    url = f"http://127.0.0.1:{int(local_port)}"
    key = f"{udid}:{local_port}:{device_port}:{'n' if network else 'u'}"
    existing = _FORWARDS.get(key)
    if existing is not None and existing.poll() is None and probe_url(url):
        return url
    if existing is not None and existing.poll() is None:
        # Forward is up but WDA not listening yet — still usable as endpoint.
        return url
    stop_iproxy(key)
    cmd = ["iproxy", "-u", udid]
    if network:
        cmd.append("-n")
    cmd.append(f"{int(local_port)}:{int(device_port)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("本机没有 iproxy（libimobiledevice）") from exc
    except OSError as exc:
        raise RuntimeError(f"iproxy 启动失败：{exc}") from exc
    _FORWARDS[key] = proc
    time.sleep(0.3)
    if proc.poll() is not None:
        raise RuntimeError(f"iproxy 立即退出（udid={udid}, network={network}）")
    SLog.i(TAG, f"iproxy {udid} → 127.0.0.1:{local_port} network={network}")
    return url


def stop_iproxy(key: str | None = None) -> None:
    keys = [key] if key else list(_FORWARDS)
    for item in keys:
        proc = _FORWARDS.pop(item, None)
        if proc is None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def candidate_urls(link: dict[str, Any]) -> list[str]:
    """Ordered WDA base URLs for one attached device."""
    core_id = str(link.get("udid") or link.get("core_id") or "").strip()
    hardware = str(link.get("hardware_udid") or "").strip() or core_id
    transport = str(link.get("transport") or "").strip().lower()
    tunnel_ip = str(link.get("tunnel_ip") or "").strip()
    port = wda_port()
    out: list[str] = []

    def add(url: str) -> None:
        url = str(url or "").rstrip("/")
        if url and url not in out:
            out.append(url)

    add(configured_url(core_id, hardware))
    # LAN / CoreDevice: tunnel is the native path (no usbmux required).
    add(format_tunnel_url(tunnel_ip, port))
    # Skip *.coredevice.local hostnames — mDNS resolve often hangs; tunnel IP is enough.
    # USB (or network-visible to libimobiledevice): localhost forward.
    usb = hardware in usbmux_udids(network=False)
    net = hardware in usbmux_udids(network=True)
    if usb or net or transport in {"wired", "usb"}:
        add(f"http://127.0.0.1:{local_forward_port(hardware)}")
    # Last resort global default (legacy single-device setups).
    add(default_wda_url())
    return out


def resolve_wda_url(link: dict[str, Any], *, start_forward: bool = True) -> str:
    """Return a reachable WDA base URL, starting USB forward when useful."""
    core_id = str(link.get("udid") or "").strip()
    hardware = str(link.get("hardware_udid") or "").strip() or core_id
    transport = str(link.get("transport") or "").strip().lower()
    errors: list[str] = []

    # Prefer bringing up iproxy early for usbmux-visible devices.
    if start_forward:
        usb = hardware in usbmux_udids(network=False)
        net = (not usb) and hardware in usbmux_udids(network=True)
        if usb or net or transport in {"wired", "usb"}:
            try:
                ensure_iproxy(
                    hardware,
                    local_port=local_forward_port(hardware),
                    network=bool(net and not usb),
                )
            except Exception as exc:
                errors.append(str(exc))

    for url in candidate_urls(link):
        if probe_url(url):
            return url
        errors.append(f"{url} 无应答")

    # Refresh tunnel IP once — CoreDevice leases rotate.
    refreshed = refresh_link_tunnel(link)
    if refreshed:
        for url in candidate_urls(refreshed):
            if probe_url(url):
                return url

    detail = "；".join(errors[:4]) or "没有候选地址"
    raise RuntimeError(
        f"设备 {link.get('name') or hardware or core_id} 的 WDA 不可达（{detail}）。"
        "局域网请确认手机上 WDA 已在跑，或设置 MINO_WDA_URL_<udid> / config.wda_urls；"
        "USB 请确认 iproxy 可用且 WDA 已启动"
    )


def refresh_link_tunnel(link: dict[str, Any]) -> dict[str, Any] | None:
    core_id = str(link.get("udid") or "").strip()
    if not core_id:
        return None
    try:
        from mino_scout.platform.ios.wda_session import describe_device

        fresh = describe_device(core_id)
    except Exception:
        return None
    if not fresh:
        return None
    merged = dict(link)
    merged.update(fresh)
    return merged


def wait_reachable(link: dict[str, Any], *, timeout_sec: float = 45.0) -> str:
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    last = ""
    while time.monotonic() < deadline:
        try:
            return resolve_wda_url(link, start_forward=True)
        except Exception as exc:
            last = str(exc)
            time.sleep(1.0)
    raise RuntimeError(last or "等待 WDA 超时")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def parse_devicectl_json(raw: str) -> dict[str, Any]:
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
