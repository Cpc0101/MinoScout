"""Node config.

Development lives in ``~/.mino-dev/scout``. Production lives in ``~/.mino/scout``.
Scout persists ``scout_id`` here on first run. CLI flags ``--nexus`` / ``--token``
override the file. ``nexus_url`` is an HTTP origin; this module derives the
WebSocket ``/node`` URL.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

def development() -> bool:
    return not getattr(sys, "frozen", False)


LAN_HOST = "mino.local"
DEFAULT_NEXUS_HTTP = f"http://{LAN_HOST}:50104"
DEFAULT_NEXUS_WS = f"ws://{LAN_HOST}:50104/node"
SCOUT_ID_LEN = 16


def _environment_dir(dev: bool) -> Path:
    if dev:
        explicit = str(os.environ.get("MINO_SCOUT_DEV_HOME") or "").strip()
        if explicit:
            return Path(explicit).expanduser().resolve()
        return Path.home() / ".mino-dev" / "scout"
    override = str(os.environ.get("MINO_SCOUT_HOME") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".mino" / "scout"



def config_dir() -> Path:
    selected = _environment_dir(development()).resolve()
    home = Path.home()
    reserved = ([home / ".mino" / "scout",
                 home / "Library/Application Support/Mino/scout",
                 home / "Library/Application Support/MinoScout",
                 home / ".config/minoscout",
                 Path(os.environ.get("APPDATA") or home) / "MinoScout"]
                if development() else [home / ".mino-dev" / "scout"])
    for other in reserved:
        other = other.resolve()
        if selected == other or selected in other.parents or other in selected.parents:
            raise ValueError("开发与正式 Scout 配置目录必须完全独立，不能相同或互相包含")
    # Marker makes separate invocations with different override environments fail closed.
    marker = selected / ".scout-environment"
    expected = "development" if development() else "production"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() != expected:
        raise ValueError("Scout 配置目录属于另一环境，禁止共用")
    return selected


def claim_environment() -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".scout-environment"
    expected = "development" if development() else "production"
    try:
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(expected)
    except FileExistsError:
        if marker.read_text(encoding="utf-8").strip() != expected:
            raise ValueError("Scout 配置目录属于另一环境，禁止共用")


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("scout_config_unreadable") from exc
    if not isinstance(data,dict): raise ValueError("scout_config_object_required")
    return data


def set_nexus_url(url: str, path: Path | None = None) -> Path:
    if development():
        resolve_runtime(nexus=url, config={})
    cfg = load_config(path)
    cfg["nexus_url"] = str(url or "").strip()
    return save_config(cfg, path)


def save_config(data: dict[str, Any], path: Path | None = None) -> Path:
    if path is None or path.resolve() == config_path():
        claim_environment()
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    return target


def sanitize_scout_id(raw: str) -> str:
    """Letters and digits only; lowercase. Hyphens / underscores / dots are dropped."""
    return "".join(c for c in str(raw or "").lower() if c.isalnum())


def _machine_id() -> str:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    if sys.platform == "darwin":
        try:
            import subprocess

            proc = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (proc.stdout or "").splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("=", 1)[-1].strip().strip('"')
        except (OSError, subprocess.TimeoutExpired):
            pass
    if os.name == "nt":
        try:
            import subprocess

            proc = subprocess.run(
                ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (proc.stdout or "").splitlines():
                if "MachineGuid" in line:
                    return line.strip().split()[-1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return platform.node() or "scout"


def generate_scout_id() -> str:
    """16 lowercase hex from hostname + machine-id. No hyphens / underscores / dots."""
    material = f"{platform.node()}|{_machine_id()}"
    if development():
        material += "|development|" + str(config_dir())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:SCOUT_ID_LEN]


def resolve_studio_id() -> str:
    return sanitize_scout_id(str(load_config().get("studio_id") or ""))


def resolve_hostname() -> str:
    return platform.node() or ""


def resolve_scout_id(*, override: str = "", persist: bool = True) -> str:
    """CLI `--node-id` wins (sanitized). Else `config.scout_id`. Else generate once."""
    sid = sanitize_scout_id(override)
    if sid:
        return sid
    path = config_path()
    cfg = load_config(path)
    existing = sanitize_scout_id(str(cfg.get("scout_id") or ""))
    if existing:
        if persist and cfg.get("scout_id") != existing:
            cfg["scout_id"] = existing
            save_config(cfg, path)
        return existing
    generated = generate_scout_id()
    if persist:
        cfg["scout_id"] = generated
        save_config(cfg, path)
    return generated


def nexus_ws_url(raw: str) -> str:
    """Turn an HTTP origin or WS URL into the Nexus node endpoint."""
    value = str(raw or "").strip()
    if not value:
        return ""
    value = value.rstrip("/")
    if value.endswith("/node"):
        root = value[: -len("/node")].rstrip("/")
        suffix = "/node"
    else:
        root, suffix = value, "/node"

    if root.startswith("wss://") or root.startswith("ws://"):
        return f"{root}{suffix}"
    if root.startswith("https://"):
        return f"wss://{root[len('https://'):]}{suffix}"
    if root.startswith("http://"):
        return f"ws://{root[len('http://'):]}{suffix}"
    return f"ws://{root}{suffix}"


def resolve_runtime(*, nexus: str = "", token: str = "", config: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return `(ws_url, token)` with CLI flags winning over the config file."""
    cfg = config if config is not None else load_config()
    flag_nexus = str(nexus or "").strip()
    flag_token = str(token or "").strip()
    if flag_nexus:
        ws = nexus_ws_url(flag_nexus)
    else:
        ws = nexus_ws_url(str(cfg.get("pending_nexus_url") or cfg.get("nexus_url") or ""))
    if not ws:
        ws = "ws://127.0.0.1:50101/node" if development() else DEFAULT_NEXUS_WS
    if development():
        target = urlsplit(ws)
        if target.scheme not in {"ws", "wss"} or target.hostname not in {"127.0.0.1", "localhost", "::1"} or target.port != 50101:
            raise ValueError("开发 Scout 只能连接本机 Nexus :50101；请检查开发配置")
    elif urlsplit(ws).port == 50101:
        raise ValueError("正式 Scout 不能连接开发 Nexus :50101；请启动独立开发 Scout")
    tok = flag_token or str(cfg.get("pending_install_token") or cfg.get("token") or "").strip()
    return ws, tok


