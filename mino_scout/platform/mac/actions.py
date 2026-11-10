"""Mac 渠道动作。启动标识是 bundle id，修饰键 primary / meta 为 command。"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from mino_scout.executors.desktop import DesktopUnavailable, get_desktop_runtime, tool_payload
from mino_scout.schemas import CapturedScreen


def application_target(package: str) -> dict[str, str]:
    if not package or any(ord(char) < 32 for char in package):
        raise DesktopUnavailable("请配置被测 PC 应用标识")
    if "." not in package or not all(char.isalnum() or char in "._-" for char in package):
        raise DesktopUnavailable("macOS 应用必须配置 bundle ID")
    return {"bundle_id": package}


def capture(device, *, compress_ratio: float = 1.0, **_ignored) -> CapturedScreen:
    try:
        ratio = float(compress_ratio)
        if not 1.0 <= ratio <= 10.0:
            raise ValueError("PC 压缩比必须在 1 到 10 之间")
        data = get_desktop_runtime().observe(device.extra, str(device.extra.get("run_id") or ""), ratio)
        return CapturedScreen(
            ok=True, source="mac", image_base64=data["image_base64"], image_mime=data["image_mime"],
            width=data["width"], height=data["height"],
            remote_detail={
                "capture_id": data["capture_id"], "session_generation": data["session_generation"],
                "capture_width": data["width"], "capture_height": data["height"], "compress_ratio": ratio,
                **{key: data[key] for key in ("capture_scope", "target", "pid", "window_id", "snapshot_id",
                                              "window_bounds", "screenshot_scale", "native_width", "native_height")},
            },
        )
    except Exception as exc:
        return CapturedScreen(ok=False, source="mac", error=str(exc))


CAPS = (
    "screenshot", "hierarchy", "launch_app", "close_app", "kill_app",
    "tap_element", "tap_xy", "input_text", "press_key",
    "swipe_direction", "swipe_element_to_element", "long_press_element",
    "set_clipboard", "get_foreground_app", "install", "exec_script",
)


def focus_chord(mode: str) -> tuple[str, list[str]]:
    if mode == "replace":
        return "A", ["cmd"]
    return "down", ["cmd"]


def key_input(params: dict) -> tuple[str, list[str]]:
    return _key_input(params, {"ctrl", "alt", "shift", "command"}, {"meta": "command", "super": "command", "primary": "command", "cmd": "command"})


def run_command(script: str, *, timeout_ms: int = 30000) -> dict:
    import subprocess

    body = str(script or "").strip()
    if not body:
        raise DesktopUnavailable("exec_script 缺 script")
    try:
        proc = subprocess.run(
            ["/bin/zsh", "-c", body], capture_output=True, text=True,
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
    return runtime._launch_app(sdk, driver, params, hint, run_id, obs, "macos")


def install(path: str, *, params: dict | None = None) -> str:
    """装 macOS 包。路径必须是本机已存在的绝对路径。

    - `.pkg`：`installer -pkg … -target /`，可用 `target` 覆盖安装目标卷。
    - `.dmg`：`hdiutil attach` 后把镜像里的 `.app` 复制到 `/Applications`（或
      `params.destination`），最后 `hdiutil detach`；没有 `.app` 时退回打开镜像。
    - `.app`：`ditto` 到 `/Applications`（或 `params.destination`），不是 `open`。
    """
    extra = dict(params or {})
    target = str(path or "").strip()
    if not target:
        raise DesktopUnavailable("install 缺 path")
    package = Path(target)
    if not package.exists():
        raise DesktopUnavailable(f"安装包不存在：{package}")
    lowered = target.lower()
    if package.suffix.lower() == ".pkg":
        return _install_pkg(package, extra)
    if package.suffix.lower() == ".dmg":
        return _install_dmg(package, extra)
    if package.suffix.lower() == ".app" or (package.is_dir() and package.name.endswith(".app")):
        destination = _destination(extra, package.name)
        from mino_scout.tools.app_package import copy_tree, PackageError

        try:
            copy_tree(package, destination)
        except PackageError as exc:
            raise DesktopUnavailable(str(exc)) from exc
        return f"已安装到 {destination}"
    raise DesktopUnavailable("macOS install 需要 .app、.pkg 或 .dmg")


def _install_pkg(package: Path, extra: dict) -> str:
    volume = str(extra.get("target") or extra.get("destination") or "/").strip() or "/"
    try:
        proc = subprocess.run(
            ["installer", "-pkg", str(package), "-target", volume],
            capture_output=True, text=True, timeout=_timeout(extra),
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopUnavailable(f"installer 超过 {_timeout(extra):.0f}s 未返回；结果未知") from exc
    if proc.returncode != 0:
        raise DesktopUnavailable((proc.stderr or proc.stdout or "pkg 安装失败")[:300])
    return f"已安装 {package.name} 到 {volume}"


def _install_dmg(package: Path, extra: dict) -> str:
    """挂载 → 找唯一 `.app` → 复制 → 卸载镜像。"""
    from mino_scout.tools.app_package import copy_tree, PackageError

    try:
        attach = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(package)],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopUnavailable("挂载 dmg 超时") from exc
    if attach.returncode != 0:
        raise DesktopUnavailable((attach.stderr or attach.stdout or "挂载 dmg 失败")[:300])
    mounts = _mount_points(attach.stdout)
    if not mounts:
        raise DesktopUnavailable("dmg 没有挂载出卷")
    mount = mounts[0]
    try:
        apps = sorted(path for path in mount.iterdir() if path.name.endswith(".app"))
        if not apps:
            raise DesktopUnavailable("镜像里没有 .app；请直接给 .app 或 .pkg 路径")
        if len(apps) > 1 and not extra.get("app_name"):
            names = "、".join(item.name for item in apps[:5])
            raise DesktopUnavailable(f"镜像里有多个 .app（{names}），用 app_name 指定一个")
        source = next(
            (item for item in apps if extra.get("app_name") and item.name == str(extra["app_name"]).strip()),
            apps[0],
        )
        destination = _destination(extra, source.name)
        try:
            copy_tree(source, destination)
        except PackageError as exc:
            raise DesktopUnavailable(str(exc)) from exc
        return f"已安装到 {destination}"
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-quiet"], capture_output=True, text=True, timeout=120)


def _mount_points(raw: str) -> list[Path]:
    import plistlib

    try:
        payload = plistlib.loads(str(raw or "").encode())
    except Exception as exc:
        raise DesktopUnavailable(f"读不到 dmg 挂载结果：{type(exc).__name__}") from exc
    out: list[Path] = []
    for entity in payload.get("system-entities") or []:
        mount = entity.get("mount-point") if isinstance(entity, dict) else ""
        if mount:
            out.append(Path(str(mount)))
    return out


def _destination(extra: dict, name: str) -> Path:
    root = str(extra.get("destination") or extra.get("install_dir") or "/Applications").strip() or "/Applications"
    return Path(root).expanduser() / name


def _timeout(extra: dict) -> float:
    try:
        value = float(extra.get("install_timeout_sec") or 1800.0)
    except (TypeError, ValueError):
        value = 1800.0
    return max(60.0, min(value, 7200.0))


def uninstall(params: dict) -> dict:
    """macOS 卸载：把 `.app` 移进废纸篓（`~/.Trash`），不驱动 Finder。

    走 Finder 需要「自动化」TCC 授权，在无人值守的节点上会静默卡住 120s。
    直接移进废纸篓等价、可预测，也不要求图形会话。
    """
    extra = dict(params or {})
    name = str(extra.get("app_name") or extra.get("path") or "").strip()
    if not name:
        raise DesktopUnavailable("uninstall_app 缺 params.path（.app 路径）或 app_name")
    target = Path(name)
    if not target.is_absolute():
        for root in ("/Applications", str(Path.home() / "Applications")):
            candidate = Path(root) / name
            if candidate.exists():
                target = candidate
                break
    if not target.exists():
        raise DesktopUnavailable(f"找不到要卸载的应用：{target}")
    trash = Path.home() / ".Trash"
    trash.mkdir(parents=True, exist_ok=True)
    destination = trash / target.name
    if destination.exists():
        destination = trash / f"{target.name}.{int(time.time())}"
    try:
        shutil.move(str(target), str(destination))
    except OSError as exc:
        raise DesktopUnavailable(f"移进废纸篓失败：{exc}") from exc
    return {"summary": f"已移除 {target.name}", "path": str(target), "trash": str(destination),
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
            raise DesktopUnavailable("Mac 没有读到界面树")
        return tool_payload(result)

    data = runtime._submit(read, timeout=15)
    tree = data.get("accessibility_tree") or data.get("ax_tree") or data.get("tree") or ""
    text = tree if isinstance(tree, str) else json.dumps(tree, ensure_ascii=False)[:4000]
    return {"source": "mac", "hierarchy_text": text, "summary": "已读取 Mac 界面结构", "extra": {"hierarchy_text": text, "text": text[:4000]}}


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
        raise DesktopUnavailable("Mac 节点点击缺少文案")
    result = runtime._await(driver.call_tool("get_window_state", json.dumps({
        "pid": bound["pid"], "window_id": bound["window_id"], "session": run_id or "hierarchy",
        "include_accessibility_tree": True, "include_screenshot": False, "display_only": False,
    })))
    if result.is_error:
        raise DesktopUnavailable("Mac 没有读到界面树")
    data = tool_payload(result)
    tree = data.get("accessibility_tree") or data.get("ax_tree") or data.get("tree") or {}
    if isinstance(tree, str):
        try:
            tree = json.loads(tree)
        except json.JSONDecodeError:
            tree = {}
    frames = []
    _collect_frames(tree, label, frames)
    if len(frames) != 1:
        raise DesktopUnavailable("Mac 界面树里没有唯一匹配的控件")
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
        texts = [str(node.get(key) or "") for key in ("title", "label", "name", "value", "description")]
        frame = node.get("frame") or node.get("bounds") or node.get("rect")
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
    shared = {"control": "ctrl", "option": "alt", "cmd": "command"}
    shared.update(aliases)
    normalized = []
    for modifier in modifiers:
        value = shared.get(modifier.strip().lower(), modifier.strip().lower())
        if value == "command":
            value = "cmd"
        if value not in allowed and value not in {"cmd"}:
            raise DesktopUnavailable("当前系统不支持这项按键修饰符")
        if value not in normalized:
            normalized.append(value)
    if not key:
        raise DesktopUnavailable("请提供要按下的键")
    return key, normalized
