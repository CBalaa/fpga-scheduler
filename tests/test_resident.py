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
from unittest.mock import call, patch

from fpga_scheduler.backends.resident import Resident
from fpga_scheduler.config import Node, load
from fpga_scheduler.models import BUSY_EXIT
from fpga_scheduler.queue import JobQueue
from fpga_scheduler.storage import Images, StorageQuotaExceeded
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
        self.init_scripts = {}
        (self.root / 'deployment/init').mkdir(parents=True)
        for p in (4, 5):
            # Resident must not inspect or execute the full project run script.
            (self.project / 'user_script/hw_run' / f'hw_run{p}.tcl').write_text(
                'error {full application hardware script must not be used}\nexit\n')
            script = self.root / 'deployment/init' / f'init{p}.tcl'
            script.write_text(self.initialization(p))
            self.init_scripts[p] = script
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
                                lock_dir=self.root / 'locks', init_scripts=self.init_scripts,
                                startup_timeout=2, command_timeout=1,
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
                if mode in ('hang', 'partial-start'):
                    os.write(fd, b'ordinary PASS\n' if mode == 'hang' else b'@@FPGA_START {"version":')
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

    @staticmethod
    def initialization(port, *, explicit=False, clock=False):
        target = f'b{port // 4}.f{port % 4}'
        # These intentionally preserve each server's observed reference order.
        allocation = (f'allocate -board {{{target}}}\nload_db -release\nload_db -db ./hw.dat\n'
                      if explicit else f'load_db -release\nload_db -db ./hw.dat -to {{{target}}}\n')
        clock_commands = ('config -clock -frequency 5529600 sys_clk\nconfig -clock -commit\n'
                          if clock else '')
        return ('# Deployment source: test reference; SHA-256 captured by Resident\n'
                + allocation + clock_commands + 'reset -name cpu_reset -value 1\n'
                'download\ninitialize\nreset -name cpu_reset -value 1\nquery -reset\n')

    def test_database_target_reference_executes_original_order_without_allocate(self):
        original = self.init_scripts[4].read_bytes()
        with patch.dict(os.environ, FAKE_REJECT_ALLOCATION='1'):
            self.ready()
            result = self.run_task(self.task(1))
        self.assertEqual(result['exit_code'], 0, result)
        commands = (self.backend.sessions[4].directory / 'control-commands').read_text().splitlines()
        self.assertEqual(commands[:2], ['load_db -release', 'load_db -db ./hw.dat -to b1.f0'])
        self.assertFalse(any(line.startswith('allocate ') for line in commands))
        self.assertEqual(self.init_scripts[4].read_bytes(), original)

    def test_explicit_reference_preserves_allocate_before_release_without_to(self):
        self.init_scripts[4].write_text(self.initialization(4, explicit=True))
        original = self.init_scripts[4].read_bytes()
        with patch.dict(os.environ, FAKE_REQUIRE_ALLOCATION='1'):
            self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
        result = self.run_task(self.task(1))
        self.assertEqual(result['exit_code'], 0, result)
        commands = (self.backend.sessions[4].directory / 'control-commands').read_text().splitlines()
        self.assertEqual(commands[:3], ['allocate -board b1.f0', 'load_db -release',
                                       'load_db -db ./hw.dat'])
        self.assertEqual(self.init_scripts[4].read_bytes(), original)

    def test_source_snapshot_preserves_all_script_bytes_and_records_origin(self):
        original = self.initialization(4).replace('\n', '\r\n').encode()
        self.init_scripts[4].write_bytes(original)
        self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
        session = self.backend.sessions[4]
        self.assertEqual((session.directory / 'init.tcl').read_bytes(), original)
        metadata = json.loads((session.directory / 'init-script.json').read_text())
        self.assertEqual(metadata['source'], str(self.init_scripts[4]))
        self.assertEqual(metadata['sha256'], hashlib.sha256(original).hexdigest())
        self.assertTrue(session.reset())
        self.assertFalse((session.directory / 'release').exists())

    def test_initialization_errors_timeout_and_unconfirmed_reset_never_become_ready(self):
        for fault in ('FAKE_INIT_ERROR', 'FAKE_INIT_REPORTED_ERROR',
                      'FAKE_INIT_DELAY', 'FAKE_RESET_QUERY_ZERO'):
            with self.subTest(fault=fault):
                self.backend.options['startup_timeout'] = .1
                with patch.dict(os.environ, {fault: '500' if fault == 'FAKE_INIT_DELAY' else '1'}):
                    self.backend._initialize(4)
                self.assertNotEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
                self.assertNotIn(4, self.backend.sessions)
                self.assertIn('Initialization failed', self.backend.states[4]['reason'])
                self.assertFalse((self.backend.root / 'ports/4/release').exists())
                if fault == 'FAKE_INIT_ERROR':
                    log = (self.backend.root / 'ports/4/control.log').read_text()
                    self.assertIn('Scheduler Tcl error: injected initialization failure', log)
                    self.assertIn('init.tcl', log)
                    self.assertIn('initialize', log)
        self.assertEqual(self.backend.states[4]['state'], 'QUARANTINED')

    def test_initializer_must_leave_reset_asserted_before_scheduler_reset_command(self):
        # A source acknowledgement alone is insufficient. initialize can alter
        # the signal, even if the script asserted it before bitstream download.
        self.init_scripts[4].write_text(self.initialization(4).replace(
            'initialize\nreset -name cpu_reset -value 1\nquery -reset\n', 'initialize\n'))
        with patch.dict(os.environ, FAKE_INIT_RELEASE_CPU='1'):
            self.backend._initialize(4)
        self.assertNotEqual(self.backend.states[4]['state'], 'READY')
        self.assertNotIn(4, self.backend.sessions)
        self.assertIn('not confirmed', self.backend.states[4]['reason'])
        commands = (self.backend.root / 'ports/4/control-commands').read_text().splitlines()
        after_initialize = commands.index('initialize') + 1
        self.assertEqual(commands[after_initialize], 'query -reset')
        self.assertFalse((self.backend.root / 'ports/4/release').exists())

    def test_reinitialize_rereads_deployment_script_and_replaces_snapshot(self):
        self.backend._initialize(4)
        old = self.backend.sessions[4]
        original_snapshot = (old.directory / 'init.tcl').read_bytes()
        changed = self.initialization(4, explicit=True).replace('# Deployment source:', '# Updated source:')
        self.init_scripts[4].write_text(changed)
        # An already running shell keeps its initialized state until rebuilt.
        self.assertEqual((old.directory / 'init.tcl').read_bytes(), original_snapshot)
        self.assertEqual(self.run_task(self.task(1))['exit_code'], 0)
        self.backend._initialize(4)
        current = self.backend.sessions[4]
        self.assertIsNot(current, old)
        self.assertNotEqual(current.epoch, old.epoch)
        self.assertIsNotNone(old.process.poll())
        self.assertEqual((current.directory / 'init.tcl').read_bytes(), changed.encode())
        self.assertEqual(self.run_task(self.task(2))['exit_code'], 0)
        commands = (current.directory / 'control-commands').read_text().splitlines()
        self.assertEqual(commands.count('allocate -board b1.f0'), 1)
        self.assertEqual((current.directory / 'downloads').read_text().count('download'), 2)

    def test_missing_initialization_file_during_recovery_never_uses_project_script(self):
        self.backend._initialize(4)
        self.init_scripts[4].unlink()
        self.backend._initialize(4)
        self.assertNotEqual(self.backend.states[4]['state'], 'READY')
        self.assertIn('init4.tcl', self.backend.states[4]['reason'])

    def test_same_session_a_b_a_and_stale_uart_boundary(self):
        self.ready()
        session = self.backend.sessions[4]
        epoch, pid = session.epoch, session.process.pid
        for i, mode in enumerate(('A', 'B', 'A'), 1):
            if i == 2:
                os.write(self.masters[4], b'@@FPGA_START {"version":1}\n@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":7}\n')
            result = self.run_task(self.task(i, mode))
            self.assertEqual(result['exit_code'], 0, result)
            self.assertIn('\n' + mode + '\n', result['uart_output'])
            self.assertEqual(result['details']['session'], epoch)
            self.assertEqual(result['details']['uvhs_pid'], pid)
            self.assertTrue(result['details']['reset_confirmed'])
        self.assertEqual((self.backend.root / 'ports/4/downloads').read_text().count('download'), 1)
        commands = (session.directory / 'control-commands').read_text().splitlines()
        self.assertEqual(commands.count('initialize'), 1)
        self.assertEqual(sum(line.startswith('load_db -db ') for line in commands), 1)
        writes = [i for i, line in enumerate(commands) if line.startswith('writemem ')]
        self.assertEqual(len(writes), 3)
        for start, end in zip(writes, writes[1:] + [len(commands)]):
            self.assertEqual(commands[start - 2:start], ['reset -name cpu_reset -value 1', 'query -reset'])
            task_commands = commands[start:end]
            readback = next(i for i, line in enumerate(task_commands) if line.startswith('readmem '))
            release = task_commands.index('reset -name cpu_reset -value 0')
            self.assertLess(readback, release)
            self.assertIn('reset -name cpu_reset -value 1', task_commands[release + 1:])
            self.assertEqual(task_commands[-1], 'query -reset')

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

    def test_abort_mid_protocol_record_with_confirmed_reset_reuses_session(self):
        self.ready()
        session = self.backend.sessions[4]
        q = JobQueue(self.root / 'partial-record.db', self.backend, self.images, cooldown=60)
        try:
            for i, cancel in ((1, False), (2, True)):
                payload = self.task(i, 'partial-start')
                payload['timeout'] = 1
                q.submit(payload)
                q.dispatch_once()
                deadline = time.monotonic() + .8
                while time.monotonic() < deadline:
                    data = self.backend.read_output(payload['id'], 0, 65536) or b''
                    if data.startswith(b'@@FPGA_START'):
                        break
                    time.sleep(.005)
                self.assertEqual(data, b'@@FPGA_START {"version":')
                if cancel:
                    q.cancel(payload['id'])
                for worker in list(q.workers):
                    worker.join(3)
                result = q.get(payload['id'])['result']
                self.assertEqual(result['execution_status'], 'cancelled' if cancel else 'timeout', result)
                self.assertTrue(result['details']['reset_confirmed'])
                self.assertTrue(result['details']['uart_record_interrupted'])
                self.assertNotIn('cleanup_error', result['details'])
                self.assertIs(self.backend.sessions[4], session)
                self.assertEqual(self.backend.states[4]['state'], 'READY')
            result = self.run_task(self.task(3))
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['details']['session'], session.epoch)
            self.assertEqual(q.db.execute('SELECT count(*) FROM cooldowns').fetchone()[0], 0)
        finally:
            q.close()

    def test_live_uart_output_available_before_timeout(self):
        self.ready()
        q = JobQueue(self.root / 'stream.db', self.backend, self.images, cooldown=0)
        payload = self.task(1, 'hang')
        payload['timeout'] = 1
        try:
            q.submit(payload)
            q.dispatch_once()
            deadline = time.monotonic() + .8
            raw = b''
            while time.monotonic() < deadline:
                raw = self.backend.read_output(payload['id'], 0, 65536) or b''
                if b'ordinary PASS' in raw:
                    break
                time.sleep(.005)
            self.assertIn(b'ordinary PASS', raw)
            self.assertEqual(q.get(payload['id'])['state'], 'running')
            self.assertFalse(q.output(payload['id'])['done'])
            for worker in list(q.workers):
                worker.join(3)
            result = q.get(payload['id'])['result']
            self.assertEqual(result['execution_status'], 'timeout')
            self.assertTrue(q.output(payload['id'])['done'])
        finally:
            q.close()

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

    def test_timeout_and_running_cancel_reuse_pid_session_without_cooldown(self):
        self.ready()
        session = self.backend.sessions[4]
        epoch, pid = session.epoch, session.process.pid
        q = JobQueue(self.root / 'abort.db', self.backend, self.images, cooldown=60)
        try:
            for i, cancel in ((1, False), (2, True)):
                payload = self.task(i, 'hang')
                payload['timeout'] = 1
                q.submit(payload)
                q.dispatch_once()
                if cancel:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if b'ordinary PASS' in (self.backend.read_output(payload['id'], 0, 65536) or b''):
                            break
                        time.sleep(.005)
                    self.assertEqual(q.cancel(payload['id'])['state'], 'cancelling')
                    self.assertEqual(q.cancel(payload['id'])['state'], 'cancelling')
                for worker in list(q.workers):
                    worker.join(4)
                task = q.get(payload['id'])
                self.assertEqual(task['state'], 'cancelled' if cancel else 'failed')
                result = task['result']
                self.assertEqual(result['execution_status'], 'cancelled' if cancel else 'timeout')
                self.assertIsNone(result['app_exit_code'])
                self.assertIn('ordinary PASS', result['uart_output'])
                self.assertTrue(result['details']['reset_confirmed'])
                self.assertIs(self.backend.sessions[4], session)
                self.assertEqual(self.backend.states[4]['state'], 'READY')
                self.assertEqual(q.db.execute('SELECT count(*) FROM cooldowns').fetchone()[0], 0)
                self.assertEqual(q.cancel(payload['id'])['state'], task['state'])
                # Crash after atomic result publication but before DB terminal
                # publication must recover the same abort, never execute again.
                with q.lock:
                    row = dict(q.db.execute('SELECT * FROM jobs WHERE id=?', (payload['id'],)).fetchone())
                recovered = self.backend.recover(row)
                self.assertEqual(recovered['execution_status'], result['execution_status'])
                self.assertEqual(recovered['details']['session'], epoch)
                hello = self.run_task(self.task(i + 10))
                self.assertEqual(hello['execution_status'], 'exited')
                self.assertEqual((hello['details']['session'], hello['details']['uvhs_pid']), (epoch, pid))
                self.assertNotIn('ordinary PASS', hello['uart_output'])
            self.assertEqual((session.directory / 'downloads').read_text().count('download'), 1)
        finally:
            q.close()

    def test_cancel_during_memory_load_never_releases_cpu_or_resets_concurrently(self):
        self.ready()
        session = self.backend.sessions[4]
        entered, proceed = threading.Event(), threading.Event()
        original = session.command
        def command(script, *args, **kwargs):
            if script.startswith('writemem'):
                entered.set()
                proceed.wait(2)
            return original(script, *args, **kwargs)
        q = JobQueue(self.root / 'load-cancel.db', self.backend, self.images)
        payload = self.task(1)
        try:
            with patch.object(session, 'command', side_effect=command):
                q.submit(payload)
                q.dispatch_once()
                self.assertTrue(entered.wait(2))
                seq = session.seq
                self.assertEqual(q.cancel(payload['id'])['state'], 'cancelling')
                time.sleep(.03)
                self.assertEqual(session.seq, seq)  # Handler did not send reset.
                self.assertIn(('local', 4), q.reserved)
                proceed.set()
                for worker in list(q.workers):
                    worker.join(4)
            result = q.get(payload['id'])['result']
            self.assertEqual(result['execution_status'], 'cancelled')
            self.assertTrue(result['details']['reset_confirmed'])
            self.assertFalse((session.directory / 'release').exists())
            self.assertFalse((self.backend.root / 'jobs' / payload['id'] / 'start-intent.json').exists())
            self.assertEqual(self.backend.states[4]['state'], 'READY')
        finally:
            proceed.set()
            q.close()

    def test_cancel_at_cpu_release_safe_point_never_starts_image(self):
        self.ready()
        session = self.backend.sessions[4]
        from fpga_scheduler.models import Cancellation
        control = Cancellation()
        original = session.command
        def command(script, *args, **kwargs):
            if script == 'reset -name cpu_reset -value 0':
                control.request('cancel at release')
            return original(script, *args, **kwargs)
        payload = self.task(1)
        self.backend.prepare(payload, 'local', self.images.freeze(payload['id'], payload['sha256']))
        with patch.object(session, 'command', side_effect=command):
            result = self.backend.run(payload, 'local', 4, cancellation=control)
        self.assertEqual(result['execution_status'], 'cancelled')
        self.assertFalse((session.directory / 'release').exists())
        self.assertEqual(self.backend.states[4]['state'], 'READY')

    def test_cancel_one_port_leaves_other_running_session_untouched(self):
        self.ready()
        sessions = {p: (s.epoch, s.process.pid) for p, s in self.backend.sessions.items()}
        q = JobQueue(self.root / 'parallel-abort.db', self.backend, self.images)
        tasks = [self.task(i, 'hang') for i in (1, 2)]
        for task in tasks:
            task['timeout'] = 5
            q.submit(task)
        try:
            q.dispatch_once()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if all(b'ordinary PASS' in (self.backend.read_output(t['id'], 0, 65536) or b'') for t in tasks):
                    break
                time.sleep(.005)
            self.assertTrue(all(self.backend.read_output(t['id'], 0, 65536) for t in tasks))
            q.cancel(tasks[0]['id'])
            deadline = time.monotonic() + 2
            while q.get(tasks[0]['id'])['state'] == 'cancelling' and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(q.get(tasks[0]['id'])['state'], 'cancelled')
            self.assertEqual(q.get(tasks[1]['id'])['state'], 'running')
            self.assertEqual(self.backend.states[4]['state'], 'READY')
            self.assertEqual(self.backend.states[5]['state'], 'BUSY')
            self.assertEqual({p: (s.epoch, s.process.pid) for p, s in self.backend.sessions.items()}, sessions)
            self.assertTrue(all(s.downloads == 1 for s in self.backend.sessions.values()))
        finally:
            q.cancel(tasks[1]['id'])
            q.close()

    def test_real_slow_load_cancel_and_missing_ack_quarantine(self):
        self.ready()
        session = self.backend.sessions[4]
        session.command('set ::env(FAKE_LOAD_DELAY) 500')
        q = JobQueue(self.root / 'slow-load.db', self.backend, self.images)
        try:
            payload = self.task(1)
            q.submit(payload)
            q.dispatch_once()
            deadline = time.monotonic() + 2
            while not (session.directory / 'loading').exists() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue((session.directory / 'loading').exists())
            self.assertTrue(session.command_pending)
            seq = session.seq
            q.cancel(payload['id'])
            time.sleep(.03)
            self.assertEqual(session.seq, seq)
            self.assertEqual(q.get(payload['id'])['state'], 'cancelling')
            for worker in list(q.workers):
                worker.join(4)
            self.assertEqual(q.get(payload['id'])['result']['execution_status'], 'cancelled')
            self.assertEqual(self.backend.states[4]['state'], 'READY')
            self.assertFalse((session.directory / 'release').exists())

            # A command that never acknowledges within its bound cannot be
            # followed by reset while Tcl may still be executing writemem.
            session.command('set ::env(FAKE_LOAD_DELAY) 2000')
            session.command_timeout = .1
            result = self.run_task(self.task(2))
            self.assertEqual(result['execution_status'], 'platform_error')
            self.assertEqual(result['details']['interrupted_status'], 'timeout')
            self.assertFalse(result['details']['reset_confirmed'])
            self.assertIn('unacknowledged', result['details']['cleanup_error'])
            self.assertEqual(self.backend.states[4]['state'], 'QUARANTINED')
            self.assertFalse((session.directory / 'release').exists())
            self.assertIs(self.backend.sessions[4], session)
            self.assertEqual(session.downloads, 1)
        finally:
            q.close()

    def test_failed_timeout_reset_quarantines_without_automatic_rebuild(self):
        self.ready()
        session = self.backend.sessions[4]
        original = session.reset
        calls = []
        def reset():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('reset query failed')
            return original()
        payload = self.task(1, 'hang')
        payload['timeout'] = 1
        with patch.object(session, 'reset', side_effect=reset):
            result = self.run_task(payload)
        self.assertEqual(result['execution_status'], 'platform_error')
        self.assertEqual(result['details']['interrupted_status'], 'timeout')
        self.assertFalse(result['details']['reset_confirmed'])
        self.assertEqual(self.backend.states[4]['state'], 'QUARANTINED')
        time.sleep(.03)
        with patch.object(self.backend, '_initialize') as initialize:
            ports = self.backend.probe('local')
            initialize.assert_not_called()
        self.assertFalse(ports[0]['available'])
        self.assertIs(self.backend.sessions[4], session)
        self.assertEqual(session.downloads, 1)
        self.assertEqual(self.run_task(self.task(2))['exit_code'], BUSY_EXIT)

    def test_cancel_uart_cleanup_failure_quarantines_and_preserves_reason(self):
        self.ready()
        from fpga_scheduler.models import Cancellation
        session = self.backend.sessions[4]
        control = Cancellation()
        original, calls = session.boundary, []
        def boundary():
            calls.append(1)
            if len(calls) == 2:
                control.request('cancel before execution')
            if len(calls) == 3:
                raise RuntimeError('UART cannot become quiet')
            return original()
        payload = self.task(1)
        self.backend.prepare(payload, 'local', self.images.freeze(payload['id'], payload['sha256']))
        with patch.object(session, 'boundary', side_effect=boundary):
            result = self.backend.run(payload, 'local', 4, cancellation=control)
        self.assertEqual(result['execution_status'], 'platform_error')
        self.assertEqual(result['details']['interrupted_status'], 'cancelled')
        self.assertIn('UART', self.backend.states[4]['reason'])
        self.assertEqual(self.backend.states[4]['state'], 'QUARANTINED')

    def test_cancel_during_unresponsive_command_is_platform_failure_and_quarantined(self):
        self.ready()
        from fpga_scheduler.models import Cancellation
        from fpga_scheduler.remote.uvhs_session import CommandTimeout
        session = self.backend.sessions[4]
        control = Cancellation()
        original = session.command
        def command(script, *args, **kwargs):
            if script.startswith('writemem'):
                control.request('client disconnected')
                raise CommandTimeout('unresponsive write command')
            return original(script, *args, **kwargs)
        payload = self.task(1)
        self.backend.prepare(payload, 'local', self.images.freeze(payload['id'], payload['sha256']))
        with patch.object(session, 'command', side_effect=command):
            result = self.backend.run(payload, 'local', 4, cancellation=control)
        self.assertEqual(result['execution_status'], 'platform_error')
        self.assertEqual(result['details']['interrupted_status'], 'cancelled')
        self.assertEqual(self.backend.states[4]['state'], 'QUARANTINED')

    def test_shared_board_same_clock_initializes_only_whitelisted_free_ports(self):
        physical = {p: dict(owner='foreign' if p == 6 else '', booked=False, power='on') for p in range(8)}
        with patch.object(self.backend, '_physical', return_value=physical):
            ports = self.backend.probe('local')
        self.assertEqual([p['number'] for p in ports], [4, 5])
        self.assertTrue(all(p['available'] for p in ports), ports)
        self.assertEqual(set(self.backend.sessions), {4, 5})
        for session in self.backend.sessions.values():
            self.assertFalse((session.directory / 'clock-configs').exists())
            self.assertEqual((session.directory / 'downloads').read_text().count('download'), 1)
        with self.assertRaisesRegex(ValueError, 'whitelist'):
            self.backend.run(self.task(1), 'local', 6)

    def test_single_reported_board_initializes_and_runs_configured_ports(self):
        with patch.dict(os.environ, FAKE_FPGA_PORTS='4 5 6 7'):
            self.ready()
            result = self.run_task(self.task(1))
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual(set(self.backend.sessions), {4, 5})
        self.assertTrue(all(not (s.directory / 'clock-configs').exists()
                            for s in self.backend.sessions.values()))

    def test_missing_target_does_not_prevent_other_target_with_verified_clock(self):
        with patch.dict(os.environ, FAKE_FPGA_PORTS='4'):
            ports = self.backend.probe('local')
        self.assertTrue(ports[0]['available'], ports)
        self.assertFalse(ports[1]['available'])
        self.assertIn('FPGA5 (B1.F1) missing from UVHS inventory', ports[1]['reasons'][0])
        self.assertEqual(set(self.backend.sessions), {4})
        self.assertFalse((self.backend.root / 'ports/5').exists())
        self.assertFalse((self.backend.sessions[4].directory / 'clock-configs').exists())

    def test_absent_target_is_reported_before_any_uart_or_clock_operation(self):
        with patch.dict(os.environ, FAKE_FPGA_PORTS='0 1 2 3'), \
             patch.object(self.backend, '_uart_busy') as uart, \
             patch.object(self.backend, '_clocks') as clocks:
            self.backend._initialize(4)
        uart.assert_not_called()
        clocks.assert_not_called()
        self.assertIn('FPGA4 (B1.F0) missing from UVHS inventory', self.backend.states[4]['reason'])
        self.assertIn('reported FPGA units: 0, 1, 2, 3', self.backend.states[4]['reason'])
        self.assertFalse(self.backend.sessions)

    def test_incomplete_target_board_never_reconfigures_shared_clock(self):
        self.init_scripts[4].write_text(self.initialization(4, clock=True))
        with patch.dict(os.environ, FAKE_FPGA_PORTS='4'), \
             patch.object(self.backend, '_clocks', return_value={(1, 6): 5529600}):
            self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'RECOVERING')
        self.assertEqual(self.backend.states[4]['attempts'], 0)
        self.assertIn('inventory missing FPGA5, FPGA6, FPGA7', self.backend.states[4]['reason'])
        self.assertFalse(self.backend.sessions)
        self.assertFalse((self.backend.root / 'ports/4').exists())

    def test_initialization_does_not_wait_for_busy_managed_sibling(self):
        with self.backend.guard:
            self.backend._initialize(5)
            sibling = self.backend.sessions[5]
            self.backend.states[5]['state'] = 'BUSY'
            ports = self.backend.probe('local')
        self.assertTrue(ports[0]['available'], ports)
        self.assertEqual(ports[1]['state'], 'BUSY')
        self.assertIs(self.backend.sessions[5], sibling)
        self.assertEqual(self.backend.states[4]['state'], 'READY')
        self.backend.states[5]['state'] = 'READY'

    def test_occupied_target_is_never_initialized_even_with_matching_clocks(self):
        for ownership in (dict(owner='foreign', booked=False), dict(owner='', booked=True)):
            physical = {p: dict(owner='', booked=False, power='on') for p in range(8)}
            physical[4].update(ownership)
            with self.subTest(**ownership), patch.object(self.backend, '_physical', return_value=physical), \
                    patch.object(self.backend, '_clocks') as clocks:
                self.backend._initialize(4)
                clocks.assert_not_called()
                self.assertNotIn(4, self.backend.sessions)
                self.assertIn('occupied', self.backend.states[4]['reason'])

    def test_shared_board_clock_mismatch_or_unknown_blocks_without_download(self):
        physical = {p: dict(owner='foreign' if p == 7 else '', booked=False, power='on') for p in range(8)}
        for clock_script in (False, True):
            self.init_scripts[4].write_text(self.initialization(4, clock=clock_script))
            for clocks in ({(1, 6): 14745600}, {(0, 6): 5529600}, {}):
                with self.subTest(clock_script=clock_script, clocks=clocks), \
                        patch.object(self.backend, '_physical', return_value=physical), \
                        patch.object(self.backend, '_clocks', return_value=clocks), \
                        patch('fpga_scheduler.backends.resident.Session') as session, \
                        patch.object(self.backend, '_lock', wraps=self.backend._lock) as lock:
                    self.backend._initialize(4)
                    session.assert_not_called()
                    self.assertNotIn(call('fpga4.lock'), lock.call_args_list)
                    self.assertFalse(self.backend.sessions)
                    self.assertEqual(self.backend.states[4]['state'], 'RECOVERING')
                    self.assertEqual(self.backend.states[4]['attempts'], 0)
                    self.assertIn('expected 5529600 Hz', self.backend.states[4]['reason'])
                    self.assertIn('FPGA7 active', self.backend.states[4]['reason'])
                    self.assertFalse((self.backend.root / 'ports/4').exists())

    def test_empty_board_executes_explicit_clock_commands_without_changes(self):
        self.init_scripts[4].write_text(self.initialization(4, clock=True))
        with patch.object(self.backend, '_clocks', return_value={(1, 6): 16260162}):
            self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
        commands = (self.backend.sessions[4].directory / 'clock-configs').read_text()
        self.assertEqual(commands.splitlines(), ['-clock -frequency 5529600 sys_clk', '-clock -commit'])

    def test_matching_shared_clock_executes_original_script_with_owned_or_booked_sibling(self):
        # Match the deployed allocate -> release -> load sequence, including
        # clock commands; sibling occupancy alone must not reject this script.
        self.init_scripts[4].write_text(self.initialization(4, explicit=True, clock=True))
        original = self.init_scripts[4].read_bytes()
        for ownership in (dict(owner='foreign', booked=False), dict(owner='', booked=True)):
            physical = {p: dict(owner='', booked=False, power='on') for p in range(8)}
            physical[6].update(ownership)
            with self.subTest(**ownership), patch.object(self.backend, '_physical', return_value=physical):
                self.backend._initialize(4)
                self.assertEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
                self.assertEqual(set(self.backend.sessions), {4})
                session = self.backend.sessions[4]
                self.assertEqual((session.directory / 'init.tcl').read_bytes(), original)
                commands = (session.directory / 'control-commands').read_text().splitlines()
                self.assertEqual(commands[:5], [
                    'allocate -board b1.f0', 'load_db -release', 'load_db -db ./hw.dat',
                    'config -clock -frequency 5529600 sys_clk', 'config -clock -commit'])
                self.assertTrue(session.reset())
                self.assertFalse((self.backend.root / 'ports/6').exists())
        self.assertEqual(self.init_scripts[4].read_bytes(), original)

    def test_matching_shared_clock_accepts_clock_script_with_active_managed_session(self):
        self.backend._initialize(5)
        sibling = self.backend.sessions[5]
        sibling_pid = sibling.process.pid
        sibling_downloads = sibling.downloads
        self.init_scripts[4].write_text(self.initialization(4, clock=True))
        self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
        self.assertIs(self.backend.sessions[5], sibling)
        self.assertEqual(sibling.process.pid, sibling_pid)
        self.assertEqual(sibling.downloads, sibling_downloads)
        commands = (self.backend.sessions[4].directory / 'clock-configs').read_text()
        self.assertEqual(commands.splitlines(), ['-clock -frequency 5529600 sys_clk', '-clock -commit'])
        self.assertTrue(sibling.reset())

    def test_fpga113_reference_initialization_then_two_jobs_reuse_same_shell(self):
        # Executable initialization steps read from fpga113's make uv_run4
        # reference hw_run4.tcl (SHA-256 2fcb89cf9291f788f1c9bdb740c301f0
        # 453a6e866c41c02d5d883ecb561eea76). Deployment appends reset/query
        # instead of the application-stage source/reset release.
        source = ('query -user\nquery -fpgas -all\nquery -version\n'
                  'allocate -board {b1.f0}\nload_db -release\nload_db -db ./hw.dat\n'
                  'config -connector\nquery -connector -type fmc\nquery -voltage\n'
                  'set sys_clk_hz 5529600\n'
                  'if {[info exists ::env(UV_RUN_SYS_CLK_HZ)]} {\n'
                  '  set sys_clk_hz $::env(UV_RUN_SYS_CLK_HZ)\n}\n'
                  'config -clock -frequency $sys_clk_hz sys_clk\n'
                  'config -clock -commit\nquery -clock\n'
                  'reset -name cpu_reset -value 1\nquery -reset\n'
                  'download\nquery -ipinfo\ninitialize\n'
                  'reset -name cpu_reset -value 1\nquery -reset\n')
        self.init_scripts[4].write_text(source)
        physical = {p: dict(owner='foreign' if p in (5, 6, 7) else '',
                            booked=False, power='on') for p in (4, 5, 6, 7)}
        with patch.object(self.backend, '_physical', return_value=physical), \
                patch.dict(os.environ, UV_RUN_SYS_CLK_HZ='5529600', FAKE_REQUIRE_ALLOCATION='1'):
            self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
        session = self.backend.sessions[4]
        pid = session.process.pid
        for ident in (1, 2):
            result = self.run_task(self.task(ident))
            self.assertEqual(result['exit_code'], 0, result)
            self.assertTrue(result['details']['reset_confirmed'])
            self.assertIs(self.backend.sessions[4], session)
            self.assertEqual(session.process.pid, pid)
        self.assertEqual((session.directory / 'init.tcl').read_bytes(), source.encode())
        commands = (session.directory / 'control-commands').read_text().splitlines()
        expected = ['query -user', 'query -fpgas -all', 'query -version',
                    'allocate -board b1.f0', 'load_db -release', 'load_db -db ./hw.dat',
                    'config -connector', 'query -connector -type fmc', 'query -voltage',
                    'config -clock -frequency 5529600 sys_clk', 'config -clock -commit',
                    'query -clock', 'reset -name cpu_reset -value 1', 'query -reset',
                    'download', 'query -ipinfo', 'initialize',
                    'reset -name cpu_reset -value 1', 'query -reset']
        self.assertEqual(commands[:len(expected)], expected)
        self.assertEqual(commands.count('initialize'), 1)
        self.assertEqual(commands.count('download'), 1)
        self.assertEqual(sum(line.startswith('writemem ') for line in commands), 2)
        self.assertEqual(sum(line.startswith('readmem ') for line in commands), 2)
        self.assertEqual(session.downloads, 1)
        self.assertEqual(set(self.backend.sessions), {4})

    def test_other_board_ownership_does_not_block_clock_initialization(self):
        self.init_scripts[4].write_text(self.initialization(4, clock=True))
        physical = {p: dict(owner='foreign' if p == 0 else '', booked=False, power='on') for p in range(8)}
        with patch.object(self.backend, '_physical', return_value=physical), \
                patch.object(self.backend, '_clocks', return_value={(1, 6): 11059200}):
            self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY', self.backend.states[4])
        self.assertEqual(set(self.backend.sessions), {4})
        commands = (self.backend.sessions[4].directory / 'clock-configs').read_text()
        self.assertEqual(commands.splitlines(), ['-clock -frequency 5529600 sys_clk', '-clock -commit'])

    def test_stop_during_first_port_initialization_does_not_start_next_port(self):
        entered, release = threading.Event(), threading.Event()

        def initialize(number):
            entered.set()
            release.wait(2)

        with patch.object(self.backend, '_initialize', side_effect=initialize) as initialize:
            worker = threading.Thread(target=self.backend.probe, args=('local',))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.backend.stop_initialization()
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(initialize.call_args_list, [call(4)])

    def test_stop_during_clock_query_cancels_new_session_but_allows_existing_task_to_drain(self):
        self.backend._initialize(4)
        session = self.backend.sessions[4]
        entered, release = threading.Event(), threading.Event()

        def clocks():
            entered.set()
            release.wait(2)
            return {(1, 6): 5529600}

        with patch.object(self.backend, '_clocks', side_effect=clocks), \
                patch('fpga_scheduler.backends.resident.Session') as new_session:
            worker = threading.Thread(target=self.backend.probe, args=('local',))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                # This call must return while probe still owns Resident.guard.
                self.backend.stop_initialization()
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            new_session.assert_not_called()
        self.assertEqual(set(self.backend.sessions), {4})
        self.assertFalse((self.backend.root / 'ports/5').exists())
        self.assertEqual(self.backend.states[5]['reason'], 'Service is stopping')
        result = self.run_task(self.task(1))
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIs(self.backend.sessions[4], session)

    def test_live_clock_parser_uses_physical_board_and_rejects_duplicates(self):
        output = ('Board Index Frequency(MHz) Clock Signal Name Type\n'
                  'B0 6 16.260162 NA PLL\nB1 6 5.529600 NA PLL\n'
                  'B1 7 14.745600 NA PLL\nB10 6 5.529600 NA PLL\n')
        with patch.object(self.backend, '_hardware_query', return_value=output):
            clocks = self.backend._clocks()
            self.assertEqual(set(clocks), {(0, 6), (1, 6), (1, 7), (10, 6)})
            self.assertAlmostEqual(clocks[10, 6], 5529600)
            for key, expected in {(0, 6): 16260162, (1, 6): 5529600, (1, 7): 14745600}.items():
                self.assertAlmostEqual(clocks[key], expected)
        with patch.object(self.backend, '_hardware_query', return_value=output + 'B1 6 5.529600 NA PLL\n'):
            with self.assertRaisesRegex(RuntimeError, 'duplicate'):
                self.backend._clocks()

    def test_reinitialize_refreshes_database_from_current_hardware_project(self):
        source = self.project / 'hw.dat'
        source.rmdir()
        previous, current = self.root / 'patch13', self.root / 'patch14'
        previous.mkdir()
        current.mkdir()
        source.symlink_to(previous)
        self.ready()
        old = self.backend.sessions[4]
        source.unlink()
        source.symlink_to(current)
        # Existing sessions keep their version until explicitly rebuilt.
        self.assertEqual((old.directory / 'hw.dat').resolve(), previous)
        self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY')
        self.assertIsNot(self.backend.sessions[4], old)
        self.assertIsNotNone(old.process.poll())
        self.assertEqual((self.backend.sessions[4].directory / 'hw.dat').resolve(), current)
        self.assertEqual((self.backend.sessions[5].directory / 'hw.dat').resolve(), previous)

    def test_initialization_repairs_broken_link_but_preserves_local_database_directory(self):
        directory = self.backend.root / 'ports/4'
        directory.mkdir(parents=True)
        database = directory / 'hw.dat'
        database.symlink_to(self.root / 'removed-old-database')
        self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY')
        self.assertEqual(database.resolve(), (self.project / 'hw.dat').resolve())
        self.backend.sessions.pop(4).close()
        database.unlink()
        database.mkdir()
        sentinel = database / 'keep'
        sentinel.write_text('local data')
        self.backend._initialize(4)
        self.assertNotIn(4, self.backend.sessions)
        self.assertIn('refusing to replace', self.backend.states[4]['reason'])
        self.assertEqual(sentinel.read_text(), 'local data')

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
        with self.assertRaisesRegex(StorageQuotaExceeded, 'quota') as caught:
            self.backend.prepare(payload, 'local', self.images.path(payload['id']))
        self.assertTrue(self.images.path(payload['id']).exists())
        self.assertEqual(caught.exception.details['error_code'], 'resident_storage_quota_exceeded')
        self.assertFalse(caught.exception.details['hardware_started'])
        self.assertGreater(caught.exception.details['requested_bytes'], caught.exception.details['limit_bytes'])
        self.assertFalse(self.backend.storage_reserved)

    def test_storage_wait_precedes_assignment_retries_and_preserves_fifo(self):
        self.ready()
        self.backend.max_storage = 1
        q = JobQueue(self.root / 'storage.db', self.backend, self.images, cooldown=60)
        first, second = self.task(1), self.task(2)
        try:
            q.submit(first)
            q.submit(second)
            with patch.object(self.backend, 'reserve_storage', wraps=self.backend.reserve_storage) as reserve:
                with patch.object(self.backend, 'prepare') as prepare, patch.object(self.backend, 'run') as run:
                    with self.assertLogs('fpga_scheduler.queue', level='WARNING'):
                        q.dispatch_once()
                    for _ in range(5):
                        q.dispatch_once()
                    self.assertEqual(reserve.call_count, 1)  # No wake/busy loop.
                    prepare.assert_not_called()
                    run.assert_not_called()
            task = q.get(first['id'])
            self.assertEqual((task['state'], task['position']), ('queued', 1))
            self.assertIsNone(task['started'])
            self.assertIsNone(task['fpga'])
            self.assertIsNone(task['error'])
            self.assertEqual(task['waiting_details']['error_code'], 'resident_storage_quota_exceeded')
            self.assertGreater(task['retry_at'], time.time())
            self.assertEqual(q.get(second['id'])['waiting_details']['blocked_by'], first['id'])
            self.assertFalse(q.reserved)
            self.assertEqual(q.db.execute('SELECT count(*) FROM cooldowns').fetchone()[0], 0)
            self.assertFalse((self.backend.root / 'jobs' / first['id']).exists())
            # Increasing capacity and reaching the retry time resumes the same
            # tickets; no resubmission or new IDs, and both ports can execute.
            self.backend.max_storage = 1073741824
            with q.db:
                q.db.execute('UPDATE jobs SET retry_at=? WHERE id=?', (time.time() - 1, first['id']))
            q.dispatch_once()
            self.assertEqual([q.get(t['id'])['fpga'] for t in (first, second)], [4, 5])
            for worker in list(q.workers):
                worker.join(5)
            for payload in (first, second):
                task = q.get(payload['id'])
                self.assertEqual(task['state'], 'completed', task)
                self.assertNotIn('waiting_details', task)
            self.assertFalse(self.backend.storage_reserved)
        finally:
            q.close()

    def test_materialized_files_are_not_reserved_twice_and_admission_is_atomic(self):
        tasks = [self.task(i) for i in (1, 2, 3)]
        self.backend.reserve_storage(tasks[0], self.images.path(tasks[0]['id']))
        budget = self.backend.storage_reserved[tasks[0]['id']]
        directory = self.backend.root / 'jobs' / tasks[0]['id']
        directory.mkdir(parents=True)
        for name in ('image.bin', 'readback.bin'):
            with (directory / name).open('wb') as handle:
                handle.truncate(1048576)
        self.backend.max_storage = 4 * budget  # Exactly two peak budgets.
        barrier = threading.Barrier(2)
        admitted, rejected = [], []
        def reserve(task):
            barrier.wait()
            try:
                self.backend.reserve_storage(task, self.images.path(task['id']))
                admitted.append(task['id'])
            except StorageQuotaExceeded as error:
                rejected.append(error.details)
        threads = [threading.Thread(target=reserve, args=(task,)) for task in tasks[1:]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(admitted), 1)
        self.assertEqual(len(rejected), 1)
        metrics = rejected[0]
        self.assertEqual(metrics['used_bytes'], 2 * 1048576)
        self.assertEqual(metrics['used_bytes'] + metrics['reserved_bytes'], metrics['limit_bytes'])
        self.assertEqual(metrics['requested_bytes'], budget)
        self.assertEqual(len(self.backend.storage_reserved), 2)

    def test_filesystem_shortage_has_distinct_diagnostics(self):
        task = self.task(1)
        usage = shutil.disk_usage(self.backend.root)
        with patch('fpga_scheduler.backends.resident.shutil.disk_usage', return_value=usage._replace(free=1)):
            with self.assertRaises(StorageQuotaExceeded) as caught:
                self.backend.reserve_storage(task, self.images.path(task['id']))
        details = caught.exception.details
        self.assertEqual(details['error_code'], 'filesystem_space_exhausted')
        self.assertEqual(details['disk_free_bytes'], 1)
        self.assertGreater(details['required_free_bytes'], 1)
        self.assertFalse(self.backend.storage_reserved)

    def test_repeated_safe_jobs_reclaim_load_files_at_capacity_boundary(self):
        payload = self.task(1)
        self.backend.reserve_storage(payload, self.images.path(payload['id']))
        budget = self.backend.storage_reserved[payload['id']]
        self.backend.release_prepared(payload['id'])
        self.ready()
        self.backend.max_storage = 2 * (budget + 524288)
        epoch = self.backend.sessions[4].epoch
        for i in range(1, 13):
            payload = payload if i == 1 else self.task(i)
            result = self.run_task(payload)
            self.assertEqual(result['exit_code'], 0, result)
            directory = self.backend.root / 'jobs' / payload['id']
            self.assertFalse((directory / 'image.bin').exists())
            self.assertFalse((directory / 'readback.bin').exists())
            for name in ('uart.raw.log', 'start-intent.json', 'result.json'):
                self.assertTrue((directory / name).exists())
            self.assertTrue(self.images.path(payload['id']).exists())
            self.assertEqual(result['details']['session'], epoch)
        self.assertEqual(self.backend.sessions[4].downloads, 1)
        self.assertLess(self.backend.storage_status()['used_bytes'], 524288)

    def test_upgrade_prunes_only_safe_load_copies_and_keeps_artifacts_and_recovery(self):
        self.backend.keep_load_files = True
        self.ready()
        payload = self.task(1)
        result = self.run_task(payload)
        directory = self.backend.root / 'jobs' / payload['id']
        self.assertTrue((directory / 'image.bin').exists())
        self.assertTrue((directory / 'readback.bin').exists())
        artifacts = directory / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'C.bin').write_bytes(b'output to retain')
        retained = {p: p.read_bytes() for p in directory.rglob('*')
                    if p.is_file() and p.name not in ('image.bin', 'readback.bin')}
        old = time.time() - 8 * 86400
        os.utime(directory, (old, old))
        protected = []
        for i in (2, 3, 4):
            task = self.task(i)
            work = self.backend.root / 'jobs' / task['id']
            work.mkdir(parents=True)
            (work / 'image.bin').write_bytes(b'keep')
            (work / 'readback.bin').write_bytes(b'keep')
            if i != 2:  # i=2 has no reliable terminal result.
                value = dict(result, details=dict(reset_confirmed=i == 4, ddr_readback_matches=i == 3))
                atomic_json(work / 'result.json', dict(id=task['id'], sha256=task['sha256'], result=value))
            protected.extend((work / 'image.bin', work / 'readback.bin'))
        self.backend.close()
        self.backend = Resident((self.node,), self.root / 'state', hardware_project=self.project,
                                lock_dir=self.root / 'locks', init_scripts=self.init_scripts)
        self.assertFalse((directory / 'image.bin').exists())
        self.assertFalse((directory / 'readback.bin').exists())
        self.assertAlmostEqual(directory.stat().st_mtime, old, places=5)
        for path, data in retained.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertTrue(all(p.read_bytes() == b'keep' for p in protected))
        recovered = self.backend.recover(dict(id=payload['id'], payload=json.dumps(payload), node='local', fpga=4))
        self.assertEqual(recovered, result)


