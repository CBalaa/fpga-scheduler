"""Linux offline board: production Resident/Session with Tcl UVHS and PTY UART.

No vendor binaries, SSH, /dev/FPGA devices, or real hardware projects are used.
Scenario images describe UART and fault behavior; no CPU instructions execute.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import pty
import shlex
import shutil
import threading
import time

from fpga_scheduler.backends.resident import Resident
from fpga_scheduler.remote.uvhs_session import atomic_json, tcl
from .scenarios import STATE_IDENTITY, check_state_directory, read_image


class ResidentSimulator(Resident):
    def __init__(self, nodes, state_dir, *, offline_nodes=(), busy_nodes=(),
                 failure_nodes=(), disconnect_nodes=(), transfer_failure_nodes=(), **options):
        if len(nodes) != 1 or any(p not in range(8) for p in nodes[0].ports):
            raise ValueError('resident-sim requires one virtual node with ports in 0..7')
        executable = shutil.which('tclsh')
        if not executable:
            raise ValueError('resident-sim requires tclsh on PATH')
        self.virtual_ports = nodes[0].ports
        self.virtual_root = Path(state_dir).resolve() / 'sim'
        check_state_directory(state_dir)
        self.virtual_root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.virtual_root / 'board.json', STATE_IDENTITY)
        self.offline = set(offline_nodes)
        self.busy = set(busy_nodes)
        self.failures = set(failure_nodes)
        self.disconnects = set(disconnect_nodes)
        self.transfer_failures = set(transfer_failure_nodes)
        self.masters = {}
        self.emissions = {}
        self.emitter_stop = threading.Event()
        self.emitter = None
        project = self.virtual_root / 'project'
        (project / 'user_script/hw_run').mkdir(parents=True, exist_ok=True)
        (project / 'hw.dat').mkdir(exist_ok=True)
        prefix = self.virtual_root / 'uart'
        for port in nodes[0].ports:
            (project / f'user_script/hw_run/hw_run{port}.tcl').write_text(
                'load_db -release\nload_db -db ./hw.dat\nconfig -clock -frequency 5529600 sys_clk\n'
                'config -clock -commit\nreset -name cpu_reset -value 1\ndownload\ninitialize\n'
                'source ./user_script/hw_run/write_image.tcl\n')
        wrapper = self.virtual_root / 'uvhs-sim'
        fixture = Path(__file__).with_name('uvhs.tcl')
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(executable) + ' ' + shlex.quote(str(fixture)) + ' "$@"\n')
        wrapper.chmod(0o700)
        node = replace(nodes[0], uvhs_shell=str(wrapper), uart_prefix=str(prefix), ssh_host='', relay_command=())
        try:
            # Retire previous owned executors before replacing any PTY links.
            super().__init__((node,), state_dir, hardware_project=project,
                             lock_dir=self.virtual_root / 'locks', **options)
            for port in node.ports:
                (self.root / 'ports' / str(port) / 'running').unlink(missing_ok=True)
                master, slave = pty.openpty()
                self.masters[port] = master
                os.set_blocking(master, False)
                link = Path(str(prefix) + str(port))
                link.unlink(missing_ok=True)
                link.symlink_to(os.ttyname(slave))
                os.close(slave)
            self.emitter = threading.Thread(target=self._emit, name='fpga-sim-uart', daemon=True)
            self.emitter.start()
        except BaseException:
            self._close_virtual()
            raise

    def probe(self, node):
        if node in self.offline:
            raise OSError('Simulated board offline')
        if node in self.busy:
            return [dict(number=p, available=False, state='UNAVAILABLE', simulated=True,
                         reasons=['Simulated external owner']) for p in self.node.ports]
        return [dict(p, simulated=True) for p in super().probe(node)]

    def prepare(self, payload, node, image, *, cancellation=None):
        if node in self.transfer_failures:
            raise OSError('Simulated image transfer failure')
        scenario = read_image(image)
        if node in self.failures:
            scenario['exit_code'] = 1
        if node in self.disconnects:
            scenario['mode'] = 'disconnect'
        super().prepare(payload, node, image, cancellation=cancellation)
        try:
            directory = self.root / 'jobs' / payload['id']
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'sim.json').write_text(json.dumps(scenario))
            (directory / 'sim.tcl').write_text('set sim_mode ' + tcl(scenario['mode']) + '\n'
                                             'set sim_duration ' + str(scenario['duration']) + '\n')
        except BaseException:
            self.release_prepared(payload['id'])
            raise

    def run(self, payload, node, port, *, cancellation=None):
        result = super().run(payload, node, port, cancellation=cancellation)
        result.setdefault('details', {})['simulated'] = True
        return result

    def recover(self, job):
        result = super().recover(job)
        if result is not None:
            result['details']['simulated'] = True
        return result

    @staticmethod
    def _pack(data):
        start = b'@@FPGA_START {"version":1}\n'
        body = (data['label'] + '\n' + data['output']).encode()
        if not body.endswith(b'\n'):
            body += b'\n'
        if data['mode'] == 'hang':
            return start + body + b'ordinary PASS (no completion record)\n', b''
        record = dict(version=1, status='exited', app_exit_code=data['exit_code'])
        if data['mode'] == 'trap':
            record = dict(version=1, status='trap', mcause='0x2', mepc='0x80000000', mtval='0x0')
        terminal = ('@@FPGA_RESULT ' + json.dumps(record) + '\n').encode()
        if data['mode'] == 'duplicate':
            terminal += terminal
        elif data['mode'] == 'malformed':
            terminal = b'@@FPGA_RESULT {invalid}\n'
        elif data['mode'] == 'missing-start':
            start = b''
        return start + body, terminal

    def _emit(self):
        while not self.emitter_stop.wait(.002):
            for port, master in tuple(self.masters.items()):
                running = self.root / 'ports' / str(port) / 'running'
                try:
                    image = running.read_text().strip()
                except FileNotFoundError:
                    self.emissions.pop(port, None)
                    continue
                state = self.emissions.get(port)
                if state is None or state['image'] != image:
                    try:
                        data = json.loads((Path(image).parent / 'sim.json').read_text())
                    except (OSError, ValueError):
                        continue
                    initial, terminal = self._pack(data)
                    state = dict(image=image, data=data, pending=initial, terminal=terminal,
                                 next=time.monotonic(), finish=time.monotonic() + data['duration'])
                    self.emissions[port] = state
                now = time.monotonic()
                if now < state['next']:
                    continue
                if not state['pending'] and now >= state['finish']:
                    if state['data']['mode'] == 'disconnect':
                        os.close(master)
                        self.masters.pop(port, None)
                        continue
                    state['pending'], state['terminal'] = state['terminal'], b''
                if state['pending']:
                    chunk = state['pending'][:state['data']['fragment_bytes']]
                    try:
                        count = os.write(master, chunk)
                    except (BlockingIOError, OSError):
                        continue
                    state['pending'] = state['pending'][count:]
                    state['next'] = now + .001

    def _close_virtual(self):
        self.emitter_stop.set()
        if self.emitter:
            self.emitter.join(timeout=5)
        for master in self.masters.values():
            os.close(master)
        self.masters.clear()
        for port in self.virtual_ports:
            (self.virtual_root / f'uart{port}').unlink(missing_ok=True)

    def close(self):
        try:
            super().close()
        finally:
            self._close_virtual()
