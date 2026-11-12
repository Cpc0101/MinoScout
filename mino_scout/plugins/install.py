"""按固定发布源安装 CLI。不接受调用方临时填的下载地址。

Scout 自己的 manifest 里如果带了 plugins 条目，用那一条。
没有的话，飞书和 Meego 走各自的官方发布源，点安装就开始下载。
"""
from __future__ import annotations

import base64
import json
import os
import uuid
from functools import wraps
import hashlib
import platform
import shutil
import tarfile
import threading
import zipfile
from pathlib import Path
from typing import Any

from mino_scout.tools.update.http_fetch import download_file, fetch_bytes, fetch_json
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.plugins import progress as PP
from mino_scout.plugins.host_exec import HostExecError, run_entry
from mino_scout.plugins.state import clear_plugin_secrets, plugin_dir, spec

TAG = "PluginInstall"
_MAX_BYTES = 512 * 1024 * 1024
_locks_guard = threading.Lock()
_locks: dict[tuple[str, str], threading.Lock] = {}


def acquire_plugin(kind: str, plugin_id: str):
    with _locks_guard:
        lock = _locks.setdefault((kind, plugin_id), threading.Lock())
    if not lock.acquire(blocking=False):
        raise PluginInstallError("tool_busy")
    return lock


class PluginInstallError(Exception):
    pass


def _os_arch() -> tuple[str, str]:
    system = platform.system()
    os_name = {"Darwin": "darwin", "Windows": "windows", "Linux": "linux"}.get(system, "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    return os_name, arch


def _manifest_plugin(kind: str, plugin_id: str) -> dict[str, Any] | None:
    from mino_scout.tools.update.self_update import manifest_url_from_config

    url = manifest_url_from_config()
    if not url:
        return None
    try:
        data = fetch_json(url, timeout=30)
    except Exception as exc:
        SLog.w(TAG, f"读 Scout manifest 失败，改走官方发布源: {exc}")
        return None
    rows = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("class") or "") == kind and str(row.get("id") or "") == plugin_id:
            return row
    return None


def _github_cli(repo: str, name_prefix: str, *, kind: str, plugin_id: str) -> dict[str, Any]:
    os_name, arch = _os_arch()
    try:
        release = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest", timeout=60)
    except Exception as exc:
        raise PluginInstallError(f"读取 {repo} 发布信息失败: {exc}") from exc
    tag = str(release.get("tag_name") or "").strip().lstrip("vV")
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    by_name = {
        str(row.get("name") or ""): str(row.get("browser_download_url") or "")
        for row in assets
        if isinstance(row, dict)
    }
    suffix = ".zip" if os_name == "windows" else ".tar.gz"
    filename = f"{name_prefix}-{tag}-{os_name}-{arch}{suffix}"
    url = by_name.get(filename) or ""
    if not url:
        raise PluginInstallError(f"{repo} 没有本机安装包 {filename}")
    checksums = by_name.get("checksums.txt") or ""
    sha = ""
    if checksums:
        blob = _download_bytes(checksums, kind, plugin_id, label="读取校验")
        sha = _checksum_for(blob.decode("utf-8", "replace"), filename)
    if len(sha) != 64:
        raise PluginInstallError(f"{repo} 的校验文件里没有 {filename}")
    return {"url": url, "sha256": sha, "filename": filename, "version": tag}


def _npm_tarball(package: str) -> dict[str, Any]:
    try:
        meta = fetch_json(f"https://registry.npmjs.org/{package}/latest", timeout=60)
    except Exception as exc:
        raise PluginInstallError(f"读取 {package} 失败: {exc}") from exc
    dist = meta.get("dist") if isinstance(meta.get("dist"), dict) else {}
    url = str(dist.get("tarball") or "")
    integrity = str(dist.get("integrity") or "")
    if not url.startswith("https://") or not integrity.startswith("sha512-"):
        raise PluginInstallError(f"{package} 没有可用的安装包")
    return {"url": url, "sha512": integrity, "filename": url.rsplit("/", 1)[-1], "version": str(meta.get("version") or "")}


def _resolve(kind: str, plugin_id: str) -> dict[str, Any]:
    listed = _manifest_plugin(kind, plugin_id)
    if listed and str(listed.get("url") or "").startswith("https://"):
        return listed
    if kind == "cli" and plugin_id == "feishu":
        return _github_cli("larksuite/cli", "lark-cli", kind=kind, plugin_id=plugin_id)
    if kind == "cli" and plugin_id == "meego":
        return _npm_tarball("@lark-project/meegle")
    raise PluginInstallError(f"没有 {kind}/{plugin_id} 的官方安装包")


