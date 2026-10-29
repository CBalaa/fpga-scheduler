import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from fpga_scheduler.backends.simulator import Simulator
from fpga_scheduler.config import Node, load
from fpga_scheduler.models import BUSY_TOKEN, Conflict, MAX_IMAGE, validate
from fpga_scheduler.queue import JobQueue
from fpga_scheduler.storage import Images, StorageQuotaExceeded

NODES = (Node('a', (4, 5)), Node('b', (4, 5)))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images = Images(self.root / 'images')
        self.backend = Simulator(NODES, duration=.05)
        self.q = JobQueue(self.root / 'jobs.db', self.backend, self.images, interval=.01)

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()

    def submit(self, i, **kwargs):
        ident = f'{i:032x}'
        digest = self.images.put(ident, b'hello')
        return self.q.submit(dict(id=ident, sha256=digest, **kwargs))

    def finish(self):
        for worker in list(self.q.workers):
            worker.join(5)
        self.assertFalse(self.q.workers)

    def test_fifo_multinode_and_exclusive_ports(self):
        for i in range(1, 10):
            self.submit(i)
        self.q.dispatch_once()
        assignments = [(self.q.get(f'{i:032x}')['node'], self.q.get(f'{i:032x}')['fpga']) for i in range(1, 5)]
        self.assertEqual(assignments, [('a', 4), ('b', 4), ('a', 5), ('b', 5)])
        self.assertEqual(self.q.get(f'{5:032x}')['position'], 1)
        self.q.dispatch_once()
        self.assertEqual(len(self.q.reserved), 4)
        while self.q.list()['total']:
            self.finish()
            self.q.dispatch_once()
        active = set()
        starts = []
        for kind, ident, node, port, when in self.backend.events:
            key = node, port
            if kind == 'start':
                self.assertNotIn(key, active)
                active.add(key)
                starts.append(ident)
            else:
                active.remove(key)
        self.assertFalse(active)
        # Assignments are FIFO; worker execution start order is not a guarantee.
        self.assertEqual(set(starts[:4]), {f'{i:032x}' for i in range(1, 5)})
        self.assertEqual(self.q.list(('completed',))['total'], 9)

    def test_busy_nodes_leave_tasks_queued(self):
        self.backend.busy.update(('a', 'b'))
        self.submit(1, timeout=1)
        self.q.dispatch_once()
        task = self.q.get(f'{1:032x}')
        self.assertEqual(task['state'], 'queued')
        self.assertIsNone(task['started'])
        self.assertFalse(self.backend.events)

    def test_offline_node_does_not_block_healthy_node(self):
        self.backend.offline.add('a')
        self.submit(1)
        self.q.dispatch_once()
        self.assertEqual(self.q.get(f'{1:032x}')['node'], 'b')
        self.assertIn('a', self.q.node_errors)
        self.finish()

    def test_incomplete_duplicate_and_invalid_inventory(self):
        original = self.backend.probe
        for invalid in ([dict(number=4, available=True)],
                        [dict(number=4, available=True)] * 2,
                        [dict(number=4, available='yes'), dict(number=5, available=True)], None):
            self.backend.probe = lambda node, value=invalid: value if node == 'a' else original(node)
            result = self.q.inventory()
            self.assertIn('a', result['errors'])
            self.assertTrue(all(p['node'] == 'b' for p in result['devices']))

    def test_idempotency_and_conflict(self):
        task = self.submit(1)
        payload = dict(id=task['id'], sha256=hashlib.sha256(b'hello').hexdigest())
        self.assertEqual(self.q.submit(payload)['id'], task['id'])
        self.assertEqual(self.q.list()['total'], 1)
        with self.assertRaises(Conflict):
            self.q.submit(dict(payload, timeout=1))
        with self.assertRaises(Conflict):
            self.images.put(task['id'], b'changed')

    def test_cancel_queued_and_running(self):
        self.submit(1)
        self.q.cancel(f'{1:032x}')
        self.assertEqual(self.q.get(f'{1:032x}')['state'], 'cancelled')
        self.submit(2)
        self.q.dispatch_once()
        self.q.cancel(f'{2:032x}')
        self.finish()
        self.assertEqual(self.q.get(f'{2:032x}')['state'], 'cancelled')

    def test_capacity_excludes_terminal_tasks(self):
        self.q.capacity = 2
        self.submit(1)
        self.submit(2)
        with self.assertRaises(OverflowError):
            self.submit(3)
        self.q.cancel(f'{1:032x}')
        self.q.submit(dict(id=f'{3:032x}', sha256=hashlib.sha256(b'hello').hexdigest()))
        self.assertEqual(self.q.list()['total'], 2)

    def test_busy_race_keeps_original_ticket(self):
        self.backend.nodes = (NODES[0],)
        self.q.port_order = {'a': (4, 5), 'b': (4, 5)}
        first = self.submit(1)
        self.submit(2)
        original = self.backend.run
        self.backend.run = lambda *a, **kw: dict(exit_code=75, output=BUSY_TOKEN, node='a', seconds=0)
        self.q.dispatch_once()
        self.finish()
        self.assertEqual(self.q.get(first['id'])['position'], 1)
        self.assertIsNone(self.q.get(first['id'])['node'])
        self.backend.run = original
        self.q.dispatch_once()
        self.finish()
        self.assertEqual(self.q.get(first['id'])['state'], 'completed')

    def test_exit_75_without_exact_marker_is_not_replayed(self):
        self.backend.run = lambda *a, **kw: dict(exit_code=75, output='Program started\n' + BUSY_TOKEN, node='a', seconds=0)
        self.submit(1)
        self.q.dispatch_once()
        self.finish()
        self.assertEqual(self.q.get(f'{1:032x}')['state'], 'failed')

    def test_unknown_outcome_and_host_specific_cooldown(self):
        self.backend.disconnects.add('a')
        self.submit(1)
        with self.assertLogs('fpga_scheduler.queue', level='ERROR'):
            self.q.dispatch_once()
            self.finish()
        task = self.q.get(f'{1:032x}')
        self.assertEqual(task['state'], 'failed')
        self.assertIn('unknown', task['error'])
        inventory = self.q.inventory()['devices']
        self.assertFalse(next(p for p in inventory if p['node'] == 'a' and p['number'] == 4)['available'])
        self.assertTrue(next(p for p in inventory if p['node'] == 'b' and p['number'] == 4)['available'])
        self.q.dispatch_once()
        self.assertEqual(len(self.backend.events), 2)

    def test_execution_failure_and_timeout_return_results(self):
        self.backend.failures.add('a')
        self.submit(1)
        self.q.dispatch_once()
        self.finish()
        self.assertEqual(self.q.get(f'{1:032x}')['result']['exit_code'], 1)
        self.backend.duration = 1.1
        self.submit(2, timeout=1)
        self.q.dispatch_once()
        self.finish()
        self.assertEqual(self.q.get(f'{2:032x}')['result']['exit_code'], 124)

    def test_failed_transfer_does_not_start_hardware(self):
        self.backend.transfer_failures.add('a')
        self.submit(1)
        with self.assertLogs('fpga_scheduler.queue', level='ERROR'):
            self.q.dispatch_once()
            self.finish()
        self.assertFalse(self.backend.events)
        self.assertIn('not started', self.q.get(f'{1:032x}')['error'])

    def test_queue_expiry(self):
        self.submit(1, queue_timeout=1)
        with self.q.db:
            self.q.db.execute('UPDATE jobs SET created=?', (time.time() - 2,))
        self.q.dispatch_once()
        self.assertEqual(self.q.get(f'{1:032x}')['state'], 'failed')
        self.assertFalse(self.backend.events)

    def test_storage_wait_survives_restart_and_expiry_explains_the_cause(self):
        def reserve(payload, image):
            raise StorageQuotaExceeded('resident', used_bytes=99, reserved_bytes=1,
                                       requested_bytes=20, limit_bytes=100)
        self.backend.reserve_storage = reserve
        task = self.submit(1, queue_timeout=1)
        with self.assertLogs('fpga_scheduler.queue', level='WARNING'):
            self.q.dispatch_once()
        waiting = self.q.get(task['id'])
        self.q.close()
        self.q = JobQueue(self.root / 'jobs.db', self.backend, self.images)
        self.assertEqual(self.q.get(task['id']), waiting)
        with self.q.db:
            self.q.db.execute('UPDATE jobs SET created=? WHERE id=?', (time.time() - 2, task['id']))
        self.q.dispatch_once()
        failed = self.q.get(task['id'])
        self.assertEqual(failed['state'], 'failed')
        self.assertIsNone(failed['started'])
        self.assertIsNone(failed['fpga'])
        self.assertEqual(failed['error_details']['error_code'], 'queue_timeout')
        self.assertEqual(failed['error_details']['cause']['error_code'], 'resident_storage_quota_exceeded')
        self.assertEqual(failed['error_details']['cause']['used_bytes'], 99)
        self.assertIn('storage capacity', failed['error'])
        self.assertFalse(self.backend.events)
        self.assertTrue(self.images.path(task['id']).exists())

    def test_cancelling_a_storage_wait_does_not_block_the_next_ticket(self):
        def reserve(payload, image):
            if payload['id'] == f'{1:032x}':
                raise StorageQuotaExceeded('resident', used_bytes=100, requested_bytes=1, limit_bytes=100)
        self.backend.reserve_storage = reserve
        first, second = self.submit(1), self.submit(2)
        with self.assertLogs('fpga_scheduler.queue', level='WARNING'):
            self.q.dispatch_once()
        self.q.cancel(first['id'])
        self.q.dispatch_once()
        self.finish()
        self.assertEqual(self.q.get(first['id'])['state'], 'cancelled')
        self.assertEqual(self.q.get(second['id'])['state'], 'completed')
        self.assertNotIn('waiting_details', self.q.get(second['id']))

    def test_prepare_storage_failure_is_retried_without_hardware_cooldown(self):
        def prepare(*args, **kwargs):
            raise StorageQuotaExceeded('resident', used_bytes=100, requested_bytes=1, limit_bytes=100)
        self.backend.prepare = prepare
        task = self.submit(1)
        with self.assertLogs('fpga_scheduler.queue', level='WARNING'):
            self.q.dispatch_once()
            self.finish()
        self.assertEqual(self.q.get(task['id'])['state'], 'queued')
        self.assertIsNone(self.q.get(task['id'])['started'])
        self.assertFalse(self.backend.events)
        self.assertFalse(self.q.reserved)
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM cooldowns').fetchone()[0], 0)

    def test_expiry_during_probe(self):
        self.submit(1, queue_timeout=1)
        with self.q.db:
            self.q.db.execute('UPDATE jobs SET created=?', (time.time() - .9,))
        original = self.backend.probe
        def slow_probe(node):
            time.sleep(.15)
            return original(node)
        self.backend.probe = slow_probe
        self.q.dispatch_once()
        self.assertEqual(self.q.get(f'{1:032x}')['state'], 'failed')
        self.assertFalse(self.backend.events)

    def test_restart_retains_queue_and_marks_running_unknown(self):
        self.submit(1)
        self.submit(2)
        with self.q.db:
            self.q.db.execute("UPDATE jobs SET state='running' WHERE id=?", (f'{1:032x}',))
        self.q.close()
        self.q = JobQueue(self.root / 'jobs.db', self.backend, self.images)
        self.assertIn('unknown', self.q.get(f'{1:032x}')['error'])
        self.assertEqual(self.q.get(f'{2:032x}')['state'], 'queued')

    def test_retention_protects_active_and_recent_terminal_input(self):
        for i in (1, 2, 3):
            self.submit(i)
        self.q.cancel(f'{2:032x}')
        self.q.cancel(f'{3:032x}')
        old = time.time() - 8 * 86400
        for i in (1, 2, 3):
            os.utime(self.images.path(f'{i:032x}').parent, (old, old))
        with self.q.db:
            self.q.db.execute('UPDATE jobs SET finished=? WHERE id=?', (old, f'{3:032x}'))
        self.backend.busy.update(('a', 'b'))
        self.q.dispatch_once()
        self.assertTrue(self.images.path(f'{1:032x}').exists())
        self.assertTrue(self.images.path(f'{2:032x}').exists())
        self.assertFalse(self.images.path(f'{3:032x}').exists())
        with self.assertRaises(KeyError):
            self.q.get(f'{3:032x}')

    def test_close_drains_running_workers_and_preserves_waiters(self):
        self.backend.duration = .15
        for i in range(1, 7):
            self.submit(i)
        self.q.dispatch_once()
        self.q.close()
        self.assertFalse(self.q.workers)
        self.q = JobQueue(self.root / 'jobs.db', self.backend, self.images)
        self.assertEqual(self.q.list(('completed',))['total'], 4)
        self.assertEqual(self.q.list()['total'], 2)

    def test_concurrent_submit_and_pagination(self):
        failures = []
        def submit(i):
            try:
                self.submit(i)
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=submit, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(failures)
        page = self.q.list(limit=5, offset=5)
        self.assertEqual(page['total'], 20)
        self.assertEqual([j['position'] for j in page['jobs']], list(range(6, 11)))


