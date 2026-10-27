"""Real detached supervisor restart, queued recovery and graceful stop."""
import json
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
from fpga_scheduler.daemon import start, status, stop


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
