"""Windows 渠道动作。启动标识是 EXE 路径或 AUMID，修饰键 primary 为 ctrl，meta 为 win。"""
from __future__ import annotations

import json
import ntpath
import subprocess
from pathlib import Path

from mino_scout.executors.desktop import DesktopUnavailable, get_desktop_runtime, tool_payload
from mino_scout.schemas import CapturedScreen


def application_target(package: str) -> dict[str, str]:
    if not package or any(ord(char) < 32 for char in package):
        raise DesktopUnavailable("请配置被测 PC 应用标识")
    drive, tail = ntpath.splitdrive(package)
    if drive and tail.startswith(("\\", "/")) and package.lower().endswith(".exe"):
        return {"path": ntpath.normpath(package)}
    if package.count("!") == 1 and all(part and all(char.isalnum() or char in "._-" for char in part) for part in package.split("!")):
        return {"aumid": package}
    raise DesktopUnavailable("Windows 应用必须配置完整 EXE 路径或 AUMID，不能使用模糊应用名")


def capture(device, *, compress_ratio: float = 1.0, **_ignored) -> CapturedScreen:
    try:
        ratio = float(compress_ratio)
        if not 1.0 <= ratio <= 10.0:
            raise ValueError("PC 压缩比必须在 1 到 10 之间")
        data = get_desktop_runtime().observe(device.extra, str(device.extra.get("run_id") or ""), ratio)
        return CapturedScreen(
            ok=True, source="win", image_base64=data["image_base64"], image_mime=data["image_mime"],
            width=data["width"], height=data["height"],
            remote_detail={
                "capture_id": data["capture_id"], "session_generation": data["session_generation"],
                "capture_width": data["width"], "capture_height": data["height"], "compress_ratio": ratio,
                **{key: data[key] for key in ("capture_scope", "target", "pid", "window_id", "snapshot_id",
                                              "window_bounds", "screenshot_scale", "native_width", "native_height")},
            },
        )
    except Exception as exc:
        return CapturedScreen(ok=False, source="win", error=str(exc))


CAPS = (
    "screenshot", "hierarchy", "launch_app", "close_app", "kill_app",
    "tap_element", "tap_xy", "input_text", "press_key",
    "swipe_direction", "swipe_element_to_element", "long_press_element",
    "set_clipboard", "get_foreground_app", "install", "exec_script",
)


def focus_chord(mode: str) -> tuple[str, list[str]]:
    if mode == "replace":
        return "A", ["ctrl"]
    return "end", ["ctrl"]


def key_input(params: dict) -> tuple[str, list[str]]:
    return _key_input(params, {"ctrl", "alt", "shift", "win"}, {"meta": "win", "super": "win", "primary": "ctrl", "command": "win", "cmd": "win"})


def run_command(script: str, *, timeout_ms: int = 30000) -> dict:
    import subprocess

    body = str(script or "").strip()
    if not body:
        raise DesktopUnavailable("exec_script 缺 script")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", body], capture_output=True, text=True,
            timeout=max(1, min(timeout_ms, 60000)) / 1000,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopUnavailable(f"脚本没有跑完：{exc}") from exc
    if proc.returncode != 0:
        raise DesktopUnavailable((proc.stderr or proc.stdout or "脚本失败")[:300])
    return {"stdout": (proc.stdout or "")[:2000]}


def write_clipboard(runtime, sdk, driver, run_id, text: str):
    runtime._await(driver.clipboard_write(sdk.ClipboardWriteInput(
        text=text, image_path=None, file_path=None, session=run_id,
    )))


