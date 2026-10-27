"""Incremental UART over real HTTP, independent readers, and final results."""
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from fpga_scheduler import Client, OutputStream, SchedulerError
from fpga_scheduler.backends.resident import Resident
from fpga_scheduler.config import Config, Node
from fpga_scheduler.remote.uvhs_session import BoundedLog
from fpga_scheduler.service import Application


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        token = self.root / 'service.token'
        token.write_text('x' * 40)
        token.chmod(0o600)
        self.app = Application(Config('simulator', self.root / 'state', token,
                                      (Node('local', (4,)),), port=0, poll_interval=.01))
        self.endpoint = 'http://127.0.0.1:' + str(self.app.server.server_address[1])
        self.client = Client(self.endpoint, token_file=token, request_timeout=2)
        self.image = self.root / 'hello.bin'
        self.image.write_bytes(b'hello')
        self.release = threading.Event()
        self.wrote = threading.Event()
        self.exit_code = 0
        self.final_text = 'first: 你好\nlast\n'
        self.logs = SimpleNamespace(root=self.root / 'uart')
        self.app.backend.read_output = lambda ident, offset, limit: Resident.read_output(
            self.logs, ident, offset, limit)
        self.app.backend.run = self.execute
        self.app.queue.start()
        self.server_thread = threading.Thread(target=self.app.server.serve_forever,
                                              kwargs={'poll_interval': .01})
        self.server_thread.start()
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.release.set()
        self.client.close()
        self.app.server.shutdown()
        self.server_thread.join()
        self.app.close()
        self.tmp.cleanup()

    def execute(self, payload, node, port, **kwargs):
        path = self.logs.root / 'jobs' / payload['id'] / 'uart.raw.log'
        log = BoundedLog(path, 1048576)
        try:
            # Deliberately split a UTF-8 character across requests.
            log.write(b'first: \xe4')
            self.wrote.set()
            if not self.release.wait(5):
                raise RuntimeError('Test did not release execution')
            log.write('你好\nlast\n'.encode()[1:])
        finally:
            log.close()
        return dict(output=self.final_text, uart_output=self.final_text, node=node,
                    seconds=.01, exit_code=self.exit_code,
                    execution_status='exited', app_exit_code=7 if self.exit_code else 0)

    def test_default_returns_stream_before_finish_and_final_result(self):
        stream = self.client.run_test(self.image, poll_interval=.01)
        self.assertIsInstance(stream, OutputStream)
        self.assertIsNone(stream.result)
        self.assertTrue(self.wrote.wait(2))
        first = next(stream)
        self.assertEqual(first, 'first: ')
        self.assertEqual(self.client.job(stream.id).state, 'running')
        self.assertIsNone(stream.result)
        self.release.set()
        self.assertEqual(first + ''.join(stream), self.final_text)
        self.assertEqual(stream.result.uart_output, self.final_text)
        self.assertTrue(stream.result.ok)
        self.assertTrue(stream.closed)

    def test_two_readers_close_resume_and_chunked_replay(self):
        task = self.client.submit(self.image)
        self.assertTrue(self.wrote.wait(2))
        first = self.client.stream_output(task.id, poll_interval=.01)
        second = self.client.stream_output(task.id, poll_interval=.01)
        self.assertEqual(next(first), next(second))
        self.assertEqual(first.offset, len(b'first: '))
        offset = first.offset
        first.close()
        self.assertEqual(self.client.job(task.id).state, 'running')
        self.release.set()
        self.assertTrue(self.client.wait(task.id, timeout=2, poll_interval=.01).result.ok)
        # Public cursor excludes a buffered partial UTF-8 character.
        with self.client.stream_output(task.id, offset=offset) as resumed:
            self.assertEqual(''.join(resumed), '你好\nlast\n')
        self.assertEqual('first: ' + ''.join(second), self.final_text)
        value = self.app.queue.output(task.id, limit=2)
        self.assertFalse(value['done'])
        self.assertNotIn('result', value['task'])
        self.assertEqual(base64.b64decode(value['data']), b'fi')

    def test_show_prints_before_bin_finishes_without_duplicate_output(self):
        class Terminal(io.StringIO):
            def __init__(self):
                super().__init__()
                self.live = threading.Event()

            def write(self, text):
                if text == 'first: ':
                    self.live.set()
                return super().write(text)

        terminal = Terminal()
        results, errors = [], []

        def run():
            try:
                results.append(self.client.run_test(self.image, stream=False, show=True, poll_interval=.01))
            except Exception as error:
                errors.append(error)

        with redirect_stdout(terminal):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(terminal.live.wait(2))
                self.assertTrue(thread.is_alive())
            finally:
                self.release.set()
                thread.join(3)
        self.assertFalse(errors)
        self.assertTrue(results[0].ok)
        self.assertEqual(terminal.getvalue().count('first: '), 1)
        self.assertIn('你好\nlast\n', terminal.getvalue())

    def test_failure_timeout_and_cancel_preserve_task_id(self):
        stream = self.client.run_test(self.image, show=False, stream=True, poll_interval=.01)
        self.assertTrue(self.wrote.wait(2))
        self.exit_code = 1
        self.release.set()
        self.assertEqual(''.join(stream), self.final_text)
        self.assertFalse(stream.result.ok)
        self.assertEqual(stream.result.app_exit_code, 7)
        self.app.queue.backend.busy.add('local')
        task = self.client.submit(self.image)
        timed = self.client.stream_output(task.id, timeout=0)
        with self.assertRaises(SchedulerError) as error:
            next(timed)
        self.assertEqual(error.exception.task_id, task.id)
        self.assertEqual(self.client.job(task.id).state, 'queued')
        self.client.cancel(task.id)
        reader = self.client.stream_output(task.id)
        self.assertEqual(list(reader), [])
        self.assertEqual(reader.result.execution_status, 'cancelled')
        self.assertIsNone(reader.result.app_exit_code)

    def test_output_auth_validation_and_image_route_isolation(self):
        task = self.client.submit(self.image)
        self.assertTrue(self.wrote.wait(2))
        route = '/jobs/' + task.id + '/output'
        cases = [(route, 'GET', 'wrong', 401),
                 (route + '?offset=-1', 'GET', 'x' * 40, 400),
                 (route + '?offset=999', 'GET', 'x' * 40, 400),
                 (route + '?offset=0&offset=0', 'GET', 'x' * 40, 400),
                 (route + '?limit=65537', 'GET', 'x' * 40, 400),
                 (route + '?bad=1', 'GET', 'x' * 40, 400),
                 ('/jobs/' + 'f' * 32 + '/output', 'GET', 'x' * 40, 404),
                 (route, 'PUT', 'x' * 40, 404)]
        for path, method, token, status in cases:
            with self.subTest(path=path, method=method):
                request = Request(self.endpoint + path, method=method,
                                  headers={'Authorization': 'Bearer ' + token})
                with self.assertRaises(HTTPError) as error:
                    self.opener.open(request, timeout=2)
                self.assertEqual(error.exception.code, status)

    def test_disconnect_does_not_cancel_and_invalid_options_do_not_submit(self):
        stream = self.client.run_test(self.image, stream=True, show=False, poll_interval=.01)
        self.assertTrue(self.wrote.wait(2))
        with patch.object(self.client, '_request', side_effect=SchedulerError('Disconnected')):
            with self.assertRaises(SchedulerError) as error:
                next(stream)
        self.assertEqual(error.exception.task_id, stream.id)
        self.assertEqual(self.client.job(stream.id).state, 'running')
        self.release.set()
        resumed = self.client.stream_output(stream.id, poll_interval=.01)
        self.assertEqual(''.join(resumed), self.final_text)
        with patch.object(self.client, 'submit') as submit:
            for options in ({'stream': 1}, {'poll_interval': 0}, {'poll_interval': True}):
                with self.assertRaises(ValueError):
                    self.client.run_test(self.image, **options)
            submit.assert_not_called()

    def test_fallback_for_backends_and_older_daemon(self):
        del self.app.backend.read_output
        self.app.backend.run = lambda payload, node, port, **kw: dict(
            output='legacy text', seconds=0, node=node, exit_code=0)
        stream = self.client.run_test(self.image, show=False, stream=True, poll_interval=.01)
        self.assertEqual(''.join(stream), 'legacy text')
        self.assertTrue(stream.result.ok)
        original = self.client._request

        def request(path, *args, **kwargs):
            if '/output?' in path:
                raise SchedulerError('Unknown endpoint', status=404)
            return original(path, *args, **kwargs)

        with patch.object(self.client, '_request', side_effect=request), redirect_stdout(io.StringIO()) as output:
            result = self.client.run_test(self.image, stream=False)
        self.assertTrue(result.ok)
        self.assertIn('legacy text', output.getvalue())

    def test_explicit_synchronous_silent_mode_returns_result(self):
        self.release.set()
        with redirect_stdout(io.StringIO()) as output:
            result = self.client.run_test(self.image, stream=False, show=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.uart_output, self.final_text)
        self.assertNotIn('first: ', output.getvalue())

    def test_large_output_drains_all_chunks_before_final_result(self):
        del self.app.backend.read_output
        text = '你好' * 40000
        self.app.backend.run = lambda payload, node, port, **kw: dict(
            output=text, seconds=0, node=node, exit_code=0)
        stream = self.client.run_test(self.image, stream=True, show=False, poll_interval=.01)
        self.assertEqual(''.join(stream), text)
        self.assertEqual(stream.offset, len(text.encode()))
        self.assertTrue(stream.result.ok)
