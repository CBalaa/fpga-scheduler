from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError

from fpga_scheduler.client import Client, SchedulerError
from fpga_scheduler.config import Config, Node
from fpga_scheduler.service import Application, StateLock, read_token

PROJECT = Path(__file__).resolve().parents[1]
TOKEN = 'a' * 48


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.token = self.root / 'token'
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)
        cfg = Config('simulator', self.root / 'state', self.token,
                     (Node('a', (4,)), Node('b', (4,))), port=0, poll_interval=.01,
                     simulator={'duration': .2})
        self.app = Application(cfg)
        self.endpoint = 'http://127.0.0.1:' + str(self.app.server.server_address[1])
        self.app.queue.start()
        self.thread = threading.Thread(target=self.app.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.client = Client(self.endpoint, token=TOKEN)
        self.image = self.root / 'image.bin'
        self.image.write_bytes(b'hello FPGA')
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.client.close()
        self.app.server.shutdown()
        self.thread.join()
        self.app.close()
        self.tmp.cleanup()

    def cli(self, *args, json_output=True):
        env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src'), 'NO_PROXY': '*', 'no_proxy': '*'}
        flags = ['--json'] if json_output else []
        return subprocess.run([sys.executable, '-m', 'fpga_scheduler', *flags, '--endpoint', self.endpoint,
                               '--token-file', str(self.token), *args], capture_output=True, text=True, env=env)

    def request(self, route, method='GET', body=None, token=TOKEN):
        request = Request(self.endpoint + route, data=body, method=method,
                          headers={'Authorization': 'Bearer ' + token})
        return self.opener.open(request, timeout=3)

    def test_sdk_round_trip_and_devices(self):
        task = self.client.submit(self.image)
        self.assertEqual(len(task.id), 32)
        done = self.client.wait(task.id, poll_interval=.01, timeout=3)
        self.assertEqual(done.state, 'completed')
        self.assertTrue(done.result.ok)
        self.assertEqual(done.result.execution_status, 'exited')
        self.assertEqual(done.result.app_exit_code, 0)
        self.assertIn('SIMULATED', done.result.output)
        self.assertEqual(self.client.jobs(('completed',))['total'], 1)
        self.assertEqual(len(self.client.devices()['devices']), 2)
        self.assertTrue(self.client.health()['dispatcher_alive'])

    def test_cli_submit_wait_list_and_health(self):
        submitted = self.cli('submit', str(self.image))
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        ident = json.loads(submitted.stdout)['id']
        waited = self.cli('wait', ident, '--poll-interval', '.01', '--timeout', '3')
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)['state'], 'completed')
        self.assertEqual(json.loads(waited.stdout)['result']['app_exit_code'], 0)
        listed = self.cli('jobs', '--state', 'completed')
        self.assertEqual(json.loads(listed.stdout)['total'], 1)
        health = self.cli('health')
        self.assertEqual(json.loads(health.stdout)['hardware_checked'], False)
        devices = self.cli('devices')
        self.assertEqual(len(json.loads(devices.stdout)['devices']), 2)
        run = self.cli('run', str(self.image))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['state'], 'completed')

    def test_cli_readable_output_over_real_http(self):
        run = self.cli('run', str(self.image), json_output=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.count('SIMULATED FPGA execution PASS'), 1)
        self.assertIn('PASS\nTask: ', run.stdout)
        self.assertIn('State: completed', run.stdout)
        self.assertIn('App exit code: 0', run.stdout)
        ident = self.client.jobs(('completed',))['jobs'][0]['id']
        for args, expected in ((('jobs', '--state', 'completed'), ('STATE', ident, 'completed')),
                               (('job', ident), ('State: completed', 'UART:', 'SIMULATED')),
                               (('devices',), ('AVAILABLE', 'NODE')),
                               (('health',), ('API: OK', 'Dispatcher: running'))):
            with self.subTest(command=args):
                process = self.cli(*args, json_output=False)
                self.assertEqual(process.returncode, 0, process.stderr)
                for text in expected:
                    self.assertIn(text, process.stdout)

    def test_cli_cancel_and_failed_wait_exit_status(self):
        self.app.queue.backend.busy.update(('a', 'b'))
        ident = self.client.submit(self.image).id
        cancelled = self.cli('cancel', ident)
        self.assertEqual(json.loads(cancelled.stdout)['state'], 'cancelled')
        waited = self.cli('wait', ident)
        self.assertEqual(waited.returncode, 1)
        self.assertEqual(json.loads(waited.stdout)['state'], 'cancelled')

    def test_nonzero_application_code_and_clean_uart_survive_http_and_cli(self):
        self.app.queue.backend.run = lambda payload, node, port, **kw: dict(
            output='verify hellohspRun> : PASS', uart_output='verify hello: PASS\n',
            exit_code=1, execution_status='exited', app_exit_code=7, details={}, node=node, seconds=.01)
        task = self.client.submit(self.image)
        done = self.client.wait(task.id, timeout=3, poll_interval=.01)
        self.assertEqual(done.state, 'failed')
        self.assertEqual(done.result.app_exit_code, 7)
        self.assertEqual(done.result.uart_output, 'verify hello: PASS\n')
        self.assertFalse(done.result.ok)
        queried = json.loads(self.cli('job', task.id).stdout)
        self.assertEqual(queried['result']['app_exit_code'], 7)
        self.assertEqual(queried['result']['uart_output'], done.result.uart_output)

    def test_shared_clients_and_queue_survives_wait_timeout(self):
        self.app.queue.backend.duration = .1
        clients = [Client(self.endpoint, token=TOKEN) for _ in range(8)]
        tasks, errors = [], []
        def submit(client):
            try:
                tasks.append(client.submit(self.image))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=submit, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(tasks), 8)
        last = max(tasks, key=lambda task: task.created)
        with self.assertRaises(SchedulerError) as caught:
            self.client.wait(last.id, timeout=0)
        self.assertEqual(caught.exception.task_id, last.id)
        for task in tasks:
            self.assertTrue(self.client.wait(task.id, timeout=3, poll_interval=.01).result.ok)
        self.assertEqual(self.client.jobs(('completed',))['total'], 8)
        self.assertTrue(any(t.position is not None for t in tasks))

    def test_frozen_image_and_idempotent_client_retry(self):
        task = self.client.submit(self.image)
        retry = self.client.submit(self.image, ident=task.id)
        self.assertEqual(task.id, retry.id)
        with self.assertRaises(SchedulerError) as error:
            self.client.submit(self.image, ident=task.id, timeout=1)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.client.jobs(('queued', 'running', 'completed'))['total'], 1)

    def test_lost_submit_response_uses_same_id(self):
        original = self.client._request
        posts = []
        def request(path, payload=None, method='GET', **kwargs):
            result = original(path, payload, method, **kwargs)
            if method == 'POST':
                posts.append(payload)
                if len(posts) == 1:
                    raise SchedulerError('Response lost')
            return result
        with patch.object(self.client, '_request', side_effect=request):
            task = self.client.submit(self.image)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(self.client.jobs(('queued', 'running', 'completed'))['total'], 1)
        self.assertTrue(self.client.wait(task.id, timeout=3, poll_interval=.01).result.ok)

    def test_unauthorized_and_unknown_routes(self):
        for route in ('/health', '/jobs', '/devices', '/jobs/' + 'a' * 32):
            with self.assertRaises(HTTPError) as caught:
                self.request(route, token='wrong')
            self.assertEqual(caught.exception.code, 401)
        for route in ('/run', '/shell', '/jobs/../../file'):
            with self.assertRaises(HTTPError) as caught:
                self.request(route, 'POST', b'{}')
            self.assertEqual(caught.exception.code, 404)

    def test_bad_json_payload_size_hash_and_filters(self):
        for route, method, body, status in (
            ('/jobs', 'POST', b'not JSON', 409),
            ('/jobs', 'POST', b'[]', 409),
            ('/jobs', 'POST', json.dumps(dict(id='a' * 32, sha256='b' * 64)).encode(), 409),
            ('/jobs/' + 'a' * 32 + '/image', 'PUT', b'', 400),
            ('/jobs?state=unknown', 'GET', None, 400),
            ('/jobs?limit=-1', 'GET', None, 400),
            ('/jobs?limit=1&limit=2', 'GET', None, 400),
            ('/jobs?bad=1', 'GET', None, 400),
            ('/jobs/' + 'a' * 32, 'GET', None, 404),
        ):
            with self.subTest(route=route, body=body):
                with self.assertRaises(HTTPError) as caught:
                    self.request(route, method, body)
                self.assertEqual(caught.exception.code, status)

    def test_client_validation_before_upload(self):
        with patch.object(self.client, '_request') as request:
            for change in ({'timeout': True}, {'idle_seconds': 0}, {'queue_timeout': 86401}):
                with self.assertRaises(ValueError):
                    self.client.submit(self.image, **change)
            request.assert_not_called()

    def test_second_application_refused_without_touching_jobs(self):
        task = self.client.submit(self.image)
        with self.assertRaises(RuntimeError):
            Application(self.app.config)
        self.assertTrue(self.client.wait(task.id, timeout=3, poll_interval=.01).result.ok)

    def test_health_reports_dead_dispatcher(self):
        self.app.queue.stopping.set()
        self.app.queue.wake.set()
        self.app.queue.dispatcher.join()
        with self.assertRaises(SchedulerError) as caught:
            self.client.health()
        self.assertEqual(caught.exception.status, 503)


class LifecycleTests(unittest.TestCase):
    def test_token_permissions_and_exclusive_creation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'token'
            env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src')}
            argv = [sys.executable, '-m', 'fpga_scheduler', '--json', 'init-token', str(path)]
            first = subprocess.run(argv, capture_output=True, env=env)
            self.assertEqual(first.returncode, 0)
            self.assertGreaterEqual(len(read_token(path)), 32)
            second = subprocess.run(argv, capture_output=True, env=env)
            self.assertEqual(second.returncode, 1)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_token(path)

    def test_state_lock_cross_process(self):
        with tempfile.TemporaryDirectory() as root:
            lock = StateLock(root)
            code = 'from fpga_scheduler.service import StateLock; import sys; StateLock(sys.argv[1])'
            env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src')}
            result = subprocess.run([sys.executable, '-c', code, root], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            lock.close()
            result = subprocess.run([sys.executable, '-c', code, root], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