def gesture(runtime, sdk, driver, capability, params, run_id, obs, bound):
    params = _resolve_node(runtime, sdk, driver, params, run_id, obs, bound)
    duration = int(params.get("duration_ms") or (600 if capability == "long_press_element" else 300))
    if capability == "long_press_element":
        x1, y1 = _milli_pair(params.get("x"), params.get("y"), obs)
        x2, y2 = x1, y1
    elif capability == "swipe_element_to_element":
        if None in (params.get("from_x"), params.get("from_y"), params.get("to_x"), params.get("to_y")):
            raise DesktopUnavailable("拖拽需要 from_x/from_y/to_x/to_y")
        x1, y1 = _milli_pair(params.get("from_x"), params.get("from_y"), obs)
        x2, y2 = _milli_pair(params.get("to_x"), params.get("to_y"), obs)
    else:
        direction = str(params.get("direction") or "up").lower()
        if None not in (params.get("from_x"), params.get("from_y"), params.get("to_x"), params.get("to_y")):
            x1, y1 = _milli_pair(params.get("from_x"), params.get("from_y"), obs)
            x2, y2 = _milli_pair(params.get("to_x"), params.get("to_y"), obs)
        else:
            points = {
                "up": (500, 750, 500, 250),
                "down": (500, 250, 500, 750),
                "left": (750, 500, 250, 500),
                "right": (250, 500, 750, 500),
            }.get(direction)
            if points is None:
                raise DesktopUnavailable(f"不支持的滑动方向 {direction}")
            x1, y1 = _milli_pair(points[0], points[1], obs)
            x2, y2 = _milli_pair(points[2], points[3], obs)
    target = sdk.ActionTarget.WINDOW(pid=bound["pid"], window_id=bound["window_id"])
    return runtime._await(driver.drag(sdk.DragInput(
        from_x=x1, from_y=y1, to_x=x2, to_y=y2, target=target, scope=None, session=run_id,
        duration_ms=max(1, duration), steps=12, button=None, modifier=None,
    )))


def tap(runtime, sdk, driver, params, run_id, obs, bound):
    params = _resolve_node(runtime, sdk, driver, params, run_id, obs, bound)
    x, y = float(params["x"]), float(params["y"])
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        raise DesktopUnavailable("桌面坐标必须在 0..1000 范围内")
    target = sdk.ActionTarget.WINDOW(pid=bound["pid"], window_id=bound["window_id"])
    return runtime._await(driver.click(sdk.ClickInput(
        target=target,
        position=sdk.ClickPosition.CAPTURED_COORDINATES(x * (obs["width"] - 1) / 1000, y * (obs["height"] - 1) / 1000, obs["capture_id"]),
        delivery_mode=sdk.InputDeliveryMode.FOREGROUND, session=run_id, button=None, count=1,
    )))


def launch(runtime, sdk, driver, params, hint, run_id, obs):
    return runtime._launch_app(sdk, driver, params, hint, run_id, obs, "windows")


def install(path: str, *, params: dict | None = None) -> str:
    """装 Windows 包。

    - `.msi`：`msiexec /i … /qn`（静默），可用 `params.install_args` 追加参数。
    - `.exe`：默认**静默尝试**（`/S`、`/silent`、`/verysilent` 逐个试），成功后返回；
      都不认时用 `params.install_args=["/quiet"]` 这类显式参数，或 `interactive=true`
      退回前台执行（此时 Scout 只能确认“进程已启动”，不能确认装成功）。
    - `.appx`/`.msix`：`Add-AppxPackage`。
    """
    extra = dict(params or {})
    target = str(path or "").strip()
    if not target:
        raise DesktopUnavailable("install 缺 path")
    if not Path(target).exists():
        raise DesktopUnavailable(f"安装包不存在：{target}")
    lowered = target.lower()
    if lowered.endswith(".msi"):
        return _install_msi(target, extra)
    if lowered.endswith((".appx", ".msix", ".appxbundle", ".msixbundle")):
        return _install_appx(target, extra)
    if lowered.endswith(".exe"):
        return _install_exe(target, extra)
    raise DesktopUnavailable("Windows install 需要 .exe、.msi 或 .appx/.msix")


_SILENT_FLAGS = (["/S"], ["/s"], ["/silent"], ["/verysilent"], ["/quiet"], ["/qn"])


def _install_msi(target: str, extra: dict) -> str:
    args = ["msiexec", "/i", target, "/qn", "/norestart", *_extra_args(extra)]
    proc = _run_install(args, extra)
    if proc.returncode not in (0, 3010):
        raise DesktopUnavailable((proc.stderr or proc.stdout or f"msiexec 退出 {proc.returncode}")[:300])
    note = "（需要重启才生效）" if proc.returncode == 3010 else ""
    return f"已安装 {Path(target).name}{note}"


