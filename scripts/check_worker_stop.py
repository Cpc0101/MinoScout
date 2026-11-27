#!/usr/bin/env python3
"""Disposable native worker/browser shutdown smoke; never touches target apps."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mino_scout.runtime.records import RecordStore
from mino_scout.runtime.worker_stop import WorkerTree, stopped
from mino_scout.server_comm.script_records import ScriptRecords
from mino_scout.platform.web.web_v5 import WebV5Runtime


def fixture(pipe, directory, browser, callback, launch):
    assert launch.wait(15), 'worker launch containment timed out'
    if os.name != 'nt':
        os.setsid()
    if browser:
        from mino_scout.platform.web.playwright_hub import apply_browsers_path
        from playwright.sync_api import sync_playwright
        apply_browsers_path()
        pw = sync_playwright().start()
        chromium = pw.chromium.launch(headless=True)
        page = chromium.new_page()
        page.set_content('<button>Local shutdown fixture</button>')
        assert page.title() == ''
    else:
        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    pipe.send({'kind': 'ready'})
    if callback:
        pipe.send({'kind': 'call', 'request': {'run_id': 'task', 'step_idx': 1,
            'sn': 'web-fixture', 'capability_id': 'fixture'}, 'timeout': 30, 'callback_request_id': 'fixture-call'})
    while True:
        Path(directory, 'writer').write_text(str(time.monotonic()))
        time.sleep(0.05)


def run(directory, browser=False, timeout=False):
    ctx = mp.get_context('spawn')
    parent, child = ctx.Pipe()
    callback = '--callback' in sys.argv
    gate = threading.Event()
    called = threading.Event()
    launch = ctx.Event()
    proc = ctx.Process(target=fixture, args=(child, str(directory), browser, callback, launch))
    proc.start()
    child.close()
    tree = WorkerTree(proc.pid, contain=True)
    launch.set()
    try:
        assert parent.poll(30), 'native fixture startup timed out'
        assert parent.recv()['kind'] == 'ready'
        tree.capture()
        assert len(tree.identities) >= 2, 'fixture must own a runtime child'
        identities = list(tree.identities.values())
        live_proof = {'contract': 'worker_process_tree.v1', 'worker_pid': proc.pid,
                      'processes': identities, 'inspection_complete': True,
                      'process_tree_stopped': True, 'process_group': tree.group, 'observed_at': time.time()}
        assert not stopped(live_proof), 'live writer accepted as stopped'
        runtime = WebV5Runtime(lanes=1)
        assert runtime.claim_external_state('attempt', run_id='task', kind='script') == 'created'
        store = RecordStore(directory / 'records')
        meta = dict(run_id='task', session_id='session', case_id='fixture', owner_user_id='owner',
                    node_id='node', sn='web-fixture', platform='web', execution_kind='case',
                    execution_started='yes', scout_process_generation='generation')
        store.create('task', 'attempt', meta, 'hash')
        store.start('task', 'attempt')
        core = SimpleNamespace(node_id='node', _web_v5=runtime,
                               binding_guard=SimpleNamespace(process_generation='generation'))
        def slow_callback(request, timeout):
            called.set()
            gate.wait(30)
            return None
        core.nexus_call = slow_callback
        manager = ScriptRecords(core, store, 'owner', 'profile')
        # Initialization normally precedes accepting work; restore the fixture
        # after recover() deliberately seals an existing running attempt.
        store = RecordStore(directory / 'active')
        store.create('task', 'attempt', meta, 'hash')
        store.start('task', 'attempt')
        manager.store = store
        stop = ctx.Event()
        if not timeout:
            stop.set()
        job = dict(task_id='task', attempt_id='attempt', run_id='task', sn='web-fixture', platform='web',
                   execution_kind='case', stop=stop, process=proc, tree=tree, lane_key='attempt', claim_state='created')
        manager.jobs['attempt'] = job
        began = time.monotonic()
        manager._monitor(job, parent, 0.1 if timeout else 30)
        if callback:
            assert called.is_set(), 'callback fixture was not dispatched'
            assert time.monotonic() - began < 12, 'callback blocked cancellation monitor'
        result = store.status('task', 'attempt')
        assert result['sealed'] and result['resource_isolated']
        assert result['execution_started'] == 'unknown' and result.get('browser_closed') is not True
        assert result['reason_code'] == ('script_timeout' if timeout else 'cancelled'), result
        assert not proc.is_alive()
        assert runtime.capacity()['lanes_free'] == 1, runtime.capacity()
        proof = manager.resource_status('task', 'attempt')
        assert proof['resource_stop']['stop_confirmed'] is True, proof
        assert proof['resource_stop']['mode'] == 'terminated_attempt', proof
        assert stopped(proof['resource_stop']['process_tree_stop'])
        assert store.status('task', 'attempt')['summary_version'] == result['summary_version']
        # A quarantined exact claim can be repaired by fresh stop verification.
        assert runtime.claim_external_state('attempt', run_id='task', kind='script') == 'created'
        runtime.quarantine_external('attempt')
        assert manager.resource_status('task', 'attempt')['resource_stop']['stop_confirmed']
        assert runtime.claim_external_state('next', run_id='next', kind='script') == 'created'
        runtime.release_external('next')
        first = Path(directory, 'writer').read_text()
        time.sleep(0.15)
        assert Path(directory, 'writer').read_text() == first, 'old writer continued after stop'
        invalid = dict(proof['resource_stop']['process_tree_stop'], worker_pid=os.getpid())
        assert not stopped(invalid)
        invalid = dict(proof['resource_stop']['process_tree_stop'], inspection_complete=False)
        assert not stopped(invalid)
        invalid = dict(proof['resource_stop']['process_tree_stop'], observed_at=10 ** 400)
        assert not stopped(invalid)
        manager.jobs['attempt'] = job
        assert not manager.resource_status('task', 'attempt')['resource_stop']['stop_confirmed']
        manager.jobs.clear()
        if '--wire-output' in sys.argv:
            output = Path(sys.argv[sys.argv.index('--wire-output') + 1])
            output.write_text(json.dumps({'summary': result, 'proof': proof,
                'events': store.events('task', 'attempt', limit=200)}))
        print('PASS:', 'chromium' if browser else 'native child', 'timeout' if timeout else 'cancel',
              'verified process stop, immutable UNKNOWN, same-generation recovery, next claim')
    finally:
        gate.set()
        if proc.is_alive():
            tree.terminate(proc)
        parent.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='scout-worker-stop-') as tmp:
        for index, timeout in enumerate((False, True)):
            path = Path(tmp) / str(index)
            path.mkdir()
            run(path, browser='--browser' in sys.argv, timeout=timeout)