def _checksum_for(text: str, filename: str) -> str:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].endswith(filename) and len(parts[0]) == 64:
            return parts[0].lower()
    return ""


_BIN_NAMES = {
    "feishu": ("lark-cli", "lark-cli.exe"),
    "meego": ("meegle", "meegle.exe", "meegle.js"),
}


def _download_bytes(url: str, kind: str, plugin_id: str, *, label: str) -> bytes:
    PP.emit("download", kind=kind, plugin_id=plugin_id, label=label, percent=8)
    try:
        return fetch_bytes(url, timeout=60)
    except Exception as exc:
        raise PluginInstallError(f"下载失败: {exc}") from exc


def _reject_path(name: str) -> None:
    if not name or name.startswith("/") or ".." in Path(name).parts:
        raise PluginInstallError("插件压缩包含目录外路径")


def _extract_archive(archive: Path, dest: Path, *, filename: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name = filename.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                _reject_path(str(info.filename or ""))
            zf.extractall(dest)
        return
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                _reject_path(member.name)
            tf.extractall(dest, filter="data")
        return
    raise PluginInstallError("不认识的安装包格式")


def _download_archive(url: str, dest: Path, kind: str, plugin_id: str) -> None:
    def on_progress(received: int, total: int) -> None:
        if received > _MAX_BYTES or (total and total > _MAX_BYTES):
            raise PluginInstallError("插件包超过 512MB")
        percent = 10 + int(60 * received / total) if total else 15
        PP.emit(
            "download",
            kind=kind,
            plugin_id=plugin_id,
            label=f"下载 {plugin_id}",
            percent=min(percent, 70),
            bytes_received=received,
            bytes_total=total,
        )

    PP.emit("download", kind=kind, plugin_id=plugin_id, label=f"下载 {plugin_id}", percent=10)
    try:
        download_file(url, dest, timeout=600, on_progress=on_progress)
    except PluginInstallError:
        raise
    except Exception as exc:
        raise PluginInstallError(f"下载失败: {exc}") from exc
    if dest.stat().st_size > _MAX_BYTES:
        raise PluginInstallError("插件包超过 512MB")


def _verify(path: Path, release: dict[str, Any]) -> None:
    sha256 = str(release.get("sha256") or "").strip().lower()
    integrity = str(release.get("sha512") or "").strip()
    blob = path.read_bytes()
    if len(sha256) == 64:
        if hashlib.sha256(blob).hexdigest() != sha256:
            raise PluginInstallError("插件包哈希不一致")
        return
    if integrity.startswith("sha512-"):
        expect = base64.b64decode(integrity.split("-", 1)[1])
        if hashlib.sha512(blob).digest() != expect:
            raise PluginInstallError("插件包哈希不一致")
        return
    raise PluginInstallError("安装包没有校验值")


def _link_invoke(root: Path, plugin_id: str) -> None:
    names = _BIN_NAMES.get(plugin_id, ())
    found: Path | None = None
    for path in root.rglob("*"):
        if path.is_file() and path.name in names:
            found = path
            break
    if found is None:
        return
    link = root / "invoke"
    if link.exists() or link.is_symlink():
        return
    try:
        link.symlink_to(found.relative_to(root))
    except OSError:
        shutil.copy2(found, link)
    if os_name_is_windows():
        return
    mode = link.stat().st_mode
    if not mode & 0o111:
        found.chmod(found.stat().st_mode | 0o755)


_INSTALL_SERIAL = threading.RLock()


def _serial_install(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _INSTALL_SERIAL:
            return fn(*args, **kwargs)
    return wrapped


@_serial_install
def install(node_id: str, kind: str, plugin_id: str, *, _held_lock=None, update=False) -> str:
    row = spec(kind, plugin_id)
    if row is None:
        raise PluginInstallError(f"未知插件 {kind}/{plugin_id}")
    if not row.get("needs_install"):
        raise PluginInstallError("这个插件不需要单独安装")
    if kind not in ("cli", "mcp"):
        raise PluginInstallError("只有 CLI、MCP 按需安装")
    from mino_scout.tools.update import update_progress as UP

    if UP.snapshot().get("active"):
        raise PluginInstallError("Scout 正在更新，等它结束再安装插件")
    lock = _held_lock or acquire_plugin(kind, plugin_id)
    dest = plugin_dir(kind, plugin_id)
    staging = dest.with_name(dest.name + ".staging-" + uuid.uuid4().hex)
    archive = staging.with_name(staging.name + ".pkg")
    try:
        PP.emit("plan", kind=kind, plugin_id=plugin_id, label="查找安装包", percent=5)
        release = _resolve(kind, plugin_id)
        url = str(release.get("url") or "").strip()
        filename = str(release.get("filename") or url.rsplit("/", 1)[-1] or "package.bin")
        if not url.startswith("https://") or "/" in filename or ".." in filename:
            raise PluginInstallError("安装包地址不完整")
        if dest.is_symlink():
            raise PluginInstallError("插件安装目录不能是外部链接")
        staging.mkdir(parents=True, exist_ok=False)
        _download_archive(url, archive, kind, plugin_id)
        PP.emit("verify", kind=kind, plugin_id=plugin_id, label="校验哈希", percent=80)
        _verify(archive, release)
        _extract_archive(archive, staging, filename=filename)
        archive.unlink(missing_ok=True)
        entry = "install.ps1" if os_name_is_windows() else "install.sh"
        script = staging / entry
        if script.is_file():
            PP.emit("install", kind=kind, plugin_id=plugin_id, label="安装", percent=90)
            try:
                ran = run_entry(
                    staging, entry, [], timeout_sec=600,
                    node_id=node_id, kind=kind, plugin_id=plugin_id,
                )
            except HostExecError as exc:
                raise PluginInstallError(str(exc)) from exc
            if int(ran.get("code") or 0) != 0:
                err = str(ran.get("stderr") or ran.get("stdout") or "安装入口失败")
                raise PluginInstallError(err)
        authorization = dest / ".authorization"
        if authorization.is_symlink():
            raise PluginInstallError("授权目录不能是外部链接")
        if authorization.is_dir():
            shutil.copytree(authorization, staging / ".authorization", dirs_exist_ok=True)
        _link_invoke(staging, plugin_id)
        if kind == 'cli' and not (staging / 'invoke').is_file():
            raise PluginInstallError("安装包没有可执行入口，保留原版本")
        (staging / ".installed").write_text(json.dumps({"version": release.get("version"), "sha256": release.get("sha256"), "sha512": release.get("sha512")}), encoding="utf-8")
        previous = dest.with_name(dest.name + ".previous-" + uuid.uuid4().hex)
        if dest.exists():
            os.replace(dest, previous)
        try:
            os.replace(staging, dest)
        except OSError:
            if previous.exists() and not dest.exists():
                os.replace(previous, dest)
            raise
        PP.emit("done", kind=kind, plugin_id=plugin_id, label="安装完成", percent=100, done=True)
        SLog.i(TAG, f"installed {kind}/{plugin_id}")
        from mino_scout.plugins.state import release_import_block

        release_import_block(kind, plugin_id)
        return "更新完成，原配置和授权已保留" if update else "安装完成"
    except Exception as exc:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        text = str(exc).strip() or "安装失败"
        PP.emit(
            "done", kind=kind, plugin_id=plugin_id,
            label=text[:80], percent=100, error=text[:300], done=True,
        )
        raise
    finally:
        if _held_lock is None:
            lock.release()


def remove(node_id: str, kind: str, plugin_id: str) -> str:
    lock = acquire_plugin(kind, plugin_id)
    try:
        row = spec(kind, plugin_id)
        if row is None or not row.get("needs_install"):
            raise PluginInstallError("仅支持卸载额外安装的插件")
        if kind == "cli" and plugin_id == "feishu":
            from mino_scout.plugins import feishu_auth
        summary = _remove(node_id, kind, plugin_id)
        if kind == "cli" and plugin_id == "feishu":
            feishu_auth.forget_uninstalled()
        return summary
    finally:
        lock.release()


def _remove(node_id: str, kind: str, plugin_id: str) -> str:
    row = spec(kind, plugin_id)
    if row is None:
        raise PluginInstallError(f"未知插件 {kind}/{plugin_id}")
    dest = plugin_dir(kind, plugin_id)
    if dest.is_dir():
        entry = "uninstall.ps1" if os_name_is_windows() else "uninstall.sh"
        if (dest / entry).is_file():
            try:
                run_entry(
                    dest, entry, [], timeout_sec=120,
                    node_id=node_id, kind=kind, plugin_id=plugin_id,
                )
            except HostExecError as exc:
                SLog.w(TAG, f"uninstall {kind}/{plugin_id}: {exc}")
        shutil.rmtree(dest, onerror=_force_writable)  # LangBot 的依赖环境目录是只读的
        shutil.rmtree(dest, ignore_errors=True)
    clear_plugin_secrets(node_id, kind, plugin_id)
    return "已移除"


def _force_writable(func, path, _exc) -> None:
    import os
    import stat

    try:
        os.chmod(path, stat.S_IRWXU)
        parent = os.path.dirname(path)
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR)
        func(path)
    except OSError:
        pass


def os_name_is_windows() -> bool:
    import os
    return os.name == "nt"
