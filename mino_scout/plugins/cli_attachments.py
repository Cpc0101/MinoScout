"""Upload scoped IM bytes with the official CLI, never a caller-supplied local path."""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import tempfile
from pathlib import Path

from mino_scout.plugins.calls import PluginCallError


def failure(summary: str, *, uncertain: bool = False) -> dict:
    return {'ok': False, 'status': 503 if uncertain else 422, 'reason_code': 'meego_attachment_upload_failed',
            'summary': summary, 'next_steps': [
                '插件凭据不等于官方 Meegle CLI 用户授权；请检查该节点 CLI 授权和目标空间的附件权限。',
                '结果未知时先核对已上传的附件，不要重复创建或上传。' if uncertain else '修正上述问题后继续原请求。']}


def _auto_field(node_id: str, options: dict) -> str:
    """模型漏填附件字段时，仅在空间里存在唯一文件字段时自动使用它。"""
    from mino_scout.plugins.cli_bridge import _meego_api

    result = _meego_api(node_id, ['workitem', 'meta-fields'], {
        'project-key': options.get('project-key'),
        'work-item-type': options.get('work-item-type'),
    })
    data = result.get('data') if isinstance(result, dict) else None
    fields = data if isinstance(data, list) else (data.get('fields') if isinstance(data, dict) else [])
    candidates = [row for row in fields or [] if isinstance(row, dict)
                  and str(row.get('field_type_key') or '') in {'multi_file', 'attachment', 'file'}
                  and row.get('field_key')]
    return str(candidates[0]['field_key']) if len(candidates) == 1 else ''


def prepare(ref: str, options: dict, origin: dict) -> tuple[bytes, str, str]:
    raise ValueError('对话附件能力已移除')


def upload(node_id: str, prepared: tuple[bytes, str, str], options: dict) -> dict:
    from mino_scout.plugins.cli_bridge import _native

    data, name, mime = prepared
    if options.get('dry-run') is True:
        return {'ok': True, 'dry_run': True, 'summary': '附件参数已预览，尚未上传',
                'data': {'name': name, 'size': len(data), 'mime_type': mime}}
    with tempfile.TemporaryDirectory(prefix='mino-meego-upload-') as directory:
        source = Path(directory) / name
        source.write_bytes(data)
        native = {key: value for key, value in options.items() if key not in {'filename', 'dry-run'}}
        native.update({'filename': name, 'content-type': mime})
        try:
            result = _native(node_id, 'meego', ['attachment', '+upload'], native, source_path=str(source))
        except PluginCallError as exc:
            message = str(exc).replace(str(source), '[附件]').replace(directory, '[临时目录]')
            return failure('Meego CLI 附件上传未完成：' + message, uncertain=True)
    current = result
    for _ in range(5):
        if not isinstance(current, dict):
            break
        if isinstance(current.get('file_token'), str) and current['file_token']:
            info = {key: current.get(key) for key in ('file_token', 'file_url', 'name', 'size', 'mime_type')}
            info.update(name=name, size=len(data), mime_type=mime)
            return {'ok': True, 'source': 'cli', 'id': info['file_token'], 'data': info,
                    **info, 'summary': f'附件已上传：{name}；尚需关联到工作项字段或评论'}
        current = current.get('data')
    return failure('上传命令已返回，但没有可确认的 file_token；不会继续创建工作项。', uncertain=True)


