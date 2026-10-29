"""Resident-style background inventory uses no hardware on the HTTP path."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from fpga_scheduler.backends.simulator import Simulator
from fpga_scheduler.client import Client
from fpga_scheduler.config import Node
from fpga_scheduler.queue import JobQueue
from fpga_scheduler.service import make_server
from fpga_scheduler.storage import Images


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Condition did not become true')


class BackgroundInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.images = Images(root / 'images')
        self.backend = Simulator((Node('local', (4,)),), duration=.2)
        self.backend.background_inventory = True
        self.queue = JobQueue(root / 'jobs.sqlite3', self.backend, self.images, interval=.01)
        self.release = threading.Event()
        self.server, self.server_thread = None, None

    def tearDown(self):
        self.release.set()
        if self.server:
            self.server.shutdown()
            self.server_thread.join(2)
            self.server.server_close()
        self.queue.close()
        self.tmp.cleanup()

    def http_client(self):
        token = 'x' * 40
        self.server = make_server(('127.0.0.1', 0), self.queue, self.images, token)
        self.server_thread = threading.Thread(target=self.server.serve_forever,
                                               kwargs={'poll_interval': .01})
        self.server_thread.start()
        return Client(f'http://127.0.0.1:{self.server.server_address[1]}', token=token,
                      request_timeout=.3)

    def test_start_initializes_without_jobs_and_devices_does_not_wait_for_probe(self):
        entered = threading.Event()
        calls = []
        failure = 'Initialization Tcl rejected: shared clock is incompatible'

        def probe(node):
            calls.append(node)
            entered.set()
            self.release.wait(2)
            return [dict(number=4, available=False, state='RECOVERING', reasons=[failure])]

        self.backend.probe = probe
        client = self.http_client()
        # A status query before startup is read-only and does not initialize.
        pending = client.devices()
        self.assertEqual(calls, [])
        self.assertFalse(pending['devices'][0]['available'])
        self.assertIsNone(pending['sampled_at'])
        self.queue.start()
        self.assertTrue(entered.wait(1))
        try:
            for _ in range(3):
                status = client.devices()  # Would time out if it joined probe.
                self.assertEqual([p['number'] for p in status['devices']], [4])
                self.assertFalse(status['devices'][0]['available'])
                self.assertTrue(status['refreshing'])
            self.assertEqual(calls, ['local'])
            self.assertEqual(self.queue.list()['total'], 0)
        finally:
            self.release.set()
        eventually(lambda: client.devices()['devices'][0]['reasons'] == [failure])
        self.assertIsNotNone(client.devices()['sampled_at'])

    def test_status_does_not_wait_for_queue_lock_or_start_another_probe(self):
        entered, locked = threading.Event(), threading.Event()
        calls = []

        def probe(node):
            calls.append(node)
            entered.set()
            self.release.wait(2)
            return [dict(number=4, available=False, reasons=['Not ready'])]

        def hold_queue_lock():
            with self.queue.lock:
                locked.set()
                self.release.wait(2)

        self.backend.probe = probe
        self.queue.start()
        self.assertTrue(entered.wait(1))
        locker = threading.Thread(target=hold_queue_lock)
        locker.start()
        self.assertTrue(locked.wait(1))
        client = self.http_client()
        try:
            self.assertFalse(client.devices()['devices'][0]['available'])
            self.assertEqual(calls, ['local'])
        finally:
            self.release.set()
            locker.join(2)

    def test_ready_snapshot_is_overlaid_while_task_is_reserved(self):
        started = threading.Event()
        original = self.backend.probe

        def stale_ready_probe(node):
            # A hardware cache may still say READY after assignment. Queue
            # reservations must override that result until execution retires.
            return [dict(number=4, state='READY', available=True, reasons=[])]

        def run(payload, node, port, **kwargs):
            started.set()
            self.release.wait(2)
            return dict(exit_code=0, output='PASS', seconds=0,
                        execution_status='exited', app_exit_code=0)

        self.backend.probe, self.backend.run = stale_ready_probe, run
        self.queue.start()
        eventually(lambda: self.queue.inventory_snapshot()['devices'][0]['available'])
        ident = '1' * 32
        digest = self.images.put(ident, b'test')
        self.queue.submit(dict(id=ident, sha256=digest))
        self.assertTrue(started.wait(1))
        try:
            self.queue.inventory()  # Publish another stale READY result.
            status = self.queue.inventory_snapshot()['devices'][0]
            self.assertFalse(status['available'])
            self.assertIn('platform task active', status['reasons'])
        finally:
            self.release.set()
        eventually(lambda: self.queue.get(ident)['state'] == 'completed')
        eventually(lambda: self.queue.inventory_snapshot()['devices'][0]['available'])
        self.backend.probe = original

    def test_shutdown_drains_probe_and_no_waiting_caller_starts_late_probe(self):
        entered = threading.Event()
        initialization_stopped = threading.Event()
        calls = []
        closed = []

        def probe(node):
            calls.append(node)
            entered.set()
            self.release.wait(2)
            self.assertEqual(closed, [])
            return [dict(number=4, available=True, reasons=[])]

        self.backend.probe = probe
        self.backend.close = lambda: closed.append(True)
        self.backend.stop_initialization = initialization_stopped.set
        self.queue.start()
        self.assertTrue(entered.wait(1))
        waiting = threading.Thread(target=self.queue.inventory)
        waiting.start()
        stopper = threading.Thread(target=self.queue.close)
        stopper.start()
        eventually(self.queue.stopping.is_set)
        try:
            self.assertTrue(stopper.is_alive())
            self.assertTrue(initialization_stopped.wait(1))
            self.assertEqual(closed, [])
            self.assertFalse(self.queue.inventory_snapshot()['devices'][0]['available'])
        finally:
            self.release.set()
        waiting.join(2)
        stopper.join(2)
        self.assertFalse(waiting.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(calls, ['local'])
        self.assertEqual(closed, [True])
        self.queue.inventory_snapshot()
        self.assertEqual(calls, ['local'])

    def test_failed_task_invalidates_ready_even_without_cooldown_or_new_sample(self):
        started, sampled, publish = threading.Event(), threading.Event(), threading.Event()
        self.queue.cooldown = 0

        def ready_probe(node):
            return [dict(number=4, state='READY', available=True, reasons=[])]

        def old_ready_probe(node):
            result = ready_probe(node)
            sampled.set()
            publish.wait(2)
            return result

        def run(payload, node, port, **kwargs):
            started.set()
            self.release.wait(2)
            return dict(exit_code=125, output='Unacknowledged command', seconds=0,
                        execution_status='platform_error', app_exit_code=None,
                        details=dict(reset_confirmed=False))

        self.backend.probe, self.backend.run = ready_probe, run
        self.queue.inventory()
        ident = '2' * 32
        digest = self.images.put(ident, b'test')
        self.queue.submit(dict(id=ident, sha256=digest))
        self.queue.dispatch_once()
        self.assertTrue(started.wait(1))
        self.backend.probe = old_ready_probe
        stale_results = []
        stale_probe = threading.Thread(target=lambda: stale_results.append(self.queue.inventory()))
        stale_probe.start()
        self.assertTrue(sampled.wait(1))
        try:
            self.release.set()
            eventually(lambda: self.queue.get(ident)['state'] == 'failed')
            status = self.queue.inventory_snapshot()['devices'][0]
            self.assertFalse(status['available'])
            self.assertIn('awaiting fresh hardware status', status['reasons'][0])
            # This sample began before the failed task retired. Publishing its
            # old READY result must not restore availability either.
            publish.set()
            stale_probe.join(2)
            self.assertFalse(stale_probe.is_alive())
            self.assertFalse(stale_results[0]['devices'][0]['available'])
            self.assertFalse(self.queue.inventory_snapshot()['devices'][0]['available'])
            self.backend.probe = ready_probe
            self.queue.inventory()
            self.assertTrue(self.queue.inventory_snapshot()['devices'][0]['available'])
        finally:
            self.release.set()
            publish.set()
            stale_probe.join(2)

    def test_task_completion_wakes_inventory_before_long_poll_interval(self):
        self.queue.interval = 60
        calls, started = [], threading.Event()

        def probe(node):
            calls.append(node)
            return [dict(number=4, state='READY', available=True, reasons=[])]

        def run(payload, node, port, **kwargs):
            started.set()
            self.release.wait(2)
            return dict(exit_code=0, output='PASS', seconds=0,
                        execution_status='exited', app_exit_code=0)

        self.backend.probe, self.backend.run = probe, run
        self.queue.start()
        eventually(lambda: self.queue.inventory_snapshot()['devices'][0]['available'])
        ident = '3' * 32
        digest = self.images.put(ident, b'test')
        self.queue.submit(dict(id=ident, sha256=digest))
        self.assertTrue(started.wait(1))
        count = len(calls)
        self.release.set()
        eventually(lambda: self.queue.get(ident)['state'] == 'completed')
        eventually(lambda: len(calls) > count)

    def test_legacy_backends_keep_synchronous_inventory(self):
        self.queue.background_inventory = False
        calls = []
        self.backend.probe = lambda node: calls.append(node) or [
            dict(number=4, available=True, reasons=[])]
        self.assertTrue(self.queue.inventory_snapshot()['devices'][0]['available'])
        self.assertEqual(calls, ['local'])
        self.queue.start()
        self.assertIsNone(self.queue.inventory_monitor)


if __name__ == '__main__':
    unittest.main()
