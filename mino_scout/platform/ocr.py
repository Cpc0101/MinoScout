"""Local OCR over the current app screenshot; explicit, unique spatial targeting."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from io import BytesIO, StringIO
from pathlib import Path

def _abort(message, code):
    from mino_scout.runtime.script_exec import ScriptAbort
    return ScriptAbort(message, reason_code=code, status="blocked")


def region_box(region):
    value = [0, 0, 1, 1] if region is None else region
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(
        type(v) not in {float, int} or not math.isfinite(v) for v in value
    ) or not (0 <= value[0] < value[2] <= 1 and 0 <= value[1] < value[3] <= 1):
        raise _abort("OCR region 必须为归一化 [left, top, right, bottom]", "ocr_config_invalid")
    return list(value)


def validate_selector(selector):
    if set(selector) - {"kind", "value", "confidence", "region", "language", "relative", "timeout_ms"}:
        raise _abort("OCR 选择器含不支持的字段", "ocr_config_invalid")
    region_box(selector.get("region"))
    confidence, timeout = selector.get("confidence", .8), selector.get("timeout_ms", 10000)
    if not isinstance(selector.get("value"), str) or not selector["value"].strip() or type(confidence) not in {int, float} or not .5 <= confidence <= 1 or type(timeout) is not int or not 1 <= timeout <= 60000:
        raise _abort("OCR 选择器的文字、置信度或超时无效", "ocr_config_invalid")
    if "language" in selector and not isinstance(selector["language"], str):
        raise _abort("OCR language 必须为文字", "ocr_config_invalid")
    relative = selector.get("relative")
    if relative is not None:
        if not isinstance(relative, dict) or set(relative) - {"text", "relation", "max_distance", "alignment"} or not isinstance(relative.get("text"), str) or not relative["text"].strip() or relative.get("relation") not in {"left", "right", "above", "below"}:
            raise _abort("OCR 相对目标配置无效", "ocr_config_invalid")
        if any(type(relative.get(k, default)) not in {int, float} or not 0 < relative.get(k, default) <= 1 for k, default in (("max_distance", .3), ("alignment", .08))):
            raise _abort("OCR 相对距离范围无效", "ocr_config_invalid")


def _tesseract(path, language, timeout):
    executable = shutil.which("tesseract")
    if not executable:
        raise _abort("执行 Scout 未安装 Tesseract 与对应语言包", "ocr_unavailable")
    result = subprocess.run([executable, str(path), "stdout", "-l", language or "eng", "--psm", "11", "tsv"],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise _abort("本地 OCR 无法识别，请核对 Tesseract 语言包", "ocr_failed")
    nodes = []
    # Group words into lines so an exact button label can contain spaces.
    lines = {}
    for row in csv.DictReader(StringIO(result.stdout), delimiter="\t"):
        if row.get("level") != "5" or not str(row.get("text") or "").strip():
            continue
        key = tuple(row.get(k) for k in ("page_num", "block_num", "par_num", "line_num"))
        x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
        lines.setdefault(key, []).append({"text": row["text"].strip(), "bounds": [x, y, x + w, y + h],
                                         "confidence": max(0, min(1, float(row["conf"]) / 100))})
    for words in lines.values():
        nodes.append({"text": " ".join(w["text"] for w in words), "confidence": min(w["confidence"] for w in words),
                      "bounds": [min(w["bounds"][0] for w in words), min(w["bounds"][1] for w in words),
                                 max(w["bounds"][2] for w in words), max(w["bounds"][3] for w in words)]})
    return nodes, "tesseract"


def _vision(path, language, timeout):
    from mino_scout.config import config_dir
    started = time.monotonic()
    source = Path(__file__).with_name("ocr_vision.swift")
    fingerprint = hashlib.sha256(source.read_bytes() + f"{platform.machine()}:{platform.mac_ver()[0]}".encode()).hexdigest()[:24]
    cache_dir = config_dir() / "cache" / "ocr"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = cache_dir / ("vision-" + fingerprint)
    if not binary.is_file():
        # Case attempts use independent processes. Reuse a compiled helper across
        # attempts; publish atomically so simultaneous first runs see no partial binary.
        with tempfile.TemporaryDirectory(prefix="compile_", dir=cache_dir) as folder:
            compiled = Path(folder) / "recognize"
            subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(Path(folder) / "modules"),
                            str(source), "-o", str(compiled)], check=True, capture_output=True, timeout=timeout)
            compiled.chmod(0o700)
            compiled.replace(binary)
    remain = timeout - (time.monotonic() - started)
    if remain <= 0:
        raise _abort("OCR 初始化超时", "ocr_timeout")
    result = subprocess.run([str(binary), str(path), language or "zh-Hans,en-US"],
                            capture_output=True, text=True, timeout=remain)
    if result.returncode:
        raise _abort("系统 OCR 未返回结果：" + result.stderr[:240], "ocr_failed")
    return json.loads(result.stdout), "vision"


_BACKENDS = {"darwin": _vision}


def recognize(image, *, language="", timeout_ms=10000):
    if type(timeout_ms) is not int or not 0 < timeout_ms <= 60000:
        raise _abort("OCR timeout_ms 必须为 1..60000", "ocr_config_invalid")
    with tempfile.TemporaryDirectory(prefix="mino_ocr_") as folder:
        path = Path(folder) / "screen.png"
        image.save(path, format="PNG")
        try:
            nodes, engine = _BACKENDS.get(sys.platform, _tesseract)(path, language, timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            raise _abort("本地 OCR 超时", "ocr_timeout") from None
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError):
            raise _abort("本地 OCR 不可用或返回格式无效", "ocr_failed") from None
    for row in nodes:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str) or type(row.get("confidence")) not in {int, float} or not 0 <= row["confidence"] <= 1:
            raise _abort("OCR 结果格式无效", "ocr_failed")
        bounds = row.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4 or not all(type(v) in {int, float} and math.isfinite(v) for v in bounds):
            raise _abort("OCR 边框格式无效", "ocr_failed")
        if not (0 <= bounds[0] < bounds[2] <= image.width and 0 <= bounds[1] < bounds[3] <= image.height):
            raise _abort("OCR 边框超出截图", "ocr_failed")
    return nodes, engine


def observe(app, *, region=None, language="", timeout_ms=10000):
    from PIL import Image
    from mino_scout.runtime.script_exec import ActionResult
    box = region_box(region)
    started = time.monotonic()
    app.screenshot(full_page=False, screenshot_params={"compress_ratio": 1.0})
    app._guard()
    image = Image.open(BytesIO(base64.b64decode(app._shot.image_base64))).convert("RGB")
    width, height = image.size
    pixels = [round(box[0] * width), round(box[1] * height), round(box[2] * width), round(box[3] * height)]
    if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
        raise _abort("OCR 区域为空", "ocr_config_invalid")
    if app.case.deadline:
        timeout_ms = min(timeout_ms, max(1, int((app.case.deadline - time.monotonic()) * 1000)))
    try:
        nodes, engine = recognize(image.crop(pixels), language=language, timeout_ms=timeout_ms)
    except Exception as exc:
        app._report("ocr", ActionResult(ok=False, status="blocked", summary=str(exc), data={
            "reason_code": getattr(exc, "reason_code", "ocr_failed")}), "local_ocr", int((time.monotonic() - started) * 1000))
        raise
    app._guard()
    for node in nodes:
        node["bounds"] = [node["bounds"][0] + pixels[0], node["bounds"][1] + pixels[1],
                          node["bounds"][2] + pixels[0], node["bounds"][3] + pixels[1]]
    result = ActionResult(ok=True, status="pass", summary="已识别当前区域文字", data={
        "nodes": nodes, "engine": engine, "region": box, "width": width, "height": height,
        "observation_id": app._observation_id, "artifact_id": app._artifact_id})
    app._report("ocr", result, "local_ocr", int((time.monotonic() - started) * 1000),
                extra={"observation_id": app._observation_id, "artifact_id": app._artifact_id})
    return result


def select_target(nodes, selector, width, height):
    text = selector.get("value")
    confidence = selector.get("confidence", .8)
    if not isinstance(text, str) or not text.strip() or type(confidence) not in {int, float} or not .5 <= confidence <= 1:
        raise _abort("OCR 定位需要文字和有效置信度阈值", "ocr_config_invalid")
    anchors = [n for n in nodes if n["text"] == text]
    if len(anchors) != 1:
        raise _abort("OCR 锚点未找到" if not anchors else "OCR 锚点不唯一", "locator_not_found" if not anchors else "locator_ambiguous")
    anchor = anchors[0]
    if anchor["confidence"] < confidence:
        raise _abort("OCR 锚点置信度不足", "ocr_low_confidence")
    relative = selector.get("relative")
    if relative is None:
        return anchor
    if not isinstance(relative, dict) or not isinstance(relative.get("text"), str):
        raise _abort("OCR relative 需要目标文字", "ocr_config_invalid")
    relation = relative.get("relation")
    distance, align = relative.get("max_distance", .3), relative.get("alignment", .08)
    if relation not in {"left", "right", "above", "below"} or any(type(v) not in {float, int} or not 0 < v <= 1 for v in (distance, align)):
        raise _abort("OCR 相对关系无效", "ocr_config_invalid")
    def center(n):
        b = n["bounds"]
        return (b[0] + b[2]) / 2 / width, (b[1] + b[3]) / 2 / height
    ax, ay = center(anchor)
    matches = []
    for node in nodes:
        if node["text"] != relative["text"]:
            continue
        x, y = center(node)
        along, across = (x - ax, abs(y - ay)) if relation in {"left", "right"} else (y - ay, abs(x - ax))
        if relation in {"left", "above"}:
            along = -along
        if 0 < along <= distance and across <= align:
            matches.append(node)
    if len(matches) != 1:
        raise _abort("OCR 相对目标未找到或不唯一", "locator_ambiguous" if matches else "locator_not_found")
    if matches[0]["confidence"] < confidence:
        raise _abort("OCR 目标置信度不足", "ocr_low_confidence")
    return matches[0]


def locate(app, selector):
    from PIL import Image
    from mino_scout.runtime.script_exec import ActionResult
    validate_selector(selector)
    result = observe(app, region=selector.get("region"), language=str(selector.get("language") or ""),
                     timeout_ms=selector.get("timeout_ms", 10000))
    width, height = result.data["width"], result.data["height"]
    target = select_target(result.data["nodes"], selector, width, height)
    b = target["bounds"]
    original = Image.open(BytesIO(base64.b64decode(app._shot.image_base64))).convert("RGB")
    original_ref = app._observation_id
    # OCR may take seconds. Recheck the target pixels before entering a write API.
    app.screenshot(full_page=False, screenshot_params={"compress_ratio": 1.0})
    current = Image.open(BytesIO(base64.b64decode(app._shot.image_base64))).convert("RGB")
    crop = [max(0, int(b[0]) - 4), max(0, int(b[1]) - 4), min(width, math.ceil(b[2]) + 4), min(height, math.ceil(b[3]) + 4)]
    if current.size != original.size or original.crop(crop).tobytes() != current.crop(crop).tobytes():
        raise _abort("OCR 定位后目标画面已变化，需重新观察", "observation_stale")
    x, y = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    params = {"point_policy": "coordinate", "ocr_observation_id": original_ref}
    if app._platform in {"mac", "win"}:
        params.update(x=x / max(1, width - 1) * 1000, y=y / max(1, height - 1) * 1000)
        params["capture_id"] = (app._shot.remote_detail or {}).get("capture_id")
        if not params["capture_id"]:
            raise _abort("桌面截图缺少有效捕获引用", "observation_insufficient")
    elif app._platform == "ios":
        from mino_scout.platform.ios.wda_session import client_for
        device = app._device_ref()
        input_width, input_height = client_for(device.udid or device.sn).window_size()
        params.update(x=round(x / width * input_width), y=round(y / height * input_height))
    elif app._platform == "web":
        from mino_scout.platform.web.playwright_hub import get_hub
        page = get_hub().current_page(
            app._sn, run_id=app.run_id, actor=app._actor,
            context_id=app._context_id, page_id=app._page_id,
        )
        viewport = page.viewport_size if page else None
        if not viewport:
            raise _abort("Web 视口尺寸不可用", "observation_insufficient")
        params.update(x=round(x / width * viewport["width"]), y=round(y / height * viewport["height"]),
                      coordinate_space="viewport_pixels")
    else:
        params.update(x=round(x), y=round(y), coordinate_space="screenshot_pixels")
    app._guard()
    app._report("ocr_locate", ActionResult(
        ok=True, status="pass", summary="已唯一定位 OCR 目标", data={"target": target, "point": params}), "local_ocr", 0,
        extra={"observation_id": app._observation_id, "artifact_id": app._artifact_id})
    return params
