"""最小可用的二进制 `AndroidManifest.xml` 解析。

只读取安装前做校验需要的四个字段：包名、versionName、versionCode、compileSdk。
不依赖 aapt / aapt2 / androguard —— Scout 的冻结包里没有 Android SDK，装个 APK
不该要求宿主机先装一套构建工具。

格式（AXML）：ResChunk_header(8) + 字符串池 + 资源表 + 若干 XML chunk。
属性值里 `TYPE_STRING` 直接取字符串池，`TYPE_INT_DEC` / `TYPE_INT_HEX` 取原始
32 位整数 —— 刚好覆盖 versionCode 与 compileSdkVersion。
"""
from __future__ import annotations

import struct
from typing import Any

_RES_STRING_POOL = 0x0001
_RES_XML = 0x0003
_RES_XML_START_NAMESPACE = 0x0100
_RES_XML_START_ELEMENT = 0x0102
_RES_XML_END_ELEMENT = 0x0103
_RES_XML_CDATA = 0x0104

_TYPE_REFERENCE = 0x01
_TYPE_STRING = 0x03
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11
_TYPE_INT_BOOLEAN = 0x12

_UTF8_FLAG = 0x100
_NO_INDEX = 0xFFFFFFFF

_ANDROID_NS = "http://schemas.android.com/apk/res/android"
_WANTED = {"package", "versionName", "versionCode", "compileSdkVersion", "compileSdk",
           "label", "minSdkVersion"}


class ManifestError(ValueError):
    pass


def manifest_identity(raw: bytes) -> dict[str, Any]:
    """→ {package, version_name, version_code, label, compile_sdk, min_sdk}。"""
    text = _parse(raw)
    manifest = text["manifest"]
    attrs = manifest["attrs"]
    package = str(attrs.get("package") or "")
    version_name = str(attrs.get("versionName") or "")
    version_code = attrs.get("versionCode")
    compile_sdk = attrs.get("compileSdkVersion") or attrs.get("compileSdk")
    label = str(attrs.get("label") or "")
    min_sdk = ""
    for node in text["elements"]:
        if node["name"] == "uses-sdk":
            value = node["attrs"].get("minSdkVersion")
            min_sdk = "" if value is None else str(value)
            break
    return {
        "package": package,
        "version_name": version_name,
        "version_code": "" if version_code is None else str(version_code),
        "label": label,
        "compile_sdk": "" if compile_sdk is None else str(compile_sdk),
        "min_sdk": min_sdk,
        "namespaces": text["namespaces"],
    }


def _parse(raw: bytes) -> dict[str, Any]:
    if len(raw) < 8:
        raise ManifestError("manifest 太短")
    chunk_type, header_size, total = struct.unpack_from("<HHI", raw, 0)
    if chunk_type != _RES_XML:
        raise ManifestError("不是 AXML（没有 RES_XML 头）")
    limit = min(total or len(raw), len(raw))
    strings: list[str] = []
    resources: list[int] = []
    elements: list[dict[str, Any]] = []
    namespaces: dict[str, str] = {}
    manifest_attrs: dict[str, Any] = {}
    offset = header_size
    while offset + 8 <= limit:
        ctype, chsize, csize = struct.unpack_from("<HHI", raw, offset)
        if csize < 8 or offset + csize > limit:
            break
        body = raw[offset + chsize: offset + csize]
        if ctype == _RES_STRING_POOL:
            strings = _string_pool(raw, offset, chsize, csize)
        elif ctype == 0x0180:  # RES_XML_RESOURCE_MAP
            resources = list(struct.unpack_from(f"<{len(body) // 4}I", body, 0))
        elif ctype == _RES_XML_START_NAMESPACE:
            prefix, uri = _namespace(body, strings)
            if prefix and uri:
                namespaces[prefix] = uri
        elif ctype == _RES_XML_START_ELEMENT:
            name, attrs = _start_element(body, strings, resources)
            elements.append({"name": name, "attrs": attrs})
            if name == "manifest":
                manifest_attrs = attrs
        offset += csize
    if not elements:
        raise ManifestError("AXML 里没有元素")
    return {"manifest": {"attrs": manifest_attrs}, "elements": elements, "namespaces": namespaces}