def configure_proxy_bypass() -> None:
    """Dial mino.local without going through a system HTTP proxy."""
    extra = ["localhost", "127.0.0.1", "::1", LAN_HOST, "0.0.0.0"]
    for key in ("no_proxy", "NO_PROXY"):
        cur = [p.strip() for p in str(os.environ.get(key) or "").split(",") if p.strip()]
        for host in extra:
            if host not in cur:
                cur.append(host)
        os.environ[key] = ",".join(cur)


def runtime_identity() -> dict[str, Any]:
    """Public deployment identity; never returns tokens or URL credentials.

    Source and frozen runtimes are separate environments. Destination aliases
    do not select the storage namespace; neither runtime may cross environments.
    """
    from urllib.parse import urlsplit
    from mino_scout.tools.update.app_version import process_build_identity
    ws, _ = resolve_runtime()
    target = urlsplit(ws)
    host = target.hostname or ""
    port = target.port
    local = host in {"127.0.0.1", "localhost", "::1"}
    environment = "development" if local and port == 50101 else (
        "production" if (local or host == LAN_HOST) and port == 50104 else "custom")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return {
        "environment": "development" if development() else "production",
        "destination_environment": environment,
        "runtime_kind": "source" if development() else "frozen",
        "config_dir": str(config_dir()),
        "nexus_origin": f"{target.scheme}://{authority}",
        "scout_id": resolve_scout_id(persist=False),
        "process_build": process_build_identity(),
        "executable": str(Path(sys.executable).absolute()),
        "python_prefix": sys.prefix,
        "python_base_prefix": sys.base_prefix,
        "source_root": str(Path(__file__).resolve().parent),
    }
