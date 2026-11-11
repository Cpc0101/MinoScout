"""iOS 通道：WDA 负责页面动作，devicectl 负责装包/卸载。

装包与 WDA 无关：`xcrun devicectl device install app` 自己解 `.ipa`，不需要 WDA
会话、也不需要手机点“信任”。所以 probe 只要看到设备就应当可用，WDA 缺失时
只收窄 `provides`，不能把整条通道判成不可用——否则 WDA 没起来就连装包一起没了。

清应用数据没有对应接口，不改写 App 沙盒。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mino_scout.executors.base import make_event_result, now_iso
from mino_scout.tools.keep_alive.log import SLog
from mino_scout.tools.app_package import PackageError
from mino_scout.schemas import EventStatus
from mino_scout.platform.ios.wda_session import (
    WdaError, app_version, client_for, device_online, install_app, installed_apps,
    list_attached_devices, uninstall_app,
)

TAG = "WdaExecutor"
_INSTALL_CAPS = frozenset({"install", "install_app", "install_apk", "uninstall_app", "get_app_version"})
_WDA_CAPS = frozenset({
    "launch_app", "close_app", "kill_app", "tap_element", "tap_xy", "exec_script",
    "input_text", "press_key", "screenshot", "hierarchy",
    "swipe_direction", "swipe_element_to_element", "long_press_element",
    "get_foreground_app", "set_clipboard",
})
_REINSTALL_WORDS = frozenset({"1", "true", "yes", "on"})


class WdaExecutor:
    id = "ios_wda"

    def __init__(self):
        # probe 之前不知道 WDA 有没有起来；保守只报不依赖 WDA 的能力。
        self._wda_ready = False
        self._attached = False

    @property
    def provides(self) -> list[str]:
        caps = set(_INSTALL_CAPS)
        if self._wda_ready:
            caps |= _WDA_CAPS
        return sorted(caps)

    def probe(self):
        """设备连上就算可用。WDA 只影响页面动作，不影响装包/卸载/读版本。

        WDA 按设备探测：局域网走 CoreDevice 隧道，USB 走 iproxy，不再只认
        全局 ``127.0.0.1:8100``。
        """
        from mino_scout.platform.ios.wda_reach import candidate_urls, probe_url
        from mino_scout.platform.ios.wda_session import device_wda_ready

        devices = [d for d in list_attached_devices() if str(d.get("state") or "") == "connected"]
        self._attached = bool(devices)
        ready_names: list[str] = []
        for row in devices:
            # Cheap path first (tunnel / configured URL); fall back to iproxy bring-up.
            if any(probe_url(url) for url in candidate_urls(row)) or device_wda_ready(row):
                ready_names.append(str(row.get("name") or row.get("udid") or ""))
        self._wda_ready = bool(ready_names)
        if self._attached:
            if self._wda_ready:
                return True, f"WDA 就绪：{', '.join(ready_names[:3])}"
            return True, "iOS 设备已连接，但各设备 WDA 均未监听；只能装包/卸载/读版本"
        return False, "没有可控的 iOS 设备（需 USB/局域网通路）"

    def supports(self, capability_id: str, low_level: dict[str, Any] | None = None) -> bool:
        del low_level
        cap = str(capability_id or "")
        # 不对外声明清数据，但收到时要明确拒绝，避免无路由失败被记成写结果未知。
        return cap in _INSTALL_CAPS or cap in _WDA_CAPS or cap == "clear_app_cache"

    def execute(self, event, ctx):
        started, tick = now_iso(), time.monotonic()
        cap = str(event.capability_id or "")
        if cap == "clear_app_cache":
            return make_event_result(
                event, status=EventStatus.DECLINED, executor_used=self.id,
                started_at=started, elapsed_ms=0,
                summary="iOS 没有清应用数据的通道",
                error="not_supported",
                raw_response={"execution": {"execution_started": "no", "phase": "route", "reason_code": "not_supported"}},
            )
        status, error, data = EventStatus.PASS, "", {}
        try:
            if str(ctx.device.platform or "") != "ios":
                return make_event_result(
                    event, status=EventStatus.DECLINED, executor_used=self.id,
                    started_at=started, elapsed_ms=0, summary="不是 iOS 设备", error="platform_mismatch",
                )
            if cap in _INSTALL_CAPS:
                data = self._package(event, ctx)
            else:
                data = self._run(event, ctx)
        except WdaError as exc:
            status, error = EventStatus.FAIL, str(exc)
            data = {**(data if isinstance(data, dict) else {}), "execution": {
                "execution_started": "yes", "phase": "completed", "source": "ios.devicectl.v1"}}
        except PackageError as exc:
            # 取包/验包阶段就失败：动作没有下发到设备。
            status, error = EventStatus.FAIL, str(exc)
            data = {"execution": {"execution_started": "no", "phase": "validate",
                                  "reason_code": exc.code, "source": "ios.devicectl.v1"}}
        except Exception as exc:
            SLog.e(TAG, f"run={ctx.run_id} cap={cap} error={type(exc).__name__}")
            status, error = EventStatus.FAIL, f"{type(exc).__name__}: {exc}"
        summary = str(data.get("summary") or "") or ("完成" if status == EventStatus.PASS else error)
        return make_event_result(
            event, status=status, executor_used=self.id, started_at=started,
            elapsed_ms=int((time.monotonic() - tick) * 1000), summary=summary[:240],
            error=error[:240], raw_response=data,
        )

    # ------------------------------------------------------------ 装包 / 卸载

    def _package(self, event, ctx) -> dict[str, Any]:
        """装包、卸载、读版本。全程只依赖 devicectl。"""
        from mino_scout.tools import app_package

        cap = str(event.capability_id or "")
        params = dict(event.params or {})
        udid = str(ctx.device.udid or ctx.device.sn or "")
        if not udid:
            raise PackageError("这台设备没有 udid，无法用 devicectl 操作", code="missing_udid")
        if cap == "get_app_version":
            bundle = str(params.get("bundle_id") or params.get("package") or params.get("bundle") or "")
            if not bundle:
                raise PackageError("读版本缺 bundle id", code="missing_bundle_id")
            info = app_version(udid, bundle)
            return {"summary": f"{info['bundle_id']} {info['version']}", **info}

        if cap == "uninstall_app":
            bundle = str(params.get("bundle_id") or params.get("package") or params.get("bundle") or "")
            if not bundle:
                raise PackageError("uninstall_app 缺 params.bundle_id 或 params.package",
                                   code="missing_bundle_id")
            online, state = device_online(udid)
            if not online:
                raise WdaError(f"设备 {udid} 当前不可用（{state}）")
            result = uninstall_app(udid, bundle)
            return {**result, "execution": _receipt()}

        source, temp = app_package.resolve_source(params, platform="ios")
        try:
            package = app_package.inspect_package(source, platform="ios")
            if not package.get("sha256"):
                raise PackageError(package.get("reason") or "读不到安装包", code="package_unreadable")
            if source.suffix.lower() not in {".ipa", ".app"}:
                raise PackageError("iOS 只接受 .ipa 或已解包的 .app；.dmg/.pkg 是 macOS 的安装包",
                                   code="unsupported_suffix")
            online, state = device_online(udid)
            if not online:
                raise WdaError(f"设备 {udid} 当前不可用（{state}）；确认数据线、解锁与开发者模式")
            bundle = str(params.get("bundle_id") or params.get("package") or package.get("bundle_id") or "")
            _check_profile(package, udid=udid, params=params)
            removed = False
            if _wants_reinstall(params) and bundle and any(
                    str(row.get("bundleIdentifier") or "") == bundle for row in installed_apps(udid)):
                uninstall_app(udid, bundle)
                removed = True
            installed = install_app(udid, str(source), timeout_sec=_timeout_sec(params))
            installed_id = installed.get("bundle_id") or bundle
            verified = _verify_installed(udid, installed_id, package)
            summary = f"已安装 {installed_id or Path(source).name} {package.get('version') or ''}".strip()
            if removed:
                summary = "已重装（先卸载）" + summary.removeprefix("已安装")
            return {
                **package,
                "summary": summary,
                "bundle_id": installed_id,
                "install_url": installed.get("install_url") or "",
                "uninstalled_before": removed,
                "verified": verified,
                "execution": _receipt(),
            }
        finally:
            app_package.release(temp)

    # ------------------------------------------------------------ WDA 页面动作

    def _run(self, event, ctx) -> dict[str, Any]:
        cap = str(event.capability_id or "")
        params = dict(event.params or {})
        udid = str(ctx.device.udid or ctx.device.sn or "")
        client = client_for(udid)
        bundle = str(params.get("bundle") or params.get("bundle_id") or params.get("package")
                     or (ctx.device.extra or {}).get("bundle_id") or (ctx.device.extra or {}).get("target_package") or "")
        if cap == "launch_app":
            if not bundle:
                raise WdaError("启动应用缺少 bundle id")
            client.activate(bundle)
            return {"summary": f"已启动 {bundle}", "bundle_id": bundle}
        if cap in {"close_app", "kill_app"}:
            if not bundle:
                raise WdaError("关闭应用缺少 bundle id")
            client.terminate(bundle)
            return {"summary": f"已关闭 {bundle}", "bundle_id": bundle}
        if cap == "exec_script":
            from mino_scout.platform.ios.script import run_command

            result = run_command(str(params.get("script") or ""), udid=udid, timeout_ms=_timeout_ms(params))
            if result["status"] != "success":
                raise WdaError(result.get("stderr") or result.get("message") or "脚本失败")
            return {"summary": result.get("message") or "已执行脚本", "stdout": result.get("stdout") or ""}
        if cap == "tap_xy":
            client.tap_xy(int(params.get("x") or 0), int(params.get("y") or 0))
            return {"summary": "已点击坐标"}
        if cap == "tap_element":
            policy = str(params.get("point_policy") or "").strip().lower()
            target = _target_text(params)
            has_xy = params.get("x") is not None and params.get("y") is not None
            if target and policy != "coordinate":
                client.tap_target(target)
                return {"summary": "已点击", "tap_strategy": "anchor", "target": target}
            if has_xy:
                client.tap_xy(int(params["x"]), int(params["y"]))
                return {"summary": "已点击", "tap_strategy": "xy", "x": int(params["x"]), "y": int(params["y"])}
            raise WdaError("tap_element 缺少 target 或坐标")
        if cap == "long_press_element":
            x, y = _point(client, params)
            duration = max(0.2, int(params.get("duration_ms") or 600) / 1000)
            client.touch_and_hold(x, y, duration)
            return {"summary": "已长按"}
        if cap == "swipe_direction":
            x1, y1, x2, y2 = _swipe_points(client, params)
            client.drag(x1, y1, x2, y2, max(0.05, int(params.get("duration_ms") or 300) / 1000))
            return {"summary": "已滑动"}
        if cap == "swipe_element_to_element":
            if None in (params.get("from_x"), params.get("from_y"), params.get("to_x"), params.get("to_y")):
                raise WdaError("拖拽需要 from_x/from_y/to_x/to_y")
            client.drag(int(params["from_x"]), int(params["from_y"]), int(params["to_x"]), int(params["to_y"]),
                        max(0.05, int(params.get("duration_ms") or 400) / 1000))
            return {"summary": "已拖拽"}
        if cap == "input_text":
            client.keys(str(params.get("text") or ""))
            return {"summary": "已输入"}
        if cap == "press_key":
            button = {"home": "home", "volume_up": "volumeUp", "volumeup": "volumeUp",
                      "volume_down": "volumeDown", "volumedown": "volumeDown"}.get(str(params.get("key") or "").strip().lower())
            if not button:
                raise WdaError("iOS 只支持 home、volume_up、volume_down")
            if button == "home":
                client.press_button("home")
                return {"summary": "已回主屏"}
            client.press_button(button)
            return {"summary": f"已按 {button}"}
        if cap == "get_foreground_app":
            info = client.active_app()
            bundle_id = str(info.get("bundleId") or "")
            expect = bundle or str(params.get("package") or "")
            return {"summary": bundle_id, "bundle_id": bundle_id, "name": info.get("name") or "",
                    "match": (bundle_id == expect) if expect else None}
        if cap == "set_clipboard":
            text = str(params.get("text") or "")
            if not text:
                raise WdaError("set_clipboard 缺 text")
            client.set_clipboard(text)
            return {"summary": f"剪贴板写入 {len(text)} 字"}
        if cap == "screenshot":
            data = client.screenshot()
            return {"summary": "已截图", "image_base64": data, "image_mime": "image/png"}
        if cap == "hierarchy":
            return {"summary": "已读取界面结构", "hierarchy_text": client.source()}
        raise WdaError(f"iOS 通道未实现 {cap}")


def _receipt(source: str = "ios.devicectl.v1") -> dict[str, Any]:
    return {"execution_started": "yes", "phase": "completed", "source": source}


def _wants_reinstall(params: dict[str, Any]) -> bool:
    for key in ("reinstall", "uninstall_before", "uninstall_first", "clean_install"):
        value = params.get(key)
        if isinstance(value, bool) and value:
            return True
        if str(value or "").strip().lower() in _REINSTALL_WORDS:
            return True
    return False


def _timeout_sec(params: dict[str, Any]) -> float:
    try:
        value = float(params.get("install_timeout_sec") or 600.0)
    except (TypeError, ValueError):
        value = 600.0
    return max(30.0, min(value, 3600.0))


def _check_profile(package: dict[str, Any], *, udid: str, params: dict[str, Any]) -> None:
    """能提前判定的装包失败，在启动 devicectl 之前说清楚。

    只处理两类：描述文件带 UDID 白名单而本机不在其中；描述文件已过期。
    签名/授权类问题必须由 devicectl 判定，不在这里猜。
    """
    signing = package.get("signing") if isinstance(package.get("signing"), dict) else {}
    if not signing or signing.get("reason"):
        return
    if params.get("skip_profile_check") in (True, "1", "true", "yes"):
        return
    devices = signing.get("devices") if isinstance(signing.get("devices"), list) else []
    if devices and not signing.get("provisions_all_devices"):
        wanted = {str(udid or "")}
        if str(params.get("device_udid") or ""):
            wanted.add(str(params["device_udid"]))
        if not (wanted & {str(item) for item in devices}):
            raise WdaError(
                f"这份描述文件只允许 {signing.get('device_count')} 台设备，{udid} 不在白名单里；"
                "换一个已登记的包，或把该设备加进描述文件后重新签名"
            )
    expires = str(signing.get("expires") or "")
    if expires:
        from datetime import datetime, timezone

        try:
            deadline = datetime.fromisoformat(expires)
        except ValueError:
            deadline = None
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline is not None and deadline < datetime.now(timezone.utc):
            raise WdaError(f"描述文件已于 {expires[:10]} 过期，需要重新签名后再装")


def _verify_installed(udid: str, bundle_id: str, package: dict[str, Any]) -> dict[str, Any]:
    """装完回读：确认 bundle 在设备上，并比对包内版本。读不到不算安装失败。"""
    if not bundle_id:
        return {"ok": False, "reason": "bundle_id_unknown"}
    try:
        rows = installed_apps(udid)
    except WdaError as exc:
        return {"ok": False, "reason": f"read_failed: {exc}"}
    for row in rows:
        if str(row.get("bundleIdentifier") or "") != bundle_id:
            continue
        installed_version = str(row.get("version") or row.get("shortVersion") or "")
        expected = str(package.get("version") or "")
        return {
            "ok": True,
            "bundle_id": bundle_id,
            "version": installed_version,
            "build": str(row.get("bundleVersion") or ""),
            "expected_version": expected,
            "version_matches": (installed_version == expected) if expected else None,
        }
    return {"ok": False, "reason": "not_found_after_install"}


def _timeout_ms(params: dict) -> int:
    try:
        value = int(params.get("timeout_ms") or 30000)
    except (TypeError, ValueError):
        value = 30000
    return max(1000, min(value, 60000))


def _target_text(params: dict) -> str:
    raw_target = params.get("target")
    if isinstance(raw_target, dict):
        return str(raw_target.get("name") or raw_target.get("text") or raw_target.get("label") or raw_target.get("content_desc") or "").strip()
    return str(raw_target or params.get("name") or "")


def _point(client, params: dict) -> tuple[int, int]:
    x, y = params.get("x"), params.get("y")
    if x is not None and y is not None:
        return int(x), int(y)
    target = _target_text(params)
    if not target:
        raise WdaError("缺少坐标或目标")
    return client.element_center(target)


def _swipe_points(client, params: dict) -> tuple[int, int, int, int]:
    width, height = client.window_size()
    direction = str(params.get("direction") or "up").lower()
    cx, cy = width // 2, height // 2
    if direction == "up":
        return cx, int(height * 0.75), cx, int(height * 0.25)
    if direction == "down":
        return cx, int(height * 0.25), cx, int(height * 0.75)
    if direction == "left":
        return int(width * 0.8), cy, int(width * 0.2), cy
    if direction == "right":
        return int(width * 0.2), cy, int(width * 0.8), cy
    raise WdaError("direction 只支持 up / down / left / right")