def _string_pool(raw: bytes, offset: int, header_size: int, size: int) -> list[str]:
    """字符串池：`stringCount/styleCount/flags/stringsStart` 在通用头之后，偏移从 chunk 起点算。

    不能像 XML 节点那样跳过 headerSize —— 那 20 字节正是这里要读的字段。
    """
    chunk = raw[offset: offset + size]
    if len(chunk) < 28:
        return []
    count, _styles, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", chunk, 8)
    del header_size
    if count <= 0 or strings_start <= 0 or strings_start >= len(chunk):
        return []
    if 28 + count * 4 > len(chunk):
        return []
    offsets = struct.unpack_from(f"<{count}I", chunk, 28)
    utf8 = bool(flags & _UTF8_FLAG)
    out: list[str] = []
    for item in offsets:
        start = strings_start + item
        if start >= len(chunk):
            out.append("")
            continue
        try:
            out.append(_read_string(chunk, start, utf8))
        except (struct.error, IndexError):
            out.append("")
    return out


def _read_string(body: bytes, start: int, utf8: bool) -> str:
    if utf8:
        # 先是 UTF-16 长度（1 或 2 字节），再是 UTF-8 字节长度（同规则）
        cursor = start
        cursor = _skip_len(body, cursor)
        length, cursor = _read_len(body, cursor)
        return body[cursor: cursor + length].decode("utf-8", "replace")
    length, cursor = _read_len16(body, start)
    return body[cursor: cursor + length * 2].decode("utf-16-le", "replace")


def _skip_len(body: bytes, cursor: int) -> int:
    return _read_len(body, cursor)[1]


def _read_len(body: bytes, cursor: int) -> tuple[int, int]:
    first = body[cursor]
    if first & 0x80:
        return ((first & 0x7F) << 8) | body[cursor + 1], cursor + 2
    return first, cursor + 1


def _read_len16(body: bytes, cursor: int) -> tuple[int, int]:
    first = struct.unpack_from("<H", body, cursor)[0]
    if first & 0x8000:
        second = struct.unpack_from("<H", body, cursor + 2)[0]
        return ((first & 0x7FFF) << 16) | second, cursor + 4
    return first, cursor + 2


def _namespace(body: bytes, strings: list[str]) -> tuple[str, str]:
    if len(body) < 8:
        return "", ""
    prefix_idx, uri_idx = struct.unpack_from("<II", body, 0)
    return _string(strings, prefix_idx), _string(strings, uri_idx)


def _string(strings: list[str], index: int) -> str:
    if index == _NO_INDEX or index < 0 or index >= len(strings):
        return ""
    return strings[index]


def _start_element(body: bytes, strings: list[str], resources: list[int]) -> tuple[str, dict[str, Any]]:
    """START_ELEMENT。`body` 已跳过 16 字节 chunk 头（8 通用 + line/comment）。

    相对 body：0 ns · 4 name · 8 attributeStart(2) · 10 attributeSize(2) ·
    12 attributeCount(2) · 14 idIndex(2) · 16 classIndex(2) · 18 styleIndex(2)，
    即 20 字节固定头，随后是 attributeCount 个 20 字节属性：
    0 ns · 4 name · 8 rawValue · 12 size(2) · 14 res0(1) · 15 dataType(1) · 16 data
    """
    del resources  # 属性名直接来自字符串池，不需要资源 id 反查
    if len(body) < 20:
        return "", {}
    name_idx = struct.unpack_from("<I", body, 4)[0]
    attr_start, attr_size, attr_count = struct.unpack_from("<HHH", body, 8)
    name = _string(strings, name_idx)
    attrs: dict[str, Any] = {}
    base = attr_start or 20
    size = attr_size or 20
    for index in range(attr_count):
        cursor = base + index * size
        if cursor + 20 > len(body):
            break
        name_ref = struct.unpack_from("<I", body, cursor + 4)[0]
        raw_value = struct.unpack_from("<I", body, cursor + 8)[0]
        value_type = body[cursor + 15]
        value_data = struct.unpack_from("<I", body, cursor + 16)[0]
        key = _string(strings, name_ref)
        if key not in _WANTED:
            continue
        value = _attr_value(value_type, value_data, strings, raw_value)
        if value is not None:
            attrs[key] = value
    return name, attrs


def _attr_value(value_type: int, data: int, strings: list[str], raw_idx: int) -> Any:
    if value_type == _TYPE_STRING:
        return _string(strings, raw_idx if raw_idx != _NO_INDEX else data)
    if value_type in (_TYPE_INT_DEC, _TYPE_INT_HEX):
        return struct.unpack("<i", struct.pack("<I", data))[0]
    if value_type == _TYPE_INT_BOOLEAN:
        return bool(data)
    if value_type == _TYPE_REFERENCE:
        # 引用型属性（如 label 指向 @string/xxx）只在字符串池里有名字时才有意义。
        return _string(strings, raw_idx) if raw_idx != _NO_INDEX else None
    return _string(strings, raw_idx) if raw_idx != _NO_INDEX else None
