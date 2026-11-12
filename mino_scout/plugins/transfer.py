"""Explicit owner-authorized portable configuration, never a heartbeat payload.

The archive contains credentials (base64 is transport encoding, not encryption).
Only catalog fields and the documented CLI application config are portable;
machine-bound OAuth sessions must be authorized again on the target machine.
"""
from __future__ import annotations

import base64
import binascii
import json
from contextlib import ExitStack

from mino_scout.plugins import state

FORMAT = 'mino-scout-config'
MAX_BYTES = 2 * 1024 * 1024
EXTRA_FIELDS = {
    ('cli', 'feishu'): (),
}


def export_config(node_id):
    entries = []
    for spec in state.CATALOG:
        kind, pid = spec['class'], spec['id']
        item = state._item(kind, pid)
        values = {}
        for field in spec['fields']:
            key = field['key']
            if field.get('secret'):
                value = state.secret_value(node_id, kind, pid, key)
                if item.get(f'has_{key}') and not value:
                    raise ValueError('凭据读取失败，未生成不完整的导出文件')
            else:
                value = str(item.get(key) or '')
            if value:
                values[key] = value
        extras = {key: str(item[key]) for key in EXTRA_FIELDS.get((kind, pid), ()) if item.get(key)}
        entry = {'class': kind, 'id': pid, 'values': values, 'extras': extras}
        if (kind, pid) == ('cli', 'feishu'):
            from mino_scout.plugins.feishu_auth import root
            path = root() / 'config.json'
            if path.is_file():
                entry['application_config'] = json.loads(path.read_text(encoding='utf-8'))
        entries.append(entry)
    return {'format': FORMAT, 'version': 1, 'items': entries}


def validate(raw):
    if not isinstance(raw, dict) or raw.get('format') != FORMAT or type(raw.get('version')) is not int or raw['version'] != 1:
        raise ValueError('不支持的配置文件或版本')
    entries = raw.get('items')
    if not isinstance(entries, list) or not entries or len(entries) > len(state.CATALOG):
        raise ValueError('配置项目列表无效')
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('配置项目格式无效')
        key = (entry.get('class'), entry.get('id'))
        if not all(isinstance(x, str) for x in key):
            raise ValueError('配置项目标识无效')
        spec = state.spec(*key)
        if spec is None or key in seen:
            raise ValueError('文件包含未知或重复插件，请先升级 Scout')
        seen.add(key)
        fields = {f['key']: f for f in spec['fields']}
        values, extras = entry.get('values'), entry.get('extras', {})
        if not isinstance(values, dict) or not isinstance(extras, dict):
            raise ValueError('配置字段格式无效')
        if set(values) - fields.keys() or set(extras) - set(EXTRA_FIELDS.get(key, ())):
            raise ValueError('文件包含不支持的配置字段')
        if any(not isinstance(v, str) or len(v) > 65536 for v in [*values.values(), *extras.values()]):
            raise ValueError('配置字段类型或长度无效')
        for name, value in values.items():
            if not fields[name].get('secret') and len(value) > 500:
                raise ValueError('非密钥配置字段不能超过 500 字符')
            if fields[name].get('type') == 'bool' and value not in ('0', '1'):
                raise ValueError('启用状态只能为 0 或 1')
        if 'application_config' in entry and (key != ('cli', 'feishu') or not isinstance(entry['application_config'], dict)):
            raise ValueError('飞书应用配置格式无效')
    return entries


