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
# 单帧上限（协议 §1）。超了要降质重发而不是分片 —— 这里只负责拦住并报错。
_MAX_FRAME_BYTES = 32 * 1024 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


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
        # msg_id -> 等待应答的 future
        self._waiters: dict[str, asyncio.Future] = {}
        # 在途的请求处理任务。必须持强引用，否则会被 GC（见 _on_frame）
        self._inflight: set[asyncio.Task] = set()
        self._im_lock: Optional[asyncio.Lock] = None
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
        from mino_scout.plugins import im_bridge
        from mino_scout.plugins import wechat as wechat_plugin

        def _im_hook(im: dict) -> None:
            asyncio.run_coroutine_threadsafe(self._emit_im_message(im), loop)

        im_bridge.set_hook(_im_hook)
        wechat_plugin.bind_node(self.core.node_id)
        # 社交插件只在 REGISTERED 的服务端归属校验完成后恢复。
        local_stopper = asyncio.create_task(self._local_stop_loop())
        try:
            backoff = _BACKOFF_START
            while not self._stop.is_set():
                try:
                    await self._connect_once()
                    backoff = _BACKOFF_START  # 成功连过一次就重置
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    from mino_scout.tools.keep_alive.runtime_sidecar import read_runtime_sidecar
                    prior = (read_runtime_sidecar(expect_pid=__import__("os").getpid()).get("connection") or {}).get("last_error")
                    self._connection("disconnected", error=str(prior or f"连接失败: {type(exc).__name__}"))
                    SLog.w(TAG, f"连接断开/失败: {type(exc).__name__}: {exc}")
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
            from mino_scout.plugins import im_bridge
            from mino_scout.plugins import wechat as wechat_plugin

            im_bridge.set_hook(None)
            wechat_plugin.stop_listener()
            try:
                from mino_scout.plugins import langbot_host, feishu_messages

                feishu_messages.set_connection(False)
                langbot_host.shutdown()
                im_bridge.stop_loopback()
            except Exception as exc:
                SLog.w(TAG, f"LangBot 退出清理: {type(exc).__name__}")
            try:
                self.core.shutdown()
            except Exception as exc:
                SLog.w(TAG, f"playwright 退出清理: {exc}")
            self._execution_pool.shutdown(wait=False)
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

    async def _connect_once(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        self._connection("connecting")
        SLog.i(TAG, f"dial {self.nexus_url}")
        async with websockets.connect(self.nexus_url, max_size=_MAX_FRAME_BYTES) as ws:
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
                self._ws = None
                self.core.binding_guard.set_connection("")
                self._connection("disconnected")

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

        from mino_scout.executors.desktop import get_desktop_runtime

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
            **epoch,
            host=await asyncio.to_thread(get_desktop_runtime().host, self.core.node_id, next((d for d in devices or [] if d.platform in {"mac", "win"}), None)),
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
        self._connection("registered", connected=True, registered=True)
        from mino_scout.tools.keep_alive.runtime_sidecar import note_plugin_access

        try:
            note_plugin_access(self.core._plugins_ready, plugin_warning)
        except OSError as exc:
            SLog.w(TAG, f"插件状态写入失败: {type(exc).__name__}")
        from mino_scout.plugins import wechat as wechat_plugin, langbot_host, feishu_messages
        wechat_plugin.bind_node(self.core.node_id)
        if self.core._plugins_ready:
            from mino_scout.plugins.state import release_installed_import_blocks

            released = release_installed_import_blocks()
            if released:
                SLog.i(TAG, f"已安装插件解除导入停用: {', '.join(released)}")
            wechat_plugin.ensure_listener()
            langbot_host.reconcile(self.core.node_id)
            feishu_messages.set_connection(True)
        else:
            wechat_plugin.stop_listener()
            feishu_messages.set_connection(False)
            langbot_host.shutdown()
        await self._send(P.MsgType.HEARTBEAT, await asyncio.to_thread(self.core.heartbeat))
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
        # 重连成功：补发 Nexus 不可达期间暂存的入站事件（不阻塞注册流程）
        if self.core._plugins_ready:
            task = asyncio.create_task(self._emit_im_message(None))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

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

    async def _announce_shutdown(self) -> None:
        if not self._intentional_stop:
            return
        try:
            await self._send(P.MsgType.HEARTBEAT, await asyncio.to_thread(self.core.heartbeat))
        except Exception:
            pass
        hb = await asyncio.to_thread(self.core.heartbeat)
        runs = ",".join(hb.active_runs) or "none"
        await self._send_framework("node.shutting_down", detail=f"active_runs={runs}")

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_sec)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._tick_heartbeat()
                await self._emit_im_message(None)
            except Exception as exc:
                SLog.w(TAG, f"心跳发送失败: {exc}")
                return

    async def _tick_heartbeat(self) -> None:
        """心跳时重探 adb / 本机设备，把热插拔放进 device_delta。"""

        def _reexec_if_stale() -> bool:
            from mino_scout.tools.update.stale_process import ensure_process_matches_app_layer

            return ensure_process_matches_app_layer(log_tag=TAG)

        if await asyncio.to_thread(_reexec_if_stale):
            self.request_shutdown()
            return

        self._keep_power()
        devices: Optional[list[P.DeviceManifest]]
        try:
            devices = await asyncio.to_thread(self.core.discover_devices)
        except Exception as exc:
            SLog.w(TAG, f"心跳重探设备失败: {exc}")
            devices = None
        delta: list[P.DeviceManifest] = []
        if devices is not None:
            delta = changed_devices(self._last_devices, devices)
        from mino_scout.tools.keep_alive.host_sample import sample_host

        host = await asyncio.to_thread(sample_host, mode=self.core.host_mode())
        hb = await asyncio.to_thread(self.core.heartbeat)
        from mino_scout.executors.desktop import get_desktop_runtime
        host.update(await asyncio.to_thread(get_desktop_runtime().host, self.core.node_id, next((d for d in devices or [] if d.platform in {"mac", "win"}), None)))
        hb.host = host
        hb.device_delta = delta
        if devices is not None:
            desktop = next((d for d in devices if d.platform in {"mac", "win"}), None)
            if desktop is not None:
                from mino_scout.executors.desktop import DesktopExecutor, INSTALL_CAPS
                # HEARTBEAT.executors is a full snapshot. Preserve every registered
                # executor when replacing the native readiness entry.
                native_id = desktop.platform
                ready = desktop.desktop_state == "ready"
                by_id = {executor.id: executor for executor in hb.executors}
                by_id[native_id] = P.ExecutorManifest(id=native_id, available=True,
                    provides=sorted(set(INSTALL_CAPS) | (set(DesktopExecutor.provides) if ready else set())),
                    reason="" if ready else desktop.blocked_reason)
                hb.executors = list(by_id.values())
        epoch = self.core.binding_guard.announce(hb.executors, hb.runtime_capabilities)
        hb.scout_process_generation = epoch["scout_process_generation"]
        hb.capability_revision = epoch["capability_revision"]
        hb.execution_binding_version = epoch["execution_binding_version"]
        hb.node_connection_generation = self.core.binding_guard.connection_generation
        from mino_scout.runtime.platform_contract import validate_platform_announcement
        validate_platform_announcement(hb)
        await self._send(P.MsgType.HEARTBEAT, hb)
        # _connection already persists the sidecar, including build diagnostics.
        await asyncio.to_thread(self._connection, "heartbeat", connected=True, registered=True)
        if devices is None:
            return
        from mino_scout.core import serials_adb_just_connected

        reconnect = serials_adb_just_connected(self._last_devices, devices)
        if reconnect:
            SLog.i(TAG, f"adb 重连，检查输入法 {reconnect}")
            await asyncio.to_thread(
                self.core.ensure_adb_keyboard_for_serials,
                reconnect,
                reason="adb_reconnect",
            )
        await asyncio.to_thread(self.core.ensure_adb_keyboard_drift_check, devices)
        if delta:
            SLog.i(TAG, f"心跳设备变化 {[d.sn for d in delta]}")
            await self._emit_device_delta(self._last_devices, devices)
        self._last_devices = {d.sn: d for d in devices}

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
        if req.capability_id in {"cancel_run", "write_stop_status"}:
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
        """工作线程里向 Nexus 发一条 EXECUTE 并等待 RESULT。连不上或超时返回 None。"""
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return None
        budget = float(timeout if timeout is not None else req.timeout_sec or 15.0)
        future = asyncio.run_coroutine_threadsafe(self._request(P.MsgType.EXECUTE, req, timeout=budget), loop)
        try:
            env = future.result(timeout=budget + 2)
        except Exception:
            return None
        payload = getattr(env, "payload", None)
        return payload if isinstance(payload, P.Result) else None

    async def _request(self, mtype: P.MsgType, payload: Any, *, timeout: float = 30.0):
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

    async def _emit_im_message(self, im: Optional[dict]) -> None:
        """发 node.plugin_im_message。ws 不可用 / 没拿到 RESULT 就进 im_pending；
        任何一次调用（含重连后的 im=None）都会先按时间顺序补发积压。
        """
        if not self.core._plugins_ready:
            return
        from mino_scout.plugins import im_store

        if self._im_lock is None:
            self._im_lock = asyncio.Lock()
        async with self._im_lock:
            if self._ws is None:
                return
            if im and im.get("channel") == "feishu_cli":
                await self._send(P.MsgType.HEARTBEAT, await asyncio.to_thread(self.core.heartbeat))
            # 旧版暂存迁入持久收件箱，平台 ACK 不能清除尚未处理的业务事件。
            await asyncio.to_thread(im_store.migrate_pending)
            queue = await asyncio.to_thread(im_store.pending_events)
            if not queue:
                return
            try:
                await self._send(P.MsgType.HEARTBEAT, await asyncio.to_thread(self.core.heartbeat))
            except Exception:
                return
            for row in queue:
                ok = self._ws is not None and await self._send_framework(
                    "node.plugin_im_message", detail="im message", extra={"im": row})
                if not ok:
                    return
                await asyncio.to_thread(im_store.sent_event, row["delivery_key"])

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
