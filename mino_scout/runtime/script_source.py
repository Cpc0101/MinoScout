"""Resolve a script's CASE_SOURCE without importing or executing the script."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mino_scout.case_sources import _bounded_range, _column_index, read_sheets

GROUPS = {"preconditions": "P", "steps": "S", "expectations": "E"}


def source_binding(tree: ast.Module) -> dict | None:
    bindings = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == "CASE_SOURCE" for target in targets):
            bindings.append(node.value)
    if not bindings:
        return None
    if len(bindings) != 1:
        raise ValueError("CASE_SOURCE 必须只声明一次")
    try:
        value = ast.literal_eval(bindings[0])
    except (ValueError, TypeError, SyntaxError):
        raise ValueError("CASE_SOURCE 必须是顶层字面量字典，不能调用函数或读取变量") from None
    validate_binding(value)
    return value


def validate_binding(value: dict) -> None:
    if not isinstance(value, dict) or value.get("kind") != "feishu_sheet":
        raise ValueError("CASE_SOURCE.kind 当前支持 feishu_sheet")
    if set(value) - {"kind", "url", "sheet_id", "range", "case_id", "columns"}:
        raise ValueError("CASE_SOURCE 含不支持的字段")
    link = urlsplit(str(value.get("url") or ""))
    host = link.hostname or ""
    if (link.scheme != "https" or link.username or link.password or link.port not in (None, 443)
            or not (host.endswith(".feishu.cn") or host == "feishu.cn" or host.endswith(".larksuite.com") or host == "larksuite.com")
            or not re.fullmatch(r"/(wiki|sheets)/[A-Za-z0-9_-]+/?", link.path)):
        raise ValueError("CASE_SOURCE.url 必须是飞书 wiki 或 sheets 链接")
    sheet = value.get("sheet_id") or parse_qs(link.query).get("sheet", [""])[0]
    if not isinstance(sheet, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sheet):
        raise ValueError("CASE_SOURCE 必须指定 sheet_id 或链接中的 sheet 参数")
    cell_range = _bounded_range(str(value.get("range") or ""))
    if not isinstance(value.get("case_id"), str) or not value["case_id"].strip():
        raise ValueError("CASE_SOURCE.case_id 必须指定飞书表格中的稳定用例 ID")
    columns = value.get("columns")
    if not isinstance(columns, dict) or set(columns) != {"case_id", *GROUPS}:
        raise ValueError("CASE_SOURCE.columns 必须指定 case_id/preconditions/steps/expectations 的列字母")
    first, last = cell_range.split(":")
    indices = []
    for column in columns.values():
        if not isinstance(column, str) or not re.fullmatch(r"[A-Z]+", column):
            raise ValueError("CASE_SOURCE.columns 使用绝对列字母，例如 A、D")
        index = _column_index(column + "1")
        if not _column_index(first) <= index <= _column_index(last):
            raise ValueError("CASE_SOURCE.columns 必须在 range 内")
        indices.append(index)
    if len(set(indices)) != len(indices):
        raise ValueError("CASE_SOURCE.columns 不能重复使用同一列")


def _clauses(text: str, prefix: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows = []
    for number, line in enumerate(lines, 1):
        explicit = re.match(r"^([PSE][1-9][0-9]*)\s*[:：.、]\s*(.+)$", line)
        if explicit:
            key, content = explicit.groups()
            if not key.startswith(prefix):
                raise ValueError("飞书条目编号与所在列类型不一致")
        else:
            key = prefix + str(number)
            content = line
        rows.append({"id": key, "text": content})
    return rows


def fetch_source(binding: dict, node_id: str, script_id: str) -> dict:
    validate_binding(binding)
    link = urlsplit(binding["url"])
    kind, token = link.path.strip("/").split("/")
    params = {"range": binding["range"],
              "sheet_id": binding.get("sheet_id") or parse_qs(link.query)["sheet"][0],
              "wiki_token" if kind == "wiki" else "spreadsheet_token": token}
    result = read_sheets(node_id, params)
    if result.get("complete") is not True or not isinstance(result.get("table"), list):
        raise ValueError("飞书用例原文未完整读取")
    offset = _column_index(binding["range"].split(":")[0])
    columns = {key: _column_index(value + "1") - offset for key, value in binding["columns"].items()}
    def cell(row, key):
        index = columns[key]
        return str(row[index] if index < len(row) and row[index] is not None else "").strip()
    table = result["table"]
    if any(not isinstance(row, list) for row in table):
        raise ValueError("飞书用例表格格式无效")
    matches = [row for row in table if cell(row, "case_id") == binding["case_id"]]
    if len(matches) != 1:
        raise ValueError("指定范围内必须唯一匹配 CASE_SOURCE.case_id，当前匹配 " + str(len(matches)) + " 条")
    raw = {key: cell(matches[0], key) for key in columns}
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    snapshot = {"case_id": script_id, "source": {"url": binding["url"], "revision": digest,
        "binding": dict(binding), "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sheet_id": result.get("sheet_id", params["sheet_id"]), "text": raw}}
    snapshot.update({key: _clauses(raw[key], prefix) for key, prefix in GROUPS.items()})
    if not snapshot["expectations"]:
        raise ValueError("飞书用例必须包含可核验的预期")
    return snapshot


def load_source(path: Path, node_id: str) -> dict | None:
    binding = source_binding(ast.parse(path.read_text(encoding="utf-8")))
    if binding is None:
        return None
    if path.with_suffix(".yaml").is_file():
        raise ValueError("CASE_SOURCE 与同名 YAML 不能同时存在，请删除重复的原文快照")
    return fetch_source(binding, node_id, path.stem)
