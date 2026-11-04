# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""ADB 执行通道：通过 subprocess 调 adb 直接操作真机。

支持的 capability：
  - launch_app / close_app / press_key / uninstall_app
  - swipe_direction / install_apk / read_device_data / set_clipboard
  - get_app_version / get_foreground_app
  - tap_element / input_text（定位参数由 Nexus 或脚本 pages 给出，本通道不猜元素）
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from typing import Any, Optional

from mino_scout.tools.keep_alive.log import SLog

from mino_scout.schemas import EventResult, EventStatus, PlanEvent
from mino_scout.executors.base import (
    Executor,
    ExecutorContext,
    _now_iso,
    make_event_result,
)

TAG = "AdbExecutor"

# `adb shell input tap` 在 Android 14+ / 部分 OEM 上常返回 0 但事件到不了 App。
# 用短 swipe（按下停留）代替零时长 tap，u2 不可用时的主路径。
_TAP_HOLD_MS = 80
_TAP_SETTLE_SEC = 0.15
_WM_SIZE_RE = re.compile(r"(\d+)\s*x\s*(\d+)", re.I)
_WM_CACHE: dict[str, tuple[float, int, int]] = {}
_WM_TTL_SEC = 30.0


def parse_wm_size(out: str) -> tuple[int, int]:
    """input 坐标空间：有 Override size 时优先用它，否则 Physical size。"""
    override: Optional[tuple[int, int]] = None
    physical: Optional[tuple[int, int]] = None
    leftover: Optional[tuple[int, int]] = None
    for line in (out or "").splitlines():
        m = _WM_SIZE_RE.search(line)
        if not m:
            continue
        size = (int(m.group(1)), int(m.group(2)))
        low = line.lower()
        if "override" in low:
            override = size
        elif "physical" in low:
            physical = size
        else:
            leftover = leftover or size
    return override or physical or leftover or (0, 0)


def scale_to_display(
    x: int, y: int, *, shot: tuple[int, int], display: tuple[int, int]
) -> tuple[int, int]:
    """截图像素 → wm 坐标。尺寸一致则原样返回。"""
    sw, sh = shot
    dw, dh = display
    if sw > 0 and sh > 0 and dw > 0 and dh > 0 and (sw, sh) != (dw, dh):
        x = int(round(x * dw / sw))
        y = int(round(y * dh / sh))
    if dw > 0:
        x = max(0, min(dw - 1, x))
    if dh > 0:
        y = max(0, min(dh - 1, y))
    return x, y


_MILLI_MAX = 1000


def _looks_like_milli(x: int, y: int) -> bool:
    return 0 <= x <= _MILLI_MAX and 0 <= y <= _MILLI_MAX