@unittest.skipUnless(shutil.which('tclsh'), 'tclsh required for resident control protocol')
class RegisteredInitializationTests(unittest.TestCase):
    """Registration is passive; only the node whitelist may touch hardware."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        (self.project / 'hw.dat').mkdir(parents=True)
        scripts = self.root / 'deployment/init'
        scripts.mkdir(parents=True)
        self.init_scripts = {}
        for number in range(4, 8):
            path = scripts / f'init{number}.tcl'
            path.write_text(ResidentTests.initialization(number, explicit=number == 6))
            self.init_scripts[number] = path
        import shlex
        self.wrapper = self.root / 'uvhs'
        self.wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(shutil.which('tclsh'))
                                + ' ' + shlex.quote(str(FIXTURE.resolve())) + ' "$@"\n')
        self.wrapper.chmod(0o755)
        # Disabled FPGA4/5 deliberately have no UART devices at all.
        for number in (6, 7):
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            (self.root / f'uart{number}').symlink_to(os.ttyname(slave))
            os.close(slave)

    def resident(self, number, *, scripts=None):
        node = Node('local', (number,), uvhs_shell=str(self.wrapper),
                    uart_prefix=str(self.root / 'uart'))
        backend = Resident((node,), self.root / 'state', hardware_project=self.project,
                           lock_dir=self.root / 'locks',
                           init_scripts=self.init_scripts if scripts is None else scripts,
                           startup_timeout=2, command_timeout=1, uart_quiet_seconds=.03)
        self.addCleanup(backend.close)
        return backend

    def test_full_registration_only_initializes_enabled_fpga(self):
        from fpga_scheduler.remote.init_script import load_init_script
        from fpga_scheduler.remote.uvhs_session import Session
        real_open = os.open
        with patch('fpga_scheduler.backends.resident.load_init_script', wraps=load_init_script) as load_script, \
             patch('fpga_scheduler.backends.resident.Session', wraps=Session) as sessions, \
             patch('fpga_scheduler.remote.uvhs_session.os.open', wraps=real_open) as opens, \
             patch.dict(os.environ, FAKE_FPGA_PORTS='4 5 6 7'):
            backend = self.resident(6)
            with patch.object(backend, '_uart_busy', wraps=backend._uart_busy) as uart:
                ports = backend.probe('local')
            self.assertEqual([call.args[0] for call in uart.call_args_list], [6])
        self.assertEqual(backend.init_scripts, self.init_scripts)
        self.assertEqual(set(backend.states), {6})
        self.assertEqual(set(backend.sessions), {6})
        self.assertEqual([(port['number'], port['available']) for port in ports], [(6, True)])
        self.assertEqual([call.args[1] for call in load_script.call_args_list], [6, 6])
        self.assertEqual([call.args[1] for call in sessions.call_args_list], [6])
        uart_paths = [str(call.args[0]) for call in opens.call_args_list
                      if str(call.args[0]).startswith(str(self.root / 'uart'))]
        self.assertEqual(uart_paths, [str(self.root / 'uart6')])
        self.assertEqual({path.name for path in (backend.root / 'ports').iterdir()}, {'6'})
        session = backend.sessions[6]
        commands = (session.directory / 'control-commands').read_text().splitlines()
        self.assertEqual(commands[:3], ['allocate -board b1.f2', 'load_db -release',
                                       'load_db -db ./hw.dat'])
        self.assertEqual(commands.count('download'), 1)
        self.assertEqual(commands.count('initialize'), 1)
        self.assertEqual((session.directory / 'init.tcl').read_bytes(), self.init_scripts[6].read_bytes())

    def test_disabled_content_and_targets_are_not_read_or_executed_until_enabled(self):
        self.init_scripts[4].write_text('error disabled_script_must_never_run\nexit\n')
        self.init_scripts[5].write_text(ResidentTests.initialization(7))
        self.init_scripts[7].write_bytes(b'\xff invalid UTF-8 Tcl\n')
        real_read_bytes = Path.read_bytes

        def enabled_only(path):
            self.assertNotIn(path, [self.init_scripts[p] for p in (4, 5, 7)],
                             'Disabled initializer content was read')
            return real_read_bytes(path)

        with patch.object(Path, 'read_bytes', enabled_only):
            backend = self.resident(6)
            ports = backend.probe('local')
        self.assertEqual([(port['number'], port['available']) for port in ports], [(6, True)])
        backend.close()
        for number, message in ((4, 'unsupported initialization command'),
                                (5, 'must match FPGA5'), (7, 'readable UTF-8')):
            with self.subTest(number=number), \
                 patch('fpga_scheduler.backends.resident.Session') as sessions, \
                 patch('fpga_scheduler.backends.resident.retire_record') as retire:
                with self.assertRaisesRegex(ValueError, message):
                    self.resident(number)
                sessions.assert_not_called()
                retire.assert_not_called()
                self.assertFalse((backend.root / 'ports' / str(number)).exists())

    def test_missing_enabled_mapping_is_rejected_before_session_retirement(self):
        scripts = {number: path for number, path in self.init_scripts.items() if number != 6}
        with patch('fpga_scheduler.backends.resident.Session') as sessions, \
             patch('fpga_scheduler.backends.resident.retire_record') as retire:
            with self.assertRaisesRegex(ValueError, 'init_scripts.*6'):
                self.resident(6, scripts=scripts)
        sessions.assert_not_called()
        retire.assert_not_called()
        self.assertFalse((self.root / 'state/resident/ports').exists())

    def test_clean_stop_switches_six_to_seven_using_unchanged_registration(self):
        originals = {number: path.read_bytes() for number, path in self.init_scripts.items()}
        first = self.resident(6)
        self.assertTrue(first.probe('local')[0]['available'])
        old = first.sessions[6]
        first.close()
        self.assertIsNotNone(old.process.poll())
        self.assertTrue(json.loads(old.record.read_text())['closed_cleanly'])

        second = self.resident(7)
        ports = second.probe('local')
        self.assertEqual([(port['number'], port['available']) for port in ports], [(7, True)])
        self.assertEqual(second.init_scripts, first.init_scripts)
        self.assertEqual(set(second.states), {7})
        self.assertEqual(set(second.sessions), {7})
        current = second.sessions[7]
        self.assertNotEqual(current.epoch, old.epoch)
        commands = (current.directory / 'control-commands').read_text().splitlines()
        self.assertEqual(commands[:2], ['load_db -release', 'load_db -db ./hw.dat -to b1.f3'])
        self.assertFalse(any(line.startswith('allocate ') for line in commands))
        self.assertEqual(commands.count('download'), 1)
        self.assertEqual(commands.count('initialize'), 1)
        self.assertEqual((old.directory / 'init.tcl').read_bytes(), originals[6])
        self.assertEqual((current.directory / 'init.tcl').read_bytes(), originals[7])
        self.assertEqual({number: path.read_bytes() for number, path in self.init_scripts.items()}, originals)
        self.assertEqual({path.name for path in (second.root / 'ports').iterdir()}, {'6', '7'})


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
            root = Path(tmp)
            (root / 'init').mkdir()
            for port in (4, 5):
                (root / 'init' / f'init{port}.tcl').write_text(ResidentTests.initialization(port))
            path = root / 'config.toml'
            scripts = '[resident.init_scripts]\n"4"="init/init4.tcl"\n"5"="init/init5.tcl"\n'
            base = ('backend="bosc-resident"\nstate_dir="state"\ntoken_file="token"\n'
                    '[resident]\nhardware_project="project"\nlock_dir="locks"\n'
                    + scripts + '[[nodes]]\nid="local"\nports=[4,5]\nuvhs_shell="/vendor/uvhs"\n')
            path.write_text(base)
            self.assertEqual(load(path).nodes[0].ports, (4,5))
            path.write_text(base.replace('lock_dir="locks"', 'lock_dir="locks"\nshared_clock_index=0'))
            self.assertEqual(load(path).resident['shared_clock_index'], 0)
            for legacy in ('true', 'false', '1'):
                path.write_text(base.replace('lock_dir="locks"', 'lock_dir="locks"\nexplicit_allocation=' + legacy))
                with self.assertRaisesRegex(ValueError, 'explicit_allocation.*init_scripts'):
                    load(path)
            path.write_text(base.replace('lock_dir="locks"', 'lock_dir="locks"\nkeep_load_files=true\nstorage_retry_seconds=0.5'))
            self.assertTrue(load(path).resident['keep_load_files'])
            self.assertEqual(load(path).resident['storage_retry_seconds'], .5)
            for bad in (base + 'ssh_host="remote"\n', base.replace('ports=[4,5]', 'ports=[4,4]'),
                        base.replace('hardware_project="project"', 'profile="dma"\nhardware_project="project"'),
                        base.replace('lock_dir="locks"', 'lock_dir="locks"\nrecovery_attempts=1.5'),
                        base.replace('lock_dir="locks"', 'lock_dir="locks"\nkeep_load_files=1'),
                        *(base.replace('lock_dir="locks"', 'lock_dir="locks"\nstorage_retry_seconds=' + value)
                          for value in ('0', '-1', 'true')),
                        *(base.replace('lock_dir="locks"', 'lock_dir="locks"\nshared_clock_index=' + value)
                          for value in ('-1', '24', 'true', '6.0'))):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    load(path)
            path.write_text(base.replace(scripts, ''))
            with self.assertRaisesRegex(ValueError, 'init_scripts.*required'):
                load(path)
            for invalid_scripts in (
                scripts.replace('"5"="init/init5.tcl"\n', ''),
                scripts.replace('"4"=', '"04"='),
                scripts.replace('init/init4.tcl', 'missing.tcl'),
                scripts.replace('init/init4.tcl', 'init'),
                scripts.replace('init/init4.tcl', '$INIT_SCRIPT'),
                scripts.replace('"init/init4.tcl"', '4'),
                scripts.replace('init/init4.tcl', ''),
            ):
                with self.subTest(invalid_scripts=invalid_scripts):
                    path.write_text(base.replace(scripts, invalid_scripts))
                    with self.assertRaisesRegex(ValueError, 'init_scripts'):
                        load(path)

    def test_initialization_paths_resolve_relative_to_config_and_preserve_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deployment = root / 'deployment'
            deployment.mkdir()
            relative = deployment / 'init4.tcl'
            absolute = root / 'init5.tcl'
            for port, script in ((4, relative), (5, absolute)):
                script.write_text(ResidentTests.initialization(port))
            config = deployment / 'config.toml'
            config.write_text('backend="bosc-resident"\nstate_dir="state"\ntoken_file="token"\n'
                              '[resident]\nhardware_project="project"\nlock_dir="locks"\n'
                              '[resident.init_scripts]\n"4"="./init4.tcl"\n'
                              f'"5"="{absolute}"\n'
                              '[[nodes]]\nid="local"\nports=[4,5]\nuvhs_shell="/vendor/uvhs"\n')
            loaded = load(config)
            self.assertEqual(loaded.resident['init_scripts'], {4: str(relative), 5: str(absolute)})
            self.assertEqual(loaded.resident['hardware_project'], str(deployment / 'project'))


class InitializationScriptTests(unittest.TestCase):
    def test_contract_rejects_application_commands_and_incompatible_targets_or_clocks(self):
        from fpga_scheduler.remote.init_script import load_init_script
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'init4.tcl'
            valid = ResidentTests.initialization(4, clock=True)
            invalid = {
                'application write': valid + 'writemem -file image.bin i_ddr\n',
                'application read': valid + 'readmem -file result.bin i_ddr\n',
                'application source': valid + 'source ./user_script/hw_run/write_image.tcl\n',
                'CPU release': valid + 'reset -name cpu_reset -value 0\n',
                'shell close': valid + 'exit\n',
                'wrong FPGA': valid.replace('b1.f0', 'b1.f1'),
                'wrong clock': valid.replace('5529600', '11059200'),
                'wrong clock signal': valid.replace('sys_clk', 'another_clk'),
                'missing bitstream download': valid.replace('download\n', ''),
                'missing initialize': valid.replace('initialize\n', ''),
                'no reset assertion': valid.replace('reset -name cpu_reset -value 1\n', ''),
                'dynamic hidden command': valid + 'eval {reset -name cpu_reset -value 0}\n',
                'command chaining': valid + 'query -reset; reset -name cpu_reset -value 0\n',
            }
            for case, text in invalid.items():
                with self.subTest(case=case):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        load_init_script(path, 4)

    def test_clock_environment_override_is_validated_without_changing_script(self):
        from fpga_scheduler.remote.init_script import load_init_script
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'init4.tcl'
            text = ResidentTests.initialization(4, clock=True).replace(
                'config -clock -frequency 5529600 sys_clk',
                'set sys_clk_hz 5529600\n'
                'if {[info exists ::env(UV_RUN_SYS_CLK_HZ)]} {\n'
                '    set sys_clk_hz $::env(UV_RUN_SYS_CLK_HZ)\n}\n'
                'config -clock -frequency $sys_clk_hz sys_clk')
            path.write_text(text)
            with patch.dict(os.environ, UV_RUN_SYS_CLK_HZ='5529600'):
                loaded = load_init_script(path, 4)
            self.assertEqual(loaded.content, text.encode())
            self.assertTrue(loaded.configures_clock)
            with patch.dict(os.environ, UV_RUN_SYS_CLK_HZ='14745600'):
                with self.assertRaises(ValueError):
                    load_init_script(path, 4)
            self.assertEqual(path.read_text(), text)
