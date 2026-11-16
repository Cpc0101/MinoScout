"""本机守护进程的 pid / status / stop。

Studio 和运维都走这三件事，不要再让人去 `launchctl unload`。
`run` 在 `scout.lock` 上 flock 后写 pid 再 dial Nexus，避免 kickstart 与旧进程双 REGISTER；
`status` 看进程在不在；`stop` 发 SIGTERM，Scout 自己发
`EXECUTE node.shutting_down` 再退出。macOS LaunchAgent、Linux systemd 与 Windows
Scheduled Task 都会在启动前检查并修复服务定义；正常退出不会被拉起来。
"""
from __future__ import annotations

import json
import os
import plistlib
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mino_scout.config import config_dir, config_path, load_config, resolve_runtime, resolve_scout_id
from mino_scout.tools.keep_alive.log import SLog

TAG = "Service"

_STALE_LOCK_WAIT_SEC = 5.0
_STALE_LOCK_POLL_SEC = 0.12
_LAUNCH_AGENT_LABEL = "com.mino.scout"
_SYSTEMD_UNIT_NAME = "mino-scout.service"


def pid_path() -> Path:
    return config_dir() / "scout.pid"


def lock_path() -> Path:
    """`mino-scout run` 单实例锁；持有期间才允许 dial Nexus。"""
    return config_dir() / "scout.lock"


def write_pid(pid: int | None = None) -> Path:
    from mino_scout.config import claim_environment
    claim_environment()
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid or os.getpid())), encoding="utf-8")
    return path


def read_pid() -> int:
    path = pid_path()
    if not path.is_file():
        return 0
    try:
        return int((path.read_text(encoding="utf-8") or "").strip() or "0")
    except (OSError, ValueError):
        return 0


def clear_pid(*, only_if: int = 0) -> None:
    path = pid_path()
    if only_if:
        if read_pid() != only_if:
            return
    try:
        path.unlink()
    except OSError:
        pass


def _pid_from_lock_file(fh: Any) -> int:
    try:
        fh.seek(0)
        raw = (fh.read() or b"").decode("utf-8", errors="ignore").strip()
    except OSError:
        return read_pid()
    if not raw:
        return read_pid()
    try:
        return int(raw.split()[0])
    except ValueError:
        return read_pid()


class _RunLockHold:
    def __init__(self) -> None:
        self._fh: Any = None

    def try_acquire(self, *, stale_wait_sec: float = _STALE_LOCK_WAIT_SEC) -> tuple[bool, str]:
        from mino_scout.config import claim_environment
        claim_environment()
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.5, float(stale_wait_sec))
        last_holder = 0
        while True:
            fh = open(path, "a+b")
            if self._try_flock_nb(fh):
                self._fh = fh
                pid = os.getpid()
                fh.seek(0)
                fh.truncate()
                fh.write(f"{pid}\n".encode("utf-8"))
                fh.flush()
                return True, ""
            holder = _pid_from_lock_file(fh)
            last_holder = holder or last_holder
            fh.close()
            if holder and pid_alive(holder) and holder != os.getpid():
                return False, f"已有 Scout 在运行（pid={holder}），本进程跳过 dial"
            if time.time() >= deadline:
                if holder and pid_alive(holder):
                    return False, f"无法取得单实例锁（pid={holder} 仍存活）"
                return False, "无法取得单实例锁（等待旧进程释放超时）"
            time.sleep(_STALE_LOCK_POLL_SEC)

    @staticmethod
    def _try_flock_nb(fh: Any) -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        except OSError:
            return False

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


_RUN_LOCK_HELD = False


def run_lock_held() -> bool:
    """本进程正持有单实例锁；旧 Scout 进程存活时不可能为真。"""
    return _RUN_LOCK_HELD


@contextmanager
def daemon_run_lock(*, stale_wait_sec: float = _STALE_LOCK_WAIT_SEC) -> Iterator[bool]:
    """Yield True 表示已取得 flock，可 dial Nexus；False 时调用方应直接退出 run。"""
    global _RUN_LOCK_HELD
    hold = _RunLockHold()
    ok, msg = hold.try_acquire(stale_wait_sec=stale_wait_sec)
    if not ok:
        if msg:
            SLog.i(TAG, msg)
        yield False
        return
    try:
        write_pid()
        _RUN_LOCK_HELD = True
        yield True
    finally:
        _RUN_LOCK_HELD = False
        hold.release()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import psutil
        try:
            return psutil.Process(pid).is_running()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if os.name == "posix":
        # 僵尸 / 复用：/proc 不存在就当死了（Linux）；macOS kill(0) 已够用
        proc = Path(f"/proc/{pid}")
        if proc.exists() and proc.is_dir():
            try:
                stat = (proc / "stat").read_text(encoding="utf-8")
                # state Z = zombie
                parts = stat.split()
                if len(parts) > 2 and parts[2] == "Z":
                    return False
            except OSError:
                pass
    return True