def _install_appx(target: str, extra: dict) -> str:
    script = f"Add-AppxPackage -Path '{target.replace(chr(39), chr(39) * 2)}'"
    proc = _run_install(["powershell", "-NoProfile", "-Command", script], extra)
    if proc.returncode != 0:
        raise DesktopUnavailable((proc.stderr or proc.stdout or "Add-AppxPackage 失败")[:300])
    return f"已安装 {Path(target).name}"


def _install_exe(target: str, extra: dict) -> str:
    """先试静默；全部非零时按用户是否声明静默决定回退。"""
    explicit = _extra_args(extra)
    interactive = bool(extra.get("interactive"))
    attempts = [explicit] if explicit else ([] if interactive else list(_SILENT_FLAGS))
    failures: list[str] = []
    for flags in attempts:
        proc = _run_install([target, *flags], extra)
        if proc.returncode == 0:
            return f"已安装 {Path(target).name}" + (f"（参数 {' '.join(flags)}）" if flags else "")
        failures.append(f"{' '.join(flags) or '<无参数>'} → rc={proc.returncode}")
    if interactive or not explicit:
        proc = _run_install([target, *explicit], extra, wait=False)
        return (f"{Path(target).name} 已启动交互式安装（pid={proc.pid}）；"
                "安装结果需要看界面确认" + (f"；静默尝试失败：{'；'.join(failures[:3])}" if failures else ""))
    raise DesktopUnavailable("exe 静默安装失败：" + "；".join(failures[:3]))


def _extra_args(extra: dict) -> list[str]:
    raw = extra.get("install_args")
    if isinstance(raw, str):
        import shlex as _shlex

        return _shlex.split(raw, posix=False)
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    return []


def _run_install(command: list[str], extra: dict, *, wait: bool = True):
    timeout = 1800.0
    try:
        timeout = max(60.0, min(float(extra.get("install_timeout_sec") or 1800.0), 7200.0))
    except (TypeError, ValueError):
        pass
    try:
        if not wait:
            return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    shell=False)
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DesktopUnavailable(f"安装器超过 {timeout:.0f}s 未返回；结果未知") from exc


def uninstall(params: dict) -> dict:
    """Windows 卸载：`.msi` 走 `msiexec /x`，产品标识走注册表查询后的同样命令。"""
    extra = dict(params or {})
    target = str(extra.get("path") or extra.get("package") or extra.get("product_code") or "").strip()
    if not target:
        raise DesktopUnavailable("uninstall_app 缺 params.path / product_code / package")
    lowered = target.lower()
    if lowered.endswith(".msi"):
        args = ["msiexec", "/x", target, "/qn", "/norestart", *_extra_args(extra)]
        summary = f"已卸载 {Path(target).name}"
    else:
        code = target if target.startswith("{") else f"{{{target}}}"
        args = ["msiexec", "/x", code, "/qn", "/norestart", *_extra_args(extra)]
        summary = f"已卸载 {code}"
    proc = _run_install(args, extra)
    if proc.returncode not in (0, 3010):
        raise DesktopUnavailable((proc.stderr or proc.stdout or f"msiexec /x 退出 {proc.returncode}")[:300])
    return {"summary": summary, "path": target,
            "execution": {"execution_started": "yes", "phase": "completed", "source": "desktop.install.v1"}}


def hierarchy(runtime, hint, run_id) -> dict:
    def read():
        runtime._check(hint, run_id)
        sdk, driver = runtime._session(run_id or "hierarchy")
        target = runtime._resolve_target(sdk, driver, hint, run_id)
        result = runtime._await(driver.call_tool("get_window_state", json.dumps({
            "pid": target["pid"], "window_id": target["window_id"], "session": run_id or "hierarchy",
            "include_accessibility_tree": True, "include_screenshot": False, "display_only": False,
        })))
        if result.is_error:
            raise DesktopUnavailable("Windows 没有读到界面树")
        return tool_payload(result)

    data = runtime._submit(read, timeout=15)
    tree = data.get("accessibility_tree") or data.get("uia_tree") or data.get("tree") or ""
    text = tree if isinstance(tree, str) else json.dumps(tree, ensure_ascii=False)[:4000]
    return {"source": "win", "hierarchy_text": text, "summary": "已读取 Windows 界面结构", "extra": {"hierarchy_text": text, "text": text[:4000]}}


