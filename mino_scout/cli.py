"""mino-scout 命令行。

    mino-scout                                 服务在跑 → 公开 status；否则尝试拉起服务
    mino-scout run                             常驻（launchd / systemd 用）
    mino-scout probe                           只探测 manifest，不连 Nexus
    mino-scout status | stop | start
    mino-scout configure nexus-url <origin>    只改 Nexus 地址（token 由 Nexus REGISTER 下发）

`probe` 子命令是部署新节点时的第一步（docs/DEVICE_SETUP.md §7）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys

from mino_scout.core import SCOUT_VERSION, ScoutCore
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.service import (
    clear_pid,
    cmd_public_status,
    collect_status,
    request_stop,
    try_start_service,
)

TAG = "CLI"


def build_core() -> ScoutCore:
    """按本机实际情况装配 executor。"""
    executors: dict = {}

    from mino_scout.executors.adb_executor import AdbExecutor

    executors["adb"] = AdbExecutor()

    try:
        from mino_scout.executors.playwright_executor import PlaywrightExecutor

        executors["playwright"] = PlaywrightExecutor()
    except ImportError as exc:
        SLog.w(TAG, f"playwright executor 未装载: {exc}")

    from mino_scout.executors.desktop import DesktopExecutor
    native = DesktopExecutor()
    if native.id in {"mac", "win"}:
        executors[native.id] = native
    from mino_scout.executors.wda_executor import WdaExecutor
    executors["ios_wda"] = WdaExecutor()
    return ScoutCore(executors)


def cmd_probe(core: ScoutCore) -> int:
    execs, devices = core.manifest()
    out = {
        "scout_id": core.scout_id,
        "node_id": core.node_id,
        "scout_version": SCOUT_VERSION,
        "executors": [
            {"id": e.id, "available": e.available, "provides": list(e.provides), "reason": e.reason}
            for e in execs
        ],
        "devices": [
            __import__("dataclasses").asdict(d)
            for d in devices
        ],
    }
    payload = json.dumps(out, ensure_ascii=True, indent=2)
    try:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except Exception:
        sys.stdout.buffer.write((payload + "\n").encode("ascii"))
        sys.stdout.buffer.flush()
    if not any(e.available for e in execs):
        print("\n没有任何可用 executor —— 这台机器现在不能作为执行节点。", file=sys.stderr)
        return 1
    if not devices:
        print("\n没发现任何设备。", file=sys.stderr)
    return 0


def cmd_status() -> int:
    st = collect_status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0 if st.get("running") else 1


def cmd_stop() -> int:
    st = request_stop()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0 if st.get("ok") else 1


def cmd_heavy_deps() -> int:
    from mino_scout.tools.update.heavy_deps import install_heavy_deps

    core = build_core()
    try:
        out = install_heavy_deps(adb_hook=core.ensure_android_adb_keyboard_on_startup)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1




def cmd_update(argv: list[str]) -> int:
    from mino_scout.tools.update.fixed_update import submit
    ap = argparse.ArgumentParser(prog="mino-scout update")
    ap.add_argument("--build-id", required=True)
    ap.add_argument("--release-tag", required=True)
    ap.add_argument("--operation-id", required=True)
    ap.add_argument("--rollback-of", default="")
    ns = ap.parse_args(argv)
    try:
        out = submit(ns.operation_id, ns.build_id, release_tag=ns.release_tag, action="rollback" if ns.rollback_of else "update", rollback_of=ns.rollback_of)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def cmd_configure(argv: list[str]) -> int:
    if argv and argv[0] == "closed-lid":
        from mino_scout.tools.keep_alive.lid_sleep import configure
        try:
            if len(argv) != 2:
                raise ValueError("用法: mino-scout configure closed-lid on|off|status")
            result = configure(argv[1])
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if len(argv) == 2 and argv[0] == "source-root":
        from pathlib import Path
        from mino_scout.config import load_config, save_config
        root = Path(argv[1]).expanduser().resolve(strict=True)
        if not root.is_dir():
            print("来源根路径必须是目录", file=sys.stderr)
            return 2
        configuration = load_config()
        configuration["case_source_roots"] = sorted(set(configuration.get("case_source_roots", [])) | {str(root)})
        save_config(configuration)
        print(json.dumps({"ok": True, "source_root": str(root)}, ensure_ascii=False))
        return 0
    if not argv or argv[0] != "nexus-url":
        print("用法: mino-scout configure nexus-url <http://host:port>", file=sys.stderr)
        return 2
    if len(argv) < 2 or not str(argv[1]).strip():
        print("缺少 Nexus 地址", file=sys.stderr)
        return 2
    from mino_scout.config import set_nexus_url

    path = set_nexus_url(argv[1])
    print(json.dumps({"ok": True, "config": str(path), "nexus_url": str(argv[1]).strip()}, ensure_ascii=False))
    return 0


def cmd_default() -> int:
    st = collect_status()
    if st.get("running"):
        return cmd_public_status()
    from mino_scout.config import config_path, resolve_runtime

    _, token = resolve_runtime()
    if not token:
        print(
            f"未配置 token。请在 Workbench 的 Scout 节点页复制安装命令，或在 {config_path()} 写入 token。",
            file=sys.stderr,
        )
        return 2
    return try_start_service()


def cmd_run() -> int:
    from mino_scout.tools.update.fixed_update import startup_allowed
    if not startup_allowed():
        print("有未完成的更新维护操作，请使用 update-status 查询并从保留目录恢复", file=sys.stderr)
        return 3
    from mino_scout.config import config_path, resolve_runtime
    from mino_scout.platform.web.playwright_hub import apply_browsers_path, install_playwright_closed_quiet
    from mino_scout.tools.update.stale_process import ensure_process_matches_app_layer

    nexus, token = resolve_runtime()
    if not token:
        print(f"需要开发或正式环境对应的 token：{config_path()}", file=sys.stderr)
        return 2

    if ensure_process_matches_app_layer(log_tag=TAG):
        return 0

    apply_browsers_path()
    install_playwright_closed_quiet()

    core = build_core()

    from mino_scout.service import daemon_run_lock
    from mino_scout.server_comm.transport.node import NodeTransport

    with daemon_run_lock() as sole:
        if not sole:
            return 0

        transport = NodeTransport(core, nexus_url=nexus, token=token)
        _install_signals(transport)
        try:
            asyncio.run(transport.run_forever())
        except KeyboardInterrupt:
            transport.request_shutdown()
            SLog.i(TAG, "收到 Ctrl-C，退出")
        finally:
            try:
                core.shutdown()
            except Exception:
                pass
            from mino_scout.tools.keep_alive.power import get_guard

            get_guard().sync([])
            clear_pid(only_if=_os_getpid())
    return 0


def _install_signals(transport) -> None:
    def _handle(signum, _frame):
        SLog.i(TAG, f"收到信号 {signum}，准备退出")
        transport.request_shutdown()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle)


def _os_getpid() -> int:
    import os

    return os.getpid()


def cmd_check(argv: list[str]) -> int:
    """check [目录]：写完用例后自检，打印提示。有提示退出码 1，没有 0。执行时不做任何预检。"""
    from pathlib import Path

    from mino_scout.runtime.script_check import check_repo

    ap = argparse.ArgumentParser(prog="mino-scout check")
    ap.add_argument("path", nargs="?", default=".")
    ns = ap.parse_args(argv)
    root = Path(ns.path).expanduser()
    if not root.is_dir():
        print(f"不是目录：{root}", file=sys.stderr)
        return 2
    hints = check_repo(root)
    for hint in hints:
        print(hint)
    print(f"{len(hints)} 条提示" if hints else "没有提示")
    return 1 if hints else 0


def main(argv: list[str] | None = None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    from mino_scout.config import configure_proxy_bypass

    configure_proxy_bypass()

    raw = list(argv) if argv is not None else sys.argv[1:]
    from mino_scout.config import development
    if development():
        if not raw:
            raw = ["run"]
        if raw[0] in {"start", "update", "update-helper", "update-recover", "heavy-deps"}:
            print("源码开发使用 run 前台启动和源码更新，不操作正式后台服务", file=sys.stderr)
            return 2
    if not raw:
        return cmd_default()
    if raw[0] in ("-h", "--help"):
        return _legacy_argparse_entry(raw)

    sub = raw[0]
    if sub == "configure-development":
        from mino_scout.config import development, load_config, resolve_runtime, save_config
        if not development():
            print("开发接入仅限源码 Scout，禁止修改正式安装", file=sys.stderr)
            return 2
        ap = argparse.ArgumentParser(prog="mino-scout configure-development")
        ap.add_argument("--nexus-url", required=True)
        ap.add_argument("--install-token", required=True)
        ns = ap.parse_args(raw[1:])
        try:
            resolve_runtime(nexus=ns.nexus_url, config={})
            if not ns.install_token.strip():
                raise ValueError("开发接入凭证不能为空")
            cfg = load_config()
            if cfg.get("token") or cfg.get("pending_install_token"):
                raise ValueError("开发目录已有接入凭证；请通过原账号处理绑定，拒绝覆盖")
            cfg.update(pending_nexus_url=ns.nexus_url, pending_install_token=ns.install_token)
            save_config(cfg)
            print("开发接入配置已写入独立目录；运行 python -m mino_scout.cli run 启动开发节点")
            return 0
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if sub == "check":
        return cmd_check(raw[1:])
    if sub == "identity":
        from mino_scout.config import runtime_identity
        print(json.dumps(runtime_identity(), ensure_ascii=False, indent=2))
        return 0
    if sub == "configure":
        return cmd_configure(raw[1:])
    if sub == "status":
        return cmd_status()
    if sub == "stop":
        return cmd_stop()
    if sub == "update":
        return cmd_update(raw[1:])
    if sub == "update-helper" and len(raw) == 2:
        from mino_scout.tools.update.fixed_update import helper
        return helper(raw[1])
    if sub == "update-recover" and len(raw) == 2:
        from mino_scout.tools.update.fixed_update import helper
        return helper(raw[1], recover=True)
    if sub == "update-status" and len(raw) == 2:
        from mino_scout.tools.update.fixed_update import status
        print(json.dumps(status(raw[1]), ensure_ascii=False, indent=2))
        return 0
    if sub == "heavy-deps":
        return cmd_heavy_deps()
    if sub in ("run", "start"):
        if sub == "start" and not raw[1:]:
            st = collect_status()
            if not st.get("running"):
                return try_start_service()
            return cmd_public_status()
        if sub == "start":
            print("start 仅用于拉起后台服务；前台常驻请用 mino-scout run", file=sys.stderr)
            return 2
        return cmd_run()
    if sub == "probe":
        return cmd_probe(build_core())

    print(f"未知命令: {sub}（可用: run start probe status stop update heavy-deps configure）", file=sys.stderr)
    return 2


def _legacy_argparse_entry(argv: list[str] | None = None) -> int:
    """保留 --help 文案。"""
    from mino_scout.config import config_path

    ap = argparse.ArgumentParser(
        prog="mino-scout",
        description="Mino Scout 执行器",
        epilog=f"无子命令时：服务已运行则公开 status，否则尝试启动服务。配置见 {config_path()}。",
    )
    ap.parse_args(argv)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        sys.exit(_legacy_argparse_entry(sys.argv[1:]))
    sys.exit(main())
