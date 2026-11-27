#!/usr/bin/env python3
"""Exercise real spawn, fragment clocks, cancellation and isolated outputs off-target."""
import multiprocessing
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from mino_scout.runtime.records import RecordStore
from mino_scout.runtime import debug_commands
from mino_scout.runtime.worker_probe import _wait
from mino_scout.server_comm.script_records import ScriptRecords
from mino_scout.server_comm import protocol as P


def run():
    with tempfile.TemporaryDirectory(prefix='debug-lifecycle-') as tmp:
        base = Path(tmp)
        saved = {key: os.environ.get(key) for key in ('MINO_SCOUT_HOME', 'MINO_SCOUT_DEV_HOME')}
        for key in saved:
            os.environ[key] = str(base / key)
        root = base / 'source'; root.mkdir()
        platform = 'win' if os.name == 'nt' else 'mac'
        (root / 'config.yaml').write_text(f'apps:\n  fixture:\n    platform: {platform}\n')
        def callback(request, timeout):
            assert request.capability_id == 'script.lease', request.capability_id
            return P.Result(run_id=request.run_id, status=P.EventStatus.PASS, data={'occupied': True})
        store = RecordStore(base / 'records')
        core = SimpleNamespace(node_id='fixture', nexus_call=callback,
            binding_guard=SimpleNamespace(process_generation='fixture'))
        manager = ScriptRecords(core, store, 'owner', 'fixture')
        def start(ident):
            payload = P.ScriptRun(run_id=ident, task_id=ident, attempt_id=ident, session_id=ident,
                case_id='debug', sn='fixture', platform=platform, repo_url='', owner_user_id='owner',
                execution_kind='debug', workspace=str(root))
            manager.submit(P.Execute(run_id=ident, sn='fixture', platform=platform, step_idx=0,
                capability_id='script.submit', params=asdict(payload), device_hint={'app_key': 'fixture'}))
            _wait(lambda: store.status(ident, ident), lambda x: x.get('debug_state') == 'ready')
            return dict(owner_user_id='owner', task_id=ident, attempt_id=ident)
        def read(identity, request=''):
            return manager.read('debug.status', {**identity, 'client_request_id': request})
        def submit(identity, request, code, timeout=10):
            return manager.read('debug.execute', {**identity, 'client_request_id': request,
                'code': code, 'timeout_sec': timeout})
        try:
            identity = start('clocks')
            with store._db('clocks') as db:
                db.execute('UPDATE attempts SET started_at=?', (time.time() - 600,))
            for request in ('first', 'second'):
                submit(identity, request, "from pathlib import Path\nPath('same-name.txt').write_text('" + request + "')\n(case.output_dir / 'sdk-output.txt').write_text('" + request + "')")
                done = _wait(lambda: read(identity, request), lambda x: x['command']['state'] == 'completed')
                assert done['command']['result']['status'] == 'completed', done
                assert done['session_age_ms'] >= 600000 and done['command']['elapsed_ms'] < 10000, done
                assert done['session_state'] == 'ready' and done['active_command'] is None, done
                assert done['execution_started'] == 'yes', done
                assert Path(done['output_workspace'], 'same-name.txt').read_text() == request
                assert Path(done['output_workspace'], 'sdk-output.txt').read_text() == request
            first_path = Path(done['output_workspace']).parent / 'first' / 'same-name.txt'
            assert first_path.read_text() == 'first'
            assert (first_path.parent / 'sdk-output.txt').read_text() == 'first'
            submit(identity, 'block', 'import time\ntime.sleep(60)', timeout=1)
            current = _wait(lambda: read(identity, 'first'), lambda x: x['active_command'] is not None)
            assert current['command']['request_id'] == 'first'
            assert current['active_command']['request_id'] == 'block'
            sealed = _wait(lambda: read(identity, 'block'), lambda x: x['sealed'])
            assert sealed['reason_code'] == 'script_timeout', sealed
            assert sealed['command']['result']['reason_code'] == 'script_timeout', sealed
            assert sealed['command']['elapsed_ms'] < 15000, sealed
            assert sealed['resource_isolated'] is True, sealed
            identity = start('cancellation')
            submit(identity, 'block', "import time\nfrom pathlib import Path\nPath('sleep-entered').write_text('ready'); time.sleep(60)", timeout=30)
            _wait(lambda: read(identity), lambda x: x['command']['state'] == 'running' and Path(x['output_workspace'], 'sleep-entered').is_file())
            manager.read('debug.cancel', {**identity, 'client_request_id': 'block'})
            sealed = _wait(lambda: read(identity), lambda x: x['sealed'])
            assert sealed['reason_code'] == 'cancelled', sealed
            assert sealed['command']['result']['status'] == 'cancelled', sealed
            assert sealed['command']['result']['reason_code'] == 'cancelled', sealed
            # A queued cancellation is settled in the queue transaction, never taken by a worker.
            store.create('queue', 'queue', dict(execution_kind='debug', owner_user_id='owner'), 'fixture')
            store.start('queue', 'queue')
            debug_commands.submit(store, 'queue', 'queue', dict(client_request_id='cancel-before-start', code='raise RuntimeError()', timeout_sec=1))
            cancelled = debug_commands.cancel(store, 'queue', 'queue', 'cancel-before-start')
            assert cancelled['started_at'] is None and cancelled['result']['execution_started'] == 'no', cancelled
            assert debug_commands.take(store, 'queue', 'queue') is None
            print('PASS: session vs fragment clocks; historical request vs active command; output isolation; hard timeout; running/queued cancellation; retained quarantine')
        finally:
            manager.close()
            for key, value in saved.items():
                if value is None: os.environ.pop(key, None)
                else: os.environ[key] = value