def _milli_pair(x, y, obs) -> tuple[float, float]:
    px, py = float(x), float(y)
    if not 0 <= px <= 1000 or not 0 <= py <= 1000:
        raise DesktopUnavailable("桌面坐标必须在 0..1000 范围内")
    return px * (obs["width"] - 1) / 1000, py * (obs["height"] - 1) / 1000


def _resolve_node(runtime, sdk, driver, params, run_id, obs, bound):
    params = dict(params or {})
    if str(params.get("point_policy") or "") != "node" or params.get("x") is not None:
        return params
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    label = str(target.get("text") or target.get("name") or target.get("label") or "").strip()
    if not label:
        raise DesktopUnavailable("Windows 节点点击缺少文案")
    result = runtime._await(driver.call_tool("get_window_state", json.dumps({
        "pid": bound["pid"], "window_id": bound["window_id"], "session": run_id or "hierarchy",
        "include_accessibility_tree": True, "include_screenshot": False, "display_only": False,
    })))
    if result.is_error:
        raise DesktopUnavailable("Windows 没有读到界面树")
    data = tool_payload(result)
    tree = data.get("accessibility_tree") or data.get("uia_tree") or data.get("tree") or {}
    if isinstance(tree, str):
        try:
            tree = json.loads(tree)
        except json.JSONDecodeError:
            tree = {}
    frames = []
    _collect_frames(tree, label, frames)
    if len(frames) != 1:
        raise DesktopUnavailable("Windows 界面树里没有唯一匹配的控件")
    frame = frames[0]
    width = float((obs.get("window_bounds") or {}).get("width") or obs["width"] or 1)
    height = float((obs.get("window_bounds") or {}).get("height") or obs["height"] or 1)
    cx = float(frame["x"]) + float(frame.get("width") or 0) / 2
    cy = float(frame["y"]) + float(frame.get("height") or 0) / 2
    origin_x = float((obs.get("window_bounds") or {}).get("x") or 0)
    origin_y = float((obs.get("window_bounds") or {}).get("y") or 0)
    if cx > width or cy > height:
        cx -= origin_x
        cy -= origin_y
    params["x"] = max(0, min(1000, cx / width * 1000))
    params["y"] = max(0, min(1000, cy / height * 1000))
    return params


def _collect_frames(node, label: str, frames: list):
    if isinstance(node, dict):
        texts = [str(node.get(key) or "") for key in ("title", "label", "name", "value", "description", "Name")]
        frame = node.get("frame") or node.get("bounds") or node.get("rect") or node.get("BoundingRectangle")
        if label in texts and isinstance(frame, dict) and "x" in frame and "y" in frame:
            frames.append(frame)
        for key in ("children", "nodes", "elements"):
            _collect_frames(node.get(key), label, frames)
    elif isinstance(node, list):
        for item in node:
            _collect_frames(item, label, frames)


def _key_input(params: dict, allowed: set[str], aliases: dict[str, str]) -> tuple[str, list[str]]:
    key = str(params.get("key") or "").strip()
    modifiers = params.get("modifiers") or []
    if not isinstance(modifiers, list) or not all(isinstance(item, str) for item in modifiers):
        raise DesktopUnavailable("按键 modifiers 必须是字符串列表")
    if "+" in key and key != "+":
        parts = key.split("+")
        modifiers = modifiers + parts[:-1]
        key = parts[-1]
    shared = {"control": "ctrl", "option": "alt"}
    shared.update(aliases)
    normalized = []
    for modifier in modifiers:
        value = shared.get(modifier.strip().lower(), modifier.strip().lower())
        if value not in allowed:
            raise DesktopUnavailable("当前系统不支持这项按键修饰符")
        if value not in normalized:
            normalized.append(value)
    if not key:
        raise DesktopUnavailable("请提供要按下的键")
    return key, normalized
