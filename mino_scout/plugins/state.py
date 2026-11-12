"""插件非密钥参数写在节点目录。心跳只报安装与是否已配置。"""
from __future__ import annotations

import json
import threading
from functools import wraps

from pathlib import Path
from typing import Any

from mino_scout.plugins.paths import data_dir
from mino_scout.plugins.secret_store import account_name, delete_secret, get_secret, put_secret

CONFIG_LOCK = threading.RLock()

def config_locked(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with CONFIG_LOCK:
            return fn(*args, **kwargs)
    return wrapped

# 插件类别。MCP 还没有具体项。bot = Webhook 只发通知。
PLUGIN_CLASSES = ("cli", "bot", "mail", "mcp")


def _f(key: str, label: str, placeholder: str = "", *, secret: bool = False,
       optional: bool = False, ftype: str = "text", group: str = "") -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key, "label": label, "placeholder": placeholder,
        "secret": secret, "optional": optional,
    }
    if ftype != "text":
        row["type"] = ftype
    if group:
        row["group"] = group
    return row


CATALOG: list[dict[str, Any]] = [
    {
        "class": "cli",
        "id": "case_api",
        "label": "用例服务 API",
        "needs_install": False,
        "fields": [
            _f("email", "登录邮箱", ""),
            _f("password", "登录密码", "", secret=True),
        ],
    },
    {
        "class": "cli",
        "id": "feishu",
        "needs_install": True,
        "fields": [
            _f("app_id", "App ID", "cli_xxx"),
            _f("app_secret", "App Secret", "", secret=True),
        ],
    },
    {
        "class": "cli",
        "id": "meego",
        "needs_install": True,
        "fields": [
            _f("base_url", "Base URL", "https://project.feishu.cn", optional=True),
            _f("plugin_id", "Plugin ID", ""),
            _f("user_key", "User Key", "空间查询及创建工作项使用的 Meego 用户身份", optional=True),
            _f("plugin_secret", "Plugin Secret", "", secret=True),
        ],
    },
    {
        "class": "bot",
        "id": "feishu_bot",
        "needs_install": False,
        "fields": [
            _f("webhook_url", "Webhook 地址", "https://open.feishu.cn/open-apis/bot/v2/hook/...", secret=True),
        ],
    },
    {
        "class": "bot",
        "id": "wecom",
        "needs_install": False,
        "fields": [
            _f("webhook_url", "Webhook 地址", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...", secret=True),
        ],
    },
    {
        "class": "mail",
        "id": "gmail",
        "needs_install": False,
        "fields": [
            _f("inbox_address", "收件邮箱", "name@gmail.com"),
            _f("app_password", "应用专用密码", "", secret=True),
        ],
    },
]


def _key(kind: str, plugin_id: str) -> str:
    return f"{kind}:{plugin_id}"


def state_path() -> Path:
    return data_dir() / "plugins" / "state.json"


def plugin_dir(kind: str, plugin_id: str) -> Path:
    return data_dir() / "plugins" / kind / plugin_id


def spec(kind: str, plugin_id: str) -> dict[str, Any] | None:
    for row in CATALOG:
        if row["class"] == kind and row["id"] == plugin_id:
            return row
    return None


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {"items": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": {}}
    if not isinstance(raw, dict):
        return {"items": {}}
    items = raw.get("items")
    if not isinstance(items, dict):
        raw["items"] = {}
    return raw


def save_state(data: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _item(kind: str, plugin_id: str) -> dict[str, Any]:
    root = load_state()
    items = root.get("items") if isinstance(root.get("items"), dict) else {}
    row = items.get(_key(kind, plugin_id))
    return dict(row) if isinstance(row, dict) else {}


def plain_value(kind: str, plugin_id: str, field: str) -> str:
    return str(_item(kind, plugin_id).get(field) or "").strip()


def secret_value(node_id: str, kind: str, plugin_id: str, field: str) -> str:
    return get_secret(account_name(node_id, kind, plugin_id, field))


def installed(kind: str, plugin_id: str) -> bool:
    row = spec(kind, plugin_id)
    if row is None:
        return False
    if not row.get("needs_install"):
        return True
    marker = plugin_dir(kind, plugin_id) / ".installed"
    return marker.is_file()


@config_locked
def release_import_block(kind: str, plugin_id: str) -> bool:
    """插件已经装好时，去掉导入时留下的停用标记。

    只解除 import_blocked。导入时关掉的对话渠道保持关闭，仍由配置页单独打开。
    """
    if not installed(kind, plugin_id):
        return False
    root = load_state()
    items = root.get("items")
    if not isinstance(items, dict):
        return False
    current = items.get(_key(kind, plugin_id))
    if not isinstance(current, dict) or not current.get("import_blocked"):
        return False
    current = dict(current)
    current.pop("import_blocked", None)
    items[_key(kind, plugin_id)] = current
    save_state(root)
    return True


@config_locked
def release_installed_import_blocks() -> list[str]:
    """把已经安装、但仍标着 import_blocked 的配置解开。"""
    root = load_state()
    items = root.get("items")
    if not isinstance(items, dict):
        return []
    released: list[str] = []
    for key, current in list(items.items()):
        if not isinstance(current, dict) or not current.get("import_blocked") or ":" not in str(key):
            continue
        kind, plugin_id = str(key).split(":", 1)
        if not installed(kind, plugin_id):
            continue
        current = dict(current)
        current.pop("import_blocked", None)
        items[key] = current
        released.append(str(key))
    if released:
        save_state(root)
    return released


def configured(node_id: str, kind: str, plugin_id: str) -> bool:
    """心跳会频繁调用。密钥是否在，只看本机清单里的标记，不去读保险库。"""
    del node_id
    row = spec(kind, plugin_id)
    if row is None or not row.get("fields"):
        return False
    item = _item(kind, plugin_id)
    if item.get("import_blocked"):
        return False
    if kind == "cli" and plugin_id == "feishu":
        from mino_scout.plugins.feishu_auth import ready
        if ready():
            return True
    for field in row["fields"]:
        key = str(field.get("key") or "")
        if field.get("optional"):
            continue
        if field.get("secret"):
            if not item.get(f"has_{key}"):
                return False
        elif not str(item.get(key) or "").strip():
            return False
    return True


def status_list(node_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in CATALOG:
        kind = str(row["class"])
        pid = str(row["id"])
        item = _item(kind, pid)
        values: dict[str, str] = {}
        saved_secrets: list[str] = []
        for field in row.get("fields") or []:
            key = str(field.get("key") or "")
            if not key:
                continue
            if field.get("secret"):
                if item.get(f"has_{key}"):
                    saved_secrets.append(key)
            else:
                text = str(item.get(key) or "").strip()
                if text:
                    values[key] = text
        entry: dict[str, Any] = {
            "class": kind,
            "id": pid,
            "installed": installed(kind, pid),
            "configured": configured(node_id, kind, pid),
            "import_blocked": bool(item.get("import_blocked")),
            "values": values,
            "saved_secrets": saved_secrets,
            "fields": [dict(f) for f in row.get("fields") or []],
        }
        if row.get("label"):
            entry["label"] = str(row["label"])
        if kind == 'cli' and pid == 'meego':
            entry['attachment_upload_version'] = 1
        out.append(entry)
    return out


@config_locked
def apply_config(
    node_id: str,
    kind: str,
    plugin_id: str,
    values: dict[str, Any],
    clear: list[str],
    *, importing: bool = False,
) -> None:
    row = spec(kind, plugin_id)
    if row is None:
        raise ValueError(f"未知插件 {kind}/{plugin_id}")
    allowed = {str(field.get("key") or ""): bool(field.get("secret")) for field in row["fields"]}
    incoming = values if isinstance(values, dict) else {}
    unknown = [k for k in incoming if k not in allowed]
    if unknown:
        raise ValueError(f"不能写这些字段: {', '.join(unknown)}")
    for key in clear or []:
        if key not in allowed:
            raise ValueError(f"不能清除字段 {key}")
    root = load_state()
    items = root.setdefault("items", {})
    current = dict(items.get(_key(kind, plugin_id)) or {})
    for key, secret in allowed.items():
        if key in (clear or []):
            if secret:
                delete_secret(account_name(node_id, kind, plugin_id, key))
                current.pop(f"has_{key}", None)
            else:
                current.pop(key, None)
            continue
        if key not in incoming:
            continue
        text = str(incoming.get(key) or "")
        if not (kind == "cli" and plugin_id == "case_api" and key == "password"):
            text = text.strip()
        if _field_type(row, key) == "bool":
            current[key] = "1" if text.lower() in ("1", "true", "yes", "on") else "0"
            continue
        if secret:
            if text:
                put_secret(account_name(node_id, kind, plugin_id, key), text)
                current[f"has_{key}"] = True
            continue
        if text:
            current[key] = text[:500]
        else:
            current.pop(key, None)
    if row.get("needs_install") and not installed(kind, plugin_id):
        current["import_blocked"] = True
        for field in row["fields"]:
            if field.get("type") == "bool":
                current[field["key"]] = "0"
    elif not importing:
        current.pop("import_blocked", None)
    items[_key(kind, plugin_id)] = current
    save_state(root)


def _field_type(row: dict[str, Any], key: str) -> str:
    for field in row.get("fields") or []:
        if field.get("key") == key:
            return str(field.get("type") or "text")
    return "text"


@config_locked
def remember_secret(node_id: str, kind: str, plugin_id: str, field: str, secret: str) -> None:
    """扫码登录这类不走表单的密钥。只在清单里记「有没有」，明文进保险库。"""
    text = str(secret or "")
    if text:
        put_secret(account_name(node_id, kind, plugin_id, field), text)
    else:
        delete_secret(account_name(node_id, kind, plugin_id, field))
    root = load_state()
    items = root.setdefault("items", {})
    current = dict(items.get(_key(kind, plugin_id)) or {})
    if text:
        current[f"has_{field}"] = True
    else:
        current.pop(f"has_{field}", None)
    items[_key(kind, plugin_id)] = current
    save_state(root)


@config_locked
def remember_plain(kind: str, plugin_id: str, values: dict[str, Any]) -> None:
    root = load_state()
    items = root.setdefault("items", {})
    current = dict(items.get(_key(kind, plugin_id)) or {})
    for key, value in values.items():
        text = str(value or "").strip()
        if text:
            current[str(key)] = text[:500]
        else:
            current.pop(str(key), None)
    items[_key(kind, plugin_id)] = current
    save_state(root)


@config_locked
def clear_plugin_secrets(node_id: str, kind: str, plugin_id: str) -> None:
    row = spec(kind, plugin_id) or {}
    for field in row.get("fields") or []:
        if field.get("secret"):
            delete_secret(account_name(node_id, kind, plugin_id, str(field.get("key") or "")))
    root = load_state()
    items = root.setdefault("items", {})
    items.pop(_key(kind, plugin_id), None)
    save_state(root)


def verify_local_owner(user_id: str, nexus_url: str) -> None:
    """首次确认账号时启用独立空存储，不继承无归属旧配置及消息。"""
    from mino_scout.config import load_config, save_config
    from urllib.parse import urlsplit
    uid = str(user_id or "").strip()
    parts = urlsplit(nexus_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if not uid or not parts.netloc:
        raise PermissionError("节点未确认账号归属，社交配置已停用")
    cfg = load_config()
    previous = str(cfg.get("plugin_owner_user_id") or "")
    previous_origin = str(cfg.get("plugin_owner_nexus") or "")
    if previous:
        def identity(value: str) -> tuple:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower()
            if host in {"localhost", "127.0.0.1", "::1"}:
                host = "loopback"
            scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
            return scheme, host, parsed.port or (443 if scheme == "wss" else 80)
        if previous != uid or identity(previous_origin) != identity(origin):
            raise PermissionError("本地插件属于其他账号或服务，禁止继承；需先隔离旧配置")
        return
    cfg["plugin_owner_user_id"] = uid
    cfg["plugin_owner_nexus"] = origin
    cfg["plugin_storage_version"] = 2
    save_config(cfg)
