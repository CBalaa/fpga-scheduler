import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from fpga_scheduler.backends.bosc import BOSC
from fpga_scheduler.config import Node
from fpga_scheduler.remote import prepare, relay, runtime

PROJECT = Path(__file__).resolve().parents[1]
AGENT = PROJECT / 'src/fpga_scheduler/remote/runtime.py'


class RuntimeTests(unittest.TestCase):
    def test_uvhs_owner_reserved_and_duplicate_rows(self):
        output = '\n'.join(f"uvhs-0 B{n//4} F{n%4} on link {'up owner' if n==3 else 'down'} {'true' if n==2 else 'false'}" for n in range(8))
        ports = runtime.parse_fpgas(output)
        self.assertEqual(ports[3]['owner'], 'owner')
        self.assertEqual(ports[4]['owner'], '')
        self.assertTrue(ports[2]['booked'])
        self.assertFalse(ports[4]['booked'])
        with self.assertRaisesRegex(RuntimeError, '[Dd]uplicate'):
            runtime.parse_fpgas(output + '\n' + output.splitlines()[0])

    def test_uvhs_inventory_accepts_installed_units_without_assuming_two_full_boards(self):
        for numbers in (range(8), range(4), range(4, 8), (4,), (0, 2, 7), range(8, 12), (40, 43)):
            with self.subTest(numbers=tuple(numbers)):
                output = '\n'.join(f'uvhs-0 B{number // 4} F{number % 4} on link down false'
                                   for number in numbers)
                ports = runtime.parse_fpgas(output)
                self.assertEqual(set(ports), set(numbers))
                for number in numbers:
                    self.assertEqual(ports[number]['number'], number)
                    self.assertEqual(ports[number]['board'], f'B{number // 4}')
                    self.assertEqual(ports[number]['fpga'], f'F{number % 4}')

    def test_uvhs_inventory_ignores_unrelated_shell_output(self):
        output = '''Command: query -fpgas -all
System Name Board FPGA Power Status Owner Booked
----------- ----- ---- ----- ------ ----- ------
[20:43:25] INFO: platform initialized
uvhs-0 ready
uvhs-0 B0 F2 off link down true
uvhs-0 B0 F3 on link up false
Command: exit
'''
        ports = runtime.parse_fpgas(output)
        self.assertEqual(set(ports), {2, 3})
        self.assertEqual(ports[2]['power'], 'off')
        self.assertTrue(ports[2]['booked'])
        self.assertEqual(ports[3]['owner'], '')

    def test_uvhs_inventory_rejects_empty_table(self):
        for output in ('', 'Command: query -fpgas -all\nSystem Name Board FPGA Power Status Owner Booked'):
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, '(?i)(no|empty|missing).*FPGA'):
                runtime.parse_fpgas(output)

    def test_uvhs_inventory_rejects_malformed_unit_rows_instead_of_hiding_them(self):
        valid = 'uvhs-0 B0 F0 on link down false\n'
        for row in (
                'uvhs-0 B0 F1 on link down',
                'uvhs-0 B0 F1 on link down false extra extra',
                'uvhs-0 Bx F1 on link down false',
                'uvhs-0 B0 F4 on link down false',
                'uvhs-0 B0 F1 unknown link down false',
                'uvhs-0 B0 F1 on disconnected down false',
                'uvhs-0 B0 F1 on link unknown false',
                'uvhs-0 B0 F1 on link up owner maybe'):
            with self.subTest(row=row), self.assertRaisesRegex(RuntimeError, '(?i)(invalid|malformed).*FPGA'):
                runtime.parse_fpgas(valid + row)

    def test_mapping_requires_release_before_exact_load(self):
        for number in range(8):
            script = 'load_db -release\nload_db -db ./hw.dat -to {b%d.f%d}\n' % divmod(number, 4)
            self.assertTrue(runtime.runtime_mapping_valid(script, number))
            self.assertFalse(runtime.runtime_mapping_valid(script, (number + 1) % 8))
        for script in ('load_db -db ./hw.dat -to {b1.f0}\nload_db -release\n',
                       '# load_db -db ./hw.dat -to {b1.f0}\n',
                       'allocate -board {b1.f0}\nload_db -release\nload_db -db ./hw.dat\n'):
            self.assertFalse(runtime.runtime_mapping_valid(script, 4))

    def test_port_lock_across_real_processes(self):
        with tempfile.TemporaryDirectory() as root, patch.object(runtime, 'ROOT', Path(root)):
            first = runtime.lock_port(4)
            env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src')}
            code = "from fpga_scheduler.remote import runtime as r; from pathlib import Path; import sys; r.ROOT=Path(sys.argv[1]); sys.exit(0 if r.lock_port(int(sys.argv[2])) else 75)"
            def attempt(port):
                return subprocess.run([sys.executable, '-c', code, root, str(port)], env=env).returncode
            self.assertEqual(attempt(4), 75)
            self.assertEqual(attempt(5), 0)
            first.close()
            self.assertEqual(attempt(4), 0)

    def test_checksum_failure_releases_port(self):
        with tempfile.TemporaryDirectory() as root, patch.object(runtime, 'ROOT', Path(root)):
            directory = Path(root) / 'jobs' / ('a' * 32)
            directory.mkdir(parents=True)
            (directory / 'input.bin').write_bytes(b'corrupt')
            args = types.SimpleNamespace(id='a' * 32, fpga=4, sha256='0' * 64)
            with patch.object(runtime, 'inventory', return_value=[dict(number=4, available=True)]):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    runtime.run_job(args)
            handle = runtime.lock_port(4)
            self.assertIsNotNone(handle)
            handle.close()

    def test_occupied_port_returns_only_safe_retry_marker(self):
        with tempfile.TemporaryDirectory() as root, patch.object(runtime, 'ROOT', Path(root)):
            lock = runtime.lock_port(4)
            output = io.StringIO()
            with patch('sys.stdout', output):
                self.assertEqual(runtime.run_job(types.SimpleNamespace(fpga=4)), 75)
            self.assertEqual(output.getvalue().strip(), runtime.BUSY_TOKEN)
            lock.close()

    def test_foreign_account_blocks_shared_board(self):
        with tempfile.TemporaryDirectory() as root, patch.object(runtime, 'ROOT', Path(root)), patch.object(runtime, 'ORDER', (4,)):
            root = Path(root)
            scripts = root / 'user_script/hw_run'
            scripts.mkdir(parents=True)
            (scripts / 'hw_run4.tcl').write_text('load_db -release\nload_db -db ./hw.dat -to {b1.f0}\n')
            uart = root / 'uart4'
            uart.touch()
            output = 'uvhs-0 B1 F0 on link down false\nuvhs-0 B1 F1 on link up other-user false'
            with patch.object(runtime, 'UART_PREFIX', str(root / 'uart')), \
                 patch.object(runtime.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=output)), \
                 patch.object(runtime.subprocess, 'check_output', return_value=''):
                ports = runtime.inventory()
            self.assertFalse(ports[0]['available'])
            self.assertIn('board in use (shared clock)', ports[0]['reasons'])

    def _inventory_with_installed_units(self, numbers, configured):
        with tempfile.TemporaryDirectory() as root, patch.object(runtime, 'ROOT', Path(root)), \
             patch.object(runtime, 'ORDER', configured):
            root = Path(root)
            scripts = root / 'user_script/hw_run'
            scripts.mkdir(parents=True)
            for number in configured:
                (scripts / f'hw_run{number}.tcl').write_text(
                    'load_db -release\nload_db -db ./hw.dat -to {b%d.f%d}\n' % divmod(number, 4))
                (root / f'uart{number}').touch()
            output = '\n'.join(f'uvhs-0 B{number // 4} F{number % 4} on link down false'
                               for number in numbers)
            with patch.object(runtime, 'UART_PREFIX', str(root / 'uart')), \
                 patch.object(runtime.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=output)), \
                 patch.object(runtime.subprocess, 'check_output', return_value=''):
                return runtime.inventory()

    def test_inventory_missing_unrelated_board_does_not_disable_configured_units(self):
        for installed, configured in ((range(4), (0, 2)), (range(4, 8), (4,))):
            with self.subTest(installed=tuple(installed), configured=configured):
                ports = self._inventory_with_installed_units(installed, configured)
                self.assertEqual([port['number'] for port in ports], list(configured))
                self.assertTrue(all(port['available'] for port in ports), ports)

    def test_inventory_partial_shared_board_cannot_approve_unchecked_clock_changes(self):
        ports = self._inventory_with_installed_units((4,), (4,))
        self.assertFalse(ports[0]['available'])
        self.assertIn('board inventory incomplete', ports[0]['reasons'])

    def test_inventory_missing_configured_unit_reports_unavailable_and_keeps_existing_unit(self):
        ports = self._inventory_with_installed_units(range(4), (4, 0))
        self.assertEqual([port['number'] for port in ports], [4, 0])
        self.assertFalse(ports[0]['available'])
        self.assertRegex(' '.join(ports[0]['reasons']), '(?i)(missing|not (?:present|reported|found))')
        self.assertTrue(ports[1]['available'], ports[1])

    def test_timeout_terminates_real_stubborn_child_and_keeps_lock_until_retired(self):
        with tempfile.TemporaryDirectory() as root:
            pidfile = Path(root) / 'pid'
            source = """import os,signal,subprocess,sys
from pathlib import Path
from fpga_scheduler.remote import runtime as r
r.ROOT=Path(sys.argv[1]); r.CLEANUP_GRACE=.15
lock=r.lock_port(4)
child=subprocess.Popen([sys.executable,'-c',"import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path("+repr(sys.argv[2])+").write_text(str(os.getpid())); time.sleep(30)"],start_new_session=True)
status=r.supervise(child,.3)
lock.close()
sys.exit(status)
"""
            env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src')}
            agent = subprocess.Popen([sys.executable, '-c', source, root, str(pidfile)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 3
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(pidfile.exists())
                with patch.object(runtime, 'ROOT', Path(root)):
                    self.assertIsNone(runtime.lock_port(4))
                stdout, stderr = agent.communicate(timeout=5)
                self.assertEqual(agent.returncode, 124, stderr)
                self.assertIn('timed out', stdout)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pidfile.read_text()), 0)
                with patch.object(runtime, 'ROOT', Path(root)):
                    lock = runtime.lock_port(4)
                    self.assertIsNotNone(lock)
                    lock.close()
            finally:
                if agent.poll() is None:
                    agent.kill()
                    agent.communicate()

    def test_successful_parent_cannot_leave_background_uart_child(self):
        with tempfile.TemporaryDirectory() as root:
            pidfile = Path(root) / 'child'
            code = "import os,subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); Path(sys.argv[1]).write_text(str(p.pid))"
            process = subprocess.Popen([sys.executable, '-c', code, str(pidfile)], start_new_session=True)
            try:
                with patch.object(runtime, 'CLEANUP_GRACE', .1):
                    self.assertEqual(runtime.supervise(process, 2), 0)
                # A reparented child may briefly remain a zombie; it cannot hold UART FDs.
                child = Path('/proc') / pidfile.read_text()
                if child.exists():
                    self.assertEqual((child / 'stat').read_text().split()[2], 'Z')
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

    def test_retention_preserves_locked_remote_job(self):
        with tempfile.TemporaryDirectory() as root, patch.object(runtime, 'ROOT', Path(root)):
            directory = Path(root) / 'jobs' / ('a' * 32)
            directory.mkdir(parents=True)
            marker = directory / 'finished'
            marker.touch()
            old = time.time() - 8 * 86400
            os.utime(marker, (old, old))
            with (directory / 'active.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                runtime.cleanup(time.time() - 7 * 86400)
                self.assertTrue(directory.exists())
            runtime.cleanup(time.time() - 7 * 86400)
            self.assertFalse(directory.exists())

    def test_prepare_runtime_preserves_source_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            project = root / 'project'
            scripts = project / 'user_script/hw_run'
            scripts.mkdir(parents=True)
            original = 'allocate -board {b0.f0}\nload_db -release\nload_db -db ./hw.dat\n'
            for n in range(8):
                (scripts / f'hw_run{n}.tcl').write_text(original)
            (project / 'hw.dat').mkdir()
            (project / 'Makefile').write_text('all:\n\ttrue\n')
            run = root / 'run.sh'
            run.write_text('exit 0\n')
            destination = root / 'runtime'
            argv = ['prepare', '--project', str(project), '--run-script', str(run), '--scheduler', str(AGENT), '--destination', str(destination)]
            with patch.object(sys, 'argv', argv), patch('sys.stdout', io.StringIO()):
                prepare.main()
                with self.assertRaises(FileExistsError):
                    prepare.main()
            self.assertEqual((scripts / 'hw_run4.tcl').read_text(), original)
            self.assertTrue((destination / 'hw.dat').is_symlink())
            self.assertIn('-to {b1.f0}', (destination / 'user_script/hw_run/hw_run4.tcl').read_text())
            rewritten = (destination / 'user_script/hw_run/hw_run4.tcl').read_text()
            self.assertNotIn('allocate -board', rewritten)
            self.assertEqual(prepare.bind_port(rewritten, 4), rewritten)
            explicit = prepare.bind_port(rewritten, 4, explicit_allocation=True)
            self.assertLess(explicit.index('load_db -release'), explicit.index('allocate -board {b1.f0}'))
            self.assertLess(explicit.index('allocate -board {b1.f0}'), explicit.index('load_db -db'))
            self.assertEqual(prepare.bind_port(explicit, 4, explicit_allocation=True), explicit)
            self.assertTrue(os.access(destination / 'run.sh', os.X_OK))
            for invalid in ('query -fpgas -all', 'load_db -db ./hw.dat\nload_db -db ./hw.dat\n',
                            'load_db -db ./hw.dat\nload_db -release\n'):
                with self.assertRaises(ValueError):
                    prepare.bind_port(invalid, 4)

    def test_prepare_native_runner_without_external_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'project'
            scripts = project / 'user_script/hw_run'
            scripts.mkdir(parents=True)
            (scripts / 'hw_run4.tcl').write_text('load_db -release\nload_db -db ./hw.dat -to {b1.f0}\n')
            (project / 'Makefile').write_text('all:\n\ttrue\n')
            (project / 'hw.dat').mkdir()
            destination = root / 'runtime'
            argv = ['prepare', '--project', str(project), '--scheduler', str(AGENT),
                    '--ports', '4', '--destination', str(destination)]
            with patch.object(sys, 'argv', argv), patch('sys.stdout', io.StringIO()):
                prepare.main()
            self.assertTrue((destination / 'runner.py').is_file())
            self.assertIn('runner.py', (destination / 'run.sh').read_text())


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.node = Node('node-a', (4, 5), ssh_host='fpga-alias', root='/path/to/fpga runtime',
                         uvhs_shell='/vendor/uvhs', ssh_config='/config/ssh')
        self.backend = BOSC((self.node,))

    def test_shell_command_is_quoted_once_and_host_is_pinned(self):
        command = self.backend.runtime('node-a', 'probe')
        outer = shlex.split(command)
        self.assertEqual(outer[:2], ['bash', '-lc'])
        inner = shlex.split(outer[2])
        self.assertEqual(inner[1], '/path/to/fpga runtime/scheduler.py')
        with patch('fpga_scheduler.backends.bosc.subprocess.run', return_value=types.SimpleNamespace(returncode=0, stdout='[]', stderr='')) as run:
            self.backend.remote('node-a', command, 60)
        argv = run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertEqual(argv[-2:], ['fpga-alias', command])
        self.assertIn('/config/ssh', argv)

    def test_nonzero_status_and_disconnect_are_not_retried(self):
        for code in (1, 255):
            with patch('fpga_scheduler.backends.bosc.subprocess.run', return_value=types.SimpleNamespace(returncode=code, stdout='output', stderr='')) as run:
                if code == 255:
                    with self.assertRaises(OSError):
                        self.backend.remote('node-a', 'command', 60)
                else:
                    self.assertEqual(self.backend.remote('node-a', 'command', 60)['exit_code'], 1)
                self.assertEqual(run.call_count, 1)

    def test_probe_validates_exit_status(self):
        with patch.object(self.backend, 'remote', return_value=dict(exit_code=0, output='[{"number":4,"available":true}]')):
            self.assertTrue(self.backend.probe('node-a')[0]['available'])
        with patch.object(self.backend, 'remote', return_value=dict(exit_code=1, output='[]')):
            with self.assertRaises(OSError):
                self.backend.probe('node-a')

    def test_login_diagnostics_on_stderr_do_not_corrupt_inventory_json(self):
        response = types.SimpleNamespace(returncode=0, stdout='[{"number":4,"available":true}]',
                                         stderr='bash: warning: setlocale: LC_ALL\n')
        with patch('fpga_scheduler.backends.bosc.subprocess.run', return_value=response):
            self.assertEqual(self.backend.probe('node-a')[0]['number'], 4)

    def test_exact_preload_retry_on_stdout_survives_login_diagnostics(self):
        response = types.SimpleNamespace(returncode=75, stdout=runtime.BUSY_TOKEN + '\n',
                                         stderr='bash: warning: setlocale: LC_ALL\n')
        payload = dict(id='a' * 32, sha256='0' * 64, timeout=60, idle_seconds=10)
        with patch('fpga_scheduler.backends.bosc.subprocess.run', return_value=response):
            result = self.backend.run(payload, 'node-a', 4)
        self.assertEqual(result['exit_code'], 75)
        self.assertEqual(result['output'], runtime.BUSY_TOKEN)
        self.assertNotIn('execution_status', result)

    def test_retry_marker_never_replays_execution_or_stderr(self):
        payload = dict(id='a' * 32, sha256='0' * 64, timeout=60, idle_seconds=10)
        for stdout, stderr, code in (
                ('Program started\n' + runtime.BUSY_TOKEN, '', 75),
                ('', runtime.BUSY_TOKEN, 75),
                (runtime.BUSY_TOKEN, '', 1)):
            response = types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
            with patch('fpga_scheduler.backends.bosc.subprocess.run', return_value=response):
                result = self.backend.run(payload, 'node-a', 4)
            self.assertEqual(result['execution_status'], 'platform_error')

    def test_input_upload_executes_helpers_locally_without_hardware(self):
        with tempfile.TemporaryDirectory() as root:
            backend = BOSC((Node('a', (4,), ssh_host='unused', root=root),))
            image = Path(root) / 'local.bin'
            image.write_bytes(bytes(range(256)) * 300)
            payload = dict(id='a' * 32, sha256=hashlib.sha256(image.read_bytes()).hexdigest())
            commands = []
            def local(node, command, timeout):
                commands.append(shlex.split(command))
                result = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=timeout)
                return dict(exit_code=result.returncode, output=result.stdout + result.stderr)
            with patch.object(backend, 'remote', side_effect=local):
                backend.prepare(payload, 'a', image)
            self.assertEqual((Path(root) / 'jobs' / payload['id'] / 'input.bin').read_bytes(), image.read_bytes())
            self.assertGreater(len(commands), 3)
            self.assertTrue(all(c[1] == '-c' for c in commands))

    def test_failed_input_transfer_and_total_deadline(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'image'
            path.write_bytes(b'x')
            payload = dict(id='a' * 32, sha256=hashlib.sha256(b'x').hexdigest())
            with patch.object(self.backend, 'remote', return_value=dict(exit_code=1)) as remote:
                with self.assertRaises(OSError):
                    self.backend.prepare(payload, 'node-a', path)
                self.assertEqual(remote.call_count, 1)
            self.backend.transfer_timeout = -1
            with patch.object(self.backend, 'remote') as remote:
                with self.assertRaises(TimeoutError):
                    self.backend.prepare(payload, 'node-a', path)
                remote.assert_not_called()

    def test_relay_data_only_and_unknown_node_rejected(self):
        config = dict(nodes={'a': 'fpga-a'})
        request = dict(node='a', command="printf '%s' '$(touch /tmp/should-not-exist)'", timeout=10)
        with patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout='ok', stderr='')) as run:
            self.assertEqual(relay.run(request, config)['exit_code'], 0)
            self.assertEqual(run.call_args.args[0][-1], request['command'])
            with self.assertRaises(ValueError):
                relay.run(dict(request, node='unknown'), config)
            self.assertEqual(run.call_count, 1)


class TransportPrivacyTests(unittest.TestCase):
    def test_timeout_exception_does_not_reveal_upload_command(self):
        node = Node('a', (4,), ssh_host='host', root='/platform')
        backend = BOSC((node,))
        secret_payload = os.urandom(16).hex()
        with patch('fpga_scheduler.backends.bosc.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(['ssh', secret_payload], 1)):
            with self.assertRaises(OSError) as caught:
                backend.remote('a', secret_payload, 1)
        import traceback
        rendered = ''.join(traceback.format_exception(caught.exception))
        self.assertNotIn(secret_payload, rendered)
