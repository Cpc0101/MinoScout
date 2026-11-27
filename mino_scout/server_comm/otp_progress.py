"""Case-local OTP telemetry. Receipts contain no mailbox, credential, UID or code."""
from __future__ import annotations

import hashlib
import threading
import time

_LOCK = threading.RLock()
_PHASES = {'prepare', 'connect', 'login', 'select', 'search', 'fetch', 'waiting', 'finished'}
_STATES = {'preparing', 'prepared', 'expired', 'waiting', 'succeeded', 'blocked', 'cancelled', 'timed_out'}
_ERRORS = {'otp_wait_stopped', 'otp_prepare_timeout', 'otp_request_expired', 'otp_wait_failed',
           'otp_mail_not_received', 'otp_mail_filter_no_match', 'otp_mail_code_not_found',
           'otp_mailbox_changed', 'otp_mailbox_boundary_unavailable', 'otp_mailbox_boundary_invalid',
           'otp_imap_host_invalid', 'otp_imap_tls_certificate_failed', 'otp_imap_tls_failed',
           'otp_imap_dns_failed', 'otp_imap_connect_timeout', 'otp_imap_read_timeout',
           'otp_imap_connection_lost', 'otp_imap_auth_failed', 'otp_imap_protocol_failed',
           'otp_imap_connection_refused', 'otp_imap_network_unreachable', 'otp_imap_connection_failed',
           'otp_environment_unavailable', 'cancelled', 'script_timeout'}


def case_lock(case):
    with _LOCK:
        if not hasattr(case, '_otp_lock'):
            case._otp_lock = threading.RLock()
            case._otp_active = set()
        return case._otp_lock


def snapshot(case, request=None):
    """Read the latest request without a guard, mailbox access or token consumption."""
    if request is not None and not isinstance(request, str):
        return {'state': 'unavailable'}
    with case_lock(case):
        tracker = getattr(case, '_otp_progress', None)
        if request is not None:
            tracker = getattr(case, '_otp_progress_by_request', {}).get(request)
        if tracker is None:
            return {'state': 'unavailable'}
        data = tracker.snapshot()
        if data['state'] in {'prepared', 'expired'} and tracker.request not in getattr(case, '_otp_requests', {}):
            data['state'] = 'invalidated'
        return data


class Progress:
    def __init__(self, case, *, state, deadline, request=''):
        self.case, self.request = case, request
        self.started = time.monotonic()
        self.started_at = time.time()
        self.command_request_id = str(getattr(case, "_debug_request_id", "") or "")
        self.ended = None
        self.deadline = deadline
        self.state, self.error_code = state, ''
        self.stats, self.diagnostics = {'phase': 'prepare'}, {}
        self._stop = threading.Event()
        self._thread = None
        self._last = 0.0
        self._lock = threading.RLock()
        with case_lock(case):
            case._otp_progress = self
        if request:
            self.bind(request)

    def bind(self, request):
        with case_lock(self.case):
            self.request = request
            registry = getattr(self.case, '_otp_progress_by_request', {})
            registry[request] = self
            while len(registry) > 32:
                registry.pop(next(iter(registry)))
            self.case._otp_progress_by_request = registry

    def snapshot(self):
        with self._lock:
            data = {'state': self.state if self.state in _STATES else 'blocked',
                    'started_at': self.started_at, 'sampled_at': time.time(),
                    **({'command_request_id': self.command_request_id} if self.command_request_id else {}),
                    'elapsed_ms': max(0, int(((self.ended if self.ended is not None else time.monotonic()) - self.started) * 1000)),
                    'remaining_ms': max(0, int((self.deadline - time.monotonic()) * 1000)),
                    'max_wait_ms': max(0, int((self.deadline - self.started) * 1000))}
            if data['state'] == 'prepared' and data['remaining_ms'] == 0:
                data['state'] = 'expired'
            data['deadline_exceeded'] = data['remaining_ms'] == 0
            if self.request:
                data['request_ref'] = hashlib.sha256(self.request.encode()).hexdigest()[:16]
            phase = self.stats.get('phase', 'waiting')
            data['phase'] = phase if phase in _PHASES else 'waiting'
            for source, keys in ((self.stats, ('polls', 'messages', 'matched')),
                                 (self.diagnostics, ('connection_attempts', 'reconnects'))):
                for key in keys:
                    value = source.get(key)
                    if type(value) is int and value >= 0:
                        data[key] = value
            if self.error_code in _ERRORS:
                data['error_code'] = self.error_code
            return data

    def publish(self, force=False):
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last < 1.0:
                return
            self._last = now
            data = self.snapshot()
        recorder = getattr(self.case, '_recorder', None)
        if recorder is not None:
            try:
                recorder.store.update_metadata(recorder.task_id, recorder.attempt_id, {'otp_wait': data})
            except Exception:
                # Telemetry cannot mask cancellation or a sealed-record outcome.
                pass

    def start(self):
        self.publish(True)
        def heartbeat():
            while not self._stop.wait(1.0):
                self.publish()
        self._thread = threading.Thread(target=heartbeat, name='otp-progress', daemon=True)
        self._thread.start()
        return self

    def finish(self, state, error_code=''):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        with self._lock:
            self.state, self.error_code = state, error_code
            self.ended = time.monotonic()
            self.stats['phase'] = 'finished'
        self.publish(True)
