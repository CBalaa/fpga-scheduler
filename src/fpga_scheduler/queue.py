"""Durable FIFO adapted from buddy-caas fpga_jobs.py (see NOTICE)."""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
import time

from .models import (ACTIVE, STATES, BUSY_EXIT, BUSY_TOKEN, Cancellation,
                     CancelledError, Conflict, cancelled_result, task_id, validate)
from .storage import StorageQuotaExceeded

LOG = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, path, backend, images, *, interval=3, capacity=256, cooldown=60, retention=7 * 86400,
                 client_heartbeat_seconds=2, client_lease_seconds=15):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.backend, self.images = backend, images
        self.nodes = tuple(n.id for n in backend.nodes)
        self.port_order = {n.id: n.ports for n in backend.nodes}
        if not self.nodes or len(set(self.nodes)) != len(self.nodes):
            raise ValueError('Invalid backend nodes')
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.lock = threading.RLock()
        self.dispatch_lock = threading.Lock()
        self.inventory_lock = threading.Lock()
        self.interval, self.capacity, self.cooldown, self.retention = interval, capacity, cooldown, retention
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.reserved, self.workers = set(), set()
        self.controls = {}
        self.heartbeat_seconds, self.lease_seconds = client_heartbeat_seconds, client_lease_seconds
        self.lease_monitor = None
        self.next_node, self.next_cleanup = 0, 0
        self.ports, self.node_errors = [], {}
        self.status_error = None
        self.dispatcher = None
        self.closed = False
        self.maintenance = None
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                started REAL, finished REAL, node TEXT, fpga INTEGER, result TEXT, error TEXT)''')
            self.db.execute('CREATE INDEX IF NOT EXISTS jobs_state_seq ON jobs(state,seq)')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
            for name in ('session_id', 'cancel_reason', 'waiting_details', 'error_details'):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
            if 'retry_at' not in columns:
                self.db.execute('ALTER TABLE jobs ADD COLUMN retry_at REAL')
            self.db.execute('CREATE INDEX IF NOT EXISTS jobs_session ON jobs(session_id,state)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS client_sessions (
                id TEXT PRIMARY KEY, heartbeat REAL NOT NULL, lease_seconds REAL NOT NULL,
                heartbeat_seconds REAL NOT NULL, state TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS cooldowns (
                node TEXT NOT NULL, fpga INTEGER NOT NULL, until REAL NOT NULL, PRIMARY KEY(node,fpga))''')
            for row in self.db.execute("SELECT * FROM jobs WHERE state IN ('running','cancelling')").fetchall():
                result = backend.recover(dict(row)) if hasattr(backend, 'recover') else None
                self.db.execute("UPDATE jobs SET state=?,finished=?,result=?,error=? WHERE id=?",
                                (self._result_state(result) if result else 'failed',
                                 time.time(), json.dumps(result) if result else None,
                                 None if result else 'Service restarted; execution outcome unknown; not replayed', row['id']))
            self._expire_sessions_locked(time.time())

    def start(self):
        if self.dispatcher is not None:
            raise RuntimeError('Dispatcher already started')
        self.dispatcher = threading.Thread(target=self.loop, name='fpga-dispatcher', daemon=True)
        self.dispatcher.start()
        self.lease_monitor = threading.Thread(target=self._lease_loop, name='fpga-client-leases', daemon=True)
        self.lease_monitor.start()

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.dispatcher:
            self.dispatcher.join()
        if self.lease_monitor:
            self.lease_monitor.join()
        # Backends have bounded transport/execution timeouts. Keep DB and the
        # service's state-directory lock alive until every assignment retires.
        with self.dispatch_lock:
            with self.lock:
                workers = list(self.workers)
            for worker in workers:
                worker.join()
            if self.maintenance is not None:
                self.maintenance.join()
            if hasattr(self.backend, 'close'):
                self.backend.close()
            with self.lock:
                if not self.closed:
                    self.db.close()
                    self.closed = True

    def open_session(self, ident, options):
        task_id(ident)
        if not getattr(self.backend, 'supports_cancellation', False):
            raise Conflict('This backend does not support client leases / running cancellation; use bosc-resident or simulator')
        if not isinstance(options, dict) or set(options) - {'heartbeat_seconds', 'lease_seconds'}:
            raise ValueError('Invalid client session options')
        heartbeat = options.get('heartbeat_seconds', self.heartbeat_seconds)
        lease = options.get('lease_seconds', self.lease_seconds)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               or not .01 <= v <= 3600 for v in (heartbeat, lease)) or heartbeat >= lease:
            raise ValueError('Require 0.01 <= heartbeat_seconds < lease_seconds <= 3600')
        with self.lock, self.db:
            if self.stopping.is_set():
                raise RuntimeError('Service is stopping')
            self._expire_sessions_locked(time.time())
            self.db.commit()
            row = self.db.execute('SELECT * FROM client_sessions WHERE id=?', (ident,)).fetchone()
            if row:
                if row['state'] != 'active':
                    raise Conflict('Client session has ended; create a new Client')
            else:
                self.db.execute('INSERT INTO client_sessions VALUES (?,?,?,?,?)',
                                (ident, time.time(), lease, heartbeat, 'active'))
                row = self.db.execute('SELECT * FROM client_sessions WHERE id=?', (ident,)).fetchone()
            return dict(id=ident, heartbeat_seconds=row['heartbeat_seconds'], lease_seconds=row['lease_seconds'])

    def heartbeat(self, ident):
        task_id(ident)
        with self.lock, self.db:
            self._require_session_locked(ident)
            self.db.execute('UPDATE client_sessions SET heartbeat=? WHERE id=?', (time.time(), ident))
        return dict(id=ident, state='active')

    def close_session(self, ident):
        task_id(ident)
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM client_sessions WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            if row['state'] == 'active':
                self._end_session_locked(ident, 'closed', 'Submitting client closed')
        self.wake.set()
        return dict(id=ident, state='closed')

    def _require_session_locked(self, ident):
        self._expire_sessions_locked(time.time())
        # Expiration is irreversible even if this late heartbeat / submit is
        # rejected and its surrounding transaction rolls back.
        self.db.commit()
        row = self.db.execute('SELECT state FROM client_sessions WHERE id=?', (ident,)).fetchone()
        if row is None or row['state'] != 'active':
            raise Conflict('Client session missing, closed or expired; tasks cannot be revived')

    def _end_session_locked(self, ident, state, reason):
        self.db.execute('UPDATE client_sessions SET state=? WHERE id=?', (state, ident))
        for row in self.db.execute("SELECT id FROM jobs WHERE session_id=? AND state IN ('queued','running','cancelling')",
                                   (ident,)).fetchall():
            self._cancel_locked(row['id'], reason)

    def _expire_sessions_locked(self, now):
        expired = self.db.execute("SELECT id FROM client_sessions WHERE state='active' AND heartbeat+lease_seconds<=?",
                                  (now,)).fetchall()
        for row in expired:
            self._end_session_locked(row['id'], 'expired', 'Submitting client lease expired')
        return bool(expired)

    def expire_sessions(self):
        with self.lock, self.db:
            expired = self._expire_sessions_locked(time.time())
        if expired:
            self.wake.set()

    def _lease_loop(self):
        # Independent of inventory / initialization, which may block for minutes.
        while not self.stopping.wait(.05):
            try:
                self.expire_sessions()
            except Exception:
                LOG.exception('Client lease pass failed')

    def submit(self, payload, session_id=None):
        payload = validate(payload)
        with self.lock, self.db:
            if self.stopping.is_set():
                raise RuntimeError('Service is stopping')
            if session_id is not None:
                task_id(session_id)
                self._require_session_locked(session_id)
            previous = self.db.execute('SELECT payload,session_id FROM jobs WHERE id=?', (payload['id'],)).fetchone()
            if previous:
                if json.loads(previous['payload']) != payload or previous['session_id'] != session_id:
                    raise Conflict('Task ID already belongs to different inputs')
            else:
                count = self.db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','cancelling')").fetchone()[0]
                if count >= self.capacity:
                    raise OverflowError('Queue is full')
                self.images.freeze(payload['id'], payload['sha256'])
                self.db.execute('INSERT INTO jobs(id,payload,state,created,session_id) VALUES (?,?,?,?,?)',
                                (payload['id'], json.dumps(payload), 'queued', time.time(), session_id))
            task = self.get(payload['id'])
        self.wake.set()
        return task

    def get(self, ident):
        task_id(ident)
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            data = {key: row[key] for key in ('id', 'state', 'created', 'started', 'finished', 'node', 'fpga', 'error')}
            if row['result']:
                data['result'] = json.loads(row['result'])
            if row['error_details']:
                data['error_details'] = json.loads(row['error_details'])
            if row['state'] == 'queued':
                data['position'] = self.db.execute("SELECT count(*) FROM jobs WHERE state='queued' AND seq<=?", (row['seq'],)).fetchone()[0]
                data['waiting_reason'] = self.status_error or 'Waiting for an available FPGA'
                if row['waiting_details']:
                    data['waiting_details'] = json.loads(row['waiting_details'])
                    data['waiting_reason'] = data['waiting_details']['message']
                    data['retry_at'] = row['retry_at']
                else:
                    head = self.db.execute("SELECT id,waiting_details,retry_at FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
                    if head and head['waiting_details']:
                        data['waiting_reason'] = 'Waiting behind an earlier task blocked by storage capacity'
                        data['waiting_details'] = {**json.loads(head['waiting_details']), 'blocked_by': head['id']}
                        data['retry_at'] = head['retry_at']
            return data

    def output(self, ident, offset=0, limit=65536):
        """Read one bounded UART chunk without holding a hardware/session lock."""
        if type(offset) is not int or offset < 0:
            raise ValueError('Output offset must be a nonnegative byte count')
        if type(limit) is not int or not 1 <= limit <= 65536:
            raise ValueError('Output limit must be within 1..65536 bytes')
        task = self.get(ident)
        reader = getattr(self.backend, 'read_output', None)
        data = reader(ident, offset, limit + 1) if reader else None
        if data is None:
            # Compatibility backends publish output only at completion.
            result = task.get('result') or {}
            text = result.get('uart_output')
            text = result.get('output', '') if text is None else text
            raw = text.encode('utf-8')
            if offset > len(raw):
                raise ValueError('Output offset exceeds available bytes')
            data = raw[offset:offset + limit + 1]
        done = task['state'] not in ACTIVE and len(data) <= limit
        data = data[:limit]
        if not done:
            task.pop('result', None)
        return dict(id=ident, offset=offset, next_offset=offset + len(data),
                    data=base64.b64encode(data).decode('ascii'), done=done, task=task)

    def artifacts(self, ident):
        from .artifacts import validate_artifacts
        task = self.get(ident)
        if task['state'] in ACTIVE:
            raise Conflict('Artifacts are available only after task completion and cleanup')
        return dict(id=ident, artifacts=validate_artifacts((task.get('result') or {}).get('artifacts', [])))

    def open_artifact(self, ident, name):
        """Open while retention holds the queue lock; a download pins the inode."""
        from .remote.runner import output_name
        output_name(name)
        with self.lock:
            items = self.artifacts(ident)['artifacts']
            item = next((item for item in items if item['name'] == name), None)
            if item is None or not hasattr(self.backend, 'artifact_path'):
                raise KeyError(name)
            path = self.backend.artifact_path(ident, name)
            handle = path.open('rb')
            if os.fstat(handle.fileno()).st_size != item['nbytes']:
                handle.close()
                raise RuntimeError('Saved artifact length mismatch')
            return handle, item

    def list(self, states=ACTIVE, limit=100, offset=0):
        if (not states or any(s not in STATES for s in states) or type(limit) is not int
                or not 1 <= limit <= 256 or type(offset) is not int or offset < 0):
            raise ValueError('Invalid task listing filters')
        with self.lock:
            marks = ','.join('?' for _ in states)
            total = self.db.execute(f'SELECT count(*) FROM jobs WHERE state IN ({marks})', states).fetchone()[0]
            ids = self.db.execute(f'SELECT id FROM jobs WHERE state IN ({marks}) ORDER BY seq LIMIT ? OFFSET ?',
                                  (*states, limit, offset)).fetchall()
            return dict(jobs=[self.get(row['id']) for row in ids], total=total, limit=limit, offset=offset)

    def cancel(self, ident):
        with self.lock, self.db:
            self._cancel_locked(ident, 'Cancellation requested')
            result = self.get(ident)
        self.wake.set()
        return result

    def _cancel_locked(self, ident, reason):
        row = self.get(ident)
        if row['state'] == 'queued':
            self.db.execute("UPDATE jobs SET state='cancelled',finished=?,cancel_reason=?,result=? WHERE id=?",
                            (time.time(), reason, json.dumps(cancelled_result(reason)), ident))
        elif row['state'] == 'running':
            control = self.controls.get(ident)
            if control is None or not getattr(self.backend, 'supports_cancellation', False):
                raise Conflict('Backend cannot safely cancel a running task')
            if control.request(reason):
                self.db.execute("UPDATE jobs SET state='cancelling',cancel_reason=? WHERE id=?", (reason, ident))

    def inventory(self):
        def probe(node):
            try:
                ports = self.backend.probe(node)
                if (not isinstance(ports, list) or any(not isinstance(p, dict) or type(p.get('number')) is not int
                                                      or type(p.get('available')) is not bool
                                                      or not isinstance(p.get('reasons', []), list)
                                                      or any(not isinstance(reason, str) for reason in p.get('reasons', []))
                                                      for p in ports)
                        or len(ports) != len(self.port_order[node])
                        or {p['number'] for p in ports} != set(self.port_order[node])):
                    raise ValueError('Incomplete or invalid inventory')
                indexed = {p['number']: p for p in ports}
                return node, [dict(indexed[p], node=node) for p in self.port_order[node]], None
            except Exception:
                return node, [], 'Device probe unavailable or invalid'
        with self.inventory_lock:
            with ThreadPoolExecutor(max_workers=len(self.nodes)) as pool:
                results = list(pool.map(probe, self.nodes))
            with self.lock:
                self.ports = [p for _, ports, _ in results for p in ports]
                self.node_errors = {node: error for node, _, error in results if error}
                self.status_error = 'Some nodes are unavailable; waiting for healthy devices' if self.node_errors else None
                cooling = {(r['node'], r['fpga']) for r in self.db.execute('SELECT node,fpga FROM cooldowns WHERE until>?', (time.time(),))}
                ports = []
                for port in self.ports:
                    reasons = list(port.get('reasons', []))
                    key = port['node'], port['number']
                    if key in self.reserved:
                        reasons.append('platform task active')
                    if key in cooling:
                        reasons.append('failure cooldown')
                    ports.append(dict(port, available=port['available'] and not reasons, reasons=reasons))
                return dict(devices=ports, errors=dict(self.node_errors))

    def dispatch_once(self):
        with self.dispatch_lock:
            self._dispatch_once()

    def _dispatch_once(self):
        do_cleanup = False
        with self.lock, self.db:
            now = time.time()
            self._expire_sessions_locked(now)
            if now >= self.next_cleanup:
                # Protect all retained jobs, including recent terminal jobs, so
                # input retention is never shorter than task-record retention.
                self.db.execute("DELETE FROM jobs WHERE state IN ('completed','failed','cancelled') AND finished<?", (now - self.retention,))
                self.db.execute('DELETE FROM cooldowns WHERE until<?', (now,))
                protected = [r['id'] for r in self.db.execute('SELECT id FROM jobs')]
                self.images.cleanup(now - self.retention, protected)
                self.next_cleanup = now + 3600
                do_cleanup = True
            waiting = self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY seq").fetchall()
            for job in waiting:
                if now - job['created'] >= json.loads(job['payload'])['queue_timeout']:
                    self._expire(job['id'])
            waiting = [j for j in waiting if now - j['created'] < json.loads(j['payload'])['queue_timeout']]
        if (do_cleanup and hasattr(self.backend, 'cleanup')
                and (self.maintenance is None or not self.maintenance.is_alive())):
            self.maintenance = threading.Thread(target=self._cleanup_remote, args=(now - self.retention,),
                                                name='fpga-retention')
            self.maintenance.start()
        if (not waiting or self.stopping.is_set()
                or (waiting[0]['retry_at'] or 0) > time.time()):
            return
        ports = self.inventory()['devices']
        with self.lock, self.db:
            if self.stopping.is_set():
                return
            by_node = {n: [(n, p['number']) for p in ports if p['node'] == n and p['available']
                           and (n, p['number']) not in self.reserved] for n in self.nodes}
            order = self.nodes[self.next_node:] + self.nodes[:self.next_node]
            free = [by_node[n][i] for i in range(max(map(len, by_node.values()), default=0)) for n in order if i < len(by_node[n])]
            for job, (node, port) in zip(waiting, free):
                if job['session_id'] is not None:
                    self._expire_sessions_locked(time.time())
                if time.time() - job['created'] >= json.loads(job['payload'])['queue_timeout']:
                    self._expire(job['id'])
                    continue
                if self.get(job['id'])['state'] != 'queued':
                    continue
                if (job['retry_at'] or 0) > time.time():
                    break  # Do not let newer FIFO tickets bypass resource waits.
                if hasattr(self.backend, 'reserve_storage'):
                    try:
                        self.backend.reserve_storage(json.loads(job['payload']), self.images.path(job['id']))
                    except StorageQuotaExceeded as error:
                        self._wait_for_storage_locked(job['id'], error)
                        break
                changed = self.db.execute("""UPDATE jobs SET state='running',node=?,fpga=?,started=?,error=NULL,
                                          error_details=NULL,waiting_details=NULL,retry_at=NULL WHERE id=? AND state='queued'""",
                                          (node, port, time.time(), job['id'])).rowcount
                if not changed:
                    if hasattr(self.backend, 'release_prepared'):
                        self.backend.release_prepared(job['id'])
                    continue
                self.next_node = (self.nodes.index(node) + 1) % len(self.nodes)
                self.reserved.add((node, port))
                control = Cancellation()
                self.controls[job['id']] = control
                worker = threading.Thread(target=self.execute, args=(job, node, port, control), name='fpga-' + job['id'])
                self.workers.add(worker)
                self.db.commit()
                try:
                    worker.start()
                except Exception:
                    # No backend call took place; retain this ticket for a later pass.
                    self.reserved.discard((node, port))
                    self.workers.discard(worker)
                    self.controls.pop(job['id'], None)
                    if hasattr(self.backend, 'release_prepared'):
                        self.backend.release_prepared(job['id'])
                    self.db.execute("UPDATE jobs SET state='queued',node=NULL,fpga=NULL,started=NULL WHERE id=?",
                                    (job['id'],))
                    self.db.commit()
                    raise

    def _cleanup_remote(self, before):
        try:
            self.backend.cleanup(before)
        except Exception:
            LOG.warning('Remote retention cleanup unavailable', exc_info=True)

    def _expire(self, ident):
        waiting = self.get(ident).get('waiting_details')
        error = 'Queue timeout; hardware was not started'
        details = dict(error_code='queue_timeout', phase='admission', hardware_started=False)
        if waiting:
            error = ('Queue timeout while waiting for storage capacity (' +
                     waiting.get('reason', 'storage unavailable') + '); hardware was not started')
            details['cause'] = waiting
        self.db.execute("""UPDATE jobs SET state='failed',finished=?,error=?,error_details=?,
                           waiting_details=NULL,retry_at=NULL WHERE id=? AND state='queued'""",
                        (time.time(), error, json.dumps(details), ident))

    def _wait_for_storage_locked(self, ident, error):
        delay = getattr(self.backend, 'storage_retry_seconds', 10)
        details = {**error.details, 'reason': error.summary,
                   'message': f'{error.summary}; retrying in {delay:g}s; hardware was not started',
                   'retry_after_seconds': delay}
        self.db.execute('''UPDATE jobs SET state='queued',started=NULL,finished=NULL,node=NULL,fpga=NULL,
                           error=NULL,result=NULL,error_details=NULL,waiting_details=?,retry_at=? WHERE id=?''',
                        (json.dumps(details), time.time() + delay, ident))
        LOG.warning('Task %s waiting for storage: %s', ident, error)

    @staticmethod
    def _result_state(result):
        if result.get('execution_status') == 'cancelled':
            return 'cancelled'
        return 'completed' if result['exit_code'] == 0 else 'failed'

    def execute(self, job, node, port, control):
        payload = json.loads(job['payload'])
        result, error, state, launched = None, None, 'failed', False
        error_details, storage_wait = None, None
        try:
            control.check()
            image = self.images.freeze(payload['id'], payload['sha256'])
            if getattr(self.backend, 'supports_cancellation', False):
                self.backend.prepare(payload, node, image, cancellation=control)
                control.check()
                launched = True
                result = self.backend.run(payload, node, port, cancellation=control)
            else:
                self.backend.prepare(payload, node, image)
                control.check()
                launched = True
                result = self.backend.run(payload, node, port)
            if result.get('error'):
                error = 'Execution connection lost; outcome unknown; not replayed'
                result = None
            elif result.get('exit_code') == BUSY_EXIT and result.get('output', '').strip() == BUSY_TOKEN:
                state, result = 'queued', None
            else:
                if (type(result.get('exit_code')) is not int or not isinstance(result.get('output'), str)
                        or type(result.get('seconds')) not in (float, int)
                        or not math.isfinite(result['seconds']) or result['seconds'] < 0):
                    raise ValueError('Invalid execution result')
                result['node'] = node
                state = self._result_state(result)
        except CancelledError as exc:
            result = cancelled_result(str(exc), node=node)
            state = 'cancelled'
        except StorageQuotaExceeded as exc:
            if launched:
                error, error_details = str(exc), {**exc.details, 'phase': 'execution', 'hardware_started': None}
            else:
                state, storage_wait = 'queued', exc
        except Exception:
            error = ('Execution connection lost; outcome unknown; not replayed' if launched else
                     'Input transfer or verification failed; hardware was not started')
            result = None
            error_details = dict(error_code='execution_outcome_unknown' if launched else 'input_prepare_failed',
                                 phase='execution' if launched else 'prepare', hardware_started=None if launched else False)
            LOG.exception('Task %s failed', job['id'])
            if not launched and control.reason:
                result, error, state = cancelled_result(control.reason, node=node), None, 'cancelled'
        finally:
            if hasattr(self.backend, 'release_prepared'):
                self.backend.release_prepared(payload['id'])
            with self.lock, self.db:
                reason = control.finish()
                if state == 'queued' and reason:
                    result, state = cancelled_result(reason, node=node), 'cancelled'
                self.db.execute('''UPDATE jobs SET state=?,result=?,error=?,error_details=?,finished=?,fpga=?,node=?,
                    started=CASE WHEN ? THEN NULL ELSE started END WHERE id=?''',
                    (state, json.dumps(result) if result else None, error, json.dumps(error_details) if error_details else None,
                     None if state == 'queued' else time.time(), None if state == 'queued' else port,
                     None if state == 'queued' else node, state == 'queued', job['id']))
                if storage_wait and state == 'queued':
                    self._wait_for_storage_locked(job['id'], storage_wait)
                self.db.commit()
                self.reserved.discard((node, port))
                clean_abort = (result and result.get('execution_status') in ('timeout', 'cancelled')
                               and result.get('details', {}).get('reset_confirmed') is True)
                if state == 'failed' and launched and not clean_abort:
                    self.db.execute('INSERT OR REPLACE INTO cooldowns VALUES (?,?,?)', (node, port, time.time() + self.cooldown))
                self.workers.discard(threading.current_thread())
                self.controls.pop(job['id'], None)
            self.wake.set()

    def loop(self):
        while not self.stopping.is_set():
            self.wake.clear()
            try:
                self.dispatch_once()
            except Exception:
                LOG.exception('Dispatcher pass failed')
                self.status_error = 'Scheduling temporarily unavailable; task retained in queue'
            self.wake.wait(self.interval)
