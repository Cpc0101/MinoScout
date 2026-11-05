from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
import re
import zipfile
from xml.etree import ElementTree

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10000
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _column_index(cell):
    letters = re.match(r"[A-Z]+", cell or "")
    if letters is None:
        raise ValueError("source_cell_reference_invalid")
    index = 0
    for letter in letters.group():
        index = index * 26 + ord(letter) - ord("A") + 1
    if index > 256:
        raise ValueError("source_column_limit_exceeded")
    return index - 1


def xlsx_table(content, worksheet):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        if len({item.filename for item in members}) != len(members) or sum(item.file_size for item in members) > 40 * MAX_BYTES:
            raise ValueError("source_xlsx_size_invalid")
        if any(item.filename.endswith("vbaProject.bin") for item in members):
            raise ValueError("source_macros_unsupported")
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheets = book.findall("s:sheets/s:sheet", NS)
        selected = next((item for item in sheets if item.get("name") == worksheet), None)
        if selected is None:
            raise ValueError("source_worksheet_required")
        relation_id = selected.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relation = next((item for item in relations if item.get("Id") == relation_id), None)
        if relation is None or relation.get("TargetMode") == "External":
            raise ValueError("source_sheet_relation_invalid")
        target = str(relation.get("Target") or "")
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if ".." in Path(target).parts or not target.startswith("xl/"):
            raise ValueError("source_sheet_relation_invalid")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(item.itertext()) for item in root.findall("s:si", NS)]
        root = ElementTree.fromstring(archive.read(target))
        rows = []
        for row in root.findall("s:sheetData/s:row", NS):
            position = int(row.get("r") or len(rows) + 1)
            if position <= len(rows) or position > MAX_ROWS:
                raise ValueError("source_row_limit_exceeded")
            while len(rows) < position - 1:
                rows.append([])
            values = []
            for cell in row.findall("s:c", NS):
                index = _column_index(cell.get("r"))
                while len(values) <= index:
                    values.append("")
                raw = cell.find("s:v", NS)
                if cell.find("s:f", NS) is not None and (raw is None or raw.text is None):
                    raise ValueError("source_formula_cached_value_missing")
                if cell.get("t") == "e":
                    raise ValueError("source_formula_error")
                value = raw.text if raw is not None else ""
                if cell.get("t") == "s":
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(item.text or "" for item in cell.findall("s:is//s:t", NS))
                elif value and cell.get("t") not in {"str", "b"}:
                    value = float(value)
                values[index] = value if value is not None else ""
            rows.append(values)
        return rows


def read_file(params):
    from mino_scout.config import load_config
    raw = str(params.get("path") or "")
    path = Path(raw)
    if not path.is_absolute() or path.name.startswith("~$") or path.suffix.lower() not in {".csv", ".xlsx"}:
        raise ValueError("source_file_path_invalid")
    resolved = path.resolve(strict=True)
    allowed = [Path(root).expanduser().resolve(strict=True) for root in load_config().get("case_source_roots", [])]
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise ValueError("source_file_root_not_authorized")
    before = resolved.stat()
    if not resolved.is_file() or before.st_size > MAX_BYTES:
        raise ValueError("source_file_size_invalid")
    with resolved.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    after = resolved.stat()
    if path.resolve(strict=True) != resolved or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(content) > MAX_BYTES:
        raise ValueError("source_file_changed_during_read")
    if path.suffix.lower() == ".xlsx":
        table = xlsx_table(content, str(params.get("worksheet") or ""))
    else:
        text = content.decode("utf-8-sig")
        table = []
        for values in csv.reader(io.StringIO(text)):
            if len(table) >= MAX_ROWS or len(values) > 256:
                raise ValueError("source_table_size_invalid")
            table.append(values)
    return {"complete": True, "table": table, "content_digest": hashlib.sha256(content).hexdigest()}


def _bounded_range(cell_range):
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)", cell_range)
    if not match or int(match[4]) - int(match[2]) + 1 > MAX_ROWS or int(match[4]) < int(match[2]):
        raise ValueError("source_explicit_bounded_range_required")
    if _column_index(match[3] + "1") < _column_index(match[1] + "1"):
        raise ValueError("source_range_invalid")
    return cell_range


def _cli_data(args):
    from mino_scout.plugins import feishu_auth
    try:
        return feishu_auth.run_json(args)
    except RuntimeError:
        raise ValueError("source_sheets_read_denied") from None


