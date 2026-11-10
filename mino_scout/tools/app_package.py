"""安装包来源解析与包内身份读取。

Scout 只接受「本机可访问的路径」或「可下载地址」，不接受经 Nexus 上传安装包：

- `path`：Scout 主机上的绝对路径。SMB / NFS 已挂载后就是普通路径。
- `url` ：`http(s)` 直链，下载到临时文件后安装。
- `url` ：`smb://[用户@]主机/共享名/文件路径`，macOS 上用 `open -g` 挂载共享盘、
  等卷出现后按剩余路径取文件（凭证走钥匙串，不接受明文密码）。

下载与挂载都产生临时状态；调用方用 `release()` 清理。
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from mino_scout.tools.keep_alive.log import SLog

TAG = "AppPackage"

_SMB_RE = re.compile(r"^smb://(?:([^@/]+)@)?([^/]+)/([^/]+)(?:/(.+))?$")
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)
# 只解析这些后缀的包内身份；其余只回报大小与摘要。
_INSPECTABLE = {".ipa", ".app", ".dmg", ".pkg", ".apk", ".apks", ".xapk", ".apkm"}
_MAX_BYTES = 2 * 1024**3


class PackageError(ValueError):
    """取包或验包失败。消息直接给用户看，不夹内部栈。"""

    def __init__(self, message: str, *, code: str = "package_invalid"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 来源解析


def resolve_source(params: dict[str, Any], *, platform: str = "") -> tuple[Path, str]:
    """→ (本机路径, 需要清理的临时文件)。给不出路径时抛 `PackageError`。"""
    raw = dict(params or {})
    path = str(raw.get("path") or "").strip()
    url = str(raw.get("url") or "").strip()
    file_name = str(raw.get("file_name") or raw.get("file") or "").strip()
    temp = ""
    if not path and url:
        if url.lower().startswith("smb://"):
            path = str(_mount_smb(url, timeout_sec=_float(raw.get("mount_timeout_sec"), 45.0)))
        elif _HTTP_RE.match(url):
            path = _download(url, file_name, platform=platform)
            temp = path
        else:
            raise PackageError("url 只支持 http(s) 直链或 smb:// 共享路径；其它协议请先挂载后用 path")
    if not path:
        raise PackageError("缺少 path 或 url")
    target = Path(path).expanduser()
    if not target.exists():
        raise PackageError(f"安装包不存在：{target}")
    if target.is_dir() and target.suffix.lower() != ".app":
        raise PackageError(f"安装包路径是目录：{target}")
    return target, temp


def release(temp: str) -> None:
    """删掉为本次安装下载的临时文件；失败不抛。"""
    if not temp:
        return
    try:
        os.remove(temp)
    except OSError:
        pass


def _float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _download(url: str, file_name: str, *, platform: str) -> str:
    from mino_scout.tools.update.http_fetch import download_file

    name = file_name or urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "package"
    suffix = Path(name).suffix.lower()
    if platform == "android" and suffix not in {".apk", ".apks", ".xapk", ".apkm"}:
        name += ".apk"
    if platform == "ios" and suffix != ".ipa":
        name += ".ipa"
    fd, temp = tempfile.mkstemp(prefix="mino_pkg_", suffix="_" + name)
    os.close(fd)
    try:
        download_file(url, Path(temp), timeout=600.0)
    except Exception as exc:
        release(temp)
        raise PackageError(f"下载失败：{type(exc).__name__}: {exc}") from exc
    if os.path.getsize(temp) == 0:
        release(temp)
        raise PackageError("下载得到的文件为空")
    if os.path.getsize(temp) > _MAX_BYTES:
        release(temp)
        raise PackageError("下载文件超过 2 GiB，拒绝安装")
    return temp


def _mount_smb(url: str, *, timeout_sec: float) -> str:
    match = _SMB_RE.match(url)
    if not match:
        raise PackageError("smb 地址要写成 smb://[用户@]主机/共享名/文件路径")
    user, host, share, rest = match.group(1) or "", match.group(2), match.group(3), (match.group(4) or "")
    if not rest:
        raise PackageError("smb 地址缺少共享盘内的文件路径")
    if sys.platform != "darwin":
        raise PackageError("smb:// 自动挂载只在 macOS 主机支持；其它系统请先挂载再传 path")
    root = _smb_root(share)
    if root is None:
        source = f"smb://{user + '@' if user else ''}{host}/{share}"
        SLog.i(TAG, f"挂载共享盘 {source}")
        try:
            subprocess.run(["open", "-g", source], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PackageError(f"挂载共享盘失败：{exc}") from exc
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline and root is None:
            time.sleep(1.0)
            root = _smb_root(share)
    if root is None:
        raise PackageError(f"共享盘 {share} 没有挂载出来：确认主机可达、钥匙串里有该共享的凭证")
    return str(root / rest)


def _smb_root(share: str) -> Path | None:
    volumes = Path("/Volumes")
    if not volumes.is_dir():
        return None
    candidates = []
    for entry in volumes.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name == share or re.fullmatch(re.escape(share) + r"\s+\d+", name):
            candidates.append(entry)
    return sorted(candidates, key=lambda item: len(item.name))[0] if candidates else None


# ---------------------------------------------------------------- 包内身份


def sha256_file(path: Path, *, limit: int = _MAX_BYTES) -> str:
    """装前摘要，用来在回执里指认「装的是哪一份字节」。

    `.app` 是目录，摘要按目录内容算（路径 + 大小 + mtime），不是读文件。
    """
    if path.is_dir():
        return _sha256_tree(path)
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            read += len(chunk)
            if read > limit:
                break
    return digest.hexdigest()


def _sha256_tree(root: Path) -> str:
    """目录摘要：相对路径 + 大小 + 修改时间，按路径排序后喂给同一个哈希。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        try:
            stat = path.stat()
        except OSError:
            continue
        row = f"{path.relative_to(root)}\0{stat.st_size}\0{int(stat.st_mtime)}\n"
        digest.update(row.encode("utf-8", "replace"))
    return digest.hexdigest()


