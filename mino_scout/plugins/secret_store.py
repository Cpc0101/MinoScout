"""Scout-local plugin secret storage.

Secrets are stored only in profile-scoped private files owned by the current
user. No operating-system credential service is used. See ``file_secrets`` for
permissions, locking, and atomic writes.
"""
from __future__ import annotations

from mino_scout.plugins.file_secrets import locked_account


class SecretStoreError(Exception):
    pass


def account_name(node_id: str, kind: str, plugin_id: str, field: str) -> str:
    from mino_scout.config import load_config
    owner = str(load_config().get("plugin_owner_user_id") or "")
    if not owner:
        raise SecretStoreError("插件账号归属尚未确认")
    from mino_scout.plugins.paths import profile_id
    profile = profile_id()
    if profile:
        return f"v2:{profile}:{node_id}:{kind}:{plugin_id}:{field}"
    return f"{owner}:{node_id}:{kind}:{plugin_id}:{field}"


def put_secret(account: str, secret: str) -> None:
    if not account:
        raise SecretStoreError("密钥条目名为空")
    try:
        with locked_account(account) as entry:
            entry.write(str(secret or ""))
    except (OSError, ValueError) as exc:
        raise SecretStoreError("本机插件密钥保存失败，请检查私有目录权限或文件格式") from exc


def get_secret(account: str) -> str:
    if not account:
        return ""
    try:
        with locked_account(account) as entry:
            present, value = entry.read()
            return (value or "") if present else ""
    except (OSError, ValueError) as exc:
        raise SecretStoreError("本机插件密钥读取失败，请检查私有目录权限或文件格式") from exc


def has_secret(account: str) -> bool:
    return bool(get_secret(account))


def delete_secret(account: str) -> None:
    if not account:
        return
    try:
        with locked_account(account) as entry:
            entry.remove()
    except (OSError, ValueError) as exc:
        raise SecretStoreError("本机插件密钥删除失败，请检查私有目录权限或文件格式") from exc