def _sheet_rows(identity, spreadsheet, sheet_id, cell_range):
    data = _cli_data(["sheets", "+cells-get", "--as", identity, "--spreadsheet-token", spreadsheet,
        "--sheet-id", sheet_id, "--range", cell_range, "--format", "json"])
    ranges = data.get("ranges") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("has_more") is True or not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], dict):
        raise ValueError("source_sheets_incomplete")
    block = ranges[0]
    if block.get("truncated") is True or not isinstance(block.get("cells"), list):
        raise ValueError("source_sheets_incomplete")
    table = []
    for row in block["cells"]:
        if not isinstance(row, list) or len(table) >= MAX_ROWS:
            raise ValueError("source_table_size_invalid")
        values = []
        for cell in row:
            value = cell.get("value") if isinstance(cell, dict) else ""
            if value is None:
                value = ""
            elif not isinstance(value, (str, int, float, bool)):
                value = str(value)
            values.append(value)
        table.append(values)
    while table and not any(str(cell).strip() for cell in table[-1]):
        table.pop()
    return table


def _read_sheets_cli(params):
    cell_range = _bounded_range(str(params.get("range") or ""))
    status = _cli_data(["auth", "status", "--json", "--verify"])
    identity = status.get("identity") if isinstance(status, dict) else ""
    if identity not in {"user", "bot"}:
        raise ValueError("source_sheets_not_configured")
    spreadsheet = str(params.get("spreadsheet_token") or "")
    wiki_token = str(params.get("wiki_token") or "")
    if bool(spreadsheet) == bool(wiki_token):
        raise ValueError("source_sheet_identity_invalid")
    if wiki_token:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", wiki_token):
            raise ValueError("source_sheet_identity_invalid")
        node = _cli_data(["wiki", "+node-get", "--as", identity, "--node-token", wiki_token, "--format", "json"])
        if str(node.get("obj_type") or "").lower() != "sheet" or not node.get("obj_token"):
            raise ValueError("source_sheets_wiki_not_sheet")
        spreadsheet = str(node["obj_token"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet):
        raise ValueError("source_sheet_identity_invalid")
    info = _cli_data(["sheets", "+workbook-info", "--as", identity, "--spreadsheet-token", spreadsheet, "--format", "json"])
    found = []
    for item in info.get("sheets") or []:
        if not isinstance(item, dict):
            continue
        sheet_id = str(item.get("sheet_id") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
            found.append((item.get("index") if type(item.get("index")) is int else 10**9, sheet_id, item.get("is_hidden") is True))
    requested = str(params.get("sheet_id") or "")
    if requested:
        if requested not in {item[1] for item in found}:
            raise ValueError("source_sheets_tab_required")
        sheet_id = requested
    else:
        visible = [item for item in found if not item[2]]
        if not visible:
            raise ValueError("source_sheets_tab_missing")
        sheet_id = min(visible)[1]
    snapshots = [_sheet_rows(identity, spreadsheet, sheet_id, cell_range) for _attempt in range(2)]
    if snapshots[0] != snapshots[1]:
        raise ValueError("source_sheets_changed_during_read")
    return {"complete": True, "table": snapshots[1], "sheet_id": sheet_id, "consistency": "conservative_double_read"}


def _wiki_spreadsheet(access_token, wiki_token):
    from mino_scout.plugins.calls import PluginCallError, _FEISHU, _json
    if not re.fullmatch(r"[A-Za-z0-9_-]+", wiki_token):
        raise ValueError("source_sheet_identity_invalid")
    try:
        response = _json("GET", f"{_FEISHU}/wiki/v2/spaces/get_node",
            headers={"Authorization": "Bearer " + access_token}, params={"token": wiki_token})
    except PluginCallError:
        raise ValueError("source_sheets_read_denied") from None
    if response.get("code") != 0:
        raise ValueError("source_sheets_read_denied")
    node = (response.get("data") or {}).get("node") or {}
    if str(node.get("obj_type") or "").lower() != "sheet" or not node.get("obj_token"):
        raise ValueError("source_sheets_wiki_not_sheet")
    return str(node["obj_token"])


def _openapi_sheet(access_token, spreadsheet, requested):
    from mino_scout.plugins.calls import PluginCallError, _FEISHU, _json
    if requested:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", requested):
            raise ValueError("source_sheets_tab_required")
        return requested
    try:
        response = _json("GET", f"{_FEISHU}/sheets/v2/spreadsheets/{spreadsheet}/metainfo",
            headers={"Authorization": "Bearer " + access_token})
    except PluginCallError:
        raise ValueError("source_sheets_read_denied") from None
    if response.get("code") != 0:
        raise ValueError("source_sheets_read_denied")
    found = []
    for item in ((response.get("data") or {}).get("sheets") or []):
        if not isinstance(item, dict) or item.get("hidden") is True:
            continue
        sheet_id = str(item.get("sheetId") or item.get("sheet_id") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
            found.append((item.get("index") if type(item.get("index")) is int else 10**9, sheet_id))
    if not found:
        raise ValueError("source_sheets_tab_missing")
    return min(found)[1]


def read_sheets(node_id, params):
    from urllib.parse import quote
    from mino_scout.plugins.calls import PluginCallError, _need, _FEISHU, _feishu_tenant_token, _json
    from mino_scout.plugins.state import plain_value, secret_value
    try:
        _need(node_id, "cli", "feishu", "飞书插件尚未配置")
    except PluginCallError:
        raise ValueError("source_sheets_not_configured") from None
    from mino_scout.plugins import feishu_auth
    if feishu_auth.ready():
        return _read_sheets_cli(params)
    try:
        token = _feishu_tenant_token(plain_value("cli", "feishu", "app_id"), secret_value(node_id, "cli", "feishu", "app_secret"))
    except PluginCallError:
        raise ValueError("source_sheets_read_denied") from None
    spreadsheet = str(params.get("spreadsheet_token") or "")
    wiki_token = str(params.get("wiki_token") or "")
    if bool(spreadsheet) == bool(wiki_token):
        raise ValueError("source_sheet_identity_invalid")
    if wiki_token:
        spreadsheet = _wiki_spreadsheet(token, wiki_token)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet):
        raise ValueError("source_sheet_identity_invalid")
    cell_range = _bounded_range(str(params.get("range") or ""))
    sheet_id = _openapi_sheet(token, spreadsheet, str(params.get("sheet_id") or ""))
    url = f"{_FEISHU}/sheets/v2/spreadsheets/{spreadsheet}/values/{quote(sheet_id + '!' + cell_range, safe='')}?valueRenderOption=ToString"
    snapshots = []
    for _attempt in range(2):
        try:
            response = _json("GET", url, headers={"Authorization": "Bearer " + token})
        except PluginCallError:
            raise ValueError("source_sheets_read_denied") from None
        if response.get("code") != 0:
            raise ValueError("source_sheets_read_denied")
        value_range = (response.get("data") or {}).get("valueRange")
        if not isinstance(value_range, dict) or "values" not in value_range or value_range.get("majorDimension", "ROWS") != "ROWS":
            raise ValueError("source_sheets_incomplete")
        snapshots.append(value_range["values"])
    if snapshots[0] != snapshots[1]:
        raise ValueError("source_sheets_changed_during_read")
    return {"complete": True, "table": snapshots[1], "sheet_id": sheet_id, "consistency": "conservative_double_read"}


def dispatch(core, request):
    from mino_scout.schemas import EventStatus
    from mino_scout.executors.base import make_event_result, now_iso
    from mino_scout.core import _event_from_execute
    params = dict(request.params or {})
    try:
        if (params.get("contract_version") != 1 or request.scout_process_generation != core.binding_guard.process_generation
                or request.node_connection_generation != core.binding_guard.connection_generation
                or request.capability_revision != core.binding_guard.revision):
            raise ValueError("source_contract_mismatch")
        if params.get("kind") == "file":
            result = read_file(params)
        elif params.get("kind") == "sheets":
            result = read_sheets(core.node_id, params)
        elif params.get("kind") == "case_api":
            if not getattr(core, "_plugins_ready", False):
                raise ValueError("source_api_owner_unverified")
            from mino_scout.case_source_api import read_api
            result = read_api(core.node_id, params)
        else:
            raise ValueError("source_kind_invalid")
        return make_event_result(_event_from_execute(request), status=EventStatus.PASS, executor_used="core",
            started_at=now_iso(), elapsed_ms=0, summary="来源读取完成", raw_response={**result,
                **{key: params.get(key) for key in ("source_id", "read_id", "generation")}})
    except Exception as exc:
        return make_event_result(_event_from_execute(request), status=EventStatus.BLOCKED, executor_used="core",
            started_at=now_iso(), elapsed_ms=0, summary="来源读取未完成", raw_response={"complete": False,
                "reason_code": str(exc) if isinstance(exc, ValueError) and str(exc).startswith("source_") else "source_read_failed"})
