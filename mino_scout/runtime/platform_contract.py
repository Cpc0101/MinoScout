"""V5 target-platform contract. Pure validation; never accepts runtime aliases.

Legacy conversion belongs to the offline migration planner, not this module.
Installing these helpers does not advertise or activate the new wire contract.
"""
from __future__ import annotations

TARGET_PLATFORMS = ("mac", "win", "android", "ios", "web")
NATIVE_PLATFORMS = frozenset({"mac", "win"})
HOST_SYSTEMS = frozenset({"mac", "win", "linux"})
PLATFORM_CONTRACT_VERSION = 1


class PlatformContractError(ValueError):
    def __init__(self, code: str, field: str = "platform"):
        self.code, self.field = code, field
        super().__init__(f"{code}:{field}")


def target_platform(value, *, field="platform") -> str:
    if type(value) is not str or value not in TARGET_PLATFORMS:
        raise PlatformContractError("target_platform_invalid", field)
    return value


def platform_set(values, *, field="platforms") -> list[str]:
    if type(values) is not list:
        raise PlatformContractError("platform_set_required", field)
    for value in values:
        target_platform(value, field=field)
    if len(values) != len(set(values)):
        raise PlatformContractError("platform_set_duplicate", field)
    return [value for value in TARGET_PLATFORMS if value in values]


def coverage(document: dict) -> dict:
    """Missing, explicitly empty and all are three distinct states."""
    scope = document.get("platform_scope")
    if scope not in ("all", "explicit"):
        raise PlatformContractError("platform_scope_required", "platform_scope")
    values = platform_set(document.get("platforms"))
    if scope == "all" and values:
        raise PlatformContractError("platform_scope_conflict", "platforms")
    if "platform" in document:
        single = target_platform(document["platform"])
        if scope != "explicit" or values != [single]:
            raise PlatformContractError("platform_scope_conflict", "platform")
    return {"platform_scope": scope, "platforms": values}


def host_os_from_system(value) -> str:
    """Only for the OS API boundary, never for a device/API platform input."""
    result = {"Darwin": "mac", "darwin": "mac", "Windows": "win",
              "win32": "win", "Linux": "linux", "linux": "linux"}.get(value)
    if result is None:
        raise PlatformContractError("host_os_unknown", "host.os")
    return result


def host_os(value) -> str:
    if type(value) is not str or value not in HOST_SYSTEMS:
        raise PlatformContractError("host_os_invalid", "host.os")
    return value


def native_sn(platform, node_id) -> str:
    target_platform(platform)
    if platform not in NATIVE_PLATFORMS:
        raise PlatformContractError("native_platform_required")
    if not isinstance(node_id, str) or not node_id or ":" in node_id or any(c.isspace() for c in node_id):
        raise PlatformContractError("node_identity_invalid", "node_id")
    return f"{platform}:{node_id}:console"


def device_identity(device: dict, *, node_id: str, host: dict) -> str:
    platform = target_platform(device.get("platform"))
    system = host_os(host.get("os"))
    sn = device.get("sn")
    if not isinstance(sn, str) or not sn:
        raise PlatformContractError("device_identity_missing", "sn")
    if any(key in device for key in ("os", "native_os", "device_platform", "target_platform")):
        raise PlatformContractError("duplicate_platform_field")
    if platform in NATIVE_PLATFORMS:
        if platform != system or sn != native_sn(platform, node_id) or device.get("session_key") != "console":
            raise PlatformContractError("native_identity_conflict", "sn")
    elif sn.startswith(("desktop:", "mac:", "win:")):
        raise PlatformContractError("native_identity_conflict", "sn")
    elif platform == "web" and sn != f"web{node_id}":
        raise PlatformContractError("web_identity_conflict", "sn")
    return platform


def execute_platform(request: dict) -> str:
    platform = target_platform(request.get("platform"))
    hint = request.get("device_hint", {})
    if not isinstance(hint, dict):
        raise PlatformContractError("device_hint_invalid", "device_hint")
    if "platform" in hint and target_platform(hint["platform"], field="device_hint.platform") != platform:
        raise PlatformContractError("platform_hint_conflict", "device_hint.platform")
    for obj in (request, hint):
        if any(key in obj for key in ("os", "native_os", "device_platform", "target_platform")):
            raise PlatformContractError("duplicate_platform_field")
    return platform


def environment_channel(channel: dict) -> dict:
    kind = channel.get("kind")
    if kind == "server":
        if "platform" in channel:
            raise PlatformContractError("server_platform_forbidden")
        return {"kind": kind}
    platform = target_platform(channel.get("platform"))
    if kind != ("web" if platform == "web" else "app"):
        raise PlatformContractError("channel_kind_conflict", "kind")
    return {"kind": kind, "platform": platform}


def batch_platforms(platforms_by_sn) -> list[str]:
    if not isinstance(platforms_by_sn, dict) or not platforms_by_sn:
        raise PlatformContractError("batch_targets_required", "platforms_by_sn")
    for sn, value in platforms_by_sn.items():
        if not isinstance(sn, str) or not sn:
            raise PlatformContractError("device_identity_missing", "platforms_by_sn")
        target_platform(value, field="platforms_by_sn")
    return [p for p in TARGET_PLATFORMS if p in platforms_by_sn.values()]


