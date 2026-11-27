"""Disposable submit/spawn/debug-fragment check with no network or device actions."""
from __future__ import annotations

import gc
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from mino_scout.runtime.records import RecordStore
from mino_scout.server_comm import protocol as P
from mino_scout.server_comm.script_records import ScriptRecords


def _wait(read, ready, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gc.collect()
        value = read()
        if ready(value):
            return value
        time.sleep(0.05)
    raise RuntimeError(f"worker_probe_timeout: {value}")


def run() -> int:
    with tempfile.TemporaryDirectory(prefix='mino-worker-probe-') as tmp:
        base = Path(tmp)
        old_home = os.environ.get('MINO_SCOUT_HOME')
        old_dev_home = os.environ.get('MINO_SCOUT_DEV_HOME')
        os.environ['MINO_SCOUT_HOME'] = str(base / 'node')
        os.environ['MINO_SCOUT_DEV_HOME'] = str(base / 'dev-node')
        try:
            root = base / 'authoring'
            root.mkdir()
            platform = 'win' if os.name == 'nt' else 'mac'
            (root / 'config.yaml').write_text(f'apps:\n  fixture:\n    platform: {platform}\n    package: worker-probe\n')
            store = RecordStore(base / 'execution')
            def callback(request, timeout):
                if request.capability_id != 'script.lease':
                    raise RuntimeError('worker_probe_unexpected_callback')
                return P.Result(run_id=request.run_id, status=P.EventStatus.PASS, data={'occupied': True})
            core = SimpleNamespace(node_id='worker-probe', nexus_call=callback,
                binding_guard=SimpleNamespace(process_generation='probe-generation'))
            manager = ScriptRecords(core, store, 'fixture-owner', 'fixture-profile')
            try:
                for index in range(3):
                    ident = f'worker-probe-{index}'
                    payload = P.ScriptRun(run_id=ident, task_id=ident, session_id=ident, attempt_id=ident,
                        case_id='probe', sn='probe-device', platform=platform, repo_url='',
                        owner_user_id='fixture-owner', execution_kind='debug', workspace=str(root))
                    req = P.Execute(run_id=ident, step_idx=0, sn=payload.sn, platform=platform,
                        capability_id='script.submit', params=asdict(payload),
                        device_hint={'app_key': 'fixture'})
                    # submit returns before spawn unpickles: only manager-owned handles may survive.
                    manager.submit(req)
                    row = _wait(lambda: store.status(ident, ident),
                        lambda row: row.get('debug_state') == 'ready' or row.get('sealed'))
                    if row.get('sealed'):
                        raise RuntimeError(f'worker_probe_start_failed: {row}')
                    identity = {'owner_user_id': 'fixture-owner', 'task_id': ident, 'attempt_id': ident}
                    for fragment in range(2):
                        request = f'fragment-{fragment}'
                        code = f"assert {fragment}+1 == {fragment+1}\ncase.nexus.note('worker_probe_fragment', index={fragment})"
                        manager.read('debug.execute', {**identity, 'client_request_id': request, 'code': code})
                        command = _wait(lambda: manager.read('debug.status', {**identity, 'client_request_id': request})['command'],
                            lambda value: value and value['state'] == 'completed')
                        if command['result']['status'] != 'completed':
                            raise RuntimeError(f'worker_probe_fragment_failed: {command}')
                    manager.cancel_run(ident)
                    _wait(lambda: store.status(ident, ident), lambda value: value.get('sealed'))
                    _wait(manager.active, lambda value: not value)
                    events = store.events(ident, ident, limit=200)['items']
                    notes = [event for event in events if event['type'] == 'script/note']
                    if len(notes) != 2 or any(event['type'] == 'tool/call' for event in events):
                        raise RuntimeError('worker_probe_evidence_invalid')
                from mino_scout.runtime.otp_probe import run as otp_probe
                otp_probe()
                from mino_scout.runtime.debug_probe import run as debug_probe
                debug_probe()
                print('PASS: real ScriptRecords.submit -> spawn -> 6 retained debug fragments -> close; forced GC; no network/device actions')
                return 0
            finally:
                manager.close()
        finally:
            if old_dev_home is None:
                os.environ.pop('MINO_SCOUT_DEV_HOME', None)
            else:
                os.environ['MINO_SCOUT_DEV_HOME'] = old_dev_home
            if old_home is None:
                os.environ.pop('MINO_SCOUT_HOME', None)
            else:
                os.environ['MINO_SCOUT_HOME'] = old_home