def create(node_id: str, options: dict, attachments: list[dict], origin: dict) -> dict:
    from mino_scout.plugins.cli_bridge import _meego_api
    from mino_scout.plugins.state import plain_value

    if not plain_value('cli', 'meego', 'plugin_id'):
        return failure('带附件创建需要已配置的 Meego 插件凭据；本次尚未上传或创建。')
    preview = _meego_api(node_id, ['workitem', 'create'], {**options, 'dry-run': True})
    if not preview or not preview.get('ok') or preview.get('needs_input'):
        return preview or failure('带附件创建需要已配置的 Meego 插件凭据；本次尚未上传或创建。')
    if not isinstance(attachments, list) or not 1 <= len(attachments) <= 4:
        raise ValueError('一次创建最多关联 4 个附件')
    params = options.get('params') or {}
    if isinstance(params, str):
        params = json.loads(params)
    fields = [dict(item) for item in params.get('fields') or []]
    for item in attachments:
        if isinstance(item, dict) and str(item.get('field_key') or '') == '__auto__':
            field_key = _auto_field(node_id, options)
            if not field_key:
                return failure('这个空间没有唯一的文件附件字段，无法自动挂载截图；请先查询字段后指定 field-key，本次尚未创建。')
            item['field_key'] = field_key
    field_modes = {}
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError('附件参数格式不正确')
        field_key, mode = str(item.get('field_key') or ''), str(item.get('resource_type', 15))
        if field_key in field_modes and field_modes[field_key] != mode:
            raise ValueError('同一个工作项字段不能同时作为附件字段和富文本图片字段')
        field_modes[field_key] = mode
        existing = next((field for field in fields if field.get('field_key') == item.get('field_key')), {})
        value = existing.get('field_value')
        if str(item.get('resource_type', 15)) == '15' and value:
            value = json.loads(value) if isinstance(value, str) else value
            if not isinstance(value, list):
                raise ValueError('附件字段原值必须为数组或表示数组的 JSON 字符串')
        elif str(item.get('resource_type', 15)) == '16' and value and not isinstance(value, str):
            raise ValueError('图片所在的富文本字段原值必须为文本')
    prepared = []
    for item in attachments:
        if not isinstance(item, dict) or set(item) - {'ref', 'field_key', 'resource_type', 'filename'}:
            raise ValueError('附件参数格式不正确')
        kind, field = str(item.get('resource_type', 15)), str(item.get('field_key') or '')
        if kind not in {'15', '16'} or not field:
            raise ValueError('创建工作项的附件必须指定实际字段，以及 resource_type=15 或 16')
        if field in {'name', 'template'}:
            raise ValueError('不能把标题或模板字段当作附件字段')
        upload_options = {'project-key': options.get('project-key'), 'work-item-type': options.get('work-item-type'),
                          'resource-type': kind, 'field-key': field, 'filename': item.get('filename')}
        prepared.append((prepare(item.get('ref'), upload_options, origin), upload_options))
    if options.get('dry-run') is True:
        return {**preview, 'attachment_count': len(prepared)}
    for media, upload_options in prepared:
        result = upload(node_id, media, upload_options)
        if not result.get('ok'):
            return {**result, 'summary': result['summary'] + ' 本次尚未发送工作项创建请求。'}
        info = result['data']
        field = next((item for item in fields if item.get('field_key') == upload_options['field-key']), None)
        if field is None:
            field = {'field_key': upload_options['field-key'], 'field_value': ''}
            fields.append(field)
        if upload_options['resource-type'] == '16':
            if not isinstance(info.get('file_url'), str) or not info['file_url'].startswith('https://'):
                return failure('图片已上传，但未返回可用于富文本的图片地址；尚未创建工作项。', uncertain=True)
            label = re.sub(r'[\[\]\\\r\n]', '_', info['name'])
            field['field_value'] = str(field.get('field_value') or '') + f"\n![{label}]({info['file_url']}) <!-- {info['file_token']} -->"
        else:
            values = field.get('field_value') or []
            if isinstance(values, str):
                values = json.loads(values)
            if not isinstance(values, list):
                return failure('附件字段原值格式不正确；附件已上传，但尚未创建工作项。', uncertain=True)
            values = values + [{'name': info['name'], 'type': info['mime_type'], 'size': str(info['size']), 'fileToken': info['file_token']}]
            field['field_value'] = json.dumps(values, ensure_ascii=False)
    result = _meego_api(node_id, ['workitem', 'create'], {**options, 'params': {**params, 'fields': fields}})
    if result and result.get('ok'):
        result['summary'] += f'；{len(prepared)} 个附件已上传并随创建请求提交'
        result['attachment_count'] = len(prepared)
    return result
