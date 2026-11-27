"""Isolated OTP wait contracts for CI and frozen worker-check; no network/device actions."""
from __future__ import annotations


def run() -> int:
    from contextlib import ExitStack
    with ExitStack() as stack:
        import copy
        import json
        import threading
        import time
        from types import SimpleNamespace
        from unittest.mock import patch
        from mino_scout.plugins import mail_otp, state
        from mino_scout.server_comm import callbacks, mail_requests, otp_progress
        from mino_scout.runtime.script_exec import ScriptAbort, ScriptHalt
        
        checks = []
        def check(name, condition):
            assert condition, name
            checks.append(name)
        def rejected(reason, call, cls=ScriptAbort):
            try:
                call()
            except cls as exc:
                check(reason, exc.reason_code == reason)
            else:
                raise AssertionError(reason)
        
        class Store:
            def __init__(self): self.events = []; self.lock = threading.Lock()
            def update_metadata(self, task, attempt, changes):
                with self.lock: self.events.append(copy.deepcopy(changes))
        
        def case():
            store = Store()
            return SimpleNamespace(core=SimpleNamespace(node_id='offline'), _guard=lambda: None, deadline=0,
                _otp_requests={}, _otp_messages={}, _mail_to='recipient@example.invalid',
                _recorder=SimpleNamespace(store=store, task_id='task-fixture', attempt_id='attempt-fixture'),
                _report=lambda *args: None)
        
        policy={'otp_mode':'gmail', 'otp_max_wait_ms':2000}
        account=SimpleNamespace(id='account-fixture', email='recipient@example.invalid', otp='', otp_policy=policy)
        settings={'inbox_address':'inbox@example.invalid', 'imap_host':'imap.example.invalid:993',
                  'app_password':'fixture-secret-no-print', 'to_address':account.email}
        boundary={'uid_validity':7,'min_uid':501}
        def prepared(c):
            with patch.object(mail_requests, 'settings', return_value=dict(settings)), patch.object(mail_otp, 'capture_otp_boundary', return_value=boundary):
                return callbacks.otp_request(c, account)
        
        stack.enter_context(patch.object(state,"plain_value",return_value="inbox@example.invalid"))
        stack.enter_context(patch.object(state,"secret_value",return_value="fixture-secret-no-print"))
        c=case(); token=prepared(c)
        check('prepared state', callbacks.otp_status(c, request=token)['state']=='prepared')
        check('case boundary scope', callbacks.otp_status(case(), request=token)=={'state':'unavailable'})
        before=copy.deepcopy(c._otp_requests)
        with patch.object(mail_otp,'fetch_otp_via_imap',side_effect=AssertionError('Unexpected mailbox read')):
            for _ in range(20): callbacks.otp_status(c, request=token)
        check('read query preserves token', c._otp_requests==before)
        check('deadline not extended', c._otp_requests[token]['expires']-time.monotonic()<=90)
        
        started=threading.Event(); release=threading.Event(); results=[]
        def fetch(**kwargs):
            kwargs['progress'].update(phase='fetch',polls=2,messages=1,matched=1)
            kwargs['diagnostics'].update(connection_attempts=1,reconnects=0)
            started.set(); assert release.wait(4)
            return '654321'
        with patch.object(mail_requests,'settings',return_value=dict(settings)), patch.object(mail_otp,'fetch_otp_via_imap',side_effect=fetch) as fetched, patch('mino_scout.runtime.secret_redaction.remember') as remember:
            worker=threading.Thread(target=lambda: results.append(callbacks.otp(c,account,request=token)))
            worker.start(); assert started.wait(2)
            time.sleep(1.05)
            progress=callbacks.otp_status(c,request=token)
            check('live existing progress', progress['state']=='waiting' and progress['phase']=='fetch' and progress['polls']==2)
            check('heartbeat persisted', any(x['otp_wait'].get('polls')==2 for x in c._recorder.store.events))
            rejected('otp_request_invalid', lambda: callbacks.otp(c,account,request=token))
            rejected('otp_request_in_progress', lambda: callbacks.otp_request(c,account))
            check('one fetch only', fetched.call_count==1)
            release.set(); worker.join(3); check('worker stopped',not worker.is_alive())
            check('fetch succeeded',results==['654321'])
            check('redaction registered',remember.call_args.args==('654321',))
        check('reservation released',not c._otp_active)
        check('completed progress', callbacks.otp_status(c,request=token)['state']=='succeeded')
        elapsed=callbacks.otp_status(c,request=token)['elapsed_ms'];time.sleep(.01)
        check('completed elapsed frozen', callbacks.otp_status(c,request=token)['elapsed_ms']==elapsed)
        serialized=json.dumps(c._recorder.store.events)
        check('no secret/OTP/address/UID/request in receipt',all(x not in serialized for x in ['654321','fixture-secret-no-print','example.invalid',token,'uid_validity','min_uid']))
        
        # A guard's BaseException is preserved and reservations/heartbeat are cleaned up.
        for reason in ('cancelled','script_timeout'):
            c=case(); token=prepared(c)
            def halted(**kwargs): raise ScriptHalt('fixture halt',reason_code=reason)
            with patch.object(mail_requests,'settings',return_value=dict(settings)),patch.object(mail_otp,'fetch_otp_via_imap',side_effect=halted):
                rejected(reason,lambda: callbacks.otp(c,account,request=token),ScriptHalt)
            check(reason+' final state',callbacks.otp_status(c,request=token)['state']==('cancelled' if reason=='cancelled' else 'timed_out'))
            check(reason+' releases active',not c._otp_active)
        
        c=case(); c.deadline=time.monotonic()+.2; token=prepared(c)
        check('token clamped to preparation case deadline',c._otp_requests[token]['expires']<=c.deadline)
        c.deadline=time.monotonic()+300
        check('later fragment cannot extend token',c._otp_requests[token]['expires']<time.monotonic()+1)
        c._otp_requests[token]['expires']=time.monotonic()-1
        with patch.object(mail_requests,'settings',return_value=dict(settings)):
            rejected('otp_request_expired',lambda:callbacks.otp(c,account,request=token))
        
        c=case(); token=prepared(c); account.otp_policy={**policy,'otp_max_wait_ms':'invalid'}
        rejected('otp_policy_invalid',lambda:callbacks.otp(c,account,request=token))
        check('invalid policy does not consume',token in c._otp_requests and not c._otp_active)
        account.otp_policy=policy
        
        # Late mailbox results must not be accepted after deadline or cancellation.
        class Mail:
            sock=None
            closed=0
            def logout(self): self.closed+=1
        mail=Mail()
        def late(*args,**kwargs):time.sleep(.025);return '765432'
        for use_monotonic in (False,True):
            kw={'max_wait_ms':1000,'deadline_monotonic':time.monotonic()+.01} if use_monotonic else {'max_wait_ms':10}
            with patch.object(mail_otp,'_open_inbox',return_value=mail),patch.object(mail_otp,'_poll_inbox',side_effect=late):
                try: mail_otp.fetch_otp_via_imap(**settings,**kw)
                except mail_otp.MailOtpError as exc: check('late result rejected',str(exc)=='otp_mail_not_received')
                else: raise AssertionError('late result accepted')
        check('connections closed after late result',mail.closed==2)
        with patch.object(mail_otp,'_open_inbox',side_effect=AssertionError('Unexpected connect')):
            try: mail_otp.fetch_otp_via_imap(**settings,deadline_ts=0)
            except mail_otp.MailOtpError: checks.append('epoch zero deadline does not connect')
            try: mail_otp.capture_otp_boundary(**{k:settings[k] for k in ('inbox_address','imap_host','app_password')},should_stop=lambda:True)
            except mail_otp.MailOtpError as exc: check('prepare cancellation before connect',str(exc)=='otp_wait_stopped')
        
        tracker=otp_progress.Progress(case(),state='waiting',deadline=time.monotonic()+10,request='query-fixture')
        tracker.stats.update(phase='raw secret here',messages='bad',matched=-1,mailbox='secret')
        tracker.error_code='otp_password_secret'
        public=tracker.snapshot()
        check('telemetry whitelist',public['phase']=='waiting' and 'messages' not in public and 'matched' not in public and 'error_code' not in public)
        check('no heartbeat leak',not any(t.name=='otp-progress' for t in threading.enumerate()))
        # Re-preparation invalidates only pending requests, not a running fetch.
        c=case(); old=prepared(c); new=prepared(c)
        check('old receipt invalidated',callbacks.otp_status(c,request=old)['state']=='invalidated')
        check('new receipt preserved',callbacks.otp_status(c,request=new)['state']=='prepared')
        
        c=case()
        def prepare_halt(**kwargs):raise ScriptHalt('fixture halt',reason_code='cancelled')
        with patch.object(mail_requests,'settings',return_value=dict(settings)),patch.object(mail_otp,'capture_otp_boundary',side_effect=prepare_halt):
            rejected('cancelled',lambda:callbacks.otp_request(c,account),ScriptHalt)
        check('cancelled prepare leaves no token or reservation',not c._otp_requests and not c._otp_active)
        
        class RejectMail(Mail):
            def uid(self,*args):return 'NO',[b'fixture protocol rejection']
        rejected_mail=RejectMail()
        with patch.object(mail_otp,'_open_inbox',return_value=rejected_mail):
            try:mail_otp.fetch_otp_via_imap(**settings,max_wait_ms=500)
            except mail_otp.MailOtpError as exc:check('NO response is protocol failure',str(exc)=='otp_imap_protocol_failed')
            else:raise AssertionError('NO response not rejected')
        check('protocol failure closes connection',rejected_mail.closed==1)
        
        # Real disposable SQLite proves progress survives the worker's in-memory state.
        import tempfile
        from pathlib import Path
        from mino_scout.runtime.records import RecordStore,Recorder
        with tempfile.TemporaryDirectory(prefix='otp-progress-record-') as root:
            store=RecordStore(Path(root))
            store.create('otp-fixture','attempt-fixture',{'owner_user_id':'fixture'},'fixture-hash')
            store.start('otp-fixture','attempt-fixture')
            c=case(); c._debug_request_id='fixture-original-command'; c._recorder=Recorder(store,'otp-fixture','attempt-fixture')
            token=prepared(c)
            persisted=store.metadata('otp-fixture','attempt-fixture')['otp_wait']
            check('SQLite progress persisted',persisted['state']=='prepared' and 'request_ref' in persisted)
            check('progress source command persists',persisted['command_request_id']=='fixture-original-command')
            c._debug_request_id='fixture-next-command'
            check('progress cannot migrate to next fragment',callbacks.otp_status(c,request=token)['command_request_id']=='fixture-original-command')
            check('progress sample has source timing',persisted['sampled_at']>=persisted['started_at'])
            events=store.events('otp-fixture','attempt-fixture',limit=100)['items']
            check('read-only record events exposes progress',any(e['type']=='run/snapshot' and e['payload'].get('otp_wait',{}).get('state')=='prepared' for e in events))
            check('SQLite event redaction',all(x not in json.dumps(events) for x in ['fixture-secret-no-print','example.invalid',token,'654321']))
        
        check('explicit mismatched UID not accepted',mail_otp._single_literal([(b'1 (UID 500 BODY[] {3}',b'old')],b'501') is None)
        check('matching UID still accepted',mail_otp._single_literal([(b'1 (UID 501 BODY[] {3}',b'new')],b'501')==b'new')
        check('invalid query input safe',callbacks.otp_status(case(),request=[])=={'state':'unavailable'})
        
        from mino_scout.runtime.script_exec import _Scout
        c=case(); token=prepared(c)
        check('public SDK existing request status',_Scout(c).otp_status(request=token)['state']=='prepared')
        check('public SDK query preserves request',token in c._otp_requests)

        print(json.dumps({'passed':len(checks),'checks':checks,'scope':'isolated fixtures; no IMAP/Nexus/UI or credentials used'},ensure_ascii=False,indent=2))
        return 0