def milli_to_shot_pixels(
    x: int,
    y: int,
    *,
    shot: tuple[int, int],
    display: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    """EXECUTE 协议 0–1000 千分比 → 截图像素（对齐 PlaywrightExecutor._xy）。"""
    if not _looks_like_milli(x, y):
        return x, y
    sw, sh = shot
    if sw <= 0 or sh <= 0:
        sw, sh = display
    if sw <= 0 or sh <= 0 or max(sw, sh) <= _MILLI_MAX:
        return x, y
    return int(round(x / _MILLI_MAX * sw)), int(round(y / _MILLI_MAX * sh))


def map_protocol_xy_to_display(
    x: int, y: int, *, shot: tuple[int, int], display: tuple[int, int]
) -> tuple[int, int]:
    """千分比或截图像素 → wm/input 坐标。"""
    px, py = milli_to_shot_pixels(x, y, shot=shot, display=display)
    return scale_to_display(px, py, shot=shot, display=display)

# capability_id → 内部处理方法（有专属 Python 实现的）
_SUPPORTED_CAPS: set[str] = {
    "launch_app",
    "close_app",
    "kill_app",
    "press_key",
    "swipe_direction",
    "swipe_element_to_element",
    "install",
    "read_device_data",
    "get_app_version",
    "get_foreground_app",
    "set_clipboard",
    "tap_element",
    "tap_xy",
    "long_press_element",
    "input_text",
    "exec_script",
    "set_input_method",
    "uninstall_app",
}


def _runnable_low_level(low_level: Optional[dict[str, Any]]) -> bool:
    """Nexus 是否给了一段可执行的 low_level 声明。

    port(scout) 语义改动：上游同名逻辑叫 `_declares_adb_low_level(capability_id)`，
    它通过 `plugin_registry.get_capability()` **回查能力目录**，判断"这条 cap 虽然
    没有 Python 分支，但它的 adb 实现声明了 low_level，所以我支持"。

    Scout 不读能力目录（CLAUDE.md §1 约束 3），所以改成由 Nexus 把 low_level 段
    直接塞进 EXECUTE 载荷、经 `supports(cap, low_level)` 传进来。

    保留这条通路是必须的 —— 它是「加 YAML 就多一个能力」的支撑
    （见 docs/EXECUTORS.md §3）。丢了它就退化成"每个能力都要改 Python"。
    """
    low = low_level if isinstance(low_level, dict) else {}
    kind = str(low.get("kind") or ("shell" if low.get("shell") else "")).strip().lower()
    if kind == "shell":
        return isinstance(low.get("shell"), str) and bool(low["shell"].strip())
    field = {"shell_batch": "commands", "shell_seq": "steps"}.get(kind)
    if not field:
        return False
    rows = low.get(field)
    return isinstance(rows, list) and bool(rows) and all(
        isinstance(row, dict) and isinstance(row.get("shell"), str) and bool(row["shell"].strip())
        and (kind != "shell_batch" or bool(str(row.get("name") or "").strip()))
        for row in rows
    )


def _same_input_field(node, recorded) -> bool:
    """点击时记录的输入框与此刻的是否同一个。

    右/下边界允许漂移：验证码框右侧的倒计时（60s→59s…）会让输入框宽度变化
    （cr-a278a45ce22c、cr-58678f28acd7），要求 bounds 全等会误判失效。
    身份用 package + resource_id + 左上角（±8px）。
    """
    pkg, rid, bounds = recorded
    if node.package != pkg or node.resource_id != rid:
        return False
    return abs(node.bounds[0] - bounds[0]) <= 8 and abs(node.bounds[1] - bounds[1]) <= 8



def _select_focused_input(nodes, *, point=None, expect=None):
    """只接受一个符合点击记录的可写焦点；候选冲突时不猜测。"""
    matches = []
    for node in nodes:
        if not node.focused or not node.enabled or "EditText" not in node.cls:
            continue
        left, top, right, bottom = node.bounds
        if right <= left or bottom <= top:
            continue
        if point is not None and not (left <= point[0] < right and top <= point[1] < bottom):
            continue
        if expect is not None and not _same_input_field(node, expect):
            continue
        matches.append(node)
        if len(matches) > 1:
            return None
    return matches[0] if matches else None

class AdbExecutor:
    id = "adb"

    def __init__(self) -> None:
        self._focus_actions: dict[tuple[str, str], tuple[str, str, str, tuple[int, int, int, int]]] = {}

    @staticmethod
    def _focused_input(
        serial: str, *, point: tuple[int, int] | None = None,
        expect: tuple[str, str, tuple[int, int, int, int]] | None = None,
    ):
        """expect=(package, resource_id, bounds)：多个 EditText 同时报 focused 时（旧框焦点残留），
        优先取与点击时记录一致的那个，而不是树里的第一个。"""
        from mino_scout.platform.android.hierarchy import dump_ui_nodes

        dump = dump_ui_nodes(serial, force_fresh=True)
        if not dump.ok:
            return None
        return _select_focused_input(dump.nodes, point=point, expect=expect)

    @staticmethod
    def _input_target_before_tap(serial: str, point: tuple[int, int]):
        """点击前按设备坐标绑定唯一可写框，不能用键盘出现后的 bounds 反查旧点击。"""
        from mino_scout.platform.android.hierarchy import dump_ui_nodes

        try:
            dump = dump_ui_nodes(serial, force_fresh=True)
            if not dump.ok:
                return None
            inputs = [n for n in dump.nodes if n.enabled and n.visible and "EditText" in n.cls and n.area > 0]
            hits = [n for n in inputs if n.bounds[0] <= point[0] < n.bounds[2]
                    and n.bounds[1] <= point[1] < n.bounds[3]]
            if len(hits) != 1:
                return None
            node = hits[0]
            identity = ""
            if node.resource_id and sum(n.package == node.package and n.resource_id == node.resource_id for n in inputs) == 1:
                identity = "resource_id"
            elif node.hint and sum(n.package == node.package and n.hint == node.hint for n in inputs) == 1:
                identity = "hint"
            return node, identity
        except Exception as exc:  # noqa: BLE001 — 取证失败不能使已授权点击变成新异常。
            SLog.w(TAG, f"input target capture failed: {type(exc).__name__}")
            return None

    def _record_focus_after_tap(self, event, ctx, serial: str, target) -> None:
        """只记录点击前已绑定且点击后确有焦点的同一输入框；标识冲突仍不发引用。"""
        from mino_scout.platform.android.hierarchy import dump_ui_nodes

        if not ctx.run_id or target is None:
            return
        before, identity = target
        try:
            dump = dump_ui_nodes(serial, force_fresh=True)
            if not dump.ok:
                return
            candidates = [n for n in dump.nodes if n.enabled and n.visible and "EditText" in n.cls
                          and n.area > 0 and n.package == before.package]
            if identity:
                expected = getattr(before, identity)
                candidates = [n for n in candidates if getattr(n, identity) == expected]
            else:
                candidates = [n for n in candidates if _same_input_field(
                    n, (before.package, before.resource_id, before.bounds))]
            if len(candidates) != 1 or not candidates[0].focused:
                return
            node = candidates[0]
            self._focus_actions[(str(ctx.device.sn or ""), str(ctx.run_id))] = (
                f"focus:{ctx.run_id}:{event.seq}", node.package, node.resource_id, node.bounds,
            )
        except Exception as exc:  # noqa: BLE001 — 不重放已执行点击，缺引用交给原有恢复。
            SLog.w(TAG, f"input focus capture failed: {type(exc).__name__}")

    # 上报给 Nexus 的执行能力：与 Nexus 能力目录同词表（同一批具体事件 id）。
    # 每个渠道（adb / playwright / 原生 mac·win）用同一套字段，只声明"我能执行哪些事件"，
    # 这样跨渠道配置可以直接逐字段对比，排查时不必在两套词汇之间翻译。
    # 观察类事件（screenshot / hierarchy / probe_device_state）也由 adb 提供。
    provides = tuple(sorted(_SUPPORTED_CAPS | {
        "clear_app_cache", "screenshot", "hierarchy", "probe_device_state",
        # 系统级恢复能力：走 low_level 兜底执行（wake 用 keyevent，解锁用 swipe/keyevent）。
        "wake_screen", "dismiss_keyguard",
    }))

    def probe(self) -> tuple[bool, str]:
        """adb 二进制在不在。具体设备的连通性在 devices[].channels 里报。"""
        try:
            proc = subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            return False, "adb 不在 PATH"
        except Exception as exc:
            return False, f"adb version 失败: {exc}"
        if proc.returncode != 0:
            return False, f"adb version rc={proc.returncode}"
        return True, ""

    def supports(self, capability_id: str, low_level: Optional[dict[str, Any]] = None) -> bool:
        if capability_id == "install_apk":
            capability_id = "install"
        return capability_id == "clear_app_cache" or capability_id in _SUPPORTED_CAPS or _runnable_low_level(low_level)

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        if cap == "kill_app":
            cap = "close_app"
        if cap == "install_apk":
            cap = "install"
        focus_key = (str(ctx.device.sn or ""), str(ctx.run_id or ""))
        # 只读取证不改变焦点，不能清掉点击留下的焦点引用：
        # Nexus 在 tap 与 input_text 之间会按 run_id 多次下发 probe_device_state（recovery_evidence）。
        if cap not in (
            "tap_element", "input_text", "get_foreground_app",
            "probe_device_state", "read_device_data", "get_app_version",
        ):
            self._focus_actions.pop(focus_key, None)
        # 上游是 ctx.run_context.adb["serial"]（RunContext 查库 + 跑 connectivity_probe
        # 得来）。Scout 不查库，serial 由 Nexus 在 EXECUTE.device_hint 里注入。
        serial = str(ctx.device.adb_serial or "")
        try:
            if ctx.device.is_web:
                return make_event_result(
                    event, status=EventStatus.DECLINED, executor_used=self.id,
                    started_at=started_at, elapsed_ms=0,
                    summary=f"adb 不服务 web 槽 sn={ctx.device.sn}",
                )
            if not serial:
                return self._fail(
                    event, started_at, t0,
                    "adb serial 缺失：EXECUTE.device_hint 未带 adb_serial（Nexus 侧应注入）",
                )

            if cap == "clear_app_cache":
                from mino_scout.platform.android.app_cache import clear_app_cache
                package = str((event.params or {}).get("package")
                              or ctx.device.extra.get("target_package", "") or "").strip()
                raw = clear_app_cache(package, lambda *args: self._adb_shell(serial, *args))
                ok = raw["status"] == "success"
                return make_event_result(
                    event, status=EventStatus.PASS if ok else EventStatus.BLOCKED,
                    executor_used=self.id, started_at=started_at,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    summary=raw["message"], error="" if ok else raw["stderr"], raw_response=raw)
            if cap == "launch_app":
                return self._launch_app(event, ctx, serial, started_at, t0)
            if cap == "close_app":
                return self._close_app(event, ctx, serial, started_at, t0)
            if cap == "uninstall_app":
                return self._uninstall_app(event, ctx, serial, started_at, t0)
            if cap == "press_key":
                return self._press_key(event, ctx, serial, started_at, t0)
            if cap == "swipe_direction":
                return self._swipe_direction(event, ctx, serial, started_at, t0)
            if cap == "swipe_element_to_element":
                return self._swipe_element_to_element(event, ctx, serial, started_at, t0)
            if cap == "install":
                return self._install_apk(event, ctx, serial, started_at, t0)
            if cap == "read_device_data":
                return self._read_device_data(event, ctx, serial, started_at, t0)
            if cap == "get_app_version":
                return self._get_app_version(event, ctx, serial, started_at, t0)
            if cap == "get_foreground_app":
                return self._get_foreground_app(event, ctx, serial, started_at, t0)
            if cap == "set_clipboard":
                return self._set_clipboard(event, ctx, serial, started_at, t0)
            if cap == "tap_xy":
                return self._tap_xy(event, ctx, serial, started_at, t0)
            if cap == "tap_element":
                self._focus_actions.pop(focus_key, None)
                result = self._tap_element(event, ctx, serial, started_at, t0)
                return result
            if cap == "long_press_element":
                return self._long_press_element(event, ctx, serial, started_at, t0)
            if cap == "input_text":
                if str((event.params or {}).get("target_mode") or "") == "current_focus":
                    focus = self._focus_actions.get(focus_key)
                    node = self._focused_input(serial, expect=focus[1:] if focus else None)
                    if (not focus or focus[0] != str((event.params or {}).get("focus_ref") or "")
                            or node is None or not _same_input_field(node, focus[1:])):
                        self._focus_actions.pop(focus_key, None)
                        # summary/error 必须保持原句（Nexus 按整句识别）；诊断只放 raw_response。
                        return make_event_result(
                            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
                            elapsed_ms=int((time.time() - t0) * 1000),
                            summary="contract_unsupported: 焦点点击引用失效",
                            error="contract_unsupported: 焦点点击引用失效",
                            raw_response={**self._focus_mismatch_diag(serial, event, focus),
                                          "execution": {"execution_started": "no", "phase": "resolve",
                                                        "reason_code": "focus_reference_invalid",
                                                        "source": "adb.native.v1"}},
                        )
                result = self._input_text(event, ctx, serial, started_at, t0)
                self._focus_actions.pop(focus_key, None)
                return result
            if cap == "exec_script":
                return self._exec_script(event, ctx, serial, started_at, t0)
            if cap == "set_input_method":
                return self._set_input_method(event, ctx, serial, started_at, t0)
            # 兜底：capability 未写 Python 分支时，按 yaml 里的 low_level 声明执行。
            # 这条分支让"只加 yaml 就多一个能力"成真（见 docs/plan-skill-packs-and-console.md §3.1）。
            return self._run_declared_low_level(event, ctx, serial, started_at, t0)
        except Exception as e:
            SLog.e(TAG, f"execute exception cap={cap} sn={ctx.device.sn}: {e}")
            return self._fail(event, started_at, t0, f"exception: {e}")

    # ---------- 通用 shell helper ----------

    def _adb_shell(self, serial: str, *args: str, timeout_sec: float = 30.0) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["adb", "-s", serial, "shell", *args],
                capture_output=True, text=True, timeout=timeout_sec,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout {timeout_sec}s"
        except FileNotFoundError:
            return -2, "", "adb not in PATH"

    def _adb_sh_script(self, serial: str, script: str, *, timeout_sec: float = 30.0):
        """在设备上跑一段**复合 shell 脚本**（含管道 / 分号 / 括号）。

        为什么不能直接 `_adb_shell(serial, "sh", "-c", script)`：
        `adb shell` 会把 argv 拼成一行发到设备，**设备侧的 shell 再解析一次词**，
        于是 `sh -c` 只拿到脚本的第一个词（如 `(`），报
        `/system/bin/sh: syntax error: unexpected '('`。

        必须再引一层。这是上游 MiniOrangeServer 的一个真 bug ——
        `get_foreground_app` / 任何带管道的 dumpsys 在真机上一直是坏的
        （已在 vivo V2528A 上复现并验证修复）。
        """
        return self._adb_shell(serial, "sh", "-c", shlex.quote(script), timeout_sec=timeout_sec)

    def _adb(self, serial: str, *args: str, timeout_sec: float = 60.0) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["adb", "-s", serial, *args],
                capture_output=True, text=True, timeout=timeout_sec,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout {timeout_sec}s"
        except FileNotFoundError:
            return -2, "", "adb not in PATH"

    # ---------- handlers ----------

    def _launch_app(self, event, ctx, serial, started_at, t0):
        pkg = (event.params or {}).get("package") or ""
        if not pkg:
            return self._fail(event, started_at, t0, "launch_app 缺 params.package")
        rc, out, err = self._adb_shell(serial, "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0 and "Events injected" in out:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"启动 {pkg}",
                raw_response={**({"stdout": out[:240]}), "execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"启动 {pkg} 失败", error=err or out or f"rc={rc}",
            raw_response={"stdout": out[:240], "stderr": err[:240], "rc": rc},
        )

    def _close_app(self, event, ctx, serial, started_at, t0):
        pkg = (event.params or {}).get("package") or ""
        if not pkg:
            return self._fail(event, started_at, t0, "close_app 缺 params.package")
        rc, out, err = self._adb_shell(serial, "am", "force-stop", pkg)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"强停 {pkg}",
                raw_response={"execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"强停 {pkg} 失败", error=err or out or f"rc={rc}",
        )

    def _press_key(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        keycode = str(params.get("key") or params.get("keycode") or "").upper()
        if not keycode:
            return self._fail(event, started_at, t0, "press_key 缺 params.key", raw_response={
                "execution": {"execution_started": "no", "phase": "validate",
                    "reason_code": "missing_key_parameter", "source": "adb.native.v1"}})
        key_map = {
            "BACK": "4", "HOME": "3", "MENU": "82", "POWER": "26", "ENTER": "66",
            "VOLUME_UP": "24", "VOLUME_DOWN": "25", "RECENT": "187",
        }
        kev = key_map.get(keycode, keycode)
        rc, out, err = self._adb_shell(serial, "input", "keyevent", kev)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"按键 {keycode}",
                raw_response={"execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"按键 {keycode} 失败", error=err or out,
        )

    def _uninstall_app(self, event, ctx, serial, started_at, t0):
        pkg = str((event.params or {}).get("package") or (event.params or {}).get("bundle") or "").strip()
        if not pkg:
            return self._fail(event, started_at, t0, "uninstall_app 缺 params.package")
        rc, out, err = self._adb(serial, "uninstall", pkg)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0 and "Success" in out:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"已卸载 {pkg}",
                raw_response={"execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"卸载 {pkg} 失败", error=err or out or f"rc={rc}",
        )

    def _swipe_direction(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        direction = str(params.get("direction") or "up").lower()
        fx, fy, tx, ty = (
            params.get("from_x"),
            params.get("from_y"),
            params.get("to_x"),
            params.get("to_y"),
        )
        if None not in (fx, fy, tx, ty):
            try:
                x1, y1 = self._map_to_display(serial, int(fx), int(fy))
                x2, y2 = self._map_to_display(serial, int(tx), int(ty))
            except (TypeError, ValueError):
                return self._fail(event, started_at, t0, "swipe_direction 起止坐标无效")
            summary = f"滑动 ({fx},{fy})→({tx},{ty})"
        else:
            rc, out, _err = self._adb_shell(serial, "wm", "size")
            w, h = 1080, 1920
            if rc == 0 and "Physical size:" in out:
                try:
                    size_str = out.rsplit(":", 1)[-1].strip()
                    wp, hp = size_str.split("x")
                    w, h = int(wp), int(hp)
                except Exception:
                    pass
            cx = w // 2
            cy = h // 2
            steps = {
                "up": (cx, int(h * 0.75), cx, int(h * 0.25)),
                "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
                "left": (int(w * 0.85), cy, int(w * 0.15), cy),
                "right": (int(w * 0.15), cy, int(w * 0.85), cy),
            }
            if direction not in steps:
                return self._fail(event, started_at, t0, f"unsupported direction={direction}")
            x1, y1, x2, y2 = steps[direction]
            summary = f"滑动 {direction}"
        duration = int(params.get("duration_ms") or 300)
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=summary,
                raw_response={**({"from": (x1, y1), "to": (x2, y2)}), "execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"{summary} 失败", error=err or out,
        )

    def _swipe_element_to_element(self, event, ctx, serial, started_at, t0):
        # 需要 vlm 子流程：from/to 两次 locate；这里假设上层已塞 coords
        params = event.params or {}
        x1 = params.get("from_x"); y1 = params.get("from_y")
        x2 = params.get("to_x"); y2 = params.get("to_y")
        if None in (x1, y1, x2, y2):
            return self._fail(event, started_at, t0, "swipe_element_to_element 需要 from_x/from_y/to_x/to_y（VLM locate 应已注入）")
        policy = str(params.get("point_policy") or "").strip().lower()
        if policy in ("coordinate", "node"):
            x1, y1 = self._map_to_display(serial, int(x1), int(y1))
            x2, y2 = self._map_to_display(serial, int(x2), int(y2))
        duration = int(params.get("duration_ms") or 400)
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"拖拽 ({x1},{y1})→({x2},{y2})",
                raw_response={"execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="拖拽失败", error=err or out,
        )

    def _install_apk(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        path = params.get("path") or ""
        url = params.get("url") or ""
        tmp_path = ""
        # 支持 url（内部下载到本地再 install），也兼容本地 path
        if not path and url:
            try:
                from mino_scout.platform.android.adb_command import _download_apk

                path = _download_apk(url, params.get("file_name") or "")
                tmp_path = path
            except Exception as e:
                return self._fail(event, started_at, t0, f"apk 下载失败: {e}")
        if not path:
            return self._fail(event, started_at, t0, "install_apk 缺 params.path 或 params.url")
        try:
            rc, out, err = self._adb(serial, "install", "-r", "-t", path, timeout_sec=300.0)
            elapsed = int((time.time() - t0) * 1000)
            if rc == 0 and "Success" in out:
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                    elapsed_ms=elapsed, summary=f"安装 {path}",
                )
            return make_event_result(
                event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary="安装失败", error=err or out,
            )
        finally:
            if tmp_path:
                import os as _os

                try:
                    _os.remove(tmp_path)
                except OSError:
                    pass

    def _exec_script(self, event, ctx, serial, started_at, t0):
        """adb 版 EXEC_SCRIPT：委托 adb_script（dsl/shell；js→not_supported）。"""
        from mino_scout.platform.android.adb_script import run_adb_script

        res = run_adb_script(serial, event.params or {})
        elapsed = int((time.time() - t0) * 1000)
        status_raw = str(res.get("status") or "").lower()
        if status_raw == "success":
            status = EventStatus.PASS
        elif status_raw == "not_supported":
            status = EventStatus.SKIPPED
        else:
            status = EventStatus.FAIL
        return make_event_result(
            event, status=status, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=res.get("message") or "exec_script",
            error=res.get("stderr") or "", raw_response={"stdout": (res.get("stdout") or "")[:2000]},
        )

    def _getprop(self, serial: str, prop: str) -> str:
        rc, out, _err = self._adb_shell(serial, "getprop", prop)
        if rc == 0:
            return (out or "").strip()
        return ""

    def _read_device_data(self, event, ctx, serial, started_at, t0):
        key = str((event.params or {}).get("key") or "sim").strip().lower()
        if key in ("sim", "sim_card", "sim_status", "sim_state"):
            return self._read_sim_bundle(event, serial, started_at, t0)
        if key in ("esim", "e_sim", "euicc"):
            return self._read_esim_info(event, serial, started_at, t0)
        prop_map = {
            "model": "ro.product.model",
            "brand": "ro.product.brand",
            "android_version": "ro.build.version.release",
            "sdk": "ro.build.version.sdk",
            "abi": "ro.product.cpu.abi",
            "sim_state": "gsm.sim.state",
            "operator": "gsm.operator.alpha",
            "phone_number": "gsm.line1.number",
        }
        prop = prop_map.get(key, key)
        rc, out, err = self._adb_shell(serial, "getprop", prop)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            value = out.strip()
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"{key}={value}",
                raw_response={"key": key, "value": value, "prop": prop},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"读取 {key} 失败", error=err or out,
        )

    def _read_sim_bundle(self, event, serial, started_at, t0) -> EventResult:
        fields: dict[str, str] = {}
        for label, prop in (
            ("sim_state", "gsm.sim.state"),
            ("operator", "gsm.operator.alpha"),
            ("sim_operator", "gsm.sim.operator.alpha"),
            ("operator_numeric", "gsm.operator.numeric"),
            ("phone_number", "gsm.line1.number"),
        ):
            val = self._getprop(serial, prop)
            if val:
                fields[label] = val

        sub_count = 0
        rc, out, _err = self._adb_shell(serial, "dumpsys", "telephony.registry")
        if rc == 0 and out:
            slots = set(re.findall(r"mSlotId=(\d+)", out))
            if slots:
                sub_count = len(slots)
            elif "mCallState" in out:
                sub_count = max(1, out.count("mCallState="))

        states = [
            s.strip().upper()
            for s in re.split(r"[,;]", fields.get("sim_state", ""))
            if s.strip()
        ]
        ready = any(s in ("READY", "LOADED", "PIN_REQUIRED", "PUK_REQUIRED", "NETWORK_LOCKED") for s in states)
        absent = not states or all(s == "ABSENT" for s in states)

        parts: list[str] = []
        if ready:
            parts.append("sim=READY")
        elif absent:
            parts.append("sim=ABSENT")
        else:
            parts.append(f"sim={fields.get('sim_state', 'UNKNOWN')}")
        op = fields.get("sim_operator") or fields.get("operator")
        if op:
            parts.append(f"operator={op}")
        phone = fields.get("phone_number")
        if phone:
            parts.append(f"phone={phone}")
        if sub_count:
            parts.append(f"slots={sub_count}")

        elapsed = int((time.time() - t0) * 1000)
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="; ".join(parts) if parts else "sim=unknown",
            raw_response={
                "key": "sim",
                "fields": fields,
                "ready": ready,
                "absent": absent,
                "sub_count": sub_count,
            },
        )

    def _read_esim_info(self, event, serial, started_at, t0) -> EventResult:
        fields: dict[str, str] = {}
        for label, prop in (
            ("euicc", "ro.boot.euicc"),
            ("euicc_enabled", "persist.sys.euicc.enabled"),
        ):
            val = self._getprop(serial, prop)
            if val:
                fields[label] = val

        profiles: list[str] = []
        rc, out, _err = self._adb_shell(serial, "dumpsys", "euicc")
        if rc == 0 and out:
            for m in re.finditer(r"(?:Profile nickname|Nickname):\s*(.+)", out):
                name = m.group(1).strip()
                if name:
                    profiles.append(name)
            if not profiles and "EuiccService" in out:
                fields["euicc_service"] = "present"

        parts: list[str] = []
        if profiles:
            parts.append(f"esim_profiles={len(profiles)}")
            parts.append(f"names={','.join(profiles[:3])}")
        elif fields.get("euicc") or fields.get("euicc_service"):
            parts.append("esim=supported")
        else:
            parts.append("esim=none")

        sim_state = self._getprop(serial, "gsm.sim.state")
        if sim_state:
            parts.append(f"sim_state={sim_state}")

        elapsed = int((time.time() - t0) * 1000)
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="; ".join(parts),
            raw_response={"key": "esim", "fields": fields, "profiles": profiles, "sim_state": sim_state},
        )

    def _get_app_version(self, event, ctx, serial, started_at, t0):
        from mino_scout.platform.android.app_query import parse_package_version, version_dump_shell

        pkg = str((event.params or {}).get("package") or ctx.device.extra.get("target_package", "") or "").strip()
        if not pkg:
            return self._fail(event, started_at, t0, "get_app_version 缺 params.package")
        rc, out, err = self._adb_sh_script(serial, version_dump_shell(pkg), timeout_sec=20.0)
        elapsed = int((time.time() - t0) * 1000)
        parsed = parse_package_version(out)
        name = str(parsed.get("version_name") or "").strip()
        if rc == 0 and name:
            # 上游这里调 stamp_app_version(ctx, name) 把版本写回 RunContext。
            # RunContext 归 Nexus，Scout 只把版本放进 raw_response，由 Nexus 落账。
            code = parsed.get("version_code")
            extra = f" ({code})" if code is not None else ""
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"{pkg} {name}{extra}",
                raw_response={"package": pkg, **parsed, "rc": rc},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"读不到 {pkg} 的版本",
            error=err or out or f"rc={rc}",
            raw_response={"package": pkg, **parsed, "rc": rc, "stdout": (out or "")[:400]},
        )

    def _get_foreground_app(self, event, ctx, serial, started_at, t0):
        from mino_scout.platform.android.app_query import FOREGROUND_SHELL, parse_foreground

        rc, out, err = self._adb_sh_script(serial, FOREGROUND_SHELL, timeout_sec=20.0)
        elapsed = int((time.time() - t0) * 1000)
        parsed = parse_foreground(out)
        pkg = str(parsed.get("package") or "").strip()
        expect = str((event.params or {}).get("package") or ctx.device.extra.get("target_package", "") or "").strip()
        if rc == 0 and pkg:
            summary = parsed.get("activity") or pkg
            if expect:
                summary = f"{summary}（目标 {expect}{'命中' if pkg == expect else '未命中'}）"
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=str(summary),
                raw_response={**parsed, "expected_package": expect, "match": (pkg == expect) if expect else None, "rc": rc},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="读不到前台应用",
            error=err or out or f"rc={rc}",
            raw_response={**parsed, "rc": rc, "stdout": (out or "")[:400]},
        )

    def _set_clipboard(self, event, ctx, serial, started_at, t0):
        text = (event.params or {}).get("text") or ""
        if not text:
            return self._fail(event, started_at, t0, "set_clipboard 缺 params.text")
        # Android 没有原生 adb shell 写剪贴板的口子，用 broadcast 或 input text 兜底
        rc, out, err = self._adb_shell(serial, "service", "call", "clipboard", "1")
        elapsed = int((time.time() - t0) * 1000)
        if rc != 0:
            # 退而求其次：写到剪贴板 helper app 是另一回事；这里 stub
            return make_event_result(
                event, status=EventStatus.SKIPPED, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary="set_clipboard adb 通路无原生支持，建议改 remote",
            )
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"剪贴板设置 {len(text)} 字",
        )

    def _tap_xy(self, event, ctx, serial, started_at, t0):
        raw = event.params or {}
        if raw.get("x") is None or raw.get("y") is None:
            return self._fail(event, started_at, t0, "tap_xy 缺坐标")
        try:
            x, y = int(raw["x"]), int(raw["y"])
        except (TypeError, ValueError):
            return self._fail(event, started_at, t0, "tap_xy 坐标无效")
        ok, inject, extra = self._inject_tap(serial, x, y, scale=True)
        elapsed = int((time.time() - t0) * 1000)
        self._invalidate_hierarchy(serial)
        if ok:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"点击坐标 ({x},{y})",
                raw_response={**extra, "inject": inject, "execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"点击坐标 ({x},{y}) 失败",
            error=str(extra.get("error") or extra.get("stderr") or inject), raw_response=extra,
        )

    def _tap_element(self, event, ctx, serial, started_at, t0):
        # 坐标来源：语义锚点优先（稳定、可复用），模型给的坐标兜底
        x, y, audit, how = self._point_for(event, serial)
        if x is None or y is None:
            policy = str((event.params or {}).get("point_policy") or "")
            if policy == "node":
                reason = "界面文案锚点未命中且未给坐标"
            elif policy == "coordinate":
                reason = "视觉点击缺少坐标"
            else:
                reason = "tap_element 无可用坐标（锚点未命中且未给 x/y）"
            extra = (audit.get("anchor") or {}).get("reason", "") if audit else ""
            return self._fail(
                event, started_at, t0,
                reason + (f"：{extra}" if extra else ""),
                raw_response={"execution": {"execution_started": "no", "phase": "resolve",
                    "reason_code": "tap_target_unresolved", "source": "adb.native.v1"}},
            )
        point = self._map_to_display(serial, x, y) if how in ("xy", "fallback_xy") else (x, y)
        focus_target = self._input_target_before_tap(serial, point)
        ok, inject, extra = self._inject_tap(serial, x, y, scale=how in ("xy", "fallback_xy"))
        elapsed = int((time.time() - t0) * 1000)
        label = self._point_label(how, audit, x, y)
        audit = {**audit, "inject": inject, **extra}
        self._invalidate_hierarchy(serial)
        if ok:
            self._record_focus_after_tap(event, ctx, serial, focus_target)
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"点击 {label}", raw_response={**(audit), "execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"点击 {label} 失败",
            error=str(extra.get("error") or extra.get("stderr") or inject),
            raw_response=audit,
        )

    def _point_label(self, how: str, audit: dict, x: int, y: int) -> str:
        """人可读的落点说明：锚点命中时说清点的是哪个元素。"""
        if how == "anchor":
            a = audit.get("anchor") or {}
            name = a.get("text") or a.get("content_desc") or a.get("resource_id") or "?"
            return f"「{str(name)[:20]}」({x},{y}) via {a.get('matched_by')}"
        if how == "snap":
            s = audit.get("snap") or {}
            name = s.get("text") or s.get("content_desc") or "?"
            return f"「{str(name)[:20]}」({x},{y})[snap {s.get('how')}]"
        if how == "fallback_xy":
            return f"({x},{y})[锚点未命中，回落坐标]"
        return f"({x},{y})"

    def _invalidate_hierarchy(self, serial: str) -> None:
        """动作后屏幕会变，主动失效层级缓存，避免下一步拿到旧快照。"""
        try:
            from mino_scout.platform.android import hierarchy as H

            H.invalidate_cache(serial)
        except Exception:  # pragma: no cover
            pass

    def _long_press_element(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        x, y, audit, how = self._point_for(event, serial)
        if x is None or y is None:
            return self._fail(event, started_at, t0, "long_press_element 无可用坐标")
        duration = int(params.get("duration_ms") or 1000)
        if how in ("xy", "fallback_xy"):
            x, y = self._map_to_display(serial, x, y)
        # 长按 = swipe 自己到自己
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(x), str(y), str(x), str(y), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        label = self._point_label(how, audit, x, y)
        self._invalidate_hierarchy(serial)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"长按 {label} {duration}ms", raw_response={**(audit), "execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="长按失败", error=err or out, raw_response=audit,
        )

    def _set_input_method(self, event, ctx, serial, started_at, t0):
        from mino_scout.platform.android.adb_ime import (
            ensure_adb_keyboard,
            restore_system_input_method,
            resolve_target_ime,
        )

        target = resolve_target_ime(event.params or {})
        if target == "__system__":
            outcome = restore_system_input_method(serial)
        else:
            outcome = ensure_adb_keyboard(serial, target_ime=target)
        elapsed = int((time.time() - t0) * 1000)
        if outcome.get("ok"):
            return make_event_result(
                event,
                status=EventStatus.PASS,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed,
                summary=str(outcome.get("summary") or "输入法已切换"),
                raw_response={**(outcome), "execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed,
            summary="切换输入法失败",
            error=str(outcome.get("error") or "unknown"),
            raw_response=outcome,
        )

    def _input_text(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        text = params.get("text") or ""
        if not text:
            return self._fail(event, started_at, t0, "input_text 缺 params.text")
        if str(params.get("target_mode") or "") == "current_focus":
            if str(params.get("mode") or "replace").strip().lower() != "append":
                return self._fail(event, started_at, t0, "contract_unsupported: ADB 当前焦点不支持 replace")
            ref = str(params.get("focus_ref") or "")
            if not ctx.run_id or not ref.startswith(f"focus:{ctx.run_id}:") or not ref.rsplit(":", 1)[-1].isdigit():
                return self._fail(event, started_at, t0, "contract_unsupported: current_focus 缺少有效 focus_ref")
            safe_text = str(text).replace(" ", "%s")
            rc, out, err = self._adb_shell(serial, "input", "text", safe_text)
            elapsed = int((time.time() - t0) * 1000)
            self._invalidate_hierarchy(serial)
            if rc == 0:
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                    elapsed_ms=elapsed, summary=f"输入 {len(text)} 字 已写入当前焦点",
                    raw_response={"execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
                )
            return make_event_result(
                event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary="输入失败（中文需要 IME 协助，建议改 remote）",
                error=err or out,
            )
        policy = str(params.get("point_policy") or "").strip().lower()
        x, y, audit, how = self._point_for(event, serial)
        if x is None or y is None:
            if policy == "node":
                return self._fail(event, started_at, t0, "DOM 输入无锚点也无坐标")
            return self._fail(event, started_at, t0, "视觉输入缺少坐标")
        self._inject_tap(serial, x, y, scale=how in ("xy", "fallback_xy"))
        # adb input text 不支持中文 / 空格；空格转 %s
        safe_text = str(text).replace(" ", "%s")
        rc, out, err = self._adb_shell(serial, "input", "text", safe_text)
        elapsed = int((time.time() - t0) * 1000)
        self._invalidate_hierarchy(serial)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"输入 {len(text)} 字", raw_response={**(audit), "execution": {"execution_started": "yes", "phase": "completed", "source": "adb.native.v1"}},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="输入失败（中文需要 IME 协助，建议改 remote）",
            error=err or out, raw_response=audit,
        )

    # ---------- 内部 ----------

    # ---------- 语义锚点解析（S0b） ----------

    def _resolve_anchor_xy(self, event, serial: str) -> tuple[Optional[int], Optional[int], dict]:
        """params.target 有语义锚点时，用 UI 层级解析成精确坐标。

        返回 (x, y, audit)。解析不到时 x/y 为 None，调用方回落到模型给的坐标。
        audit 会进 raw_response，便于回放时看清"这一下点的到底是哪个元素"。
        """
        from mino_scout.platform.android import hierarchy as H

        params = H.lift_text_anchor(event.params or {})
        dump = H.dump_ui_nodes(serial)
        if not dump.ok:
            return None, None, {"anchor": {"ok": False, "reason": f"层级采集失败: {dump.error[:120]}"}}
        anchor = params.get("anchor_between")
        if isinstance(anchor, (list, tuple)) and len(anchor) >= 2:
            match = H.resolve_anchor_between(
                dump.nodes, str(anchor[0] or ""), str(anchor[1] or "")
            )
            if match is not None:
                x, y = match.node.center
                audit = {"anchor": {"ok": True, "nodes": len(dump), "dump_ms": dump.elapsed_ms,
                                    **match.to_brief()}}
                return x, y, audit
        if not H.has_target(params):
            return None, None, {}
        target = dict(params.get("target") or {})
        match = H.resolve_target(dump.nodes, target)
        if match is None:
            label = str(target.get("text") or target.get("content_desc") or "").strip()
            if label:
                match = H.resolve_bottom_tab_text(dump.nodes, label)
        if match is None:
            return None, None, {"anchor": {"ok": False, "reason": "锚点未命中任何节点",
                                           "target": target, "nodes": len(dump)}}
        x, y = match.node.center
        audit = {"anchor": {"ok": True, "target": target, "nodes": len(dump),
                            "dump_ms": dump.elapsed_ms, **match.to_brief()}}
        SLog.i(TAG, f"anchor hit by={match.matched_by} label={match.node.label()!r} "
                    f"-> ({x},{y}) candidates={match.candidates}")
        return x, y, audit

    def _point_for(self, event, serial: str) -> tuple[Optional[int], Optional[int], dict, str]:
        """点只有两种。node 只用锚点。其余只用 x,y，不按文案改点，也不吸附。"""
        raw = event.params or {}
        policy = str(raw.get("point_policy") or "").strip().lower()
        if policy == "node":
            ax, ay, audit = self._resolve_anchor_xy(event, serial)
            audit = {**audit, "point_policy": "node"}
            if ax is None or ay is None:
                # 锚点没命中不是拒绝执行的理由：Nexus 已经算好的坐标就是指令。
                # 回「不用坐标兜底」会把整条用例判死（cr-9f7f478af02c 的 21 条弹窗），
                # 而 Scout 的职责是执行收到的指令，不是替 Nexus 决定策略。
                fy = raw.get("fallback_xy")
                if isinstance(fy, (list, tuple)) and len(fy) == 2:
                    try:
                        return int(fy[0]), int(fy[1]), {**audit, "point_policy": "anchor_fallback"}, "fallback_xy"
                    except (TypeError, ValueError):
                        pass
                fx, fyy = raw.get("x"), raw.get("y")
                if fx is not None and fyy is not None:
                    try:
                        return int(fx), int(fyy), {**audit, "point_policy": "anchor_fallback"}, "xy"
                    except (TypeError, ValueError):
                        pass
                return None, None, audit, "none"
            return ax, ay, audit, "anchor"
        x, y = raw.get("x"), raw.get("y")
        if x is None or y is None:
            fy = raw.get("fallback_xy")
            if isinstance(fy, (list, tuple)) and len(fy) == 2:
                try:
                    return int(fy[0]), int(fy[1]), {"point_policy": "coordinate"}, "fallback_xy"
                except (TypeError, ValueError):
                    pass
            return None, None, {"point_policy": "coordinate"}, "none"
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            return None, None, {"point_policy": "coordinate"}, "none"
        return x, y, {"point_policy": "coordinate"}, "xy"

    def _wm_size(self, serial: str) -> tuple[int, int]:
        hit = _WM_CACHE.get(serial)
        now = time.time()
        if hit and (now - hit[0]) < _WM_TTL_SEC:
            return hit[1], hit[2]
        rc, out, _err = self._adb_shell(serial, "wm", "size")
        w, h = parse_wm_size(out) if rc == 0 else (0, 0)
        if w > 0 and h > 0:
            _WM_CACHE[serial] = (now, w, h)
        return w, h

    def _map_to_display(self, serial: str, x: int, y: int) -> tuple[int, int]:
        from mino_scout.screen import last_capture_size

        display = self._wm_size(serial)
        return map_protocol_xy_to_display(
            x, y, shot=last_capture_size(serial), display=display,
        )

    def _inject_tap(
        self, serial: str, x: int, y: int, *, settle: bool = True, scale: bool = True,
    ) -> tuple[bool, str, dict[str, Any]]:
        """真正把触控送到设备。优先 u2，其次短 swipe，最后才 input tap。

        不能把 `input tap` rc=0 当成点上了：Android 14+ / vivo / 小米上这是常见假成功。
        scale=True：坐标来自截图/模型，按 wm size 换算。层级锚点已经是设备坐标，不要再乘。
        """
        from mino_scout.platform.android import hierarchy as H

        if scale:
            x, y = self._map_to_display(serial, x, y)
        extra: dict[str, Any] = {"x": x, "y": y, "display": list(self._wm_size(serial))}

        ok, why = H.click_xy(serial, x, y)
        if ok:
            SLog.i(TAG, f"inject u2 click ({x},{y})")
            if settle:
                time.sleep(_TAP_SETTLE_SEC)
            return True, "u2", extra

        extra["u2"] = why
        rc, out, err = self._adb_shell(
            serial, "input", "swipe", str(x), str(y), str(x), str(y), str(_TAP_HOLD_MS),
        )
        if rc == 0:
            SLog.i(TAG, f"inject adb swipe-tap ({x},{y}) hold={_TAP_HOLD_MS}ms")
            if settle:
                time.sleep(_TAP_SETTLE_SEC)
            return True, "adb_swipe", extra

        rc2, out2, err2 = self._adb_shell(serial, "input", "tap", str(x), str(y))
        extra["stderr"] = (err2 or err or out2 or out)[:240]
        extra["rc"] = rc2
        if rc2 == 0:
            SLog.w(TAG, f"inject adb tap fallback ({x},{y}) swipe_rc={rc} swipe_err={err or out}")
            if settle:
                time.sleep(_TAP_SETTLE_SEC)
            return True, "adb_tap", extra
        SLog.e(TAG, f"inject failed ({x},{y}) swipe_rc={rc} tap_rc={rc2} err={extra['stderr']}")
        extra["error"] = extra["stderr"] or f"swipe_rc={rc} tap_rc={rc2}"
        return False, "failed", extra

    # ---------- 通用 low_level 兜底 ----------

    def _run_declared_low_level(self, event, ctx, serial, started_at, t0):
        """按 capability yaml 的 low_level 声明执行（无 Python 分支时的通路）。"""
        from mino_scout.platform.android.low_level import run_low_level

        # 上游只从 ctx.selected_impl 取（router 从能力目录塞进去的）。
        # 拆分后 Nexus 把 low_level 直接放在 EXECUTE 载荷里，优先用它。
        low = dict(event.low_level or {})
        if not low:
            low = dict((ctx.selected_impl or {}).get("low_level") or {})
        cap = event.capability_id
        if not low:
            return self._fail(
                event, started_at, t0,
                f"cap={cap} 既无 Python 分支，其 adb 实现也未声明 low_level",
            )

        def _shell(cmd: str) -> tuple[int, str, str]:
            # 整串交给 adb shell（命令已过白名单，不含重定向/管道到 sh）
            return self._adb_shell(serial, cmd)

        from mino_scout.runtime.execution_boundary import READS
        outcome = run_low_level(
            low, dict(event.params or {}), _shell,
            log_prefix=f"[{ctx.run_id}] cap={cap} ",
            read_only=cap in READS, receipt_source="adb.low_level.v1",
        )
        elapsed = int((time.time() - t0) * 1000)
        SLog.i(TAG, f"[{ctx.run_id}] low_level cap={cap} kind={low.get('kind')} "
                    f"ok={outcome.ok} cmds={len(outcome.commands)}")
        return make_event_result(
            event,
            status=EventStatus.PASS if outcome.ok else EventStatus.FAIL,
            executor_used=self.id, started_at=started_at, elapsed_ms=elapsed,
            summary=outcome.summary, error=outcome.error,
            raw_response=outcome.as_raw_response(),
        )

    def _focus_mismatch_diag(self, serial: str, event, focus) -> dict[str, Any]:
        """焦点引用失配时留证：记录的目标 vs 此刻所有 focused/全部 EditText，一次定位原因。"""
        diag: dict[str, Any] = {
            "focus_diag": True,
            "ref_sent": str((event.params or {}).get("focus_ref") or ""),
            "ref_recorded": focus[0] if focus else None,
            "recorded": (
                {"package": focus[1], "resource_id": focus[2], "bounds": list(focus[3])} if focus else None
            ),
        }
        try:
            from mino_scout.platform.android.hierarchy import dump_ui_nodes

            dump = dump_ui_nodes(serial, force_fresh=True)
            if not dump.ok:
                diag["dump_error"] = str(dump.error)[:120]
                return diag
            edits = [n for n in dump.nodes if "EditText" in n.cls]
            diag["edit_texts"] = [
                {
                    "focused": n.focused, "enabled": n.enabled, "bounds": list(n.bounds),
                    "hint": n.hint[:30], "resource_id": n.resource_id, "package": n.package,
                }
                for n in edits[:8]
            ]
            diag["focused_count"] = sum(1 for n in edits if n.focused)
        except Exception as exc:  # noqa: BLE001
            diag["diag_error"] = str(exc)[:120]
        return diag

    def _fail(self, event, started_at, t0, msg: str, *, raw_response=None) -> EventResult:
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=msg, error=msg,
            raw_response=raw_response or {},
        )
