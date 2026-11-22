from __future__ import annotations

import json
import re
import hashlib
from urllib.parse import quote, urlsplit

from mino_scout.plugins.calls import PluginCallError, _json, _meego_token, _need
from mino_scout.plugins.host_exec import HostExecError, run_entry
from mino_scout.plugins.state import plugin_dir, plain_value, secret_value

_WORD = re.compile(r'^[A-Za-z+][A-Za-z0-9_.+-]{0,100}$')
_FLAG = re.compile(r'^[a-z][a-z0-9-]{0,70}$')
_LOCAL = {'auth', 'config', 'install', 'uninstall', 'upgrade', 'update-cli', 'completion', 'mcp', 'api', 'raw', 'server', 'daemon', 'event', 'events', 'version', 'skills', 'doctor', 'extension', 'telemetry', 'ai-handoff'}
_OPTIONS = {'help', 'params', 'data', 'json', 'body', 'fields'}
_BLOCKED_OPTIONS = {'file', 'files', 'input', 'output', 'out', 'output-dir', 'save', 'save-to', 'upload', 'upload-file', 'download', 'download-to', 'config', 'config-dir', 'profile', 'host', 'base-url', 'url', 'api-url', 'api-base', 'endpoint', 'header', 'headers', 'token', 'access-token', 'user-access-token', 'app-secret', 'plugin-secret', 'verbose', 'debug', 'trace', 'as'}
_PAGING_OPTIONS = {'all', 'auto-paginate', 'page-all'}
_READ = {'get', 'list', 'search', 'query', 'fetch', 'read', 'batch-get', 'batch_get', 'batch-query', 'batch_query', 'mget', 'me', 'todo', 'agenda', '+fetch', '+search', '+list', '+agenda', '+messages-list', '+messages-get', '+chat-list', '+chat-get', '+chat-search', '+chats-list', '+messages-search', '+thread-list', '+thread-get'}
_MEEGO_API = {'meta-types', 'meta-fields', 'meta-templates', 'get', 'create'}
_MEEGO_LABELS = {'project.list': '查询可访问空间', 'project.get': '查询空间详情',
                 'meta-types': '查询工作项类型', 'meta-fields': '查询字段',
                 'meta-templates': '查询模板', 'get': '查询工作项', 'create': '创建工作项'}


def _question(option: str, summary: str) -> dict:
    return {'ok': False, 'source': 'open_api', 'needs_input': True,
            'field': 'options.' + option, 'summary': summary}


def _meego_failure(action: str, message: str, *, project: str = '', phase: str = 'api', code=None) -> dict:
    permission = any(word in message.lower() for word in ('check token perm', 'permission', 'forbidden', 'access denied', '无权限', '权限不足'))
    label = _MEEGO_LABELS.get(action, action)
    if phase == 'authentication':
        summary = f'Meego 插件鉴权未通过，尚未发送「{label}」请求。'
        steps = ['在 Scout → 配置 → Meego 核对 Plugin ID、Plugin Secret 和 Base URL 是否属于同一个插件及环境。',
                 '确认该插件可用；换票通过后还需单独校验目标空间的业务接口权限。']
        status, reason = (403 if permission else 422), 'meego_authentication_failed'
    elif permission:
        summary = f'Meego 拒绝了「{label}」：当前插件未通过接口权限校验。'
        steps = [f'在飞书项目的插件管理中检查「{label}」对应的 OpenAPI 权限，并发布包含该权限的版本。',
                 '确认目标空间已安装或启用该插件版本；可先查询空间列表、通过名称或链接别名解析真实 project_key。',
                 '业务操作使用的 User Key 在 Scout → 配置 → Meego 中设置，并确认该用户有目标空间的相应权限。']
        status, reason = 403, 'meego_permission_denied'
    else:
        summary = f'Meego「{label}」未完成：{message}'
        steps = ['在 Scout → 配置 → Meego 检查插件凭据、User Key 和目标空间配置。']
        status, reason = (503 if phase == 'api' and action == 'create' else 422), 'meego_request_failed'
        if status == 503:
            steps = ['先查询 Meego 中是否已生成该工作项，确认结果前不要重复提交。']
    return {'ok': False, 'source': 'open_api', 'status': status, 'reason_code': reason,
            'summary': summary, 'next_steps': steps, 'error_code': code,
            'error_message': message, 'phase': phase, 'action': action, 'project_key': project}