class InputTests(unittest.TestCase):
    def test_validation_rejects_shell_paths_booleans_and_bounds(self):
        payload = dict(id='a' * 32, sha256='b' * 64)
        for invalid in (None, [], dict(payload, id='../x'), dict(payload, command='ls'),
                        dict(payload, timeout=True), dict(payload, timeout=601),
                        dict(payload, idle_seconds=0), dict(payload, queue_timeout=86401),
                        dict(payload, sha256='x' * 64)):
            with self.assertRaises(ValueError):
                validate(invalid)
        self.assertEqual(validate(payload)['timeout'], 600)

    def test_upload_validation_hash_and_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            images = Images(root)
            for content in (b'', b'x' * (MAX_IMAGE + 1)):
                with self.assertRaises(ValueError):
                    images.put('a' * 32, content)
            digest = images.put('a' * 32, b'hello')
            images = Images(root)
            with self.assertRaises(ValueError):
                images.freeze('a' * 32, '0' * 64)
            with self.assertRaises(ValueError):
                images.freeze('b' * 32, digest)
            images.freeze('a' * 32, digest)
            with self.assertRaises(Conflict):
                images.put('a' * 32, b'hello')

    def test_configuration_paths_nodes_and_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.toml'
            base = 'backend="simulator"\nstate_dir="state"\ntoken_file="token"\n[[nodes]]\nid="a"\nports=[4,5]\n'
            path.write_text(base)
            cfg = load(path)
            self.assertEqual(cfg.state_dir, Path(root) / 'state')
            self.assertEqual(cfg.nodes[0].ports, (4, 5))
            for text in (base.replace('[4,5]', '[4,4]'), base.replace('[4,5]', '[true]'),
                         'port=true\n' + base, 'oops=1\n' + base,
                         'client_heartbeat_seconds=0\n' + base,
                         'client_heartbeat_seconds=0.001\n' + base,
                         'client_lease_seconds=4000\n' + base,
                         'client_heartbeat_seconds=20\n' + base,
                         base + '[[nodes]]\nid="a"\nports=[1]\n'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load(path)

    def test_bosc_configuration_supports_ports_beyond_two_boards(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.toml'
            for port in (43, 8, 0):
                (Path(root) / f'init{port}.tcl').write_text('# config path fixture\n')
            for backend in ('bosc', 'bosc-resident'):
                base = ('backend="' + backend + '"\nstate_dir="state"\ntoken_file="token"\n'
                        '[resident]\nhardware_project="project"\nlock_dir="locks"\n'
                        '[resident.init_scripts]\n"43"="init43.tcl"\n"8"="init8.tcl"\n"0"="init0.tcl"\n'
                        '[[nodes]]\nid="a"\nports=[43,8,0]\n'
                        'root="/runtime"\nuvhs_shell="/vendor/uvhs2_shell"\n')
                if backend == 'bosc':
                    base += 'ssh_host="fpga"\n'
                with self.subTest(backend=backend):
                    path.write_text(base)
                    self.assertEqual(load(path).nodes[0].ports, (43, 8, 0))
                for ports in ('[-1]', '[43,43]', '[true]', '[8.0]', '[]'):
                    with self.subTest(backend=backend, ports=ports):
                        path.write_text(base.replace('[43,8,0]', ports))
                        with self.assertRaisesRegex(ValueError, 'distinct nonnegative integers'):
                            load(path)


class FailureBoundaryTests(unittest.TestCase):
    setUp = QueueTests.setUp
    tearDown = QueueTests.tearDown
    submit = QueueTests.submit
    finish = QueueTests.finish

    def test_error_only_response_has_no_partial_result(self):
        self.backend.run = lambda *args, **kwargs: dict(error='ssh')
        task = self.submit(1)
        self.q.dispatch_once()
        self.finish()
        result = self.q.get(task['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('result', result)
        self.assertIn('unknown', result['error'])

    def test_nonfinite_result_is_failed_and_remains_queryable(self):
        self.backend.run = lambda *args, **kwargs: dict(exit_code=0, output='invalid', seconds=float('nan'))
        task = self.submit(1)
        with self.assertLogs('fpga_scheduler.queue', level='ERROR'):
            self.q.dispatch_once()
            self.finish()
        result = self.q.get(task['id'])
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('result', result)

    def test_retention_does_not_block_healthy_dispatch(self):
        release = threading.Event()
        called = threading.Event()
        def cleanup(before):
            called.set()
            release.wait(5)
        self.backend.cleanup = cleanup
        self.submit(1)
        try:
            self.q.dispatch_once()
            self.assertTrue(called.wait(1))
            self.finish()
            self.assertEqual(self.q.get(f'{1:032x}')['state'], 'completed')
            self.assertTrue(self.q.maintenance.is_alive())
        finally:
            release.set()
