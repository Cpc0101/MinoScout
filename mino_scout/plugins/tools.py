"""Read-only facts for the download centre; no dependency preparation here."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from mino_scout.config import config_dir
from mino_scout.plugins.state import installed, configured, plugin_dir


def snapshot(core):
    from mino_scout.tools.update.app_version import runtime_version_facts
    from mino_scout.platform.android.adb_ime import resolve_local_apk
    from mino_scout.tools.update.install_plan import runtime_chromium_revision
    ready = bool(getattr(core, '_plugins_ready', False))
    versions = runtime_version_facts()
    source = versions['runtime_environment'] == 'development'
    items = [dict(tool_id='scout.core', label='Scout 核心', purpose='源码节点运行' if source else '节点运行', state='installed', version=versions['running_version'],
                  **versions, detail='源码修改后重启节点生效；运行版本和本地源码版本分别显示。' if source else '支持固定版本在线升级；下载校验后切换安装，重新连接并核对运行版本。')]
    revision = runtime_chromium_revision(config_dir() / 'bin')
    from mino_scout.platform.web.playwright_hub import browsers_dir
    root_override = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    browser_root = Path(root_override) if root_override else browsers_dir()
    if not root_override and not browser_root.is_dir():
        browser_root = Path.home() / 'Library/Caches/ms-playwright' if sys.platform == 'darwin' else Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'ms-playwright' if os.name == 'nt' else Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'ms-playwright'
    if not revision:
        try:
            import playwright
            path = Path(playwright.__file__).parent / 'driver/package/browsers.json'
            revision = next(str(r['revision']) for r in json.loads(path.read_text())['browsers'] if r['name'] == 'chromium')
        except (ImportError, OSError, ValueError, KeyError, StopIteration):
            pass
    matched = browser_root / f'chromium-{revision}'
    executable = any(p.is_file() for p in (matched / 'chrome-linux/chrome', matched / 'chrome-linux64/chrome', matched / 'chrome-win/chrome.exe', matched / 'chrome-win64/chrome.exe', matched / 'chrome-mac/Chromium.app/Contents/MacOS/Chromium', matched / 'chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium', matched / 'chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium')) if revision else False
    from mino_scout.plugins.components import chromium_root
    browser_root = chromium_root()
    matched = browser_root / f'chromium-{revision}'
    executable = executable or any(path.is_file() for path in matched.rglob('chrome')) or any(path.is_file() for path in matched.rglob('Chromium'))
    items.append(dict(tool_id='chromium', label='Chromium', purpose='Web 自动化', state='matched' if executable else 'missing' if revision else 'unknown', version=revision or None,
                      installable=ready and not executable, updatable=ready and executable, detail='安装或更新当前 Scout 运行时匹配的 Chromium；保留其他浏览器版本。'))
    apk = resolve_local_apk()
    items.append(dict(tool_id='adb.keyboard', label='ADB Keyboard', purpose='Android 文本输入', state='cached' if apk else 'missing',
                      installable=ready and not apk, updatable=ready and bool(apk), detail='电脑 APK 缓存为可选资源，设备已安装即可使用；仅给设备安装输入法时需要 APK。源码运行也不自带此第三方 APK。' + ('当前未找到 ADB，请检查 ADB 运行环境。' if not shutil.which('adb') else ''), device_state='unknown', devices=[{'sn': sn, **dict(fact)} for sn, fact in dict(getattr(core, '_adb_ime_facts', {})).items()]))
    for plugin_id, label in (('feishu', '飞书 CLI'), ('meego', 'Meego CLI')):
        present = installed('cli', plugin_id) if ready else None
        config = configured(core.node_id, 'cli', plugin_id) if ready else None
        version = None
        if present:
            try:
                marker = json.loads((plugin_dir('cli', plugin_id) / '.installed').read_text())
                version = marker.get('version') if isinstance(marker, dict) else None
            except (OSError, ValueError):
                pass  # Legacy "ok" marker does not prove a version.
        items.append(dict(tool_id=f'cli.{plugin_id}', label=label, purpose='飞书工作项接入' if plugin_id == 'feishu' else '工作项接入', state='installed' if present else 'missing' if ready else 'unknown',
                          version=version, configured=config, installable=ready and not present, updatable=ready and bool(present), config_key=f'cli:{plugin_id}',
                          detail='安装与升级保留配置和授权；消息接入及工作项功能在配置页管理。'))
    from mino_scout.tools.update.channel_packages import appium_item
    items.append(appium_item(ready))
    from mino_scout.plugins.state import _item
    for tool in items:
        key = tool['tool_id']
        if key in ('cli.feishu', 'cli.meego'):
            kind, pid = 'cli', key.split('.')[1]
            tool.update(plugin_class=kind, plugin_id=pid, removable=ready and installed(kind, pid), configured=configured(core.node_id, kind, pid) if ready else None)
            tool['required_by_config'] = bool(_item(kind, pid).get('import_blocked'))
    return dict(version=2, items=items)


def handle(core, req, cmd, event, started):
    from mino_scout.executors.base import make_event_result
    from mino_scout.schemas import EventStatus
    from mino_scout.plugins import tool_tasks
    params = dict(req.params or {})
    try:
        if cmd == 'tools':
            body = {'tools': snapshot(core)}
        elif not getattr(core, '_plugins_ready', False):
            raise ValueError('plugin_owner_unverified')
        elif cmd == 'tool_task_get':
            body = {'task': tool_tasks.get(str(params.get('task_id') or ''))}
        else:
            body = {'task': tool_tasks.submit(core.node_id, str(params.get('task_id') or ''), str(params.get('tool_id') or ''), str(params.get('action') or ''))}
        return make_event_result(event, status=EventStatus.PASS, executor_used='core', started_at=started, elapsed_ms=0, summary='工具状态', raw_response=body)
    except (ValueError, OSError, RuntimeError) as exc:
        return make_event_result(event, status=EventStatus.FAIL, executor_used='core', started_at=started, elapsed_ms=0, summary=str(exc), error=str(exc))