def _result(source: str, *, data=None, content: str = '', dry_run: bool = False) -> dict:
    response = {'ok': True, 'source': source, 'data': data} if data is not None else {'ok': True, 'source': source, 'content': content}
    # 回执保留业务 ID，IM 续接或重复消息不再创建第二条工作项。
    current = data
    for _ in range(3):
        if not isinstance(current, dict):
            break
        identifier = next((current[key] for key in ('work_item_id', 'message_id', 'document_id', 'record_id', 'id')
                           if isinstance(current.get(key), (str, int)) and not isinstance(current[key], bool)), None)
        if identifier is not None:
            response['id'] = str(identifier)
            break
        current = current.get('data')
    preview = content or json.dumps(data, ensure_ascii=False, default=str)
    response['summary'] = ('参数预览，尚未提交：' if dry_run else '执行结果：') + preview[:3500]
    if len(preview) > 3500:
        response['summary'] += '\n…结果已截断'
    if dry_run:
        response['dry_run'] = True
    return response


def _arguments(params: dict) -> tuple[list[str], dict, str]:
    command = params.get('command')
    options = params.get('options') or {}
    mode = params.get('mode')
    if mode not in ('read', 'write') or not isinstance(command, list) or not 1 <= len(command) <= 8:
        raise PluginCallError('请指定 CLI 命令及读写类型')
    if any(not isinstance(part, str) or not _WORD.fullmatch(part) for part in command):
        raise PluginCallError('command 只能包含命令名称，参数请放在 options 中')
    if not isinstance(options, dict) or len(options) > 40 or len(json.dumps(options, ensure_ascii=False)) > 64000:
        raise PluginCallError('CLI 参数格式错误或内容过大')
    if '' in options:
        if options[''] not in (None, '', [], {}):
            raise PluginCallError('options 中有未命名且带内容的参数；请使用具体 CLI 参数名，不要填写空键。本次没有执行操作')
        options = {key: value for key, value in options.items() if key != ''}
    if command[0] in _LOCAL and command != ['auth', 'status']:
        raise PluginCallError('登录、配置和 CLI 管理请在 Scout 配置中处理')
    if any(re.search(r'(^|[._+-])(delete|remove|destroy|purge|erase|trash|revoke|drop|rm|batch-delete)([._+-]|$)', part, re.I) for part in command):
        raise PluginCallError('助手不提供删除操作')
    for key, value in options.items():
        if not isinstance(key, str) or not _FLAG.fullmatch(key) or key in _BLOCKED_OPTIONS or key == 'path' or key.endswith(('-file', '-path', '-dir')):
            raise PluginCallError(f'不支持的 CLI 参数名：{key!r}；请使用具体长参数名，未执行操作')
        if key in _PAGING_OPTIONS and mode != 'read':
            raise PluginCallError(f'{key} 只用于查询命令，写操作不能使用全量/自动翻页')
        if key in _OPTIONS and isinstance(value, str) and value.lstrip().startswith('@'):
            raise PluginCallError('请直接传 JSON 内容，不能读取节点本地文件')
    return command, options, mode


def _read_command(command: list[str], options: dict) -> bool:
    if command[0] in ('help', 'schema', 'inspect') or command == ['auth', 'status'] or options.get('help') is True:
        return True
    action = command[-1]
    if action in _READ or action.startswith(('meta-', 'list-', 'get-', 'search-', 'query-', 'batch-get-', 'batch-query-')):
        return True
    # Official CLI uses +resource-action; +*-list/+*-get/+*-search are reads.
    return action.startswith('+') and action.endswith(('-list', '-get', '-search', '-query'))