def public_status() -> dict[str, Any]:
    """不含 token 明文，供裸 `mino-scout` 默认输出。"""
    st = collect_status()
    st.pop("has_token", None)
    return st


def cmd_public_status() -> int:
    print(json.dumps(public_status(), ensure_ascii=False, indent=2))
    return 0


def _service_argv() -> tuple[Path, list[str]]:
    from mino_scout.tools.keep_alive.process_control import run_argv

    argv = run_argv()
    return Path(argv[0]).expanduser().resolve(), argv[1:]


def _service_environment() -> dict[str, str]:
    """Environment needed by a login-launched Scout, including frozen TLS CAs."""
    prefix = config_dir()
    environment = {
        "MINO_SCOUT_HOME": str(prefix),
        "HOME": str(Path.home()),
        "PLAYWRIGHT_BROWSERS_PATH": str(prefix / "bin" / "ms-playwright"),
    }
    if os.name == "nt":
        environment["USERPROFILE"] = str(Path.home())
    elif sys.platform == "darwin":
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
    else:
        environment["PATH"] = str(os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    try:
        from mino_scout.runtime.startup import configure_tls_environment

        environment.update(configure_tls_environment())
    except Exception as exc:
        SLog.w(TAG, f"服务 CA 环境初始化失败: {exc}")
    return environment


def _write_if_changed(path: Path, data: bytes, *, mode: int | None = None) -> bool:
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return True


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCH_AGENT_LABEL}.plist"


def _launch_agent_payload() -> dict[str, Any]:
    prefix = config_dir()
    binary, arguments = _service_argv()
    return {
        "Label": _LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(binary), *arguments],
        "WorkingDirectory": str(prefix),
        "EnvironmentVariables": _service_environment(),
        "RunAtLoad": True,
        "KeepAlive": {"Crashed": True},
        "StandardOutPath": str(prefix / "logs" / "scout.log"),
        "StandardErrorPath": str(prefix / "logs" / "scout.err.log"),
    }


def ensure_launch_agent() -> tuple[Path, bool]:
    """Create or repair the per-user macOS LaunchAgent before starting Scout."""
    path = launch_agent_path()
    payload = _launch_agent_payload()
    (config_dir() / "logs").mkdir(parents=True, exist_ok=True)
    return path, _write_if_changed(path, plistlib.dumps(payload), mode=0o644)


def systemd_unit_path() -> Path:
    """Per-user on normal Linux installs, system-wide only when Scout runs as root."""
    if os.geteuid() == 0:
        return Path("/etc/systemd/system") / _SYSTEMD_UNIT_NAME
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return config_home.expanduser() / "systemd" / "user" / _SYSTEMD_UNIT_NAME


def _systemd_quote(value: str) -> str:
    escaped = str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _systemd_unit_text() -> str:
    prefix = config_dir()
    binary, arguments = _service_argv()
    wanted_by = "multi-user.target" if os.geteuid() == 0 else "default.target"
    command = " ".join(_systemd_quote(part) for part in [str(binary), *arguments])
    lines = [
        "[Unit]",
        "Description=Mino Scout",
        "After=network-online.target",
    ]
    if os.geteuid() == 0:
        lines.append("Wants=network-online.target")
    lines.extend([
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={command}",
        f"WorkingDirectory={_systemd_quote(str(prefix))}",
    ])
    lines.extend(f"Environment={_systemd_quote(f'{key}={value}')}" for key, value in _service_environment().items())
    lines.extend([
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "[Install]",
        f"WantedBy={wanted_by}",
        "",
    ])
    return "\n".join(lines)


def ensure_systemd_unit() -> tuple[Path, bool]:
    path = systemd_unit_path()
    return path, _write_if_changed(path, _systemd_unit_text().encode("utf-8"), mode=0o644)


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def windows_task_name() -> str:
    import hashlib

    suffix = hashlib.sha256(str(config_dir()).lower().encode("utf-8")).hexdigest()[:12]
    return f"Mino Scout {suffix}"


def windows_launcher_path() -> Path:
    return config_dir() / "run-scout.ps1"