def inspect_package(path: Path, *, platform: str = "") -> dict[str, Any]:
    """读包内身份与摘要。读不出来不抛，返回带 `reason` 的空结果。"""
    out: dict[str, Any] = {"file": str(path), "size": _size(path), "sha256": "", "reason": ""}
    try:
        out["sha256"] = sha256_file(path)
    except OSError as exc:
        out["reason"] = f"unreadable: {exc}"
        return out
    suffix = path.suffix.lower()
    bare_app = path.is_dir() and suffix != ".app"
    if bare_app:
        out["reason"] = "not_a_bundle: 目录必须用 .app 结尾"
        return out
    if suffix not in _INSPECTABLE:
        out["reason"] = f"unsupported_suffix: {suffix or 'none'}"
        return out
    try:
        if suffix == ".ipa":
            out.update(_inspect_ipa(path))
        elif suffix == ".app":
            out.update(_inspect_bundle(path))
        elif suffix == ".apk":
            out.update(_inspect_apk(path))
        elif suffix in {".dmg", ".pkg", ".apks", ".xapk", ".apkm"}:
            out["reason"] = "identity_not_read_without_mount"
    except Exception as exc:  # 验包是旁路，不能因为读不出元数据拒绝安装
        out["reason"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def _size(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        return path.stat().st_size
    except OSError:
        return 0


def _inspect_bundle(bundle: Path) -> dict[str, Any]:
    info_plist = bundle / "Contents" / "Info.plist"
    if not info_plist.is_file():
        info_plist = bundle / "Info.plist"
    if not info_plist.is_file():
        return {"reason": "info_plist_missing"}
    with info_plist.open("rb") as handle:
        info = plistlib.load(handle)
    return {
        "bundle_id": str(info.get("CFBundleIdentifier") or ""),
        "version": str(info.get("CFBundleShortVersionString") or ""),
        "build": str(info.get("CFBundleVersion") or ""),
        "name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or ""),
        "executable": str(info.get("CFBundleExecutable") or ""),
    }


def _inspect_apk(path: Path) -> dict[str, Any]:
    """APK 身份来自二进制 `AndroidManifest.xml`。不依赖 aapt / aapt2。"""
    from mino_scout.tools import axml

    with zipfile.ZipFile(path) as archive:
        raw = archive.read("AndroidManifest.xml")
    info = axml.manifest_identity(raw)
    return {
        "bundle_id": info.get("package", ""),
        "version": info.get("version_name", ""),
        "build": info.get("version_code", ""),
        "name": info.get("label", ""),
        "compile_sdk": info.get("compile_sdk", ""),
        "min_sdk": info.get("min_sdk", ""),
    }


def _inspect_ipa(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        bundles = sorted({
            name.split("/")[1] for name in names
            if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)
        })
        if not bundles:
            raise PackageError("IPA 结构无效：找不到 Payload/*.app/Info.plist")
        bundle_name = bundles[0]
        prefix = f"Payload/{bundle_name}/"
        info = plistlib.loads(archive.read(prefix + "Info.plist"))
        profile_name = prefix + "embedded.mobileprovision"
        profile = _read_profile(archive.read(profile_name)) if profile_name in names else {}
    return {
        "bundle_id": str(info.get("CFBundleIdentifier") or ""),
        "version": str(info.get("CFBundleShortVersionString") or ""),
        "build": str(info.get("CFBundleVersion") or ""),
        "name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or ""),
        "executable": str(info.get("CFBundleExecutable") or ""),
        "app_bundle": prefix.rstrip("/"),
        "signing": profile,
    }


_PROFILE_PLIST = re.compile(rb"<\?xml.*?</plist>", re.DOTALL)


def _read_profile(raw: bytes) -> dict[str, Any]:
    """描述文件是 CMS 包着的 plist；只取明文 plist 段，不校验证书链。"""
    match = _PROFILE_PLIST.search(raw)
    if not match:
        return {"reason": "profile_not_plain_plist"}
    try:
        payload = plistlib.loads(match.group(0))
    except Exception as exc:
        return {"reason": f"profile_parse_failed: {type(exc).__name__}"}
    entitlements = payload.get("Entitlements") if isinstance(payload.get("Entitlements"), dict) else {}
    devices = payload.get("ProvisionedDevices") or payload.get("ProvisionedDeviceIDs") or []
    expiry = payload.get("ExpirationDate")
    return {
        "name": str(payload.get("Name") or ""),
        "app_id": str(entitlements.get("application-identifier")
                      or payload.get("AppIDName") or ""),
        "team": list(payload.get("TeamIdentifier") or []),
        "devices": [str(item) for item in devices][:512],
        "device_count": len(devices),
        "expires": expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry or ""),
        "provisions_all_devices": bool(payload.get("ProvisionsAllDevices")),
        # 0 台且不是企业包 → App Store / TestFlight 描述文件，没有 UDID 白名单。
        "kind": ("enterprise" if payload.get("ProvisionsAllDevices")
                 else "adhoc" if devices else "store"),
    }


def copy_tree(source: Path, destination: Path) -> None:
    """`.app` 落盘：先删旧的再 `ditto`，失败时保留原目录。"""
    backup = destination.with_name(destination.name + ".mino-previous-" + str(int(time.time())))
    if destination.exists():
        shutil.move(str(destination), str(backup))
    try:
        proc = subprocess.run(
            ["ditto", str(source), str(destination)],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if backup.exists() and not destination.exists():
            shutil.move(str(backup), str(destination))
        raise PackageError(f"复制应用失败：{exc}") from exc
    if proc.returncode != 0:
        if backup.exists() and not destination.exists():
            shutil.move(str(backup), str(destination))
        raise PackageError((proc.stderr or proc.stdout or "ditto 失败")[:300])
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