def _argv(command: list[str], options: dict) -> list[str]:
    result = list(command)
    for key, value in options.items():
        if value is None:
            continue
        values = value if isinstance(value, list) and key == 'set' else [value]
        for item in values:
            text = json.dumps(item, ensure_ascii=False, separators=(',', ':')) if isinstance(item, (dict, list, bool)) else str(item)
            result.append(f'--{key}={text}')
    return result


def _native(node_id: str, plugin_id: str, command: list[str], options: dict, *, source_path: str = '') -> dict:
    root = plugin_dir('cli', plugin_id)
    extra = {}
    argv = _argv(command, options)
    if source_path:
        argv.insert(len(command), source_path)
    if plugin_id == 'feishu':
        from mino_scout.plugins import feishu_auth
        if not feishu_auth.ready():
            raise PluginCallError('请先在该 Scout 的飞书 CLI 配置中完成授权：用户扫码或应用机器人凭据至少一种可用')
        extra['LARKSUITE_CLI_CONFIG_DIR'] = str(feishu_auth.root())
        if command[0] not in ('help', 'schema', 'auth'):
            # 文档/表格等用户态命令用 user；IM 发送/会话命令必须用 bot。
            # 飞书的“自己给自己发私聊”在 user 身份下不成立，硬编码 user 会把主动推送卡死。
            identity = 'bot' if command[0] == 'im' else 'user'
            argv.append(f'--as={identity}')
    else:
        home = root / '.authorization'
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        extra.update(HOME=str(home), USERPROFILE=str(home), MEEGLE_NO_UPDATE_CHECK='1',
                     MEEGLE_AI_HANDOFF='disabled', MEEGLE_TELEMETRY='disabled')
        profile = 'mino-' + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
        argv.append('--profile=' + profile)
        host = urlsplit(plain_value('cli', 'meego', 'base_url') or 'https://project.feishu.cn').netloc
        extra['MEEGLE_HOST'] = host
    try:
        result = run_entry(root, 'invoke', argv, timeout_sec=90, node_id=node_id,
                           kind='cli', plugin_id=plugin_id, extra_env=extra)
    except HostExecError as exc:
        raise PluginCallError(str(exc)) from exc
    content = str(result.get('stdout') or '')
    if result.get('code'):
        detail = str(result.get('stderr') or content or 'CLI 未返回成功结果')
        raise PluginCallError(detail)
    try:
        data = json.loads(content)
    except ValueError:
        return _result('cli', content=content, dry_run=options.get('dry-run') is True)
    if isinstance(data, dict) and (data.get('ok') is False or data.get('error') or data.get('code', 0) not in (None, 0, '0', 200, '200')):
        raise PluginCallError(str(data.get('error') or data.get('message') or 'CLI 执行失败'))
    return _result('cli', data=data, dry_run=options.get('dry-run') is True)


