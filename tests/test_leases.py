"""Client lifetime is independent of HTTP reads; hardware retirement is serial."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fpga_scheduler import Client, SchedulerError
from fpga_scheduler.backends.simulator import Simulator
from fpga_scheduler.config import Config, Node
from fpga_scheduler.models import BUSY_TOKEN, Conflict
from fpga_scheduler.queue import JobQueue
from fpga_scheduler.service import Application
from fpga_scheduler.storage import Images

PROJECT = Path(__file__).resolve().parents[1]
TOKEN = 'lease-test-' * 5


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Condition did not become true before deadline')


class LeaseQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images = Images(self.root / 'images')
        self.backend = Simulator((Node('local', (4,)),), duration=3)
        self.q = JobQueue(self.root / 'jobs.db', self.backend, self.images, cooldown=60)
        self.owner = 'a' * 32
        self.q.open_session(self.owner, dict(heartbeat_seconds=.05, lease_seconds=.3))

    def tearDown(self):
        for worker in list(self.q.workers):
            self.q.cancel(worker.name.removeprefix('fpga-'))
        self.q.close()
        self.tmp.cleanup()

    def submit(self, i=1):
        ident = f'{i:032x}'
        return self.q.submit(dict(id=ident, sha256=self.images.put(ident, b'image')), self.owner)

    def finish(self):
        for worker in list(self.q.workers):
            worker.join(4)
        self.assertFalse(self.q.workers)

    def expire(self):
        with self.q.lock, self.q.db:
            self.q.db.execute('UPDATE client_sessions SET heartbeat=? WHERE id=?', (time.time() - 5, self.owner))

    def test_expired_queued_and_late_heartbeat_cannot_revive(self):
        task = self.submit()
        self.expire()
        with self.assertRaises(Conflict):
            self.q.heartbeat(self.owner)
        self.assertEqual(self.q.get(task['id'])['state'], 'cancelled')
        self.assertFalse(self.backend.events)
        with self.assertRaises(Conflict):
            self.q.open_session(self.owner, {})
        with self.assertRaises(Conflict):
            self.submit(2)
        self.assertEqual(self.q.cancel(task['id'])['state'], 'cancelled')

    def test_preparation_cancel_does_not_start_or_release_slot_early(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend.prepare
        def prepare(*args, **kwargs):
            entered.set()
            release.wait(2)
            original(*args, **kwargs)
        task = self.submit()
        self.submit(2)
        with patch.object(self.backend, 'prepare', side_effect=prepare):
            self.q.dispatch_once()
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.q.cancel(task['id'])['state'], 'cancelling')
            self.assertIsNone(self.q.get(task['id'])['finished'])
            self.assertIn(('local', 4), self.q.reserved)
            self.q.dispatch_once()
            self.assertFalse(self.backend.events)
            self.assertEqual(self.q.get(f'{2:032x}')['state'], 'queued')
            release.set()
            self.finish()
        self.assertEqual(self.q.get(task['id'])['state'], 'cancelled')
        self.assertFalse(self.backend.events)
        self.assertFalse(self.q.reserved)
        self.assertEqual(self.q.db.execute('SELECT count(*) FROM cooldowns').fetchone()[0], 0)

    def test_cancel_during_inventory_never_assigns_stale_waiter(self):
        task = self.submit()
        original = self.backend.probe
        def probe(node):
            self.q.cancel(task['id'])
            return original(node)
        with patch.object(self.backend, 'probe', side_effect=probe):
            self.q.dispatch_once()
        self.assertEqual(self.q.get(task['id'])['state'], 'cancelled')
        self.assertFalse(self.backend.events)

    def test_busy_retry_loses_to_accepted_cancel(self):
        task = self.submit()
        def busy(*args, **kwargs):
            self.q.cancel(task['id'])
            return dict(exit_code=75, output=BUSY_TOKEN, seconds=0)
        with patch.object(self.backend, 'run', side_effect=busy):
            self.q.dispatch_once()
            self.finish()
        self.assertEqual(self.q.get(task['id'])['state'], 'cancelled')

    def test_completed_decision_wins_late_cancel_during_publication(self):
        task = self.submit()
        def completed(payload, node, port, *, cancellation):
            cancellation.finish()
            self.assertEqual(self.q.cancel(task['id'])['state'], 'running')
            return dict(exit_code=0, output='done', seconds=0)
        with patch.object(self.backend, 'run', side_effect=completed):
            self.q.dispatch_once()
            self.finish()
        self.assertEqual(self.q.cancel(task['id'])['state'], 'completed')

    def test_restart_expires_dead_owner_preserves_live_queue_no_replay(self):
        first, second = self.submit(), self.submit(2)
        with self.q.db:
            self.q.db.execute("UPDATE jobs SET state='cancelling' WHERE id=?", (first['id'],))
        self.expire()
        self.q.close()
        self.q = JobQueue(self.root / 'jobs.db', self.backend, self.images)
        self.assertEqual(self.q.get(first['id'])['state'], 'failed')
        self.assertIn('unknown', self.q.get(first['id'])['error'])
        self.assertEqual(self.q.get(second['id'])['state'], 'cancelled')
        self.assertFalse(self.backend.events)

    def test_restart_uses_reliable_cancelled_terminal_result(self):
        task = self.submit()
        self.q.cancel(task['id'])
        saved = self.q.get(task['id'])['result']
        with self.q.db:
            self.q.db.execute("UPDATE jobs SET state='cancelling',result=NULL WHERE id=?", (task['id'],))
        self.q.close()
        self.backend.recover = lambda job: saved
        self.q = JobQueue(self.root / 'jobs.db', self.backend, self.images)
        self.assertEqual(self.q.get(task['id'])['state'], 'cancelled')
        self.assertEqual(self.q.get(task['id'])['result'], saved)
        self.assertFalse(self.backend.events)

    def test_legacy_backend_explicitly_rejects_leases_and_running_cancel(self):
        self.backend.supports_cancellation = False
        with self.assertRaisesRegex(Conflict, 'does not support'):
            self.q.open_session('b' * 32, {})
        task = self.submit()
        self.q.dispatch_once()
        with self.assertRaisesRegex(Conflict, 'cannot safely cancel'):
            self.q.cancel(task['id'])
        self.backend.supports_cancellation = True
        self.q.cancel(task['id'])
        self.finish()


class ClientLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.token = self.root / 'token'
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        self.app = Application(Config('simulator', self.root / 'state', self.token,
                                      (Node('local', (4,)),), port=0, poll_interval=.01,
                                      client_heartbeat_seconds=.1, client_lease_seconds=.6,
                                      simulator=dict(duration=5)))
        self.endpoint = 'http://127.0.0.1:' + str(self.app.server.server_address[1])
        self.app.queue.start()
        self.server_thread = threading.Thread(target=self.app.server.serve_forever,
                                              kwargs={'poll_interval': .01})
        self.server_thread.start()
        self.image = self.root / 'hello.bin'
        self.image.write_bytes(b'hello')
        self.clients, self.processes = [], []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        for client in self.clients:
            client.close()
        # Retire any killed process's outstanding jobs before server shutdown.
        for task in self.app.queue.list()['jobs']:
            self.app.queue.cancel(task['id'])
        self.app.server.shutdown()
        self.server_thread.join()
        self.app.close()
        self.tmp.cleanup()

    def client(self, **kwargs):
        client = Client(self.endpoint, token_file=self.token, request_timeout=1, **kwargs)
        self.clients.append(client)
        return client

    def test_heartbeat_without_output_iteration_and_close_reader(self):
        client = self.client()
        reader = client.run_test(self.image)
        reader.close()
        time.sleep(.85)
        self.assertEqual(client.job(reader.id).state, 'running')
        self.assertTrue(client._heartbeat_thread.daemon)
        client.cancel(reader.id)
        done = client.wait(reader.id, timeout=2, poll_interval=.01)
        self.assertEqual(done.state, 'cancelled')
        self.assertEqual(done.result.execution_status, 'cancelled')

    def test_independent_clients_multiple_jobs_and_idempotent_close(self):
        a, b = self.client(), self.client()
        tasks = [a.submit(self.image) for _ in range(3)]
        other = b.submit(self.image)
        self.assertNotEqual(a._session_id, b._session_id)
        a.close()
        a.close()
        for task in tasks:
            self.assertEqual(b.wait(task.id, timeout=2, poll_interval=.01).state, 'cancelled')
        self.assertIn(b.job(other.id).state, ('queued', 'running'))
        with self.assertRaises(SchedulerError):
            a.submit(self.image)

    def test_query_and_output_readers_do_not_extend_owner_lease(self):
        a, observer = self.client(), self.client()
        task = a.submit(self.image)
        a._heartbeat_stop.set()  # Simulate an unreachable submitter.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            observed = observer.job(task.id)
            observer._request('/jobs/' + task.id + '/output?offset=0')
            if observed.state == 'cancelled':
                break
            time.sleep(.03)
        self.assertEqual(observer.job(task.id).state, 'cancelled')
        self.assertIsNone(observer._heartbeat_thread)
        with self.assertRaises(SchedulerError) as caught:
            a._request('/sessions/' + a._session_id + '/heartbeat', {}, 'POST')
        self.assertEqual(caught.exception.status, 409)

    def test_expiry_continues_while_inventory_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.backend.probe
        def probe(node):
            entered.set()
            release.wait(2)
            return original(node)
        owner = self.client()
        with patch.object(self.app.backend, 'probe', side_effect=probe):
            task = owner.submit(self.image)
            self.assertTrue(entered.wait(1))
            owner._heartbeat_stop.set()
            try:
                eventually(lambda: self.app.queue.get(task.id)['state'] == 'cancelled', timeout=1.5)
                self.assertFalse(self.app.backend.events)
            finally:
                release.set()

    def test_short_heartbeat_failure_recovers_same_session(self):
        a = self.client()
        original = a._request
        failures = []
        def request(path, *args, **kwargs):
            if path.endswith('/heartbeat') and len(failures) < 2:
                failures.append(path)
                raise SchedulerError('transient network failure')
            return original(path, *args, **kwargs)
        with patch.object(a, '_request', side_effect=request):
            task = a.submit(self.image)
            time.sleep(.85)
            self.assertEqual(a.job(task.id).state, 'running')
            self.assertEqual(len(failures), 2)

    @unittest.skipUnless(hasattr(os, 'fork'), 'fork requires Unix')
    def test_inherited_client_gets_independent_child_session(self):
        owner = self.client()
        first = owner.submit(self.image)
        parent_session = owner._session_id
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                child_task = owner.submit(self.image)
                os.write(write_fd, json.dumps(dict(id=child_task.id, session=owner._session_id)).encode())
                owner.close()
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        try:
            child = json.loads(os.read(read_fd, 4096))
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertNotEqual(child['session'], parent_session)
            self.assertEqual(owner.job(child['id']).state, 'cancelled')
            self.assertIn(owner.job(first.id).state, ('queued', 'running'))
        finally:
            os.close(read_fd)

    def child(self, mode):
        code = '''import json, sys, time
from fpga_scheduler import Client
c = Client(sys.argv[1], token_file=sys.argv[2], request_timeout=1)
t = c.submit(sys.argv[3])
print(json.dumps({'id': t.id}), flush=True)
if sys.argv[4] == 'exception':
    raise RuntimeError('deliberate unhandled exception')
if sys.argv[4] != 'normal':
    time.sleep(30)
'''
        env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src'), 'NO_PROXY': '*', 'no_proxy': '*'}
        process = subprocess.Popen([sys.executable, '-c', code, self.endpoint,
                                    str(self.token), str(self.image), mode],
                                   env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        ident = json.loads(process.stdout.readline())['id']
        return process, ident

    def test_normal_exit_and_unhandled_exception_proactively_cancel(self):
        self.app.backend.busy.add('local')
        observer = self.client()
        for mode in ('normal', 'exception'):
            with self.subTest(mode=mode):
                process, ident = self.child(mode)
                process.communicate(timeout=3)
                task = observer.wait(ident, timeout=2, poll_interval=.01)
                self.assertEqual(task.state, 'cancelled')
                self.assertEqual(task.result.details['cancel_reason'], 'Submitting client closed')
                self.assertFalse(self.app.backend.events)

    def test_ctrl_c_cancels_running_and_sigkill_uses_lease(self):
        observer = self.client()
        for signum in (signal.SIGINT, signal.SIGKILL):
            with self.subTest(signum=signum):
                process, ident = self.child('wait')
                eventually(lambda: observer.job(ident).state == 'running')
                process.send_signal(signum)
                process.communicate(timeout=3)
                task = observer.wait(ident, timeout=2, poll_interval=.01)
                self.assertEqual(task.state, 'cancelled')
                reason = 'Submitting client closed' if signum == signal.SIGINT else 'Submitting client lease expired'
                self.assertEqual(task.result.details['cancel_reason'], reason)

    def test_session_header_required_and_job_cannot_change_owner(self):
        a, b = self.client(), self.client()
        task = a.submit(self.image)
        with self.assertRaises(SchedulerError) as caught:
            b.submit(self.image, ident=task.id)
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(SchedulerError) as caught:
            a._request('/jobs', {}, 'POST')
        self.assertEqual(caught.exception.status, 409)

    def test_foreground_cli_prints_output_and_ctrl_c_cancels(self):
        env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src'), 'NO_PROXY': '*', 'no_proxy': '*'}
        command = [sys.executable, '-m', 'fpga_scheduler', '--endpoint', self.endpoint,
                   '--token-file', str(self.token), 'run', str(self.image)]
        process = subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        eventually(lambda: bool(self.app.backend.events))
        ident = self.app.backend.events[-1][1]
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 130, stderr)
        self.assertIn(ident, stderr)
        observer = self.client()
        task = observer.wait(ident, timeout=2, poll_interval=.01)
        self.assertEqual(task.state, 'cancelled')
        self.assertEqual(task.result.details['cancel_reason'], 'Submitting client closed')

    def test_session_api_validates_options_and_closed_identity(self):
        client = self.client()
        for options in ({'heartbeat_seconds': True}, {'lease_seconds': .001},
                        {'heartbeat_seconds': 2, 'lease_seconds': 1}, {'extra': 1}):
            with self.subTest(options=options), self.assertRaises(SchedulerError) as caught:
                client._request('/sessions/' + 'c' * 32, options, 'PUT')
            self.assertEqual(caught.exception.status, 400)
        task = client.submit(self.image)
        client.close()
        with self.assertRaises(SchedulerError) as caught:
            client._request('/sessions/' + client._session_id, {}, 'PUT')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.app.queue.get(task.id)['state'] in ('cancelling', 'cancelled'), True)

    def test_expired_session_is_not_recreated_for_more_work(self):
        a = self.client()
        task = a.submit(self.image)
        a._heartbeat_stop.set()
        eventually(lambda: self.app.queue.get(task.id)['state'] == 'cancelled')
        with self.assertRaises(SchedulerError) as caught:
            a.submit(self.image)
        self.assertEqual(caught.exception.status, 409)


if __name__ == '__main__':
    unittest.main()
