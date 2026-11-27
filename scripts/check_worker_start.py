#!/usr/bin/env python3
"""Check the actual worker submit path; all records live in a disposable directory."""
import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def _fail_before_worker(*args):
    raise SystemExit(77)


def failure_receipt():
    import os
    import tempfile
    from dataclasses import asdict
    from types import SimpleNamespace
    from unittest.mock import patch
    from mino_scout.runtime.records import RecordStore
    from mino_scout.runtime.worker_probe import _wait
    from mino_scout.server_comm import script_records as records, protocol as P
    context = multiprocessing.get_context('spawn')
    def process(**kwargs):
        return context.Process(**{**kwargs, 'target': _fail_before_worker})
    replacement = SimpleNamespace(Event=context.Event, Pipe=context.Pipe, Process=process)
    with tempfile.TemporaryDirectory(prefix='worker-start-failure-') as tmp:
        root = Path(tmp) / 'source'; root.mkdir()
        platform = 'win' if os.name == 'nt' else 'mac'
        (root / 'config.yaml').write_text(f'apps:\n  fixture:\n    platform: {platform}\n')
        store = RecordStore(Path(tmp) / 'records')
        core = SimpleNamespace(node_id='fixture', binding_guard=SimpleNamespace(process_generation='fixture'))
        manager = records.ScriptRecords(core, store, 'fixture', 'fixture')
        payload = P.ScriptRun(run_id='failure', task_id='failure', session_id='failure', attempt_id='failure',
            case_id='fixture', sn='fixture', platform=platform, repo_url='', owner_user_id='fixture',
            execution_kind='debug', workspace=str(root))
        request = P.Execute(run_id='failure', step_idx=0, sn='fixture', capability_id='script.submit',
            platform=platform, params=asdict(payload), device_hint={'app_key': 'fixture'})
        try:
            with patch.object(records.multiprocessing, 'get_context', return_value=replacement):
                manager.submit(request)
            result = _wait(lambda: store.status('failure', 'failure'), lambda row: row['sealed'])
            assert result['reason_code'] == 'worker_start_failed', result
            assert result['execution_started'] == 'no' and result['failure_stage'] == 'admission', result
            assert result['resource_isolated'] is False and result['started_at'] is None, result
            assert 'exitcode=77' in result['summary'], result
            print('PASS: pre-execution worker exit reports startup failure, not unknown device write')
        finally:
            manager.close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    from mino_scout.runtime.worker_probe import run
    result = run()
    failure_receipt()
    raise SystemExit(result)
