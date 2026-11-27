"""Attempt-scoped process identities and independently verified forced shutdown."""
from __future__ import annotations

import math
import os
import time

import psutil


def _live(identity):
    try:
        process = psutil.Process(identity['pid'])
        if process.create_time() != identity['created_at']:
            return None
        return process if process.is_running() and process.status() != psutil.STATUS_ZOMBIE else None
    except psutil.NoSuchProcess:
        return None


def _group_members(group):
    members = []
    if group is None:
        return members
    for candidate in psutil.process_iter(['pid']):
        try:
            process = psutil.Process(candidate.pid)
            if os.getpgid(process.pid) == group and process.status() != psutil.STATUS_ZOMBIE:
                members.append(process)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    return members


def _finite_positive(value):
    if type(value) not in (int, float) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def stopped(proof):
    """Recheck recorded identities without killing anything or changing the verdict."""
    if not isinstance(proof, dict) or proof.get('contract') != 'worker_process_tree.v1':
        return False
    if os.name == 'nt' and proof.get('windows_job_stopped') is not True:
        return False
    if proof.get('inspection_complete') is not True or proof.get('process_tree_stopped') is not True:
        return False
    identities = proof.get('processes')
    pid = proof.get('worker_pid')
    if type(pid) is not int or pid <= 0 or not isinstance(identities, list) or not identities:
        return False
    seen = set()
    for identity in identities:
        if not isinstance(identity, dict) or type(identity.get('pid')) is not int or identity['pid'] <= 0:
            return False
        created = identity.get('created_at')
        if not _finite_positive(created) or identity['pid'] in seen:
            return False
        seen.add(identity['pid'])
    observed = proof.get('observed_at')
    if pid not in seen or not _finite_positive(observed):
        return False
    group = proof.get('process_group')
    if group is not None and (os.name == 'nt' or type(group) is not int or group != pid or group == os.getpgrp()):
        return False
    try:
        return not any(_live(identity) is not None for identity in identities) and not _group_members(group)
    except (psutil.Error, OSError):
        return False


class WorkerTree:
    def __init__(self, pid, *, contain=False):
        self.pid = pid
        self.identities = {}
        self.group = pid if os.name != "nt" and pid != os.getpgrp() else None
        self.complete = True
        self.job = None
        self.job_stopped = False
        self.capture()
        if os.name == "nt" and contain:
            try:
                from mino_scout.runtime.worker_job import WorkerJob
                self.job = WorkerJob(pid)
            except OSError:
                self.complete = False

    def _remember(self, process):
        identity = {'pid': process.pid, 'created_at': process.create_time()}
        previous = self.identities.get(process.pid)
        if previous is not None and previous != identity:
            self.complete = False
        else:
            self.identities[process.pid] = identity

    def capture(self, *, include_group=False):
        try:
            root_identity = self.identities.get(self.pid)
            root = _live(root_identity) if root_identity else psutil.Process(self.pid)
            if root is not None:
                self._remember(root)
                if os.name != 'nt' and os.getpgid(self.pid) == self.pid:
                    self.group = self.pid
            # Remembered children can still fork after their parent is reaped.
            for identity in list(self.identities.values()):
                try:
                    parent = _live(identity)
                    if parent is not None:
                        for child in parent.children(recursive=True):
                            try:
                                self._remember(child)
                            except psutil.NoSuchProcess:
                                continue
                except psutil.NoSuchProcess:
                    continue
                except (psutil.Error, OSError):
                    self.complete = False
            # Do not adopt a new process tree when the old group ID is reused.
            if self.group is not None and root_identity:
                try:
                    leader = psutil.Process(self.group)
                    if leader.create_time() != root_identity['created_at']:
                        self.complete = False
                        return
                except psutil.NoSuchProcess:
                    pass
            for process in _group_members(self.group) if include_group else ():
                try:
                    self._remember(process)
                except psutil.NoSuchProcess:
                    continue
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass
        except (psutil.Error, OSError):
            self.complete = False

    def terminate(self, process):
        """Freeze writers before enumerating and killing their known process tree."""
        self.capture(include_group=True)
        suspended = []
        try:
            # Suspend each identity on both platforms and
            # recapture until it cannot create another untracked descendant.
            for _ in range(8):
                before = len(self.identities)
                for identity in list(self.identities.values()):
                    try:
                        target = _live(identity)
                        if target is not None and target.pid not in suspended:
                            target.suspend()
                            suspended.append(target.pid)
                    except psutil.NoSuchProcess:
                        continue
                    except (psutil.Error, OSError):
                        self.complete = False
                self.capture(include_group=True)
                if len(self.identities) == before:
                    break
            else:
                self.complete = False
            targets = []
            for identity in reversed(list(self.identities.values())):
                try:
                    target = _live(identity)
                    if target is not None:
                        target.kill()
                        targets.append(target)
                except psutil.NoSuchProcess:
                    continue
                except (psutil.Error, OSError):
                    self.complete = False
            if self.job is not None:
                self.job_stopped = self.job.terminate()
            # Signal only identity-checked processes, never a reusable raw PGID.
            # multiprocessing alone reaps its worker; psutil.wait() would consume
            # that exit status and leave Process.is_alive() permanently unknown.
            psutil.wait_procs([target for target in targets if target.pid != self.pid], timeout=2)
        except (psutil.Error, OSError):
            self.complete = False
        finally:
            # Never leave a partially inspected descendant indefinitely frozen.
            for pid in suspended:
                try:
                    target = _live(self.identities[pid])
                    if target is not None:
                        target.kill()
                except (psutil.Error, OSError):
                    self.complete = False
                    try:
                        target = _live(self.identities[pid])
                        if target is not None:
                            target.resume()
                    except (psutil.Error, OSError):
                        pass
            try:
                if process.is_alive() and self.pid in self.identities and _live(self.identities[self.pid]) is not None:
                    process.kill()
                process.join(timeout=2)
            except (psutil.Error, OSError):
                self.complete = False
        proof = {'contract': 'worker_process_tree.v1', 'worker_pid': self.pid,
                 'processes': list(self.identities.values()), 'process_group': self.group,
                 'inspection_complete': self.complete and self.pid in self.identities,
                 **({'windows_job_stopped': self.job_stopped} if os.name == 'nt' else {}),
                 'process_tree_stopped': True, 'observed_at': time.time()}
        proof['process_tree_stopped'] = stopped(proof) and not process.is_alive()
        if self.job is not None:
            self.job.close()
            self.job = None
        return proof
