"""node daemon transport：反向 dial Nexus 的 WebSocket 客户端。

协议见 docs/PROTOCOL.md。这一层只做四件事：
  1. 连接与重连（指数退避）、REGISTER / HEARTBEAT（心跳重探设备，报 device_delta）
  2. 收帧 → 解码 → 交给 ScoutCore → 编码 → 回帧
  3. 超时/异常兜底：**任何情况下都要回一条 RESULT**，不能让 Nexus 干等
  4. 未知消息类型按协议 §7 记 warn 并忽略（不可关连接）

**业务逻辑一律不在这里。** core 不知道网络存在（CLAUDE.md §2.2）。
"""
from __future__ import annotations

from dataclasses import replace
import asyncio
import contextvars
import os
from concurrent.futures import ThreadPoolExecutor
import json
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional

from mino_scout.server_comm import protocol as P
from mino_scout.core import (
    ScoutCore,
)
from mino_scout.tools.update.app_version import report_scout_version, process_build_identity
from mino_scout.tools.keep_alive.log import SLog, current_node_id
from mino_scout.schemas import EventStatus

TAG = "NodeTransport"

_BACKOFF_START = 1.0
_BACKOFF_MAX = 30.0
# 定时全量重发设备表：心跳只带增量，一旦某条增量帧在 Nexus 侧丢失/部分应用，
# 设备表会一直停在旧状态。定期把「当前设备 + 已消失设备的墓碑」整体重发一次，
# 让 Nexus 自愈，而不需要重连。
_DEVICE_FULL_SYNC_MIN_SEC = 300.0
_DEVICE_TOMBSTONE_MAX = 64
# 单帧上限（协议 §1）。超了要降质重发而不是分片 —— 这里只负责拦住并报错。
_MAX_FRAME_BYTES = 32 * 1024 * 1024