def registration_shadow(host, devices, runtime_capabilities) -> dict:
    """Bounded, value-free diagnostics. Never changes registration/routing."""
    from collections import Counter
    counts = Counter()
    try:
        host_os((host or {}).get("os"))
    except PlatformContractError as exc:
        counts[exc.code] += 1
    for device in devices:
        try:
            target_platform(device.get("platform"))
        except PlatformContractError as exc:
            counts[exc.code] += 1
        if any(device.get(k) for k in ("os", "native_os", "device_platform", "target_platform")):
            counts["duplicate_platform_field"] += 1
    if runtime_capabilities is None:
        counts["runtime_capabilities_unreported"] += 1
    else:
        for capability in runtime_capabilities:
            try:
                if not isinstance(capability, dict):
                    raise PlatformContractError("runtime_capability_invalid")
                if "platform" in capability:
                    target_platform(capability["platform"])
                elif "platforms" in capability:
                    platform_set(capability["platforms"])
                else:
                    raise PlatformContractError("runtime_capability_platform_missing")
            except PlatformContractError as exc:
                counts[exc.code] += 1
    return {"mode": "shadow", "target_version": PLATFORM_CONTRACT_VERSION,
            "activated": False, "observed_device_count": len(devices),
            "issues": dict(sorted(counts.items())), "migration_required": bool(counts)}


def native_request_platform(request: dict, *, node_id: str, host_system: str) -> str:
    """Validate canonical native requests without interpreting aliases or SN prefixes."""
    platform = execute_platform(request)
    if platform not in NATIVE_PLATFORMS:
        raise PlatformContractError("native_platform_required")
    device_identity({"platform": platform, "sn": request.get("sn"),
                     "session_key": (request.get("device_hint") or {}).get("session_key")},
                    node_id=node_id, host={"os": host_os_from_system(host_system)})
    return platform


def runtime_capabilities_contract(rows, *, system):
    """Validate nested declarations before they can influence routing or revision."""
    if not isinstance(rows, list) or len(rows) > 256:
        raise PlatformContractError("runtime_capabilities_invalid")
    seen = set()
    budget = [4096]
    def walk(value, depth=0):
        budget[0] -= 1
        if depth > 12 or budget[0] < 0:
            raise PlatformContractError("capability_contract_too_large")
        if isinstance(value, dict):
            if any(k in value for k in ("os", "native_os", "device_platform", "target_platform")):
                raise PlatformContractError("duplicate_platform_field")
            if "platform" in value:
                target_platform(value["platform"])
            if "platforms" in value or "platform_scope" in value:
                coverage(value)
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
    for row in rows:
        if not isinstance(row, dict):
            raise PlatformContractError("runtime_capability_invalid")
        walk(row)
        cap, executor = row.get("id"), row.get("executor")
        if not isinstance(cap, str) or not cap or not isinstance(executor, str) or not executor:
            raise PlatformContractError("runtime_capability_identity_required")
        if "platform" in row:
            platforms = [target_platform(row["platform"])]
        else:
            c = coverage(row)
            platforms = list(TARGET_PLATFORMS) if c["platform_scope"] == "all" else c["platforms"]
        for p in platforms:
            key = (cap, executor, p)
            if key in seen:
                raise PlatformContractError("runtime_capability_duplicate")
            seen.add(key)
            if executor in NATIVE_PLATFORMS and (p != executor or p != system):
                raise PlatformContractError("executor_platform_conflict")
            if executor in {"adb", "playwright", "ios_wda"} and p != {"adb": "android", "playwright": "web", "ios_wda": "ios"}[executor]:
                raise PlatformContractError("executor_platform_conflict")
        if cap != "input_text":
            continue
        if type(row.get("contract_version")) is not int or row["contract_version"] != 2:
            raise PlatformContractError("input_contract_version_invalid")
        combos = row.get("supported_combinations")
        if not isinstance(combos, list) or not combos or len(combos) > 16:
            raise PlatformContractError("input_combinations_invalid")
        native_modes = {"current_focus": {"replace", "append"}}
        allowed = {"mac": native_modes, "win": native_modes,
                   "adb": {"current_focus": {"append"}, "coordinate": {"replace"}},
                   "ios_wda": {"current_focus": {"replace"}},
                   "playwright": {"current_focus": {"replace", "append"}, "coordinate": {"replace"}, "node": {"replace"}}}.get(executor, {})
        modes_seen = set()
        for combo in combos:
            if not isinstance(combo, dict) or set(combo) != {"target_mode", "write_modes"}:
                raise PlatformContractError("input_combination_invalid")
            mode, writes = combo["target_mode"], combo["write_modes"]
            if not isinstance(mode, str) or mode not in allowed or mode in modes_seen:
                raise PlatformContractError("input_target_mode_invalid")
            modes_seen.add(mode)
            if (not isinstance(writes, list) or not writes or
                any(not isinstance(w, str) or w not in allowed[mode] for w in writes) or
                len(set(writes)) != len(writes)):
                raise PlatformContractError("input_write_modes_invalid")


def validate_platform_announcement(message, *, fallback_host=None):
    from dataclasses import asdict
    if type(message.platform_contract_version) is not int or message.platform_contract_version != 1:
        raise PlatformContractError("platform_contract_upgrade_required")
    host = message.host or fallback_host
    if not isinstance(host, dict):
        raise PlatformContractError("host_os_invalid", "host.os")
    system = host_os(host.get("os"))
    if getattr(message, "platform", ""):
        raise PlatformContractError("duplicate_platform_field", "REGISTER.platform")
    devices = getattr(message, "devices", getattr(message, "device_delta", []))
    seen = set()
    for device in devices:
        wire = {k: v for k, v in asdict(device).items() if k != "os" or v}
        device_identity(wire, node_id=message.node_id, host=host)
        if device.sn in seen:
            raise PlatformContractError("device_identity_duplicate", "sn")
        seen.add(device.sn)
    runtime_capabilities_contract(message.runtime_capabilities, system=system)
