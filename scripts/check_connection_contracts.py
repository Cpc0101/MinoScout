#!/usr/bin/env python3
"""Offline connection/summary contracts in disposable records; no network or devices."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mino_scout.runtime import script_exec as runtime
from mino_scout.runtime.records import RecordStore
from mino_scout.server_comm import nexus_link, protocol as P
from mino_scout.server_comm.transport.node import NodeTransport


def admission():
    events = []
    connected = threading.Event()
    case = SimpleNamespace(core=SimpleNamespace(_record_connection=connected), _seq=0,
        _debug_write_error=None, _latched_error=None, _recorder=SimpleNamespace(append=lambda *v: events.append(v)),
        _source_ref='', _source_hash='', _step_id='', _role='do')
    app = object.__new__(runtime.App)
    app._case, app.key = case, 'offline'
    app._web_identity = lambda: {}
    app._guard = lambda: None
    connected.set()
    app._begin_action('tap', {})
    assert len(events) == 1 and events[0][0] == 'tool/call'
    connected.clear()
    events.clear()
    timer = threading.Timer(0.15, connected.set)
    timer.start()
    try:
        app._begin_action('tap', {})
    except runtime.ScriptAbort as exc:
        assert exc.reason_code == 'node_connection_revalidation_required'
        assert exc.details['execution_started'] == 'no'
    else:
        raise AssertionError('reconnected event bypassed binding revalidation')
    timer.join()
    assert not events
    case._latched_error = None
    # Disconnection/reconnection between actions must not erase the binding fence.
    from mino_scout.server_comm.script_records import ScriptRecords
    changed = threading.Event()
    manager = object.__new__(ScriptRecords)
    manager.lock = threading.Lock()
    manager.jobs = {'attempt': {'connected': connected, 'connection_changed': changed}}
    manager.connection(False)
    manager.connection(True)
    case.core._record_connection_changed = changed
    try:
        app._begin_action('tap', {})
    except runtime.ScriptAbort as exc:
        assert exc.reason_code == 'node_connection_revalidation_required'
    else:
        raise AssertionError('connection changed between actions was missed')
    assert not events
    case._latched_error = None
    try:
        app._guard_write_connection()
    except runtime.ScriptAbort as exc:
        assert exc.reason_code == 'node_connection_revalidation_required'
    else:
        raise AssertionError('native write boundary ignored connection fence')
    case._latched_error = None
    del case.core._record_connection_changed
    connected.clear()
    # Advance only the admission clock; no real minute-long sleep.
    with patch.object(runtime.time, 'monotonic', side_effect=[0, 61]):
        try:
            app._begin_action('tap', {})
        except runtime.ScriptAbort as exc:
            assert exc.reason_code == 'node_disconnected' and exc.details['execution_started'] == 'no'
        else:
            raise AssertionError('disconnected write admitted')
    assert not events
    for code in ('cancelled', 'script_timeout'):
        def guard():
            raise runtime.ScriptHalt(code, reason_code=code)
        app._guard = guard
        try:
            app._begin_action('tap', {})
        except runtime.ScriptHalt as exc:
            assert exc.reason_code == code
        else:
            raise AssertionError('halt ignored')
    app._guard = lambda: None
    case._latched_error = runtime.ScriptAbort('unknown write', reason_code='write_unknown', status='blocked')
    connected.set()
    try:
        app._begin_action('tap', {})
    except runtime.ScriptAbort as exc:
        assert exc is case._latched_error
    else:
        raise AssertionError('unknown write latch lost')
    assert not events


def callbacks():
    case = SimpleNamespace(core=SimpleNamespace(), run_id='run', session_id='session',
                           _sn='offline', _platform='android', _recorder=None)
    for code in nexus_link.NexusCallbackError.CODES:
        calls = []
        def ask(*args):
            calls.append(args)
            raise nexus_link.NexusCallbackError(code)
        case.core.nexus_call = ask
        try:
            nexus_link.call(case, 'script.callback', {'op': 'account'}, 1)
        except runtime.ScriptAbort as exc:
            assert exc.reason_code == code and exc.status == 'blocked'
        else:
            raise AssertionError('callback error lost')
        assert len(calls) == 1
    for reply, code in [(None, 'nexus_unreachable'), (object(), 'nexus_invalid_reply'),
                        (P.Result(run_id='run', status='blocked', summary='denied', error='denied'), 'nexus_rejected')]:
        case.core.nexus_call = lambda *args: reply
        try:
            nexus_link.call(case, 'script.callback', {}, 1)
        except runtime.ScriptAbort as exc:
            assert exc.reason_code == code
        else:
            raise AssertionError('bad reply accepted')
    case.core.nexus_call = lambda *args: P.Result(run_id='run', status='pass', data={'value': 1}, extra={'data': {'value': 0, 'other': 2}})
    assert nexus_link.call(case, 'script.callback', {}, 1) == {'value': 1, 'other': 2}
    def never_send(*args):
        raise AssertionError('invalid budget was sent')
    case.core.nexus_call = never_send
    for budget in (float('nan'), float('inf'), float('-inf'), 0, -1, True, None, 'bad', 10**400):
        try:
            nexus_link.call(case, 'script.callback', {}, budget)
        except runtime.ScriptAbort as exc:
            assert exc.reason_code == 'nexus_invalid_budget'
        else:
            raise AssertionError('invalid budget accepted')
    for reply in (P.Result(run_id='other', status='pass'),
                  P.Result(run_id='run', step_idx=0, status='pass'),
                  P.Result(run_id='run', status='invalid'),
                  P.Result(run_id='run', status='pass', data=[])):
        case.core.nexus_call = lambda *args: reply
        try:
            nexus_link.call(case, 'script.callback', {}, 1)
        except runtime.ScriptAbort as exc:
            assert exc.reason_code in {'nexus_reply_identity_mismatch', 'nexus_invalid_reply'}
        else:
            raise AssertionError('wrong RESULT accepted')


def pipe_callbacks():
    from dataclasses import asdict
    class Pipe:
        def send(self, message):
            self.sent = message
            self.messages = [
                {'callback_request_id': 'late', 'reply': asdict(P.Result(run_id='run', status='pass', data={'late': True}))},
                {'callback_request_id': message['callback_request_id'],
                 'reply': asdict(P.Result(run_id='run', status='pass', data={'current': True}))}]
        def poll(self, timeout):
            return bool(self.messages)
        def recv(self):
            return self.messages.pop(0)
    pipe = Pipe()
    request = P.Execute(run_id='run', step_idx=-1, sn='offline', capability_id='script.callback')
    assert nexus_link.pipe_call(pipe, request, 1).data == {'current': True}
    assert pipe.sent['timeout'] == 1


async def transport_callbacks():
    transport = object.__new__(NodeTransport)
    transport._loop = asyncio.get_running_loop()
    transport._ws = object()
    transport.core = SimpleNamespace(binding_guard=SimpleNamespace(connection_generation='generation'))
    transport._waiters = {}
    sent = []
    async def send(*args, **kwargs):
        sent.append(kwargs['msg_id'])
    transport._send = send
    try:
        await asyncio.to_thread(transport.ask_nexus, P.Execute(run_id='run', step_idx=-1, sn='offline', capability_id='script.callback'), 0.02)
    except nexus_link.NexusCallbackError as exc:
        assert exc.code == 'nexus_timeout'
    else:
        raise AssertionError('transport timeout lost')
    assert len(sent) == 1 and not transport._waiters
    async def disconnected_send(*args, **kwargs):
        sent.append(kwargs['msg_id'])
        transport._waiters[kwargs['msg_id']].set_exception(nexus_link.NexusCallbackError('nexus_disconnected'))
    transport._send = disconnected_send
    try:
        await asyncio.to_thread(transport.ask_nexus, P.Execute(run_id='run', step_idx=-1, sn='offline', capability_id='script.callback'), 1)
    except nexus_link.NexusCallbackError as exc:
        assert exc.code == 'nexus_disconnected'
    else:
        raise AssertionError('transport disconnect lost')
    assert len(sent) == 2 and not transport._waiters


async def outbox(root):
    store = RecordStore(root)
    meta = {'run_id': 'run', 'sn': 'offline', 'platform': 'android'}
    store.create('task', 'attempt', meta, 'hash')
    old = store.pending()[0]['summary_version']
    terminal = store.finish('task', 'attempt', {'status': 'blocked', 'reason_code': 'write_unknown', 'resource_isolated': True})
    store.acknowledge('task', 'attempt', old)
    assert store.pending()[0]['sealed'] and store.pending()[0]['resource_isolated']
    transport = object.__new__(NodeTransport)
    transport.core = SimpleNamespace(_script_records=SimpleNamespace(store=store))
    transport._ws = object()
    for mode in ('none', 'reject', 'mismatch', 'old', 'malformed', 'valid'):
        async def request(*args, **kwargs):
            if mode == 'none':
                return None
            ack = {'attempt_id': 'wrong' if mode == 'mismatch' else 'attempt',
                   'ack_version': old if mode == 'old' else None if mode == 'malformed' else terminal['summary_version']}
            return SimpleNamespace(payload=P.Result(run_id='run', status='blocked' if mode == 'reject' else 'pass', data=ack))
        transport._request = request
        await transport._sync_records()
        assert bool(store.pending()) == (mode != 'valid')
    # ACKed terminal facts remain local, without re-enqueuing or unsealing.
    assert store.status('task', 'attempt')['sealed']
    assert store.status('task', 'attempt')['resource_isolated']
    # A running snapshot is in flight while a newer terminal summary is published.
    store.create('race', 'attempt', meta, 'hash')
    store.start('race', 'attempt')
    while store.status('race', 'attempt')['summary_version'] < 748:
        store.append('race', 'attempt', 'script/note', {})
    async def racing_request(mtype, request, **kwargs):
        assert request.params['summary_version'] == 748
        while store.status('race', 'attempt')['summary_version'] < 832:
            store.append('race', 'attempt', 'script/note', {})
        store.finish('race', 'attempt', {'status': 'blocked', 'reason_code': 'write_unknown', 'resource_isolated': True})
        return SimpleNamespace(payload=P.Result(run_id='run', status='pass', data={'attempt_id': 'attempt', 'ack_version': 748}))
    transport._request = racing_request
    await transport._sync_records()
    remaining = store.pending()
    assert len(remaining) == 1 and remaining[0]['summary_version'] == 833 and remaining[0]['sealed']
    assert remaining[0]['resource_isolated']
    async def final_request(mtype, request, **kwargs):
        assert request.params['summary_version'] == 833 and request.params['sealed']
        return SimpleNamespace(payload=P.Result(run_id='run', status='pass', data={'attempt_id': 'attempt', 'ack_version': 833}))
    transport._request = final_request
    await transport._sync_records()
    assert not store.pending()
    for i in range(7):
        store.create(f'task{i}', 'attempt', meta, 'hash')
        store.finish(f'task{i}', 'attempt', {'status': 'blocked'})
    seen, cursor = set(), None
    for _ in range(3):
        batch = store.pending(4, cursor)
        seen.update(row['task_id'] for row in batch)
        cursor = (batch[-1]['task_id'], batch[-1]['attempt_id'])
    assert len(seen) == 7


def main():
    admission()
    callbacks()
    pipe_callbacks()
    asyncio.run(transport_callbacks())
    with tempfile.TemporaryDirectory(prefix='scout-connection-smoke-') as root:
        asyncio.run(outbox(Path(root)))
    print('PASS: bounded pre-write admission, reconnect safe-fail, persistent binding fence, cancellation/deadline, unknown-write latch')
    print('PASS: callback transport/timeout/rejection distinction, single send, reply data merge')
    print('PASS: terminal outbox, stale/malformed ACK retention, valid ACK, isolation, rotating fairness')


if __name__ == '__main__':
    main()
