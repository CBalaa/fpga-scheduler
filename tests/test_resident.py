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

    def test_database_allocation_mode_does_not_allocate(self):
        with patch.dict(os.environ, {'FAKE_REJECT_ALLOCATION': '1'}):
            self.ready()
            result = self.run_task(self.task(1))
        self.assertEqual(result['exit_code'], 0, result)

    def test_explicit_allocation_mode_allocates_after_release(self):
        self.backend.explicit_allocation = True
        self.backend.bootstraps = {p: self.backend._bootstrap(p) for p in self.backend.node.ports}
        with patch.dict(os.environ, {'FAKE_REQUIRE_ALLOCATION': '1'}):
            self.ready()
            result = self.run_task(self.task(1))
        self.assertEqual(result['exit_code'], 0, result)

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
        for clocks in ({(1, 6): 14745600}, {(0, 6): 5529600}, {}):
            with self.subTest(clocks=clocks), patch.object(self.backend, '_physical', return_value=physical), \
                    patch.object(self.backend, '_clocks', return_value=clocks):
                self.backend._initialize(4)
                self.assertFalse(self.backend.sessions)
                self.assertEqual(self.backend.states[4]['state'], 'RECOVERING')
                self.assertEqual(self.backend.states[4]['attempts'], 0)
                self.assertIn('expected 5529600 Hz', self.backend.states[4]['reason'])
                self.assertIn('FPGA7 active', self.backend.states[4]['reason'])
                self.assertFalse((self.backend.root / 'ports/4/downloads').exists())

    def test_empty_board_can_configure_clock_and_environment_cannot_override_profile(self):
        path = self.project / 'user_script/hw_run/hw_run4.tcl'
        path.write_text(path.read_text().replace(
            'config -clock -frequency 5529600 sys_clk',
            'set sys_clk_hz 5529600\n'
            'if {[info exists ::env(UV_RUN_SYS_CLK_HZ)]} {set sys_clk_hz $::env(UV_RUN_SYS_CLK_HZ)}\n'
            'config -clock -frequency $sys_clk_hz sys_clk'))
        self.backend.bootstraps[4] = self.backend._bootstrap(4)
        with patch.object(self.backend, '_clocks', return_value={(1, 6): 16260162}), \
                patch.dict(os.environ, UV_RUN_SYS_CLK_HZ='14745600'):
            self.backend._initialize(4)
        self.assertEqual(self.backend.states[4]['state'], 'READY')
        commands = (self.backend.sessions[4].directory / 'clock-configs').read_text()
        self.assertIn('-frequency 5529600 sys_clk', commands)
        self.assertIn('-commit', commands)
        self.assertNotIn('14745600', commands)

    def test_live_clock_parser_uses_physical_board_and_rejects_duplicates(self):
        output = ('Board Index Frequency(MHz) Clock Signal Name Type\n'
                  'B0 6 16.260162 NA PLL\nB1 6 5.529600 NA PLL\n'
                  'B1 7 14.745600 NA PLL\n')
        with patch.object(self.backend, '_hardware_query', return_value=output):
            clocks = self.backend._clocks()
            self.assertEqual(set(clocks), {(0, 6), (1, 6), (1, 7)})
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
                                lock_dir=self.root / 'locks')
        self.assertFalse((directory / 'image.bin').exists())
        self.assertFalse((directory / 'readback.bin').exists())
        self.assertAlmostEqual(directory.stat().st_mtime, old, places=5)
        for path, data in retained.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertTrue(all(p.read_bytes() == b'keep' for p in protected))
        recovered = self.backend.recover(dict(id=payload['id'], payload=json.dumps(payload), node='local', fpga=4))
        self.assertEqual(recovered, result)


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
            path.write_text(base.replace('lock_dir="locks"', 'lock_dir="locks"\nshared_clock_index=0'))
            self.assertEqual(load(path).resident['shared_clock_index'], 0)
            path.write_text(base.replace('lock_dir="locks"', 'lock_dir="locks"\nexplicit_allocation=true'))
            self.assertTrue(load(path).resident['explicit_allocation'])
            path.write_text(base.replace('lock_dir="locks"', 'lock_dir="locks"\nkeep_load_files=true\nstorage_retry_seconds=0.5'))
            self.assertTrue(load(path).resident['keep_load_files'])
            self.assertEqual(load(path).resident['storage_retry_seconds'], .5)
            for bad in (base + 'ssh_host="remote"\n', base.replace('ports=[4,5]', 'ports=[4,4]'),
                        base.replace('hardware_project="project"', 'profile="dma"\nhardware_project="project"'),
                        base.replace('lock_dir="locks"', 'lock_dir="locks"\nrecovery_attempts=1.5'),
                        base.replace('lock_dir="locks"', 'lock_dir="locks"\nexplicit_allocation=1'),
                        base.replace('lock_dir="locks"', 'lock_dir="locks"\nkeep_load_files=1'),
                        *(base.replace('lock_dir="locks"', 'lock_dir="locks"\nstorage_retry_seconds=' + value)
                          for value in ('0', '-1', 'true')),
                        *(base.replace('lock_dir="locks"', 'lock_dir="locks"\nshared_clock_index=' + value)
                          for value in ('-1', '24', 'true', '6.0'))):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    load(path)