def _meego_api(node_id: str, command: list[str], options: dict) -> dict | None:
    if command[0] in ('help', 'inspect'):
        return _result('open_api', data={'commands': {'project': ['list', 'get'], 'workitem': sorted(_MEEGO_API)},
                'project_options': {'list': [], 'get': ['project-keys 或 simple-names（数组）']},
                'resource': 'workitem', 'required_options': ['project-key'],
                'create_options': ['work-item-type', 'params'],
                'create_params': {'fields': [{'field_key': 'name', 'field_value': '缺陷标题'}, {'field_key': 'template', 'field_value': '模板 ID'}]},
                'note': '现有插件凭据支持这些工作项操作；其他命令需要该节点的官方 CLI 用户授权。类型、字段和模板须查询目标空间，不默认使用 bug。'})
    is_project = len(command) == 2 and command[0] == 'project' and command[1] in {'list', 'get'}
    if not is_project and (len(command) != 2 or command[0] != 'workitem' or command[1] not in _MEEGO_API):
        return None
    allowed = {'project-keys', 'simple-names', 'help', 'dry-run'} if is_project else {'project-key', 'work-item-type', 'work-item-id', 'params', 'dry-run', 'name', 'template-id', 'help'}
    if set(options) - allowed:
        raise PluginCallError('插件凭据模式不支持这些参数：' + '、'.join(sorted(set(options) - allowed)))
    if options.get('help') is True:
        return _meego_api(node_id, ['help'], {})
    project = str(options.get('project-key') or '').strip()
    item_type = str(options.get('work-item-type') or '').strip()
    if not project and not is_project:
        return _question('project-key', '要在哪个飞书项目空间处理？可以提供空间链接或 project-key；也可以先查询当前账号可访问的空间。')
    action = 'project.' + command[1] if is_project else command[1]
    prefix = '/open_api/' + quote(project, safe='')
    body = {}
    method = 'GET'
    user_key = plain_value('cli', 'meego', 'user_key')
    if is_project:
        if not user_key:
            return {'ok': False, 'source': 'open_api', 'status': 422, 'reason_code': 'meego_user_key_missing',
                    'summary': '查询 Meego 空间还缺少 User Key；插件换票成功不代表已经配置业务用户身份。',
                    'next_steps': ['在 Scout → 配置 → Meego 中填写 Meego 的 User Key，再继续当前请求；不要在聊天中发送密钥。']}
        method, path, body = 'POST', '/open_api/projects', {'user_key': user_key}
        if action == 'project.get':
            path += '/detail'
            for option, field in (('project-keys', 'project_keys'), ('simple-names', 'simple_names')):
                values = options.get(option)
                if values is not None:
                    if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value.strip() for value in values):
                        raise PluginCallError(option + ' 必须是非空字符串数组')
                    body[field] = values
            if not body.get('project_keys') and not body.get('simple_names'):
                return _question('simple-names', '请提供要查询的空间链接或别名，或先查询可访问空间列表。')
    elif action == 'meta-types':
        path = prefix + '/work_item/all-types'
    elif action == 'meta-fields':
        method, path = 'POST', prefix + '/field/all'
        if item_type:
            body['work_item_type_key'] = item_type
    elif action == 'meta-templates':
        if not item_type:
            raise PluginCallError('查询模板需要 work-item-type')
        path = prefix + '/work_item/' + quote(item_type, safe='') + '/meta'
    elif action == 'get':
        identifier = str(options.get('work-item-id') or '')
        if not identifier.isdigit() or not item_type:
            raise PluginCallError('查询工作项需要 work-item-type 和数字 work-item-id')
        method, path = 'POST', prefix + '/work_item/' + quote(item_type, safe='') + '/query'
        body = {'work_item_ids': [int(identifier)]}
    else:
        params = options.get('params') or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError as exc:
                raise PluginCallError('params 必须是 JSON 对象') from exc
        if not isinstance(params, dict) or not isinstance(params.get('fields', []), list):
            raise PluginCallError('params.fields 必须是字段列表')
        fields = list(params.get('fields') or [])
        if any(not isinstance(field, dict) or not field.get('field_key') or 'field_value' not in field for field in fields):
            raise PluginCallError('每个字段需要 field_key 和 field_value')
        name = str(options.get('name') or params.get('name') or '')
        template = options.get('template-id', params.get('template_id'))
        pairs = []
        for field in fields:
            if field['field_key'] == 'name':
                name = str(field['field_value'] or '')
            elif field['field_key'] == 'template':
                template = field['field_value']
            else:
                pairs.append(field)
        if not name.strip():
            return _question('name', '这个 Bug 的标题是什么？')
        if not item_type or not str(template or '').isdigit():
            raise PluginCallError('创建工作项需要已查询确认的类型、标题和数字模板 ID')
        method, path = 'POST', prefix + '/work_item/create'
        body = {'work_item_type_key': item_type, 'name': name.strip(), 'template_id': int(template), 'field_value_pairs': pairs}
    if options.get('dry-run') is True:
        return _result('open_api', data={'method': method, 'path': path, 'body': body}, dry_run=True)
    base = plain_value('cli', 'meego', 'base_url') or 'https://project.feishu.cn'
    try:
        token = _meego_token(base, plain_value('cli', 'meego', 'plugin_id'), secret_value(node_id, 'cli', 'meego', 'plugin_secret'))
    except PluginCallError as exc:
        return _meego_failure(action, str(exc), project=project, phase='authentication', code=getattr(exc, 'code', None))
    headers = {'X-PLUGIN-TOKEN': token}
    if user_key:
        headers['X-USER-KEY'] = user_key
    try:
        data = _json(method, base.rstrip('/') + path, headers=headers, json_body=body if method == 'POST' else None)
    except PluginCallError as exc:
        return _meego_failure(action, str(exc), project=project)
    error = data.get('error') if isinstance(data.get('error'), dict) else {}
    code = error.get('code', data.get('err_code', data.get('code', 0)))
    if code not in (None, 0, '0'):
        return _meego_failure(action, str(error.get('msg') or data.get('err_msg') or data.get('msg') or 'Meego 操作失败'), project=project, code=code)
    if 'data' not in data:
        return _meego_failure(action, 'Meego 未返回可确认的业务结果，请先查询当前状态', project=project)
    result = data.get('data')
    if action == 'create':
        if not isinstance(result, (str, int)) or isinstance(result, bool) or not str(result).isdigit() or int(result) <= 0:
            return _meego_failure(action, '创建请求已发送，但没有返回工作项 ID；请先查询，不能重复提交', project=project)
        return {**_result('open_api', data={'id': str(result), 'name': body['name']}),
                'summary': f"已创建工作项 {result}：{body['name']}", 'action': action}
    return {**_result('open_api', data=result), 'action': action}


