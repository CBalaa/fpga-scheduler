"""Repository-only simulated board exercises real service, Resident and SDK paths."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fpga_scheduler import Client
from fpga_scheduler.config import Node, load
from fpga_scheduler.remote.uvhs_session import fingerprint, retire_record
from fpga_scheduler.storage import Images

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))

from fpga_sim_support.board import ResidentSimulator
from fpga_sim_support.runtime import create_application, load_local_config
from fpga_sim_support.scenarios import create_environment, read_image, write_image


def eventually(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Simulation condition did not become true')


class ImageTests(unittest.TestCase):
    def test_production_cli_and_config_have_no_local_simulator_entry(self):
        from contextlib import redirect_stderr
        import io
        from fpga_scheduler.cli import parser
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            parser().parse_args(['sim', 'init', 'unused'])
        self.assertEqual(error.exception.code, 2)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.toml'
            config.write_text('backend="resident-sim"\nstate_dir="state"\ntoken_file="token"\n'
                              '[[nodes]]\nid="sim"\nports=[4]\n')
            with self.assertRaisesRegex(ValueError, 'backend must be'):
                load(config)

    def test_roundtrip_validation_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'signed.bin'
            write_image(path, label='标记 A', exit_code=-2147483648, fragment_bytes=1)
            self.assertEqual(read_image(path)['exit_code'], -2147483648)
            self.assertEqual(read_image(path)['label'], '标记 A')
            with self.assertRaises(FileExistsError):
                write_image(path)
            for scenario in (dict(mode='oops'), dict(duration=float('nan')), dict(exit_code=True),
                             dict(exit_code=2**31), dict(fragment_bytes=0), dict(surprise=True)):
                with self.assertRaises(ValueError):
                    write_image(Path(tmp) / 'invalid.bin', **scenario)
            path.write_bytes(b'real arbitrary instructions')
            with self.assertRaisesRegex(ValueError, 'scenario images'):
                read_image(path)

    @unittest.skipUnless(shutil.which('tclsh'), 'tclsh required')
    def test_init_uses_only_private_virtual_hardware_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'offline'
            result = create_environment(root, ports=(5,))
            config = load(result['config'])
            self.assertEqual(config.backend, 'bosc-resident')
            self.assertEqual(config.nodes[0].ports, (5,))
            self.assertEqual(config.bind, '127.0.0.1')
            self.assertEqual(config.token_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.nodes[0].uvhs_shell, str(root / 'state/sim/uvhs-sim'))
            with self.assertRaises(FileExistsError):
                create_environment(root)
            config_file = Path(result['config'])
            config_file.write_text(config_file.read_text().replace(
                'hardware_project = "./state/sim/project"', 'hardware_project = "/real/project"'))
            with self.assertRaisesRegex(ValueError, 'isolated localhost config'):
                load_local_config(root)

    @unittest.skipUnless(shutil.which('tclsh'), 'tclsh required')
    def test_existing_hardware_state_is_rejected_before_retiring_any_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'sim'
            create_environment(root)
            state = root / 'state'
            record = state / 'resident/ports/4/session.json'
            record.parent.mkdir(parents=True)
            record.write_text('do not read or retire this real executor record')
            with self.assertRaisesRegex(ValueError, 'non-simulation state_dir'):
                ResidentSimulator((Node('sim', (4,)),), state)
            with patch('subprocess.Popen') as popen:
                with self.assertRaisesRegex(ValueError, 'non-simulation state_dir'):
                    create_application(root)
                popen.assert_not_called()
            self.assertEqual(record.read_text(), 'do not read or retire this real executor record')
            self.assertFalse((state / 'sim').exists())


@unittest.skipUnless(shutil.which('tclsh'), 'tclsh required')
class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = ResidentSimulator((Node('sim', (4, 5)),), self.root / 'state',
                                         startup_timeout=2, command_timeout=.2,
                                         uart_quiet_seconds=.02, recovery_backoff=.01)
        self.images = Images(self.root / 'state/images')
        self.index = 0
        self.assertTrue(all(p['available'] for p in self.backend.probe('sim')))

    def tearDown(self):
        self.backend.close()
        self.tmp.cleanup()

    def run_scenario(self, port=4, timeout=2, **scenario):
        self.index += 1
        ident = f'{self.index:032x}'
        image = self.root / f'{ident}.bin'
        write_image(image, **{'duration': .04, **scenario})
        payload = dict(id=ident, sha256=self.images.put(ident, image.read_bytes()), timeout=timeout)
        self.backend.prepare(payload, 'sim', self.images.freeze(ident, payload['sha256']))
        return self.backend.run(payload, 'sim', port)

    def test_a_b_a_signed_exits_keep_pid_session_and_one_download(self):
        session = self.backend.sessions[4]
        for label, code in (('A', 0), ('B', -2147483648), ('A', 2147483647)):
            result = self.run_scenario(label=label, exit_code=code, fragment_bytes=1, output='no trailing newline')
            self.assertEqual(result['execution_status'], 'exited', result)
            self.assertEqual(result['app_exit_code'], code)
            self.assertEqual(result['details']['session'], session.epoch)
            self.assertEqual(result['details']['uvhs_pid'], session.process.pid)
            self.assertTrue(result['details']['ddr_readback_matches'])
            self.assertIn('\n' + label + '\n', result['uart_output'])
        self.assertEqual((session.directory / 'downloads').read_text().splitlines(), ['download'])

    def test_timeout_reuses_session_and_next_image_has_no_stale_uart(self):
        session = self.backend.sessions[4]
        result = self.run_scenario(mode='hang', label='old-job', timeout=1)
        self.assertEqual(result['execution_status'], 'timeout', result)
        self.assertIsNone(result['app_exit_code'])
        self.assertTrue(result['details']['reset_confirmed'])
        result = self.run_scenario(label='new-job')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertNotIn('old-job', result['uart_output'])
        self.assertIs(self.backend.sessions[4], session)

    def test_protocol_trap_memory_and_command_failures(self):
        for mode, expected in (('trap', 'trap'), ('duplicate', 'protocol_error'),
                               ('missing-start', 'protocol_error'), ('malformed', 'protocol_error'),
                               ('readback-error', 'platform_error'), ('command-error', 'platform_error')):
            with self.subTest(mode=mode):
                result = self.run_scenario(mode=mode)
                self.assertEqual(result['execution_status'], expected, result)
                if mode in ('readback-error', 'command-error'):
                    path = self.backend.root / 'jobs' / f'{self.index:032x}' / 'start-intent.json'
                    self.assertFalse(path.exists())
                eventually(lambda: all(p['available'] for p in self.backend.probe('sim')))

    def test_cleanup_error_preserves_app_zero_and_quarantines_on_abort(self):
        result = self.run_scenario(mode='cleanup-error')
        self.assertEqual(result['execution_status'], 'platform_error', result)
        self.assertEqual(result['app_exit_code'], 0)
        self.assertFalse(result['details']['reset_confirmed'])
        # Cleanup after a cancellation must quarantine rather than reinitialize.
        self.backend.states[4].update(state='RECOVERING', next_retry=0)
        eventually(lambda: all(p['available'] for p in self.backend.probe('sim')))
        result = self.run_scenario(mode='cleanup-error', duration=3, timeout=1)
        self.assertEqual(result['execution_status'], 'platform_error', result)
        self.assertEqual(result['details']['interrupted_status'], 'timeout')
        self.assertEqual(self.backend.states[4]['state'], 'QUARANTINED')
        self.assertTrue(self.run_scenario(port=5)['exit_code'] == 0)

    def test_missing_command_ack_quarantines_and_never_releases_cpu(self):
        result = self.run_scenario(mode='command-timeout', duration=.4)
        self.assertEqual(result['execution_status'], 'platform_error', result)
        self.assertFalse(result['details']['application_started'])
        self.assertEqual(self.backend.states[4]['state'], 'QUARANTINED')

    def test_disconnect_and_uvhs_exit_are_platform_faults(self):
        for port, mode in ((4, 'disconnect'), (5, 'uvhs-exit')):
            result = self.run_scenario(port=port, mode=mode)
            self.assertEqual(result['execution_status'], 'platform_error', result)
            self.assertNotEqual(result['exit_code'], 0)


@unittest.skipUnless(shutil.which('tclsh'), 'tclsh required')
class SimulationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'sim'
        info = create_environment(self.root)
        self.app = create_application(self.root, port=0)
        self.url = f'http://127.0.0.1:{self.app.server.server_address[1]}'
        self.app.queue.start()
        self.thread = threading.Thread(target=self.app.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.client = Client(self.url, token_file=info['token_file'])
        self.client.devices()

    def tearDown(self):
        self.client.close()
        self.app.server.shutdown()
        self.thread.join()
        self.app.close()
        self.tmp.cleanup()

    def test_incremental_stream_arrives_while_job_is_running(self):
        with self.client.run_test(self.root / 'images/hello.bin', timeout=3, poll_interval=.01) as stream:
            first = next(stream)
            self.assertTrue(first)
            self.assertEqual(self.client.job(stream.id).state, 'running')
            text = first + ''.join(stream)
            self.assertIn('SIMULATED FPGA hello: PASS', text)
            self.assertTrue(stream.result.ok)
            self.assertTrue(stream.result.details['simulated'])

    def test_fifo_two_ports_cancel_and_client_close_reuse_sessions(self):
        sessions = {p: s.epoch for p, s in self.app.backend.sessions.items()}
        tasks = [self.client.submit(self.root / 'images/hang.bin', timeout=3) for _ in range(4)]
        eventually(lambda: all(self.client.job(t.id).state == 'running' for t in tasks[:2]))
        self.assertEqual({self.client.job(t.id).fpga for t in tasks[:2]}, {4, 5})
        self.assertEqual([self.client.job(t.id).position for t in tasks[2:]], [1, 2])
        self.client.cancel(tasks[2].id)
        self.client.cancel(tasks[0].id)
        eventually(lambda: self.client.job(tasks[3].id).state == 'running')
        self.assertEqual(self.client.job(tasks[3].id).fpga, self.client.job(tasks[0].id).fpga)
        self.assertEqual(self.client.job(tasks[1].id).state, 'running')
        self.client.close()
        eventually(lambda: all(self.client.job(t.id).state == 'cancelled' for t in tasks))
        self.assertEqual({p: s.epoch for p, s in self.app.backend.sessions.items()}, sessions)
        with Client(self.url, token_file=self.root / 'service.token') as observer:
            task = observer.submit(self.root / 'images/hello.bin', timeout=3)
            self.assertTrue(observer.wait(task.id, timeout=4, poll_interval=.01).result.ok)

    def test_invalid_real_bin_fails_before_release(self):
        image = self.root / 'real.bin'
        image.write_bytes(b'pretend real RISC-V binary')
        task = self.client.submit(image, timeout=2)
        task = self.client.wait(task.id, timeout=3, poll_interval=.01)
        self.assertEqual(task.state, 'failed')
        self.assertIn('hardware was not started', task.error)
        self.assertFalse((self.app.backend.root / 'jobs' / task.id / 'start-intent.json').exists())

    def test_killed_client_lease_cancels_without_restarting_virtual_session(self):
        session_ids = {p: s.epoch for p, s in self.app.backend.sessions.items()}
        env = {**os.environ, 'PYTHONPATH': str(PROJECT / 'src'), 'NO_PROXY': '*', 'no_proxy': '*'}
        script = ('import sys,time; from fpga_scheduler import Client; '
                  'c=Client(sys.argv[1], token_file=sys.argv[2], heartbeat_seconds=.05, lease_seconds=.5); '
                  't=c.submit(sys.argv[3], timeout=5); print(t.id,flush=True); time.sleep(30)')
        process = subprocess.Popen([sys.executable, '-c', script, self.url,
                                    str(self.root / 'service.token'), str(self.root / 'images/hang.bin')],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ident = process.stdout.readline().strip()
            eventually(lambda: self.client.job(ident).state == 'running')
            process.kill()
            process.wait(timeout=3)
            eventually(lambda: self.client.job(ident).state == 'cancelled')
            self.assertTrue(self.client.job(ident).result.details['reset_confirmed'])
            self.assertEqual({p: s.epoch for p, s in self.app.backend.sessions.items()}, session_ids)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()


@unittest.skipUnless(shutil.which('tclsh'), 'tclsh required')
class SimulationProcessTests(unittest.TestCase):
    def test_local_script_crash_restart_unknown_no_replay_and_stop_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'sim'
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = {**os.environ, 'NO_PROXY': '*', 'no_proxy': '*'}
            env.pop('PYTHONPATH', None)
            script = str(PROJECT / 'tools/fpga_sim.py')
            init = subprocess.run([sys.executable, script, 'init', str(root),
                                   '--ports', '4', '--port', str(port)],
                                  env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(init.returncode, 0, init.stderr)
            token = root / 'service.token'
            process = None
            with Client(f'http://127.0.0.1:{port}', token_file=token, request_timeout=1) as client, \
                    (root / 'process.log').open('a') as log:
                def launch():
                    child = subprocess.Popen([sys.executable, script, 'serve', str(root)],
                                             env=env, stdout=log, stderr=log)
                    def healthy():
                        if child.poll() is not None:
                            self.fail((root / 'process.log').read_text())
                        try:
                            return client.health()['dispatcher_alive']
                        except Exception:
                            return False
                    try:
                        eventually(healthy, timeout=10)
                    except BaseException:
                        if child.poll() is None:
                            child.terminate()
                            child.wait(timeout=10)
                        raise
                    return child

                try:
                    process = launch()
                    first = client.submit(root / 'images/hang.bin', timeout=5)
                    second = client.submit(root / 'images/hello.bin', timeout=3)
                    marker = root / 'state/resident/jobs' / first.id / 'start-intent.json'
                    eventually(marker.exists)
                    old_session = json.loads((root / 'state/resident/ports/4/session.json').read_text())
                    process.kill()
                    process.wait(timeout=5)
                    process = launch()
                    self.assertEqual(client.job(first.id).state, 'failed')
                    self.assertIn('unknown', client.job(first.id).error)
                    self.assertTrue(client.wait(second.id, timeout=10, poll_interval=.02).result.ok)
                    self.assertIsNone(fingerprint(old_session['pid']))
                    self.assertFalse((root / 'state/resident/jobs' / first.id / 'result.json').exists())
                    # Each process lifetime initializes once, and the unknown first
                    # task never reaches the board again after the restart.
                    self.assertEqual((root / 'state/resident/ports/4/downloads').read_text().splitlines(),
                                     ['download', 'download'])
                    run = client.submit(root / 'images/hello.bin', timeout=3)
                    eventually(lambda: client.job(run.id).state == 'running')
                    queued = client.submit(root / 'images/hello.bin', timeout=3)
                    process.send_signal(signal.SIGINT)
                    self.assertEqual(process.wait(timeout=10), 0)
                    process = launch()
                    self.assertEqual(client.job(run.id).state, 'completed')
                    self.assertTrue(client.wait(queued.id, timeout=10, poll_interval=.02).result.ok)
                    # Production CLI remains a normal client of the local HTTP service.
                    env['PYTHONPATH'] = str(PROJECT / 'src')
                    command = subprocess.run([sys.executable, '-m', 'fpga_scheduler', '--endpoint', client.endpoint,
                                              '--token-file', str(token), '--json', 'run',
                                              str(root / 'images/hello.bin'), '--timeout', '3'],
                                             env=env, capture_output=True, text=True, timeout=8)
                    self.assertEqual(command.returncode, 0, command.stderr)
                    self.assertEqual(json.loads(command.stdout)['state'], 'completed')
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
                    for record in (root / 'state/resident/ports').glob('*/session.json'):
                        retire_record(record)