def windows_startup_path() -> Path:
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    suffix = windows_task_name().rsplit(" ", 1)[-1]
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"MinoScout-{suffix}.cmd"


def _windows_launcher_text() -> str:
    prefix = config_dir()
    binary, arguments = _service_argv()
    lines = ["$ErrorActionPreference = 'Stop'"]
    lines.extend(f"$env:{key} = {_powershell_quote(value)}" for key, value in _service_environment().items())
    lines.append(f"Set-Location -LiteralPath {_powershell_quote(str(prefix))}")
    command = " ".join(_powershell_quote(part) for part in [str(binary), *arguments])
    lines.append(f"& {command}")
    lines.append("")
    return "\n".join(lines)


def _windows_startup_text(launcher: Path) -> str:
    rendered = str(launcher).replace("%", "%%")
    return (
        "@echo off\r\n"
        "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass "
        f'-File "{rendered}"\r\n'
    )


def ensure_windows_autostart() -> tuple[str, bool]:
    """Register a per-user logon task; retain Startup-folder fallback on failure."""
    import base64
    import subprocess

    launcher = windows_launcher_path()
    _write_if_changed(launcher, _windows_launcher_text().encode("utf-8"), mode=0o600)
    task = windows_task_name()
    script = f"""
$ErrorActionPreference = 'Stop'
$taskName = {_powershell_quote(task)}
$launcher = {_powershell_quote(str(launcher))}
$prefix = {_powershell_quote(str(config_dir()))}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{{0}}"' -f $launcher
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $prefix
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    registered = False
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        registered = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        registered = False

    startup = windows_startup_path()
    if registered:
        try:
            startup.unlink()
        except OSError:
            pass
    else:
        _write_if_changed(startup, _windows_startup_text(launcher).encode("utf-8"), mode=0o600)
        SLog.w(TAG, f"计划任务注册失败，已使用启动目录兜底: {startup}")
    return task, registered


def try_start_service(*, wait_sec: float = 4.0) -> int:
    """拉起平台服务；启动前自动创建或修复对应服务的启动定义。"""
    import subprocess
    import sys

    if pid_alive(read_pid()):
        return cmd_public_status()

    from mino_scout.config import development
    if development():
        from mino_scout.tools.update.development_runtime import start
        return start(wait_sec=wait_sec)

    started = False
    strict_service = False
    if sys.platform == "darwin":
        strict_service = True
        uid = os.getuid()
        target = f"gui/{uid}/{_LAUNCH_AGENT_LABEL}"
        try:
            plist, changed = ensure_launch_agent()
        except Exception as exc:
            SLog.w(TAG, f"LaunchAgent 初始化失败: {exc}")
            plist, changed = launch_agent_path(), False
        if changed:
            try:
                subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        commands = (
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
            ["launchctl", "kickstart", target],
            ["launchctl", "load", str(plist)],
            ["launchctl", "kickstart", target],
        )
        for cmd in commands:
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=15)
                if result.returncode == 0:
                    started = True
            except (OSError, subprocess.TimeoutExpired):
                continue
    elif sys.platform.startswith("linux"):
        try:
            ensure_systemd_unit()
        except Exception as exc:
            SLog.w(TAG, f"systemd unit 初始化失败: {exc}")
        scope = ["systemctl"] if os.geteuid() == 0 else ["systemctl", "--user"]
        commands = (
            [*scope, "daemon-reload"],
            [*scope, "enable", "--now", _SYSTEMD_UNIT_NAME],
        )
        for cmd in commands:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=15)
                if r.returncode == 0:
                    started = True
            except (OSError, subprocess.TimeoutExpired):
                continue

    elif os.name == "nt":
        task = windows_task_name()
        try:
            task, _ = ensure_windows_autostart()
        except Exception as exc:
            SLog.w(TAG, f"Windows 自启动初始化失败: {exc}")
        try:
            result = subprocess.run(["schtasks.exe", "/Run", "/TN", task], capture_output=True, timeout=15)
            started = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass

    deadline = time.time() + max(0.5, wait_sec)
    while time.time() < deadline:
        if pid_alive(read_pid()):
            return cmd_public_status()
        time.sleep(0.25)

    if not started and not strict_service:
        SLog.i(TAG, "未找到系统服务，后台启动 mino-scout run")
        from mino_scout.tools.keep_alive.process_control import spawn_run
        spawn_run()
        deadline = time.time() + max(0.5, wait_sec)
        while time.time() < deadline:
            if pid_alive(read_pid()):
                return cmd_public_status()
            time.sleep(0.25)

    st = public_status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0 if st.get("running") else 1


def collect_status() -> dict[str, Any]:
    pid = read_pid()
    running = pid_alive(pid)
    ws, token = resolve_runtime()
    cfg = load_config()
    layers: dict[str, str] | None = None
    layers_file = config_dir() / "bin" / "layers.txt"
    if layers_file.is_file():
        try:
            from mino_scout.tools.update.install_plan import parse_layers_txt

            layers = parse_layers_txt(layers_file.read_text(encoding="utf-8"))
        except Exception:
            layers = None

    side: dict[str, Any] = {}
    if running:
        try:
            from mino_scout.tools.keep_alive.runtime_sidecar import read_runtime_sidecar

            side = read_runtime_sidecar(expect_pid=pid) or {}
        except Exception:
            side = {}

    try:
        from mino_scout.tools.update.app_version import resolve_status_versions

        ver_bundle = resolve_status_versions(
            config_version=str(cfg.get("version") or ""),
            layers=layers,
            sidecar=side,
            running=running,
        )
    except Exception:
        ver_bundle = {"version": str(cfg.get("version") or "")}

    out: dict[str, Any] = {
        "running": running,
        "pid": pid if running else 0,
        "pid_file": str(pid_path()),
        "config": str(config_path()),
        "nexus": ws,
        "has_token": bool(token),
        "scout_id": resolve_scout_id(),
        **ver_bundle,
    }
    from mino_scout.config import runtime_identity
    out["configured_identity"] = runtime_identity()
    # A status command describes itself separately from the live daemon.
    out["running_identity"] = side.get("runtime_identity") if running else None
    from mino_scout.tools.keep_alive.runtime_sidecar import connection_status, read_update_status
    out["connection"] = connection_status(side, running=running)
    update = read_update_status()
    if update:
        out["update"] = update
    # 与旧 Studio / 脚本兼容：三字段与 version 对齐，避免 0.1.42 vs 0.1.43 混显。
    canon = str(out.get("version") or "")
    out["app_semver"] = canon
    out["reported_version"] = canon
    out["process_version"] = str(out.get("running_version") or canon)
    bin_path = config_dir() / "bin" / ("mino-scout.exe" if os.name == "nt" else "mino-scout")
    if bin_path.is_file():
        out["binary"] = str(bin_path)
    if layers:
        out["installed_layers"] = layers
    if running:
        try:
            from mino_scout.tools.keep_alive.power import get_guard

            out["power"] = get_guard().status()
        except Exception:
            out["power"] = {}
    return out


def schedule_reexec(*, delay_sec: float = 1.5) -> dict[str, Any]:
    """向后兼容：委托 reexec_spawn（热更后请优先 import reexec_spawn）。"""
    from importlib import import_module

    return import_module("mino_scout.tools.update.reexec_spawn").schedule_reexec(delay_sec=delay_sec)


def request_stop(*, timeout_sec: float = 15.0) -> dict[str, Any]:
    pid = read_pid()
    if not pid_alive(pid):
        clear_pid()
        return {"ok": True, "already": True, "running": False, "pid": 0}

    SLog.i(TAG, f"停止 pid={pid}")
    created_at = 0.0
    if os.name == "nt":
        import psutil
        from mino_scout.tools.keep_alive.process_control import request_local_stop
        try:
            created_at = psutil.Process(pid).create_time()
            request_local_stop(pid)
        except psutil.NoSuchProcess:
            return {"ok": True, "already": True, "running": False, "pid": 0}
    else:
        _signal(pid, signal.SIGTERM)
    deadline = time.time() + max(1.0, timeout_sec)
    while time.time() < deadline:
        if not pid_alive(pid):
            clear_pid(only_if=pid)
            return {"ok": True, "already": False, "running": False, "pid": pid}
        time.sleep(0.2)

    SLog.w(TAG, f"SIGTERM 超时，改发 SIGKILL pid={pid}")
    if os.name == "nt":
        from mino_scout.tools.keep_alive.process_control import terminate_windows
        terminate_windows(pid, created_at)
    else:
        _signal(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    time.sleep(0.4)
    alive = pid_alive(pid)
    if not alive:
        clear_pid(only_if=pid)
    return {"ok": not alive, "already": False, "running": alive, "pid": pid, "killed": True}


def _signal(pid: int, sig: int) -> None:
    if os.name == "nt":
        raise RuntimeError("Windows process control must not use POSIX signals")
    try:
        os.kill(pid, sig)
    except OSError as exc:
        SLog.w(TAG, f"kill({pid}, {sig}) 失败: {exc}")
