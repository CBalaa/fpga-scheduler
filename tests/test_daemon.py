"""Real detached supervisor restart, queued recovery and graceful stop."""
import json
from contextlib import redirect_stderr
import io
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from fpga_scheduler import Client, SchedulerError
from fpga_scheduler.daemon import SERVICE_STARTUP_EXIT, _capture_stderr, start, status, stop
from fpga_scheduler.remote.uvhs_session import fingerprint


class StatusTests(unittest.TestCase):
    def query(self, client):
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(state_dir=Path(tmp))
            record = dict(pid=123, start_ticks='456')
            with patch('fpga_scheduler.daemon.load', return_value=config), \
                    patch('fpga_scheduler.daemon._record', return_value=record), \
                    patch('fpga_scheduler.daemon._alive', return_value=True), \
                    patch('fpga_scheduler.daemon._client', return_value=client):
                return status('config.toml')

    def test_slow_ports_keep_successful_health_and_queue(self):
        client = Mock()
        client.health.return_value = dict(dispatcher_alive=True, stopping=False)
        client.jobs.return_value = dict(total=0, jobs=[])
        client.devices.side_effect = SchedulerError('Connection or response failed (TimeoutError)')
        value = self.query(client)
        self.assertTrue(value['api']['dispatcher_alive'])
        self.assertEqual(value['queue']['total'], 0)
        self.assertNotIn('api_error', value)
        self.assertIn('TimeoutError', value['ports_error'])
        self.assertIn('initialization or recovery', value['ports_note'])

    def test_queue_error_does_not_hide_ports_or_api(self):
        client = Mock()
        client.health.return_value = dict(dispatcher_alive=True)
        client.jobs.side_effect = SchedulerError('Queue unavailable')
        client.devices.return_value = dict(devices=[], errors={})
        value = self.query(client)
        self.assertTrue(value['api']['dispatcher_alive'])
        self.assertEqual(value['ports'], client.devices.return_value)
        self.assertEqual(value['queue_error'], 'Queue unavailable')
        self.assertNotIn('api_error', value)

    def test_health_failure_is_reported_as_api_error(self):
        client = Mock()
        client.health.side_effect = SchedulerError('Connection refused')
        value = self.query(client)
        self.assertIsNone(value['api'])
        self.assertEqual(value['api_error'], 'Connection refused')
        client.jobs.assert_not_called()
        client.devices.assert_not_called()


class StartupDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.blocker = socket.socket()
        self.blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.blocker.bind(('127.0.0.1', 0))
        self.port = self.blocker.getsockname()[1]
        self.addCleanup(self.blocker.close)
        (self.root / 'token').write_text('x' * 40)
        (self.root / 'token').chmod(0o600)
        self.config = self.root / 'config.toml'
        self.config.write_text('backend="simulator"\nstate_dir="state"\ntoken_file="token"\n'
                               f'port={self.port}\npoll_interval=0.01\n[[nodes]]\nid="test"\nports=[4]\n')
        self.environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                            'NO_PROXY': '*', 'no_proxy': '*'}
        self.pidfile = self.root / 'state/daemon/supervisor.json'

    def test_cli_classifies_constructor_error_separately_from_runtime_crash(self):
        from fpga_scheduler.cli import main
        for constructor_failed in (True, False):
            output = io.StringIO()
            application = Mock()
            failure = RuntimeError('FPGA6 recorded session cleanup was not confirmed')
            if constructor_failed:
                application.side_effect = failure
            else:
                application.return_value.serve.side_effect = failure
            with self.subTest(constructor_failed=constructor_failed), redirect_stderr(output), \
                    patch('fpga_scheduler.service.Application', application), \
                    patch('logging.handlers.RotatingFileHandler'), patch('logging.basicConfig'):
                code = main(['serve', '--config', str(self.config), '--json'])
            self.assertEqual(code, SERVICE_STARTUP_EXIT if constructor_failed else 1)
            self.assertEqual(json.loads(output.getvalue())['error'], str(failure))

    def test_initial_constructor_failure_is_fast_visible_and_preserves_disabled_process(self):
        self.blocker.listen()
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        disabled = self.root / 'state/resident/ports/6/session.json'
        disabled.parent.mkdir(parents=True)
        snapshot = json.dumps(dict(pid=sleeper.pid, start_ticks=fingerprint(sleeper.pid), dirty=True))
        disabled.write_text(snapshot)
        try:
            with patch.dict(os.environ, self.environment):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, 'Address already in use') as failure:
                    start(self.config, timeout=15)
                self.assertLess(time.monotonic() - started, 5)
                self.assertIn('service-stderr.log', str(failure.exception))
                stop(self.config, timeout=5)
            record = json.loads(self.pidfile.read_text())
            self.assertEqual(record['startup_error']['exit_code'], SERVICE_STARTUP_EXIT)
            self.assertEqual(record['restarts'], 0)
            self.assertTrue(record['stopped'])
            self.assertIn('Address already in use', (self.pidfile.parent / 'service-stderr.log').read_text())
            self.assertIsNone(sleeper.poll(), 'disabled executor was signalled by supervisor cleanup')
            self.assertEqual(disabled.read_text(), snapshot)
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=5)
            with patch.dict(os.environ, self.environment):
                stop(self.config, timeout=5)

    def test_constructor_error_during_recovery_keeps_restart_policy(self):
        self.blocker.close()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        client = Client(f'http://127.0.0.1:{self.port}', token_file=self.root / 'token', request_timeout=.2)
        try:
            with patch.dict(os.environ, self.environment):
                launched = start(self.config, timeout=5)
                os.kill(launched['supervisor']['child']['pid'], signal.SIGKILL)
                deadline = time.monotonic() + .8
                while True:
                    try:
                        blocker.bind(('127.0.0.1', self.port))
                        blocker.listen()
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.01)
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    record = json.loads(self.pidfile.read_text())
                    if record.get('last_error', {}).get('exit_code') == SERVICE_STARTUP_EXIT:
                        break
                    time.sleep(.02)
                self.assertEqual(record['last_error']['exit_code'], SERVICE_STARTUP_EXIT)
                self.assertIn('Address already in use', record['last_error']['message'])
                self.assertNotIn('startup_error', record)
                self.assertFalse(record['stopping'])
                blocker.close()
                deadline = time.monotonic() + 10
                recovered = None
                while time.monotonic() < deadline:
                    try:
                        recovered = client.health()
                        break
                    except SchedulerError:
                        time.sleep(.05)
                self.assertIsNotNone(recovered, 'supervisor stopped retrying after recovery constructor failure')
                self.assertNotEqual(recovered['pid'], launched['api']['pid'])
                self.assertTrue(stop(self.config, timeout=5)['stopped'])
        finally:
            blocker.close()
            client.close()
            with patch.dict(os.environ, self.environment):
                stop(self.config, timeout=5)

    def test_service_stderr_capture_has_bounded_tail_and_rotating_storage(self):
        path = self.root / 'stderr.log'
        handler = RotatingFileHandler(path, maxBytes=65536, backupCount=2)
        process = subprocess.Popen([sys.executable, '-c',
                                    "import sys; sys.stderr.buffer.write(b'x'*300000+b'\\nSpecific startup failure\\n')"],
                                   stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        try:
            reader, done, tail = _capture_stderr(process, handler)
            self.assertEqual(process.wait(timeout=5), 0)
            done.set()
            reader.join(timeout=2)
            self.assertFalse(reader.is_alive())
            self.assertLessEqual(len(tail), 8192)
            self.assertTrue(tail.endswith(b'Specific startup failure\n'))
            logs = list(self.root.glob('stderr.log*'))
            self.assertLessEqual(len(logs), 3)
            self.assertLessEqual(sum(log.stat().st_size for log in logs), 3 * 65536)
            self.assertIn('Specific startup failure', path.read_text())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            handler.close()


class SupervisorTests(unittest.TestCase):
    def test_cli_lifecycle_with_environment_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            token = root / 'token'
            token.write_text('x' * 40)
            token.chmod(0o600)
            config = root / 'config.toml'
            config.write_text(f'backend="simulator"\nstate_dir="state"\ntoken_file="token"\nport={port}\npoll_interval=0.01\n[[nodes]]\nid="test"\nports=[4]\n')
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                   'FPGA_SCHEDULER_CONFIG': str(config), 'NO_PROXY': '*', 'no_proxy': '*'}
            try:
                for action in ('start', 'status', 'stop'):
                    process = subprocess.run([sys.executable, '-m', 'fpga_scheduler', 'daemon', action, '--json'],
                                             capture_output=True, text=True, env=env, timeout=15)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    result = json.loads(process.stdout)
                    if action == 'status':
                        self.assertTrue(result['supervisor_alive'])
                        self.assertTrue(result['api']['dispatcher_alive'])
                    elif action == 'stop':
                        self.assertTrue(result['stopped'])
                self.assertFalse(status(config)['supervisor_alive'])
            finally:
                stop(config, timeout=5)

    def test_crash_restart_unknown_no_replay_and_graceful_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            token = root / 'token'
            token.write_text('x' * 40); token.chmod(0o600)
            config = root / 'config.toml'
            config.write_text(f'backend="simulator"\nstate_dir="state"\ntoken_file="token"\nport={port}\npoll_interval=0.01\n[simulator]\nduration=0.6\n[[nodes]]\nid="test"\nports=[4]\n')
            image = root / 'image.bin'; image.write_bytes(b'hello')
            client = Client(f'http://127.0.0.1:{port}', token_file=token, request_timeout=1)
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
            # start() inherits environment, as it does in an installed deployment.
            from unittest.mock import patch
            with patch.dict(os.environ, env):
                try:
                    launched = start(config, timeout=5)
                    with self.assertRaises(RuntimeError):
                        start(config, timeout=1)
                    first = client.submit(image)
                    end = time.monotonic() + 3
                    while client.job(first.id).state != 'running' and time.monotonic() < end:
                        time.sleep(.01)
                    second = client.submit(image)
                    record = status(config)['supervisor']
                    os.kill(record['child']['pid'], signal.SIGKILL)
                    end = time.monotonic() + 8
                    while time.monotonic() < end:
                        try:
                            task = client.job(first.id)
                            if task.state == 'failed':
                                break
                        except SchedulerError:
                            pass
                        time.sleep(.05)
                    self.assertIn('unknown', task.error)
                    self.assertTrue(client.wait(second.id, timeout=5, poll_interval=.02).result.ok)
                    third = client.submit(image)
                    end = time.monotonic() + 3
                    while client.job(third.id).state != 'running' and time.monotonic() < end:
                        time.sleep(.01)
                    fourth = client.submit(image)
                    self.assertTrue(stop(config, timeout=5)['stopped'])
                    start(config, timeout=5)
                    self.assertEqual(client.job(third.id).state, 'completed')
                    self.assertTrue(client.wait(fourth.id, timeout=5, poll_interval=.02).result.ok)
                    self.assertEqual(client.job(first.id).state, 'failed')
                finally:
                    stop(config, timeout=5)
