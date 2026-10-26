"""Durable FIFO adapted from buddy-caas fpga_jobs.py (see NOTICE)."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time

from .models import ACTIVE, STATES, BUSY_EXIT, BUSY_TOKEN, Conflict, task_id, validate

LOG = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, path, backend, images, *, interval=3, capacity=256, cooldown=60, retention=7 * 86400):
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
            self.db.execute('''CREATE TABLE IF NOT EXISTS cooldowns (
                node TEXT NOT NULL, fpga INTEGER NOT NULL, until REAL NOT NULL, PRIMARY KEY(node,fpga))''')
            for row in self.db.execute("SELECT * FROM jobs WHERE state='running'").fetchall():
                result = backend.recover(dict(row)) if hasattr(backend, 'recover') else None
                self.db.execute("UPDATE jobs SET state=?,finished=?,result=?,error=? WHERE id=?",
                                ('completed' if result and result['exit_code'] == 0 else 'failed',
                                 time.time(), json.dumps(result) if result else None,
                                 None if result else 'Service restarted; execution outcome unknown; not replayed', row['id']))

    def start(self):
        if self.dispatcher is not None:
            raise RuntimeError('Dispatcher already started')
        self.dispatcher = threading.Thread(target=self.loop, name='fpga-dispatcher', daemon=True)
        self.dispatcher.start()

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.dispatcher:
            self.dispatcher.join()
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

    def submit(self, payload):
        payload = validate(payload)
        with self.lock, self.db:
            if self.stopping.is_set():
                raise RuntimeError('Service is stopping')
            previous = self.db.execute('SELECT payload FROM jobs WHERE id=?', (payload['id'],)).fetchone()
            if previous:
                if json.loads(previous['payload']) != payload:
                    raise Conflict('Task ID already belongs to different inputs')
            else:
                count = self.db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
                if count >= self.capacity:
                    raise OverflowError('Queue is full')
                self.images.freeze(payload['id'], payload['sha256'])
                self.db.execute('INSERT INTO jobs(id,payload,state,created) VALUES (?,?,?,?)',
                                (payload['id'], json.dumps(payload), 'queued', time.time()))
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
            if row['state'] == 'queued':
                data['position'] = self.db.execute("SELECT count(*) FROM jobs WHERE state='queued' AND seq<=?", (row['seq'],)).fetchone()[0]
                data['waiting_reason'] = self.status_error or 'Waiting for an available FPGA'
            return data

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
            row = self.get(ident)
            if row['state'] == 'running':
                raise Conflict('Running tasks cannot be cancelled')
            if row['state'] == 'queued':
                self.db.execute("UPDATE jobs SET state='cancelled',finished=? WHERE id=?", (time.time(), ident))
            result = self.get(ident)
        self.wake.set()
        return result

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
        if not waiting or self.stopping.is_set():
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
                if time.time() - job['created'] >= json.loads(job['payload'])['queue_timeout']:
                    self._expire(job['id'])
                    continue
                changed = self.db.execute("UPDATE jobs SET state='running',node=?,fpga=?,started=?,error=NULL WHERE id=? AND state='queued'",
                                          (node, port, time.time(), job['id'])).rowcount
                if not changed:
                    continue
                self.next_node = (self.nodes.index(node) + 1) % len(self.nodes)
                self.reserved.add((node, port))
                worker = threading.Thread(target=self.execute, args=(job, node, port), name='fpga-' + job['id'])
                self.workers.add(worker)
                self.db.commit()
                try:
                    worker.start()
                except Exception:
                    # No backend call took place; retain this ticket for a later pass.
                    self.reserved.discard((node, port))
                    self.workers.discard(worker)
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
        self.db.execute("UPDATE jobs SET state='failed',finished=?,error=? WHERE id=? AND state='queued'",
                        (time.time(), 'Queue timeout; hardware was not started', ident))

    def execute(self, job, node, port):
        payload = json.loads(job['payload'])
        result, error, state, launched = None, None, 'failed', False
        try:
            image = self.images.freeze(payload['id'], payload['sha256'])
            self.backend.prepare(payload, node, image)
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
                state = 'completed' if result['exit_code'] == 0 else 'failed'
        except Exception:
            error = ('Execution connection lost; outcome unknown; not replayed' if launched else
                     'Input transfer or verification failed; hardware was not started')
            result = None
            LOG.exception('Task %s failed', job['id'])
        finally:
            with self.lock, self.db:
                self.db.execute('''UPDATE jobs SET state=?,result=?,error=?,finished=?,fpga=?,node=?,
                    started=CASE WHEN ? THEN NULL ELSE started END WHERE id=?''',
                    (state, json.dumps(result) if result else None, error,
                     None if state == 'queued' else time.time(), None if state == 'queued' else port,
                     None if state == 'queued' else node, state == 'queued', job['id']))
                self.db.commit()
                self.reserved.discard((node, port))
                if state == 'failed':
                    self.db.execute('INSERT OR REPLACE INTO cooldowns VALUES (?,?,?)', (node, port, time.time() + self.cooldown))
                self.workers.discard(threading.current_thread())
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
