"""Real service process restart/shutdown and standalone remote execution."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from fpga_scheduler import Client, SchedulerError

PROJECT = Path(__file__).resolve().parents[1]
AGENT = PROJECT / 'src/fpga_scheduler/remote/runtime.py'


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        token = self.root / 'token'
        token.write_text('z' * 48)
        token.chmod(0o600)
        self.config = self.root / 'config.toml'
        self.config.write_text(f'''backend="simulator"
state_dir="state"
token_file="token"
bind="127.0.0.1"
port={self.port}
poll_interval=0.02
[simulator]
duration=0.8
[[nodes]]
id="a"
ports=[4]
''')
        self.client = Client(f'http://127.0.0.1:{self.port}', token_file=token, request_timeout=1)
        self.env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src'), 'NO_PROXY': '*', 'no_proxy': '*'}
        self.processes = []
        self.image = self.root / 'image'
        self.image.write_bytes(b'data')

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.tmp.cleanup()

    def start(self):
        process = subprocess.Popen([sys.executable, '-m', 'fpga_scheduler', 'serve', '--config', str(self.config)],
                                   cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail('Service exited: ' + process.communicate()[1])
            try:
                self.client.health()
                return process
            except SchedulerError:
                time.sleep(.02)
        self.fail('Service did not start')

    def wait_running(self, ident):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.client.job(ident).state == 'running':
                return
            time.sleep(.01)
        self.fail('Task did not start')

    def test_sigterm_drains_current_job_and_restart_keeps_queue(self):
        process = self.start()
        first = self.client.submit(self.image)
        self.wait_running(first.id)
        second = self.client.submit(self.image)
        self.assertEqual(second.state, 'queued')
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.start()
        self.assertEqual(self.client.job(first.id).state, 'completed')
        self.assertTrue(self.client.wait(second.id, timeout=4, poll_interval=.02).result.ok)

    def test_sigkill_marks_running_unknown_without_replay(self):
        process = self.start()
        first = self.client.submit(self.image)
        self.wait_running(first.id)
        second = self.client.submit(self.image)
        process.kill()
        process.communicate(timeout=3)
        self.start()
        failed = self.client.job(first.id)
        self.assertEqual(failed.state, 'failed')
        self.assertIn('unknown', failed.error)
        self.assertTrue(self.client.wait(second.id, timeout=4, poll_interval=.02).result.ok)
        self.assertEqual(self.client.job(first.id).state, 'failed')


class StandaloneAgentTests(unittest.TestCase):
    def test_actual_agent_isolated_run_maps_and_pads_input(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            ident = 'a' * 32
            directory = root / 'jobs' / ident
            directory.mkdir(parents=True)
            content = b'hello FPGA'
            (directory / 'input.bin').write_bytes(content)
            scripts = root / 'user_script/hw_run'
            scripts.mkdir(parents=True)
            (scripts / 'hw_run4.tcl').write_text('load_db -release\nload_db -db ./hw.dat -to {b1.f0}\n')
            (root / 'Makefile').write_text('all:\n\ttrue\n')
            (root / 'hw.dat').mkdir()
            (root / 'uart4').touch()
            uvhs = root / 'fake-uvhs'
            uvhs.write_text('#!/bin/sh\n' + '\n'.join(f"echo 'uvhs-0 B{n//4} F{n%4} on link down false'" for n in range(8)) + '\n')
            uvhs.chmod(0o755)
            (root / 'run.sh').write_text('''#!/bin/sh
set -eu
[ "$1" = "--fpga=4" ]
[ "$2" = "--workdir=$(dirname "$UV_RUN_WORKDIR")" ]
mkdir -p "$UV_RUN_OUT_ROOT"
printf 'verify hello: PASS\n' > "$UV_RUN_OUT_ROOT/uart.log"
cat "$UV_RUN_OUT_ROOT/uart.log"
''')
            result = subprocess.run([sys.executable, str(AGENT), '--root', str(root), '--uvhs', str(uvhs),
                                     '--uart-prefix', str(root / 'uart'), '--ports', '4', 'run', ident, '4',
                                     hashlib.sha256(content).hexdigest(), '3', '1'],
                                    capture_output=True, text=True, cwd=root, timeout=8,
                                    env={**os.environ, 'PYTHONPATH': ''})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('verify hello: PASS', result.stdout)
            self.assertEqual((directory / 'input.bin').read_bytes(), content)
            padded = (directory / 'input_padded.bin').read_bytes()
            self.assertEqual(len(padded), 1048576)
            self.assertEqual(padded[:len(content)], content)
            self.assertEqual(set(padded[len(content):]), {0})
            self.assertTrue((directory / 'finished').exists())
            self.assertTrue((directory / 'Makefile').is_symlink())
            self.assertTrue((directory / 'output/uart.log').is_file())
