from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import zipfile

from mino_scout.plugins import progress as PP


def chromium_root():
    from mino_scout.platform.web.playwright_hub import browsers_dir
    return Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or browsers_dir())


def chromium_ready():
    from playwright._impl._driver import compute_driver_executable
    node, cli = compute_driver_executable()
    result = subprocess.run([str(node), str(cli), 'install', '--dry-run', 'chromium'],
        capture_output=True, text=True, timeout=20, env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(chromium_root())})
    if result.returncode:
        return False
    paths = re.findall(r'Install location:\s*(.+)', result.stdout)
    return bool(paths) and all(Path(path.strip()).is_dir() for path in paths)


def chromium(action):
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    node, cli = compute_driver_executable()
    PP.emit('install', kind='tool', plugin_id='chromium', label='下载与当前 Scout 匹配的 Chromium', percent=15)
    env = {**get_driver_env(), 'PLAYWRIGHT_BROWSERS_PATH': str(chromium_root()), 'PLAYWRIGHT_SKIP_BROWSER_GC': '1'}
    result = subprocess.run([str(node), str(cli), 'install', 'chromium'], env=env, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        raise RuntimeError('Chromium 下载失败：' + (result.stderr or result.stdout)[-500:])
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(chromium_root()))
    if not chromium_ready():
        raise RuntimeError('Chromium 安装后未找到完整浏览器资源')
    PP.emit('done', kind='tool', plugin_id='chromium', label='Chromium 已匹配当前 Scout', percent=100, done=True)
    return 'Chromium 已匹配当前 Scout；浏览器版本随 Scout 运行时更新'


def adb_keyboard(action):
    from mino_scout.platform.android.adb_ime import cached_apk_path, apk_download_url, DEFAULT_APK_URL, resolve_local_apk
    from mino_scout.tools.update.http_fetch import fetch_json, fetch_bytes
    from mino_scout.config import config_dir
    if action == 'install' and resolve_local_apk() is not None:
        return '本机已有可用 APK，无需重复下载；设备安装状态单独显示'
    PP.emit('download', kind='tool', plugin_id='adb.keyboard', label='读取 ADB Keyboard 固定版本', percent=10)
    source = apk_download_url()
    if source == DEFAULT_APK_URL:
        metadata = fetch_json('https://api.github.com/repos/senzhk/ADBKeyboard/contents/ADBKeyboard.apk')
        blob_id = str(metadata.get('sha') or '')
        if not re.fullmatch(r'[0-9a-f]{40}', blob_id):
            raise ValueError('ADB Keyboard 发布对象无效')
        blob = fetch_json('https://api.github.com/repos/senzhk/ADBKeyboard/git/blobs/' + blob_id)
        if blob.get('encoding') != 'base64':
            raise ValueError('ADB Keyboard 发布对象编码无效')
        payload = base64.b64decode(blob['content'])
        if hashlib.sha1(b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest() != blob_id:
            raise ValueError('ADB Keyboard 固定对象校验失败')
        version = blob_id[:12]
    else:
        expected = str(os.environ.get('MINO_SCOUT_ADB_KEYBOARD_APK_SHA256') or '').lower()
        if not source.startswith('https://') or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('自定义 APK 发布源需要 HTTPS 地址及 MINO_SCOUT_ADB_KEYBOARD_APK_SHA256')
        payload = fetch_bytes(source)
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError('ADB Keyboard SHA256 不匹配')
        version = expected[:12]
    destination = cached_apk_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or destination.exists() and destination.stat().st_nlink != 1:
        raise ValueError('APK 缓存不能是外部链接')
    temporary = destination.with_name('.' + destination.name + '.' + uuid.uuid4().hex)
    temporary.write_bytes(payload)
    try:
        with zipfile.ZipFile(temporary) as archive:
            if 'AndroidManifest.xml' not in archive.namelist():
                raise ValueError('发布文件不是 Android APK')
        if destination.exists():
            import shutil
            shutil.copy2(destination, destination.with_name(destination.name + '.previous-' + uuid.uuid4().hex))
        os.replace(temporary, destination)
        marker = config_dir() / 'cache/adb-keyboard.json'
        temp_marker = marker.with_name('.' + marker.name + '.' + uuid.uuid4().hex)
        temp_marker.write_text(json.dumps({'version': version, 'sha256': hashlib.sha256(payload).hexdigest()}))
        os.replace(temp_marker, marker)
    finally:
        temporary.unlink(missing_ok=True)
    PP.emit('done', kind='tool', plugin_id='adb.keyboard', label='ADB Keyboard 电脑缓存已更新', percent=100, done=True)
    return 'ADB Keyboard 电脑缓存已更新；设备安装状态单独显示'


def install(node_id, tool_id, action, lock):
    if tool_id == 'chromium':
        return chromium(action)
    if tool_id == 'adb.keyboard':
        return adb_keyboard(action)
    if tool_id == 'appium':
        from mino_scout.tools.update.channel_packages import install_appium
        return install_appium(action)
    if tool_id == 'langbot':
        raise ValueError('该组件已移除')
    from mino_scout.plugins.install import install as install_plugin
    from mino_scout.plugins.state import installed
    kind, plugin_id = 'cli', tool_id.split('.')[1]
    if action == 'install' and installed(kind, plugin_id):
        from mino_scout.plugins.state import release_import_block

        release_import_block(kind, plugin_id)
        return '组件已安装，原配置和授权已保留'
    return install_plugin(node_id, kind, plugin_id, _held_lock=lock, update=action == 'update')