def call(node_id: str, plugin_id: str, params: dict) -> dict:
    if plugin_id not in ('feishu', 'meego'):
        raise PluginCallError('仅支持飞书和 Meego CLI')
    if plugin_id == 'feishu':
        from mino_scout.plugins import feishu_auth
        if not feishu_auth.ready():
            _need(node_id, 'cli', plugin_id, '请先在该 Scout 下载并配置对应 CLI')
    else:
        _need(node_id, 'cli', plugin_id, '请先在该 Scout 下载并配置对应 CLI')
    try:
        command, options, mode = _arguments(params)
    except PluginCallError as exc:
        return {'ok': False, 'status': 422, 'reason_code': 'cli_invalid_arguments',
                'summary': str(exc), 'next_steps': ['检查 command 只包含命令名称，options 使用非空参数名；本次没有执行操作。']}
    if mode == 'read' and not _read_command(command, options):
        raise PluginCallError('此命令不是已识别的查询，请通过 cli.execute 执行写操作')
    if params.get('attachment_ref') or params.get('attachments'):
        from mino_scout.plugins import cli_attachments
        try:
            if mode != 'write' or plugin_id != 'meego':
                raise ValueError('当前受控附件上传仅支持 Meego 写操作')
            origin = params.get('attachment_origin') or {}
            if params.get('attachment_ref') and not params.get('attachments') and command == ['attachment', '+upload']:
                return cli_attachments.upload(node_id, cli_attachments.prepare(params['attachment_ref'], options, origin), options)
            if params.get('attachments') and not params.get('attachment_ref') and command == ['workitem', 'create']:
                return cli_attachments.create(node_id, options, params['attachments'], origin)
            raise ValueError('附件引用仅用于 attachment +upload 或带附件的 workitem create')
        except (ValueError, TypeError, KeyError, PermissionError) as exc:
            return cli_attachments.failure(str(exc))
    if command == ['attachment', '+upload'] and options.get('help') is not True:
        return {'ok': False, 'status': 422, 'summary': '上传必须使用会话提供的 attachment_ref，不能传本地路径。'}
    if plugin_id == 'meego' and plain_value('cli', 'meego', 'plugin_id'):
        try:
            response = _meego_api(node_id, command, options)
        except PluginCallError as exc:
            # 此处只剩请求发出前的本地参数校验，不能当成写入结果未知。
            return {'ok': False, 'source': 'open_api', 'status': 422,
                    'reason_code': 'meego_invalid_arguments', 'summary': str(exc),
                    'next_steps': ['先查询目标空间的工作项类型、字段和模板，再补齐创建参数。']}
        if response is not None:
            return response
    return _native(node_id, plugin_id, command, options)