def import_config(node_id, raw):
    entries = validate(raw)  # Entire archive validated before any write.
    from mino_scout.plugins.install import acquire_plugin
    from mino_scout.plugins.secret_store import account_name, put_secret, delete_secret
    from mino_scout.plugins import feishu_auth
    with ExitStack() as stack:
        for entry in sorted(entries, key=lambda e: (e['class'], e['id'])):
            lock = acquire_plugin(entry['class'], entry['id'])
            stack.callback(lock.release)
        before = state.load_state()
        previous = export_config(node_id)
        auth_path = feishu_auth.root() / 'config.json'
        auth_status = feishu_auth.root() / 'mino-status.json'
        auth_before = auth_path.read_bytes() if auth_path.is_file() else None
        status_before = auth_status.read_bytes() if auth_status.is_file() else None
        keys = {(e['class'], e['id']) for e in entries}
        missing = []
        warnings = []
        try:
            for entry in entries:
                kind, pid = entry['class'], entry['id']
                spec = state.spec(kind, pid)
                values = entry['values']
                state.apply_config(node_id, kind, pid, values,
                                   [f['key'] for f in spec['fields'] if not values.get(f['key'])], importing=True)
                extras = {key: entry.get('extras', {}).get(key, '') for key in EXTRA_FIELDS.get((kind, pid), ())}
                state.remember_plain(kind, pid, extras)
                root = state.load_state()
                item = root['items'][f'{kind}:{pid}']
                needed = bool(any(value not in ('', '0') for value in values.values()) or any(value not in ('', '0') for value in extras.values()) or entry.get('application_config'))
                blocked = bool(needed and spec.get('needs_install') and not state.installed(kind, pid))
                if blocked:
                    item['import_blocked'] = True
                    missing.append(f'{kind}.{pid}')
                else:
                    item.pop('import_blocked', None)
                state.save_state(root)
                if (kind, pid) == ('cli', 'feishu'):
                    auth_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if 'application_config' in entry:
                        auth_path.write_text(json.dumps(entry['application_config'], ensure_ascii=False), encoding='utf-8')
                        auth_path.chmod(0o600)
                        warnings.append('飞书 CLI 应用配置已迁移，设备绑定的登录授权需在目标节点重新扫码。')
                    else:
                        auth_path.unlink(missing_ok=True)
                    feishu_auth._save(auth_path.parent, {'state': 'idle'})
        except Exception:
            try:
                for entry in previous['items']:
                    key = (entry['class'], entry['id'])
                    if key not in keys:
                        continue
                    for field in state.spec(*key)['fields']:
                        if field.get('secret'):
                            account = account_name(node_id, *key, field['key'])
                            value = entry['values'].get(field['key'], '')
                            put_secret(account, value) if value else delete_secret(account)
                state.save_state(before)
                if ('cli', 'feishu') in keys:
                    for path, content in ((auth_path, auth_before), (auth_status, status_before)):
                        if content is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_bytes(content)
                            path.chmod(0o600)
            except Exception:
                raise ValueError('配置写入失败且回滚未完成，服务已停止；请检查本机凭据存储后重新导入') from None
            raise ValueError('配置写入失败，已恢复原配置；请检查本机凭据存储') from None
    return {'missing_tools': missing, 'warnings': warnings, 'count': len(entries)}


def handle(core, req, cmd, event, started):
    from mino_scout.plugins.service import _blocked_plugin, _fail
    from mino_scout.executors.base import make_event_result
    from mino_scout.schemas import EventStatus
    if not getattr(core, '_plugins_ready', False):
        return _blocked_plugin(core, event, started)
    try:
        with state.CONFIG_LOCK:
            if cmd == 'config_export':
                payload = json.dumps(export_config(core.node_id), ensure_ascii=False).encode('utf-8')
                if len(payload) > MAX_BYTES:
                    raise ValueError('配置文件超过 2 MB')
                data = {'archive': base64.b64encode(payload).decode('ascii')}
            else:
                encoded = req.params.get('archive')
                if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 4:
                    raise ValueError('配置文件过大或格式无效')
                try:
                    payload = base64.b64decode(encoded, validate=True)
                    raw = json.loads(payload)
                except (ValueError, binascii.Error, UnicodeError):
                    raise ValueError('配置文件不是有效的 JSON') from None
                data = import_config(core.node_id, raw)
        result = make_event_result(event, status=EventStatus.PASS, executor_used='core',
                                   started_at=started, elapsed_ms=0, summary='配置已导出' if cmd == 'config_export' else '配置已导入',
                                   raw_response={} if cmd == 'config_export' else data)
        if cmd == 'config_export':
            result.raw_response = data
        return result
    except Exception as exc:
        text = str(exc) if isinstance(exc, ValueError) else '配置迁移失败，请检查节点凭据存储与插件任务'
        return _fail(event, started, text, cmd)
