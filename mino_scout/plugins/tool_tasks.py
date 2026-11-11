"""Durable, account-scoped CLI installation receipts. Never replay on restart."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mino_scout.plugins.paths import data_dir, profile_id

_PROCESS = uuid.uuid4().hex
_GATE = threading.RLock()
_TERMINAL = {'succeeded', 'failed', 'interrupted'}
_LOCAL = threading.local()
_INSTALL_QUEUE = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plugin-install")


def _db(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'tool_tasks.sqlite3', timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS tool_tasks (task_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL, process TEXT NOT NULL, active_tool TEXT UNIQUE, payload TEXT NOT NULL)')
    return db


def _read(db, task_id):
    row = db.execute('SELECT process, payload FROM tool_tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return None
    body = json.loads(row[1])
    if row[0] != _PROCESS and body['state'] not in _TERMINAL:
        body.update(state='interrupted', active=False, error='Scout 进程已重启，安装结果未确认；不会自动重装', updated_at=time.time())
        _write(db, body)
    return body


def _write(db, body):
    db.execute('INSERT INTO tool_tasks VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET active_tool=excluded.active_tool,payload=excluded.payload',
               (body['task_id'], body['tool_id'], _PROCESS, None if body['state'] in _TERMINAL else body['tool_id'], json.dumps(body, ensure_ascii=False)))
    db.commit()


def get(task_id: str):
    _validate_id(task_id)
    with _GATE, _db(data_dir()) as db:
        return _read(db, task_id)


def _validate_id(task_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', task_id):
        raise ValueError('invalid_task_id')


def record_progress(payload):
    context = getattr(_LOCAL, 'task', None)
    if not context:
        return
    root, task_id = context
    payload['task_id'] = task_id
    with _GATE, _db(root) as db:
        body = _read(db, task_id)
        if body and body['state'] not in _TERMINAL:
            if payload['stage'] != 'done':
                body['last_stage'] = payload['stage']
            body.update({k: payload[k] for k in ('stage', 'label', 'percent', 'bytes_received', 'bytes_total')})
            body['updated_at'] = time.time()
            _write(db, body)


def has_active():
    with _GATE, _db(data_dir()) as db:
        for (task_id,) in db.execute('SELECT task_id FROM tool_tasks WHERE active_tool IS NOT NULL').fetchall():
            task = _read(db, task_id)
            if task and task['state'] not in _TERMINAL:
                return True
    return False


def submit(node_id: str, task_id: str, tool_id: str, action: str):
    from mino_scout.plugins.install import acquire_plugin
    from mino_scout.plugins.components import install
    _validate_id(task_id)
    if tool_id not in ('cli.feishu', 'cli.meego', 'chromium', 'adb.keyboard', 'appium') or action not in ('install', 'update'):
        raise ValueError('unsupported_tool_action')
    profile = profile_id()
    if not profile:
        raise ValueError('plugin_owner_unverified')
    root = data_dir()
    with _GATE, _db(root) as db:
        old = _read(db, task_id)
        if old:
            if old['tool_id'] != tool_id or old['action'] != action:
                raise ValueError('task_parameters_conflict')
            return old
        for (old_id,) in db.execute('SELECT task_id FROM tool_tasks WHERE active_tool IS NOT NULL').fetchall():
            _read(db, old_id)
        kind, plugin_id = ('tool', tool_id) if tool_id in ('chromium', 'adb.keyboard', 'appium') else ('cli', tool_id.split('.')[1])
        lock = acquire_plugin(kind, plugin_id)
        body = dict(task_id=task_id, tool_id=tool_id, action=action, state='pending', active=True,
                    stage='pending', label='已排队，等待前一项安装结束', error='', created_at=time.time(), updated_at=time.time())
        try:
            if db.execute('SELECT 1 FROM tool_tasks WHERE active_tool=?', (tool_id,)).fetchone():
                raise ValueError('tool_busy')
            _write(db, body)
        except Exception:
            lock.release()
            raise

        def worker():
            _LOCAL.task = (root, task_id)
            try:
                if profile_id() != profile:
                    raise ValueError('plugin_owner_changed')
                with _GATE, _db(root) as worker_db:
                    body.update(state='running', label='正在更新' if action == 'update' else '正在安装', updated_at=time.time())
                    _write(worker_db, body)
                message = install(node_id, tool_id, action, lock)
                final = dict(state='succeeded', label=message or ('更新完成' if action == 'update' else '安装完成'), error='')
            except Exception as exc:
                # Do not persist subprocess output, URLs or credentials in public receipts.
                from mino_scout.tools.keep_alive.log import SLog
                SLog.e('ToolTask', f'{task_id}: {exc}')
                with _GATE, _db(root) as failed_db:
                    failed = _read(failed_db, task_id) or {}
                stage = str(failed.get('last_stage') or failed.get('stage') or '准备')
                final = dict(state='failed', label='更新失败' if action == 'update' else '安装失败', error=f'{stage} 阶段失败（{type(exc).__name__}），请查看节点安装日志中的任务 {task_id}')
            finally:
                if lock.locked():
                    lock.release()
                _LOCAL.task = None
            with _GATE, _db(root) as worker_db:
                current = _read(worker_db, task_id) or body
                current.update(final, active=False, updated_at=time.time())
                _write(worker_db, current)

        try:
            _INSTALL_QUEUE.submit(worker)
        except Exception:
            body.update(state='failed', active=False, error='安装线程启动失败')
            _write(db, body)
            lock.release()
            raise
        return dict(body)
