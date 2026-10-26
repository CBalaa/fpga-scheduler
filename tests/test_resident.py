"""Resident integration: real Tcl, PTY UART, durable queue, no hardware."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import pty
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fpga_scheduler.backends.resident import Resident
from fpga_scheduler.config import Node, load
from fpga_scheduler.models import BUSY_EXIT
from fpga_scheduler.queue import JobQueue
from fpga_scheduler.storage import Images
from fpga_scheduler.remote.uvhs_session import BoundedLog, atomic_json, fingerprint, retire_record

FIXTURE = Path(__file__).parent / 'fixtures/resident_uvhs.tcl'


@unittest.skipUnless(shutil.which('tclsh'), 'tclsh required for resident control protocol')
class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        (self.project / 'user_script/hw_run').mkdir(parents=True)
        (self.project / 'hw.dat').mkdir()
        for p in (4, 5):
            (self.project / 'user_script/hw_run' / f'hw_run{p}.tcl').write_text(
                'load_db -release\nload_db -db ./hw.dat\nconfig -clock -frequency 5529600 sys_clk\n'
                'config -clock -commit\nreset -name cpu_reset -value 1\n'
                'download\ninitialize\nsource ./user_script/hw_run/write_image.tcl\n')
        wrapper = self.root / 'uvhs'
        import shlex
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(shutil.which('tclsh')) + ' ' + shlex.quote(str(FIXTURE.resolve())) + ' "$@"\n')
        wrapper.chmod(0o755)
        self.masters = {}
        for p in (4, 5):
            master, slave = pty.openpty()
            (self.root / f'uart{p}').symlink_to(os.ttyname(slave))
            os.close(slave)
            self.masters[p] = master
        self.node = Node('local', (4, 5), uvhs_shell=str(wrapper), uart_prefix=str(self.root / 'uart'))
        self.backend = Resident((self.node,), self.root / 'state', hardware_project=self.project,
                                lock_dir=self.root / 'locks', startup_timeout=2, command_timeout=1,
                                uart_quiet_seconds=.03, recovery_backoff=.01)
        self.images = Images(self.root / 'state/images')
        self.stop_emitter = threading.Event()
        self.emitter = threading.Thread(target=self.emit)
        self.emitter.start()

    def tearDown(self):
        self.backend.close()
        self.stop_emitter.set()
        self.emitter.join()
        for fd in self.masters.values():
            os.close(fd)
        self.tmp.cleanup()

    def emit(self):
        seen = {4: 0, 5: 0}
        while not self.stop_emitter.wait(.005):
            for p, fd in self.masters.items():
                path = self.backend.root / 'ports' / str(p) / 'release'
                if not path.exists():
                    continue
                lines = path.read_text().splitlines()
                if len(lines) <= seen[p]:
                    continue
                mode = lines[seen[p]]
                seen[p] += 1
                if mode == 'hang':
                    os.write(fd, b'ordinary PASS\n')
                    continue
                record = dict(version=1, status='exited', app_exit_code=int(mode) if mode.lstrip('-').isdigit() else 0)
                if mode == 'trap':
                    record = dict(version=1, status='trap', mcause='0x2', mepc='0x80000000', mtval='0x0')
                terminal = ('@@FPGA_RESULT ' + json.dumps(record) + '\n').encode()
                data = b'@@FPGA_START {"version":1}\n' + mode.encode() + b'\n' + terminal
                if mode == 'duplicate':
                    data += terminal
                for i in range(0, len(data), 11):
                    try:
                        os.write(fd, data[i:i + 11])
                    except OSError:
                        break
                    time.sleep(.001)

    def task(self, i, mode='hello', **kwargs):
        ident = f'{i:032x}'
        digest = self.images.put(ident, mode.encode())
        return dict(id=ident, sha256=digest, timeout=2, idle_seconds=1, queue_timeout=60, **kwargs)

    def run_task(self, payload, port=4):
        image = self.images.freeze(payload['id'], payload['sha256'])
        self.backend.prepare(payload, 'local', image)
        return self.backend.run(payload, 'local', port)

    def ready(self):
        ports = self.backend.probe('local')
        self.assertTrue(all(p['available'] for p in ports), ports)

    def test_same_session_a_b_a_and_stale_uart_boundary(self):
        self.ready()
        epoch = self.backend.sessions[4].epoch
        for i, mode in enumerate(('A', 'B', 'A'), 1):
            if i == 2:
                os.write(self.masters[4], b'@@FPGA_START {"version":1}\n@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":7}\n')
            result = self.run_task(self.task(i, mode))
            self.assertEqual(result['exit_code'], 0, result)
            self.assertIn('\n' + mode + '\n', result['uart_output'])
            self.assertEqual(result['details']['session'], epoch)
            self.assertTrue(result['details']['reset_confirmed'])
        self.assertEqual((self.backend.root / 'ports/4/downloads').read_text().count('download'), 1)

    def test_signed_exit_trap_duplicate_and_false_pass(self):
        self.ready()
        for i, mode in enumerate(('-2147483648', '2147483647', 'trap', 'duplicate', 'hang'), 1):
            payload = self.task(i, mode)
            if mode == 'hang':
                payload['timeout'] = 1
            result = self.run_task(payload)
            if mode.lstrip('-').isdigit():
                self.assertEqual(result['app_exit_code'], int(mode))
                self.assertEqual(result['execution_status'], 'exited')
            else:
                self.assertEqual(result['execution_status'], dict(trap='trap', duplicate='protocol_error', hang='timeout')[mode], result)
            self.assertNotEqual(result['exit_code'], 0)
            if self.backend.states[4]['state'] != 'READY':
                time.sleep(.02)
                self.ready()

    def test_readback_and_command_failure_never_release_cpu(self):
        self.ready()
        for i, env in enumerate(('FAKE_READBACK_ERROR', 'FAKE_COMMAND_ERROR'), 1):
            # Existing Tcl subprocess reads its inherited environment; seed it
            # through the same controlled command channel for the fault test.
            self.backend.sessions[4].command('set ::env(' + env + ') 1')
            result = self.run_task(self.task(i))
            self.assertEqual(result['execution_status'], 'platform_error', result)
            self.assertFalse((self.backend.root / 'jobs' / f'{i:032x}' / 'start-intent.json').exists())
            time.sleep(.02)
            self.ready()

    def test_cleanup_failure_overrides_app_zero(self):
        self.ready()
        session = self.backend.sessions[4]
        original = session.reset
        calls = [0]
        def reset():
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError('injected cleanup error')
            return original()
        with patch.object(session, 'reset', side_effect=reset):
            result = self.run_task(self.task(1))
        self.assertEqual(result['app_exit_code'], 0)
        self.assertEqual(result['execution_status'], 'platform_error')
        self.assertNotEqual(result['exit_code'], 0)

    def test_whitelist_busy_and_shared_board_wait(self):
        physical = {p: dict(owner='foreign' if p == 6 else '', booked=False, power='on') for p in range(8)}
        with patch.object(self.backend, '_physical', return_value=physical):
            ports = self.backend.probe('local')
        self.assertEqual([p['number'] for p in ports], [4, 5])
        self.assertTrue(all(not p['available'] for p in ports))
        self.assertFalse(self.backend.sessions)
        self.ready()
        with self.backend.guard:
            self.backend.states[5].update(state='BUSY')
            self.backend.states[4].update(state='RECOVERING')
        with patch.object(self.backend, '_physical') as query:
            self.backend.probe('local')
            query.assert_not_called()
        self.backend.states[5]['state'] = 'READY'
        self.backend.states[4]['state'] = 'READY'
        with self.assertRaisesRegex(ValueError, 'whitelist'):
            self.backend.run(self.task(1), 'local', 6)

    def test_fifo_two_ports_cancel_idempotence_recovery_and_retention(self):
        self.ready()
        q = JobQueue(self.root / 'jobs.db', self.backend, self.images, interval=.01, cooldown=0)
        tasks = [self.task(i, str(i)) for i in range(1, 7)]
        for payload in tasks:
            q.submit(payload)
        q.submit(tasks[0])
        q.cancel(tasks[-1]['id'])
        q.dispatch_once()
        self.assertEqual([q.get(t['id'])['fpga'] for t in tasks[:2]], [4, 5])
        self.assertEqual(q.get(tasks[2]['id'])['state'], 'queued')
        while q.list()['total']:
            for worker in list(q.workers):
                worker.join(5)
            q.dispatch_once()
        row = q.db.execute('SELECT * FROM jobs WHERE id=?', (tasks[0]['id'],)).fetchone()
        self.assertEqual(self.backend.recover(dict(row))['app_exit_code'], 1)
        # Simulate crash after terminal file publication and before SQLite commit.
        with q.db:
            q.db.execute("UPDATE jobs SET state='running',result=NULL WHERE id=?", (tasks[0]['id'],))
        q.close()
        q2 = JobQueue(self.root / 'jobs.db', self.backend, self.images)
        try:
            self.assertEqual(q2.get(tasks[0]['id'])['result']['app_exit_code'], 1)
            self.assertEqual(q2.get(tasks[-1]['id'])['state'], 'cancelled')
            self.backend.cleanup(time.time() + 10)
            self.assertTrue((self.backend.root / 'jobs' / tasks[0]['id']).exists())
        finally:
            q2.close()

    def test_reset_command_echo_cannot_substitute_for_query(self):
        self.ready()
        session = self.backend.sessions[4]
        session.command('set ::env(FAKE_RESET_QUERY_ZERO) 1')
        try:
            with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
                session.reset()
        finally:
            session.command('unset ::env(FAKE_RESET_QUERY_ZERO)')

    def test_storage_limit_does_not_delete_inputs(self):
        payload = self.task(1)
        self.backend.max_storage = 1
        with self.assertRaisesRegex(OSError, 'quota'):
            self.backend.prepare(payload, 'local', self.images.path(payload['id']))
        self.assertTrue(self.images.path(payload['id']).exists())


class LogTests(unittest.TestCase):
    def test_rotation_and_task_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'control.log'
            log = BoundedLog(path, 8, rotate=True)
            log.write(b'12345678'); log.write(b'next'); log.close()
            self.assertEqual(path.read_bytes(), b'next')
            self.assertEqual(path.with_suffix('.log.1').read_bytes(), b'12345678')
            log = BoundedLog(Path(tmp) / 'task.log', 4)
            with self.assertRaises(RuntimeError):
                log.write(b'12345')
            log.close()

class RecoveryAndQuotaTests(unittest.TestCase):
    def test_pid_leader_exit_does_not_hide_owned_grandchild(self):
        import subprocess
        import signal
        from fpga_scheduler.remote.uvhs_session import group_members
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_file = root / 'child'
            code = "import os,subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(.2)"
            leader = subprocess.Popen([sys.executable, '-c', code, str(child_file)], start_new_session=True)
            try:
                record = dict(pid=leader.pid, start_ticks=fingerprint(leader.pid))
                path = root / 'session.json'; atomic_json(path, record)
                end = time.monotonic() + 3
                while not child_file.exists() and time.monotonic() < end:
                    time.sleep(.01)
                leader.wait(timeout=3)
                self.assertTrue(group_members(record))
                retire_record(path)
                self.assertFalse(group_members(record))
            finally:
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                leader.wait()

    def test_input_quota_never_evicts_active_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = Images(Path(tmp), max_bytes=6)
            images.put('1' * 32, b'abcd')
            images.freeze('1' * 32, hashlib.sha256(b'abcd').hexdigest())
            with self.assertRaises(OverflowError):
                images.put('2' * 32, b'xyz')
            self.assertEqual(images.path('1' * 32).read_bytes(), b'abcd')

    def test_resident_configuration_rejects_ssh_and_bad_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            base = ('backend="bosc-resident"\nstate_dir="state"\ntoken_file="token"\n'
                    '[resident]\nhardware_project="project"\nlock_dir="locks"\n'
                    '[[nodes]]\nid="local"\nports=[4,5]\nuvhs_shell="/vendor/uvhs"\n')
            path.write_text(base)
            self.assertEqual(load(path).nodes[0].ports, (4,5))
            for bad in (base + 'ssh_host="remote"\n', base.replace('ports=[4,5]', 'ports=[4,4]'),
                        base.replace('hardware_project="project"', 'profile="dma"\nhardware_project="project"'),
                        base.replace('lock_dir="locks"', 'lock_dir="locks"\nrecovery_attempts=1.5')):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    load(path)