class _HeartbeatSendTimeout(asyncio.TimeoutError):
    """心跳发不出去且连接已被主动断开：必须重连，不能带着半开连接继续跑。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _announce_host(
    node_id: str,
    devices: Optional[list[P.DeviceManifest]] = None,
    *,
    mode: str = "running",
) -> dict[str, Any]:
    """HEARTBEAT/REGISTER.host：本机功耗采样 + 桌面就绪状态。"""
    from mino_scout.executors.desktop import get_desktop_runtime
    from mino_scout.tools.keep_alive.host_sample import sample_host

    host = sample_host(mode=mode)
    desktop = next((d for d in devices or [] if d.platform in {"mac", "win"}), None)
    host.update(get_desktop_runtime().host(node_id, desktop))
    # 空采样字段不要编码出去，免得 Hub 用 null 覆盖掉上一次有效的 CPU/内存数字。
    return {key: value for key, value in host.items() if value is not None}


_seq = 0


def _msg_id() -> str:
    """单调递增 + 时间戳。不引 ulid 依赖，够唯一即可。"""
    global _seq
    _seq += 1
    return f"{int(time.time() * 1000):013d}{_seq:06d}"


def _apply_registered_credentials(reg: P.Registered, core: ScoutCore, transport: NodeTransport) -> None:
    from mino_scout.config import load_config, sanitize_scout_id, save_config

    cfg = load_config()
    changed = False
    new_id = sanitize_scout_id(str(getattr(reg, "node_id", "") or ""))
    if new_id and new_id != core.node_id:
        core.node_id = new_id
        core.scout_id = new_id
        cfg["scout_id"] = new_id
        changed = True
        current_node_id.set(new_id)
    new_tok = str(getattr(reg, "node_token", "") or "").strip()
    if new_tok:
        cfg["token"] = new_tok
        transport.token = new_tok
        if cfg.get("pending_nexus_url"):
            cfg["nexus_url"] = cfg["pending_nexus_url"]
        cfg.pop("pending_install_token", None)
        cfg.pop("pending_nexus_url", None)
        changed = True
    if changed:
        save_config(cfg)


def changed_devices(
    prev: dict[str, P.DeviceManifest], now: list[P.DeviceManifest],
) -> list[P.DeviceManifest]:
    """对照上一轮发现结果，只返回变化的设备（协议 §4.3 device_delta）。

    新出现 / 通道变化用当前 manifest；消失的设备把通道标成 disconnected，
    这样 node.device_lost 丢了也能靠心跳把 Nexus 缓存改过来。
    """
    current = {d.sn: d for d in now}
    out: list[P.DeviceManifest] = []
    for sn, old in prev.items():
        if sn not in current:
            out.append(
                replace(old, channels={ch: "disconnected" for ch in (old.channels or {})})
            )
    for sn, dev in current.items():
        old = prev.get(sn)
        if old is None or old != dev:
            out.append(dev)
    return out


class NodeTransport:
    def __init__(
        self,
        core: ScoutCore,
        *,
        nexus_url: str,
        token: str,
        heartbeat_sec: int = 15,
    ):
        self.core = core
        from mino_scout.config import resolve_runtime
        self.nexus_url = resolve_runtime(nexus=nexus_url, token=token)[0]
        self.token = token
        self.heartbeat_sec = heartbeat_sec
        self._ws: Any = None
        self._loop: Any = None
        self._session_token = ""
        self._stop = asyncio.Event()
        self._intentional_stop = False
        self._seen_register = False
        self._last_devices: dict[str, P.DeviceManifest] = {}
        # 设备发现是阻塞 IO（adb / devicectl 单台最慢可到 20s+）。它绝不能跑在
        # 心跳路径上：心跳只带上一轮缓存，发现放独立单线程后台任务，超时就放弃
        # 本轮结果。单线程 + 任务去重保证卡住的探针不会越堆越多。
        self._device_refresh_task: Optional[asyncio.Task] = None
        self._device_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-device-probe")
        # run_in_executor 的超时只放弃等待，不会打断仍卡在 adb/devicectl 里的线程。
        # 单独标记「底层探针仍在跑」，否则每次心跳都会再排一个任务，队列越积越长。
        self._device_probe_inflight = False
        # 探针开始时刻与代次：卡死看门狗换池后，旧探针的完成回调不能清新探针的状态。
        self._device_probe_started_at = 0.0
        self._device_probe_token = 0
        # 还没确认送达的设备变化。心跳发送成功后清空，失败就留到下一拍重发。
        self._pending_device_delta: list[P.DeviceManifest] = []
        self._delta_generation = 0
        self._pending_delta_is_full = False
        # 消失的设备保留成 disconnected 墓碑，全量同步时一起发，Nexus 才能把它
        # 标成离线而不是一直显示 connected。
        self._device_tombstones: dict[str, P.DeviceManifest] = {}
        self._next_full_sync_at = 0.0
        # 心跳与后台重探都可能发设备状态；串行化发送，避免较早的快照晚到后
        # 把 Nexus 的设备表覆盖回旧状态。
        self._heartbeat_send_lock = asyncio.Lock()
        # 收据重传是网络请求，不能占着心跳节拍。
        self._record_sync_task: Optional[asyncio.Task] = None
        # msg_id -> 等待应答的 future
        self._waiters: dict[str, asyncio.Future] = {}
        # 在途的请求处理任务。必须持强引用，否则会被 GC（见 _on_frame）
        self._inflight: set[asyncio.Task] = set()
        # Long synchronous execution wrappers must not consume heartbeat,
        # source-identity and stop-watch workers in asyncio's default pool.
        self._execution_pool = ThreadPoolExecutor(
            max_workers=min(32, (os.cpu_count() or 1) + 4),
            thread_name_prefix="scout-request",
        )

    # ---------------- 生命周期 ----------------

    def _connection(self, state, **kwargs):
        try:
            from mino_scout.tools.keep_alive.runtime_sidecar import note_connection
            note_connection(state, **kwargs)
        except OSError as exc:
            SLog.w(TAG, f"连接状态写入失败: {type(exc).__name__}")

    async def _local_stop_loop(self):
        from mino_scout.tools.keep_alive.process_control import local_stop_requested
        while not self._stop.is_set():
            if await asyncio.to_thread(local_stop_requested):
                self.request_shutdown()
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def run_forever(self) -> None:
        """连不上就退避重连，无限重试（协议 §1）。"""
        current_node_id.set(self.core.node_id)
        from mino_scout.tools.keep_alive.power import HOLDER_NEXUS, get_guard

        guard = get_guard()
        # 重连窗口也要压住：release 只在进程退出。绑 active_runs 会让待命时睡着。
        guard.acquire(HOLDER_NEXUS)
        loop = asyncio.get_running_loop()
        self._loop = loop
        self.core.nexus_call = self.ask_nexus
        from mino_scout.tools.update import update_progress as UP

        def _progress_hook(payload: dict) -> None:
            asyncio.run_coroutine_threadsafe(self._emit_update_progress(payload), loop)

        UP.set_progress_hook(_progress_hook)
        from mino_scout.plugins import progress as plugin_progress

        def _plugin_progress_hook(payload: dict) -> None:
            asyncio.run_coroutine_threadsafe(self._emit_plugin_progress(payload), loop)

        plugin_progress.set_progress_hook(_plugin_progress_hook)
        local_stopper = asyncio.create_task(self._local_stop_loop())
        try:
            backoff = _BACKOFF_START
            while not self._stop.is_set():
                self._connection_registered = False
                try:
                    await self._connect_once()
                    backoff = _BACKOFF_START  # 成功连过一次就重置
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._connection("disconnected", error=f"连接失败: {type(exc).__name__}")
                    from mino_scout.runtime.secret_redaction import sanitize
                    cause = exc.__cause__ or exc.__context__
                    diagnostic = {**getattr(self, "_last_disconnect", {}),
                        "exception_type": type(exc).__name__,
                        "cause_type": type(cause).__name__ if cause else "",
                        "cause_errno": getattr(cause, "errno", None),
                        "received_close_code": getattr(getattr(exc, "rcvd", None), "code", None),
                        "sent_close_code": getattr(getattr(exc, "sent", None), "code", None)}
                    SLog.w(TAG, f"连接断开/失败: {sanitize(str(exc))[:300]} diagnostic={json.dumps(diagnostic, sort_keys=True)}")
                if self._connection_registered:
                    backoff = _BACKOFF_START
                if self._stop.is_set():
                    break
                # 加抖动，避免多节点同时重连打爆 Nexus
                wait = min(backoff, _BACKOFF_MAX) * (0.8 + 0.4 * random.random())
                SLog.i(TAG, f"{wait:.1f}s 后重连 {self.nexus_url}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                self._keep_power()
                backoff = min(backoff * 2, _BACKOFF_MAX)
        finally:
            self.core.nexus_call = None
            self._loop = None
            local_stopper.cancel()
            await asyncio.gather(local_stopper, return_exceptions=True)
            self._connection("stopped")
            UP.set_progress_hook(None)
            from mino_scout.plugins import progress as plugin_progress

            plugin_progress.set_progress_hook(None)
            try:
                self.core.shutdown()
            except Exception as exc:
                SLog.w(TAG, f"playwright 退出清理: {exc}")
            await self._cancel_device_refresh()
            await self._cancel_record_sync()
            self._execution_pool.shutdown(wait=False)
            self._device_pool.shutdown(wait=False)
            guard.release(HOLDER_NEXUS)

    def _keep_power(self) -> None:
        """休眠时不要把 caffeinate 拉回来。"""
        if self.core.host_mode() == "asleep":
            return
        from mino_scout.tools.keep_alive.power import get_guard

        get_guard().keepalive()

    def stop(self) -> None:
        self.request_shutdown()

    def request_shutdown(self) -> None:
        """人主动停（SIGTERM / `mino-scout stop`）。断线重连不要走这条。"""
        self._intentional_stop = True
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._abort_ws(ws)))

    async def _abort_ws(self, ws: Any) -> None:
        try:
            await ws.close()
        except Exception:
            pass

    @staticmethod
    def _abort_transport(ws: Any) -> None:
        """websockets 的 close() 卡住时直接掐 TCP；recv 才会立刻醒来去重连。"""
        transport = getattr(ws, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass

    async def _connect_once(self) -> None:
        import ssl

        import certifi
        import websockets
        from websockets.exceptions import ConnectionClosed

        self._connection("connecting")
        SLog.i(TAG, f"dial {self.nexus_url}")
        # Frozen macOS builds often lack a usable system CA store; pin certifi.
        connect_kw: dict[str, Any] = {"max_size": _MAX_FRAME_BYTES}
        if str(self.nexus_url or "").startswith("wss://"):
            connect_kw["ssl"] = ssl.create_default_context(cafile=certifi.where())
        self._last_disconnect = {}
        async with websockets.connect(self.nexus_url, **connect_kw) as ws:
            connected_at = time.monotonic()
            self._ws = ws
            self._connection("registering", connected=True)
            # 顺序要紧：**先起接收循环，再 REGISTER**。
            # REGISTER 要等 REGISTERED 应答，而应答只能由接收循环派发；
            # 反过来写会死等到超时，且一条帧都处理不到。
            recv = asyncio.create_task(self._recv_loop(ws))
            stopper = asyncio.create_task(self._stop.wait())
            hb: Optional[asyncio.Task] = None
            try:
                await self._register()
                hb = asyncio.create_task(self._heartbeat_loop())
                done, _pending = await asyncio.wait(
                    {recv, stopper, hb}, return_when=asyncio.FIRST_COMPLETED,
                )
                if stopper in done:
                    await self._announce_shutdown()
                    await ws.close()
                elif hb in done:
                    # Propagate heartbeat failures to the reconnect loop, too.
                    await hb
                    await ws.close()
                # asyncio.wait does not retrieve exceptions from completed tasks.
                await recv
            except ConnectionClosed as exc:
                if getattr(exc.rcvd, "code", None) != 1012:
                    raise
                SLog.i(TAG, "Nexus 正在重启，连接关闭后将自动重连")
            finally:
                connection_tasks = [task for task in (hb, recv, stopper) if task is not None]
                for task in connection_tasks:
                    if not task.done():
                        task.cancel()
                # Drain every task even when REGISTER, shutdown or recv failed.
                await asyncio.gather(*connection_tasks, return_exceptions=True)
                self._last_disconnect = {
                    "connected_seconds": round(time.monotonic() - connected_at, 3),
                    "registered": bool(getattr(self, "_connection_registered", False)),
                    "ping_latency_seconds": getattr(ws, "latency", None)}
                self._ws = None
                records = getattr(self.core, "_script_records", None)
                if records:
                    records.connection(False)
                self.core.binding_guard.set_connection("")
                # Pending callbacks belong to this connection, not the next one.
                from mino_scout.server_comm.nexus_link import NexusCallbackError
                for waiter in list(self._waiters.values()):
                    if not waiter.done():
                        waiter.set_exception(NexusCallbackError("nexus_disconnected"))
                self._connection("disconnected")
                await self._cancel_device_refresh()
                await self._cancel_record_sync()

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            await self._on_frame(raw)

    # ---------------- REGISTER / HEARTBEAT ----------------

    async def _register(self) -> None:
        import platform

        self.core._plugins_ready = False
        self.core._plugin_warning = ""
        self.core.binding_guard.set_connection("")

        def _reexec_if_stale() -> bool:
            from mino_scout.tools.update.stale_process import ensure_process_matches_app_layer

            return ensure_process_matches_app_layer(log_tag=TAG)

        if await asyncio.to_thread(_reexec_if_stale):
            self.request_shutdown()
            return

        # manifest() 会跑连通性探测，里面有阻塞调用；playwright 的 sync API
        # 更是**明确拒绝在事件循环里被调用**（"Please use the Async API instead"）。
        # 必须丢线程池 —— 直接 await 会让 playwright 永远上报不可用。
        execs, devices = await asyncio.to_thread(self.core.manifest)
        from mino_scout.config import resolve_hostname, resolve_studio_id

        from mino_scout.runtime.input_contract import runtime_input_capabilities

        runtime_caps = runtime_input_capabilities()
        epoch = self.core.binding_guard.announce(execs, runtime_caps)
        payload = P.Register(
            node_id=self.core.node_id,
            token=self.token,
            platform="",  # host.os is the only host system field
            platform_contract_version=1,
            arch=platform.machine(),
            scout_version=report_scout_version(),
            account_isolation_version=1,
            hostname=resolve_hostname(),
            studio_id=resolve_studio_id(),
            executors=execs,
            devices=devices,
            plugins=[],  # 首次注册不得携带未验证 owner 的旧插件配置。
            plugins_ready=False,
            plugin_warning="正在核验插件账号归属",
            runtime_capabilities=runtime_caps,
            record_api_version=1,
            **epoch,
            host=await asyncio.to_thread(
                _announce_host, self.core.node_id, devices, mode=self.core.host_mode(),
            ),
        )
        from mino_scout.runtime.platform_contract import validate_platform_announcement
        validate_platform_announcement(payload)
        reply = await self._request(P.MsgType.REGISTER, payload)
        if reply is None:
            raise RuntimeError("REGISTER 没等到 REGISTERED")
        reg: P.Registered = reply.payload
        if not reg.accepted:
            self._connection("registration_rejected", connected=True, error=reg.reason or "Nexus 拒绝注册")
            from mino_scout.config import load_config, nexus_ws_url, save_config

            cfg = load_config()
            if cfg.get("pending_install_token") == self.token:
                cfg.pop("pending_install_token", None)
                cfg.pop("pending_nexus_url", None)
                save_config(cfg)
                self.token = str(cfg.get("token") or "")
                from mino_scout.config import resolve_runtime
                self.nexus_url = resolve_runtime(nexus=str(cfg.get("nexus_url") or self.nexus_url), token=self.token)[0]
                if not self.token:
                    self.request_shutdown()
            # 不要立刻重试 —— 退避由 run_forever 负责（协议 §4.2）
            raise RuntimeError(f"Nexus 拒绝注册: {reg.reason or '未给原因'}")
        if reg.platform_contract_version != 1 or type(reg.platform_contract_version) is not int:
            raise RuntimeError("Nexus 未确认平台合同 v1，请升级 Nexus")
        self.core.binding_guard.set_connection(reg.node_connection_generation)
        self._session_token = reg.session_token
        self.heartbeat_sec = reg.heartbeat_interval_sec or self.heartbeat_sec
        avail = [e.id for e in execs if e.available]
        SLog.i(
            TAG,
            f"已注册 node={self.core.node_id} nexus=v{reg.nexus_version} "
            f"executors={avail} devices={len(devices)} hb={self.heartbeat_sec}s",
        )
        for w in reg.warnings or []:
            SLog.w(TAG, f"Nexus: {w}")
        from mino_scout.plugins.state import verify_local_owner

        plugin_warning = ""
        try:
            verify_local_owner(str(getattr(reg, "owner_user_id", "") or ""), self.nexus_url)
        except PermissionError as exc:
            plugin_warning = str(exc)
            SLog.w(TAG, f"插件账号校验失败；节点继续在线: {exc}")
        else:
            self.core._plugins_ready = True
        self.core._plugin_warning = plugin_warning
        _apply_registered_credentials(reg, self.core, self)
        from mino_scout.server_comm.script_records import bind as bind_records
        records = await asyncio.to_thread(bind_records, self.core, reg.owner_user_id, self.nexus_url)
        records.connection(True)
        self._connection_registered = True
        self._connection("registered", connected=True, registered=True)
        self._start_record_sync()
        from mino_scout.tools.keep_alive.runtime_sidecar import note_plugin_access

        try:
            note_plugin_access(self.core._plugins_ready, plugin_warning)
        except OSError as exc:
            SLog.w(TAG, f"插件状态写入失败: {type(exc).__name__}")
        if self.core._plugins_ready:
            from mino_scout.plugins.state import release_installed_import_blocks

            released = release_installed_import_blocks()
            if released:
                SLog.i(TAG, f"已安装插件解除导入停用: {', '.join(released)}")
        await self._send(P.MsgType.HEARTBEAT, await asyncio.to_thread(self._heartbeat_payload, devices))
        try:
            from mino_scout.tools.keep_alive.runtime_sidecar import write_runtime_sidecar

            write_runtime_sidecar()
        except Exception:
            pass
        if not self._seen_register:
            from mino_scout.tools.update.heavy_deps import schedule_heavy_deps

            schedule_heavy_deps(adb_hook=self.core.ensure_android_adb_keyboard_on_startup)
        if self._seen_register:
            await self._emit_device_delta(self._last_devices, devices)
        self._seen_register = True
        self._last_devices = {d.sn: d for d in devices}
        # REGISTER 已带完整设备表；作废旧连接上未送达的增量，避免重连后把
        # 注册快照又覆盖回旧状态。
        self._delta_generation += 1
        self._pending_device_delta = []
        self._pending_delta_is_full = False
        self._device_tombstones.clear()
        self._next_full_sync_at = time.monotonic() + max(
            _DEVICE_FULL_SYNC_MIN_SEC, 20.0 * float(self.heartbeat_sec),
        )

    async def _emit_device_delta(
        self, prev: dict[str, P.DeviceManifest], now: list[P.DeviceManifest],
    ) -> None:
        """对照上一轮发现结果发 EXECUTE node.device_*。权威状态仍以 HEARTBEAT.device_delta 为准。"""
        current = {d.sn: d for d in now}
        for sn, old in prev.items():
            if sn not in current:
                await self._send_framework(
                    "node.device_lost", sn=sn, platform=old.platform, detail="重探后消失",
                )
        for sn, dev in current.items():
            if sn not in prev:
                await self._send_framework(
                    "node.device_found", sn=sn, platform=dev.platform, detail="重探后出现",
                )
                continue
            if dict(prev[sn].channels) != dict(dev.channels):
                await self._send_framework(
                    "node.channel_changed",
                    sn=sn,
                    platform=dev.platform,
                    detail=f"{prev[sn].channels} → {dev.channels}",
                )

    def _heartbeat_payload(
        self,
        devices: Optional[list[P.DeviceManifest]] = None,
        *,
        device_delta: Optional[list[P.DeviceManifest]] = None,
    ) -> P.Heartbeat:
        """统一附带 host 采样与桌面状态，避免 REGISTER 后的瘦心跳把 Hub 上的指标冲掉。"""
        hb = self.core.heartbeat()
        hb.host = _announce_host(self.core.node_id, devices, mode=self.core.host_mode())
        if device_delta is not None:
            hb.device_delta = device_delta
        if devices is not None:
            desktop = next((d for d in devices if d.platform in {"mac", "win"}), None)
            if desktop is not None:
                from mino_scout.executors.desktop import INSTALL_CAPS, host_caps
                native_id = desktop.platform
                ready = desktop.desktop_state == "ready"
                by_id = {executor.id: executor for executor in (hb.executors or [])}
                by_id[native_id] = P.ExecutorManifest(
                    id=native_id,
                    available=True,
                    provides=sorted(set(INSTALL_CAPS) | (set(host_caps()) if ready else set())),
                    reason="" if ready else desktop.blocked_reason,
                )
                hb.executors = list(by_id.values())
        epoch = self.core.binding_guard.announce(hb.executors, hb.runtime_capabilities)
        hb.scout_process_generation = epoch["scout_process_generation"]
        hb.capability_revision = epoch["capability_revision"]
        hb.execution_binding_version = epoch["execution_binding_version"]
        hb.node_connection_generation = self.core.binding_guard.connection_generation
        from mino_scout.runtime.platform_contract import validate_platform_announcement
        validate_platform_announcement(hb)
        return hb

    async def _announce_shutdown(self) -> None:
        if not self._intentional_stop:
            return
        try:
            await self._send(
                P.MsgType.HEARTBEAT,
                await asyncio.to_thread(self._heartbeat_payload, list(self._last_devices.values()) or None),
            )
        except Exception:
            pass
        hb = await asyncio.to_thread(self.core.heartbeat)
        runs = ",".join(hb.active_runs) or "none"
        await self._send_framework("node.shutting_down", detail=f"active_runs={runs}")

    async def _heartbeat_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time() + float(self.heartbeat_sec)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, next_at - loop.time()))
                return
            except asyncio.TimeoutError:
                pass
            started = loop.time()
            try:
                await self._tick_heartbeat()
            except _HeartbeatSendTimeout:
                # 发送通道已经停滞，继续每拍重试只会把节点拖着不发心跳；
                # 交给连接循环重建 WebSocket。
                raise
            except Exception as exc:
                # 单次失败不能停掉整轮心跳，否则只会剩 REGISTER 后的瘦心跳，
                # Hub 上的 CPU/内存采样永远写不进去。
                SLog.w(TAG, f"心跳发送失败: {exc}")
            # 固定节拍而非「上一轮结束后再等 15s」：单轮慢一点不会把周期越拖越长。
            period = max(1.0, float(self.heartbeat_sec))
            next_at = started + period
            if next_at <= loop.time():
                next_at = loop.time() + period

    async def _tick_heartbeat(self) -> None:
        """先发缓存心跳，再后台重探设备。

        设备发现（`adb devices` / `xcrun devicectl`）是同步阻塞调用，单次可达
        20s+，曾经直接跑在这里，导致心跳间隔超过 Nexus 的存活窗口、节点被判离线。
        现在心跳只发上一轮缓存，发现结果回来后再补一条带 device_delta 的心跳。
        """

        def _reexec_if_stale() -> bool:
            from mino_scout.tools.update.stale_process import ensure_process_matches_app_layer

            return ensure_process_matches_app_layer(log_tag=TAG)

        try:
            restarting = await asyncio.wait_for(asyncio.to_thread(_reexec_if_stale), timeout=5.0)
        except asyncio.TimeoutError:
            SLog.w(TAG, "检查进程是否过期超过 5s，本轮跳过（不影响心跳）")
            restarting = False
        except Exception as exc:
            SLog.w(TAG, f"检查进程是否过期失败: {type(exc).__name__}")
            restarting = False
        if restarting:
            self.request_shutdown()
            return

        self._keep_power()
        cached = list(self._last_devices.values()) or None
        async with self._heartbeat_send_lock:
            generation, delta = self._delta_to_send()
            hb = await asyncio.to_thread(self._heartbeat_payload, cached, device_delta=delta)
            await self._send_heartbeat(hb)
            if generation:
                self._mark_delta_sent(generation)
        self._start_device_refresh()
        self._start_record_sync()
        # _connection already persists the sidecar, including build diagnostics.
        await asyncio.to_thread(self._connection, "heartbeat", connected=True, registered=True)

    def _device_probe_timeout(self) -> float:
        # adb 列表 10s + iOS 列表 10s + details 共享 12s，再加一点调度余量。
        return max(20.0, float(self.heartbeat_sec) * 3.0)

    def _device_probe_watchdog(self) -> float:
        # 线程内的 adb/devicectl 自己都有超时；到这个量级仍不返回就是真卡死了。
        # 换掉线程池恢复设备表刷新，避免一次卡死让设备状态永久停更。
        return max(90.0, 4.0 * self._device_probe_timeout())

    def _heartbeat_send_timeout(self) -> float:
        # 对端不读、TCP 半开时 ws.send 可能永久挂起，心跳循环会就此停摆。
        return max(5.0, float(self.heartbeat_sec))

    async def _send_heartbeat(self, hb: P.Heartbeat) -> None:
        timeout = self._heartbeat_send_timeout()
        try:
            await asyncio.wait_for(self._send(P.MsgType.HEARTBEAT, hb), timeout=timeout)
        except asyncio.TimeoutError:
            SLog.w(TAG, f"心跳发送超过 {timeout:.1f}s，主动断开重连")
            ws = self._ws
            if ws is not None:
                try:
                    await asyncio.wait_for(self._abort_ws(ws), timeout=2.0)
                except Exception:
                    # close() 可能卡在同一张半开连接上；直接 abort 底层传输，
                    # 否则 recv 等不到 EOF，连接循环永远不会重连。
                    self._abort_transport(ws)
            raise _HeartbeatSendTimeout(f"heartbeat send stalled > {timeout:.1f}s") from None

    def _queue_delta(self, delta: list[P.DeviceManifest]) -> int:
        """把未送达的设备变化并入待发集合，返回本次发送代次。

        复查期间设备可能连续插拔，不能直接用新 delta 覆盖上一帧未送达的状态；
        按 SN 合并后，Nexus 只会看到这件事的最终状态。
        """
        if not delta:
            return 0
        merged = {d.sn: d for d in self._pending_device_delta}
        for device in delta:
            merged[device.sn] = device
        self._pending_device_delta = list(merged.values())
        self._delta_generation += 1
        return self._delta_generation

    def _delta_to_send(self) -> tuple[int, Optional[list[P.DeviceManifest]]]:
        """取本拍要捎带的设备状态：未送达增量优先，否则到点发一次全量。

        返回 0 表示本拍只有普通心跳。全量快照只在发送成功后推进下一周期，
        所以发送失败不会把自愈机会直接跳过一整个周期。
        """
        if self._pending_device_delta:
            return self._delta_generation, list(self._pending_device_delta)
        if not self._last_devices and not self._device_tombstones:
            return 0, None
        now = time.monotonic()
        if now < self._next_full_sync_at:
            return 0, None
        current = set(self._last_devices)
        snapshot = list(self._last_devices.values())
        snapshot.extend(device for sn, device in self._device_tombstones.items() if sn not in current)
        self._pending_device_delta = snapshot
        self._pending_delta_is_full = True
        self._delta_generation += 1
        return self._delta_generation, list(snapshot)

    def _mark_delta_sent(self, generation: int) -> None:
        """只确认自己发出的那一代；期间并入的新变化留给下一拍。"""
        if not generation or generation != self._delta_generation:
            return
        was_full = self._pending_delta_is_full
        self._pending_device_delta = []
        self._pending_delta_is_full = False
        if was_full:
            interval = max(_DEVICE_FULL_SYNC_MIN_SEC, 20.0 * float(self.heartbeat_sec))
            self._next_full_sync_at = time.monotonic() + interval

    def _note_tombstones(
        self, prev: dict[str, P.DeviceManifest], devices: list[P.DeviceManifest],
    ) -> None:
        """记住消失的设备，供下一轮全量同步把 Nexus 的旧缓存纠正为 disconnected。"""
        current = {device.sn for device in devices}
        for sn, old in prev.items():
            if sn in current:
                continue
            self._device_tombstones.pop(sn, None)
            self._device_tombstones[sn] = replace(
                old, channels={ch: "disconnected" for ch in (old.channels or {})},
            )
        for sn in current:
            self._device_tombstones.pop(sn, None)
        while len(self._device_tombstones) > _DEVICE_TOMBSTONE_MAX:
            oldest = next(iter(self._device_tombstones))
            self._device_tombstones.pop(oldest, None)

    def _start_device_refresh(self) -> None:
        if not self._seen_register:
            return
        if self._device_probe_inflight:
            stuck = time.monotonic() - self._device_probe_started_at
            if stuck <= self._device_probe_watchdog():
                return
            SLog.w(TAG, f"设备探针卡死 {stuck:.0f}s，重建探针线程池")
            self._retire_device_pool()
        running = self._device_refresh_task
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._device_refresh())
        self._device_refresh_task = task
        task.add_done_callback(self._device_refresh_done)

    def _retire_device_pool(self) -> None:
        old = self._device_pool
        self._device_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scout-device-probe")
        # 旧线程之后返回时，它的完成回调会带着旧 token，不能清新探针的 inflight。
        self._device_probe_token += 1
        self._device_probe_inflight = False
        self._device_probe_started_at = 0.0
        old.shutdown(wait=False, cancel_futures=True)

    def _device_probe_done(self, token: int) -> None:
        if token == self._device_probe_token:
            self._device_probe_inflight = False

    def _device_refresh_done(self, task: asyncio.Task) -> None:
        if self._device_refresh_task is task:
            self._device_refresh_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            SLog.w(TAG, f"后台重探设备失败: {type(exc).__name__}: {exc}")

    async def _cancel_device_refresh(self) -> None:
        task = self._device_refresh_task
        self._device_refresh_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _start_record_sync(self) -> None:
        if not self._seen_register or self._ws is None:
            return
        if getattr(self.core, "_script_records", None) is None:
            return
        running = self._record_sync_task
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._record_sync_once())
        self._record_sync_task = task
        task.add_done_callback(self._record_sync_done)

    def _record_sync_done(self, task: asyncio.Task) -> None:
        if self._record_sync_task is task:
            self._record_sync_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            SLog.w(TAG, f"后台同步收据失败: {type(exc).__name__}: {exc}")

    async def _record_sync_once(self) -> None:
        try:
            await self._sync_records()
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            SLog.w(TAG, f"后台同步收据失败: {type(exc).__name__}: {exc}")

    async def _cancel_record_sync(self) -> None:
        task = self._record_sync_task
        self._record_sync_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _device_refresh(self) -> None:
        """后台重探设备；超时只丢本轮结果，心跳照常发。"""
        loop = asyncio.get_running_loop()
        if self._device_probe_inflight:
            return
        self._device_probe_inflight = True
        self._device_probe_started_at = time.monotonic()
        self._device_probe_token += 1
        token = self._device_probe_token
        probe = loop.run_in_executor(self._device_pool, self.core.discover_devices)
        probe.add_done_callback(lambda _fut, token=token: self._device_probe_done(token))
        try:
            devices = await asyncio.wait_for(
                asyncio.shield(probe), timeout=self._device_probe_timeout(),
            )
        except asyncio.TimeoutError:
            SLog.w(TAG, f"设备重探超过 {self._device_probe_timeout():.1f}s，放弃本轮（心跳不受影响）")
            return
        except Exception as exc:
            SLog.w(TAG, f"设备重探失败，保留上一轮设备表: {type(exc).__name__}: {exc}")
            return
        if devices is None:
            return
        delta = changed_devices(self._last_devices, devices)
        prev = self._last_devices
        self._last_devices = {d.sn: d for d in devices}
        self._note_tombstones(prev, devices)
        sent = False
        if delta:
            SLog.i(TAG, f"心跳设备变化 {[d.sn for d in delta]}")
            if self._ws is not None:
                # 权威状态用心跳 delta，先发；框架事件要等 Nexus 回执，不能挡在前面。
                async with self._heartbeat_send_lock:
                    generation = self._queue_delta(delta)
                    pending = list(self._pending_device_delta)
                    hb = await asyncio.to_thread(self._heartbeat_payload, devices, device_delta=pending)
                    try:
                        await self._send_heartbeat(hb)
                    except Exception as exc:
                        # 不丢变化：留到下一拍心跳重发，避免一次网络抖动让 Nexus 永久停在旧表。
                        SLog.w(TAG, f"设备变化心跳发送失败，下一拍重试: {type(exc).__name__}: {exc}")
                    else:
                        self._mark_delta_sent(generation)
                        sent = True
        if delta and sent:
            try:
                await asyncio.wait_for(self._emit_device_delta(prev, devices), timeout=15.0)
            except Exception as exc:
                SLog.w(TAG, f"设备变化事件发送失败（已由心跳 delta 兜底）: {type(exc).__name__}: {exc}")
        # 输入法维护是 adb shell 同步调用，只放在后台任务里，且失败不影响心跳。
        from mino_scout.core import serials_adb_just_connected

        try:
            reconnect = serials_adb_just_connected(prev, devices)
            if reconnect:
                SLog.i(TAG, f"adb 重连，检查输入法 {reconnect}")
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.core.ensure_adb_keyboard_for_serials,
                        reconnect,
                        reason="adb_reconnect",
                    ),
                    timeout=30.0,
                )
            await asyncio.wait_for(
                asyncio.to_thread(self.core.ensure_adb_keyboard_drift_check, devices),
                timeout=30.0,
            )
        except Exception as exc:
            SLog.w(TAG, f"后台输入法维护失败: {type(exc).__name__}: {exc}")

    # ---------------- 收帧 ----------------

    async def _on_frame(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            SLog.w(TAG, f"收到二进制帧 {len(raw)} 字节，协议只用文本帧，忽略")
            return
        try:
            data = json.loads(raw)
        except Exception as exc:
            SLog.e(TAG, f"帧不是合法 JSON，忽略: {exc}")
            return
        try:
            env = P.loads(data)
        except P.UnknownMessageType as exc:
            # 协议 §7：未知 type 记 warn 并忽略，不可关连接
            SLog.w(TAG, f"未知消息类型 {exc}，忽略（协议 §7）")
            return
        except Exception as exc:
            SLog.e(TAG, f"解码失败，忽略: {type(exc).__name__}: {exc}")
            return

        # 应答类消息交给等待者
        if env.reply_to and env.reply_to in self._waiters:
            fut = self._waiters.pop(env.reply_to)
            if not fut.done():
                fut.set_result(env)
            return

        handler = {
            P.MsgType.EXECUTE: self._on_execute,
        }.get(env.type)
        if handler is None:
            SLog.w(TAG, f"收到不该由 Scout 处理的消息 {env.type.value}，忽略")
            return
        # 每条请求独立处理，慢的那条不挡住后面的。
        # 必须持引用：asyncio 只保弱引用，不留着会被 GC 掉，表现为"请求静默消失"。
        task = asyncio.create_task(self._guarded(handler, env))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _guarded(self, handler, env: P.Envelope) -> None:
        """兜底：handler 无论怎么炸，都要回一条 RESULT。

        协议 §6 规定 Nexus 会等 RESULT；这里静默失败会让它干等到超时。
        """
        request_connection = self._ws
        try:
            await handler(env)
        except Exception as exc:
            SLog.e(TAG, f"处理 {env.type.value} 抛异常: {type(exc).__name__}: {exc}")
            if self._ws is not request_connection:
                SLog.w(TAG, "异常回执属于旧连接，禁止向重连后的会话补发")
                return
            run_id = str(getattr(env.payload, "run_id", "") or "")
            step_idx = int(getattr(env.payload, "step_idx", -1) or -1)
            await self._reply_result(
                env,
                P.Result(
                    run_id=run_id, step_idx=step_idx, status=EventStatus.FAIL,
                    summary=f"scout 内部错误：{type(exc).__name__}",
                    error=str(exc)[:400], executor_used="transport",
                ),
            )

    # ---------------- 请求处理 ----------------

    async def _on_execute(self, env: P.Envelope) -> None:
        await self._run_execute(env, env.payload)

    async def _run_execute(self, env: P.Envelope, req: P.Execute) -> None:
        # core 是同步的（executor 全是阻塞 IO），丢线程池，别卡住事件循环
        received = time.perf_counter()
        request_connection = self._ws
        timing = {}
        def execute():
            entered = time.perf_counter()
            timing["queue_ms"] = (entered - received) * 1000
            try:
                return self.core.execute(req)
            finally:
                timing["core_ms"] = (time.perf_counter() - entered) * 1000
        if req.capability_id in {"cancel_run", "write_stop_status", "script.submit"} or req.capability_id.startswith(("record.", "debug.")):
            result = await asyncio.to_thread(execute)
        else:
            context = contextvars.copy_context()
            result = await asyncio.get_running_loop().run_in_executor(
                self._execution_pool, context.run, execute)
        SLog.i(TAG, f"RESULT run={req.run_id} step={req.step_idx} sn={req.sn} cap={req.capability_id} "
                    f"executor={result.executor_used} status={result.status.value} elapsed_ms={result.elapsed_ms}")
        reply_started = time.perf_counter()
        if self._ws is not request_connection:
            SLog.w(TAG, "执行回执属于旧连接，禁止向重连后的会话补发；保留原执行收据")
            return
        await self._reply_result(env, _result_from_event(req, result))
        SLog.i(TAG, f"PERF run={req.run_id} step={req.step_idx} cap={req.capability_id} "
                    f"queue_ms={timing['queue_ms']:.2f} core_ms={timing['core_ms']:.2f} "
                    f"reply_ms={(time.perf_counter() - reply_started) * 1000:.2f} "
                    f"total_ms={(time.perf_counter() - received) * 1000:.2f}")
        self._apply_node_side_effects(result)

    def _apply_node_side_effects(self, ev) -> None:
        extra = dict(getattr(ev, "raw_response", None) or {})
        if extra.get("_scout_reexec"):
            # 热更 app 层后必须从磁盘加载本模块，不能 import 已缓存的旧 service.schedule_reexec
            from importlib import import_module

            import_module("mino_scout.tools.update.reexec_spawn").schedule_reexec()
        if extra.get("_scout_shutdown"):
            self.request_shutdown()

    # ---------------- 发送 ----------------

    async def _send(self, mtype: P.MsgType, payload: Any, *, reply_to: Optional[str] = None, msg_id: Optional[str] = None) -> str:
        if self._ws is None:
            raise RuntimeError("未连接")
        env = P.Envelope(type=mtype, msg_id=msg_id or _msg_id(), ts=_now_iso(), payload=payload, reply_to=reply_to)
        text = json.dumps(P.dumps(env), ensure_ascii=False)
        size = len(text.encode())
        if size > _MAX_FRAME_BYTES:
            raise RuntimeError(f"帧过大 {size} > {_MAX_FRAME_BYTES}（协议 §1：应降质重发，不分片）")
        await self._ws.send(text)
        return env.msg_id

    async def _sync_records(self) -> None:
        """Retry durable summaries with new envelopes; never replay a device command."""
        records = getattr(self.core, "_script_records", None)
        if records is None or self._ws is None:
            return
        pending = await asyncio.to_thread(records.store.pending, 4, getattr(self, "_record_sync_cursor", None))
        if pending:
            self._record_sync_cursor = (pending[-1]["task_id"], pending[-1]["attempt_id"])

        async def send_summary(summary):
            if self._ws is None:
                return
            request = P.Execute(run_id=summary["run_id"], step_idx=-1, sn=summary["sn"], platform=summary["platform"],
                                capability_id="record.sync", params=summary, timeout_sec=2)
            try:
                response = await self._request(P.MsgType.EXECUTE, request, timeout=2)
                result = getattr(response, "payload", None)
                if result is None or str(getattr(result.status, "value", result.status)) != "pass":
                    SLog.w(TAG, f"record.sync pending task={summary['task_id']} attempt={summary['attempt_id']} "
                           f"version={summary['summary_version']} cause={'no_reply' if result is None else 'rejected'}")
                    return
                from mino_scout.server_comm.nexus_link import validate_reply
                validate_reply(request, result)
                ack = dict(result.data or {})
                if (ack.get("attempt_id") == summary["attempt_id"]
                        and type(ack.get("ack_version")) is int
                        and ack["ack_version"] >= summary["summary_version"]):
                    await asyncio.to_thread(records.store.acknowledge, summary["task_id"], summary["attempt_id"], summary["summary_version"])
                    SLog.i(TAG, f"record.sync ack task={summary['task_id']} attempt={summary['attempt_id']} "
                           f"sent_version={summary['summary_version']} ack_version={ack['ack_version']} "
                           f"status={summary['status']} sealed={summary['sealed']}")
                else:
                    SLog.w(TAG, f"record.sync pending task={summary['task_id']} attempt={summary['attempt_id']} "
                           f"version={summary['summary_version']} cause=ack_mismatch")
            except (OSError, ValueError, TypeError, RuntimeError, asyncio.TimeoutError) as exc:
                SLog.w(TAG, f"record.sync pending task={summary['task_id']} attempt={summary['attempt_id']} "
                       f"version={summary['summary_version']} cause={type(exc).__name__}")
                return

        await asyncio.gather(*(send_summary(summary) for summary in pending))

    async def _reply_result(self, req_env: P.Envelope, result: P.Result) -> None:
        request_connection = self._ws
        generation = self.core.binding_guard.connection_generation
        try:
            result.extra = {**(result.extra or {}), "execution_identity": {
                "schema_version": 1, "node_id": self.core.node_id,
                "scout_process_generation": self.core.binding_guard.process_generation,
                "node_connection_generation": generation,
                "build": await asyncio.to_thread(process_build_identity),
            }}
            if self._ws is not request_connection or self.core.binding_guard.connection_generation != generation:
                SLog.w(TAG, "生成回执期间连接已改变，保留收据但不跨连接发送")
                return
            await self._send(P.MsgType.RESULT, result, reply_to=req_env.msg_id)
        except Exception as exc:
            SLog.e(TAG, f"回 RESULT 失败（等待方按执行模式处理结果缺失）: {exc}")

    def ask_nexus(self, req: P.Execute, timeout: float | None = None):
        """Send once from a worker; preserve transport failures without replay."""
        from mino_scout.server_comm.nexus_link import NexusCallbackError, callback_budget, validate_reply
        budget = callback_budget(timeout if timeout is not None else req.timeout_sec)
        from concurrent.futures import TimeoutError as FutureTimeout
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            raise NexusCallbackError("nexus_unreachable")
        if self._ws is None or not self.core.binding_guard.connection_generation:
            raise NexusCallbackError("nexus_disconnected")
        future = asyncio.run_coroutine_threadsafe(self._request(P.MsgType.EXECUTE, req, timeout=budget), loop)
        try:
            env = future.result(timeout=budget + 2)
        except FutureTimeout:
            future.cancel()
            raise NexusCallbackError("nexus_timeout") from None
        except NexusCallbackError:
            raise
        except Exception:
            future.cancel()
            raise NexusCallbackError("nexus_transport_error") from None
        if env is None:
            raise NexusCallbackError("nexus_timeout")
        payload = getattr(env, "payload", None)
        return validate_reply(req, payload)

    async def _request(self, mtype: P.MsgType, payload: Any, *, timeout: float = 30.0):
        from mino_scout.server_comm.nexus_link import callback_budget
        timeout = callback_budget(timeout)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        msg_id = _msg_id()
        self._waiters[msg_id] = fut
        try:
            # Register before send can yield; include transport backpressure in
            # the original request budget, with no automatic resubmission.
            async with asyncio.timeout(timeout):
                await self._send(mtype, payload, msg_id=msg_id)
                return await fut
        except asyncio.TimeoutError:
            SLog.w(TAG, f"{mtype.value} 等应答超时 {timeout}s")
            return None
        finally:
            self._waiters.pop(msg_id, None)
            if not fut.done():
                fut.cancel()

    async def _emit_plugin_progress(self, payload: dict) -> None:
        if self._ws is None or not self.core._plugins_ready:
            return
        await self._send_framework(
            "node.plugin_progress",
            detail=str(payload.get("label") or payload.get("stage") or ""),
            progress=dict(payload),
        )

    async def _emit_update_progress(self, payload: dict) -> None:
        if self._ws is None:
            return
        await self._send_framework(
            "node.update_progress",
            detail=str(payload.get("label") or payload.get("stage") or ""),
            progress=dict(payload),
        )

    async def _send_framework(
        self,
        capability_id: str,
        *,
        sn: str = "",
        platform: str = "",
        detail: str = "",
        severity: str = "info",
        progress: dict | None = None,
        extra: dict | None = None,
    ) -> bool:
        """S→N 框架事件，形状与能力调用相同。等 RESULT；丢了靠心跳收敛。

        返回是否拿到了 RESULT（IM 入站用它决定要不要暂存）。
        """
        event = capability_id.split(".", 1)[-1] if capability_id.startswith("node.") else capability_id
        params: dict[str, Any] = {
            "node_id": self.core.node_id,
            "event": event,
            "detail": detail,
            "severity": severity,
        }
        if progress:
            params["progress"] = dict(progress)
        for key, value in (extra or {}).items():
            if key not in params:
                params[str(key)] = value
        req = P.Execute(
            run_id="",
            step_idx=-1,
            sn=sn,
            capability_id=capability_id,
            params=params,
            timeout_sec=5.0,
            device_id=sn,
            platform=platform,
        )
        try:
            reply = await self._request(P.MsgType.EXECUTE, req, timeout=5.0)
            if reply is None:
                SLog.w(TAG, f"{capability_id} 未获 RESULT，等心跳收敛")
                return False
            return True
        except Exception:
            return False


_SKIP_EXTRA_KEYS = frozenset(
    {"image_base64", "image_mime", "width", "height", "summary", "error", "source", "elapsed_ms"}
)


def _result_from_event(req: P.Execute, ev) -> P.Result:
    """EventResult → 协议的 Result。截图填现有 image_* 字段，同时放进 data。"""
    raw = dict(ev.raw_response or {})
    image_base64 = str(raw.get("image_base64") or "")
    image_mime = str(raw.get("image_mime") or "")
    width = int(raw.get("width") or 0)
    height = int(raw.get("height") or 0)

    if isinstance(raw.get("extra"), dict) and raw["extra"]:
        extra = {k: v for k, v in dict(raw["extra"]).items() if not str(k).startswith("_")}
    else:
        extra = {
            k: v for k, v in raw.items()
            if k not in _SKIP_EXTRA_KEYS and k != "extra" and not str(k).startswith("_")
        }

    data = {k: v for k, v in raw.items() if k != "extra" and not str(k).startswith("_")}
    if image_base64:
        data.setdefault("image_base64", image_base64)
        data.setdefault("image_mime", image_mime)
        data.setdefault("width", width)
        data.setdefault("height", height)

    return P.Result(
        run_id=req.run_id,
        step_idx=req.step_idx,
        status=ev.status,
        summary=ev.summary,
        error=ev.error,
        executor_used=ev.executor_used,
        source=ev.executor_used or str(raw.get("source") or ""),
        elapsed_ms=ev.elapsed_ms,
        attempts=[
            P.Attempt(
                executor=str(a.get("executor") or ""),
                status=EventStatus(a.get("status") or "fail"),
                elapsed_ms=int(a.get("elapsed_ms") or 0),
                error=str(a.get("error") or ""),
            )
            for a in (ev.attempts or [])
        ],
        image_base64=image_base64,
        image_mime=image_mime,
        width=width,
        height=height,
        extra=extra,
        data=data,
    )
