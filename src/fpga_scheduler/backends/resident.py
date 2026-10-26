"""Local, allowlisted NH-only resident workers; never SSH back to the host."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from ..models import BUSY_EXIT, BUSY_TOKEN
from ..remote.prepare import bind_port
from ..remote.runner import platform_error
from ..remote.runtime import parse_fpgas
from ..remote.uvhs_session import Session, atomic_json, fingerprint, retire_record


class Resident:
    def __init__(self, nodes, state_dir, *, hardware_project, lock_dir,
                 recovery_attempts=3, recovery_backoff=10, max_storage_bytes=1073741824, **options):
        if len(nodes) != 1:
            raise ValueError('Resident backend manages exactly one local node')
        self.nodes, self.node = nodes, nodes[0]
        self.root = Path(state_dir) / 'resident'
        self.root.mkdir(parents=True, exist_ok=True)
        self.project, self.locks = Path(hardware_project), Path(lock_dir)
        self.locks.mkdir(parents=True, exist_ok=True)
        self.options = options
        self.recovery_attempts, self.backoff = recovery_attempts, recovery_backoff
        self.max_storage = max_storage_bytes
        self.guard = threading.RLock()
        self.sessions = {}
        self.states = {p: dict(state='RECOVERING', attempts=0, next_retry=0, reason='Not initialized') for p in self.node.ports}
        self.inputs = {}
        self.storage_reserved = {}
        self.stopped = False
        self.bootstraps = {p: self._bootstrap(p) for p in self.node.ports}
        # No dispatcher exists yet. Retire recorded old executors first, including
        # inherited port locks. Dirty hardware must still pass full initialization.
        for path in (self.root / 'probes').glob('*.json'):
            retire_record(path)
            path.unlink()
        for path in (self.root / 'ports').glob('*/session.json'):
            if int(path.parent.name) not in self.node.ports:
                record = json.loads(path.read_text())
                if not record.get('closed_cleanly'):
                    raise RuntimeError('Stop with the previous whitelist before changing ports')
            else:
                retire_record(path)

    def _bootstrap(self, number):
        source = bind_port((self.project / 'user_script/hw_run' / f'hw_run{number}.tcl').read_text(), number)
        active = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#'))
        anchor = 'source ./user_script/hw_run/write_image.tcl'
        if active.count(anchor) != 1:
            raise ValueError('Unsupported hardware script: expected one write_image boundary')
        prefix = active.split(anchor)[0]
        if (len(re.findall(r'^download\s*$', prefix, re.M)) != 1 or
                len(re.findall(r'^initialize\s*$', prefix, re.M)) != 1 or
                re.search(r'^\s*(source|exec|writemem|readmem)\b', prefix, re.M) or
                'reset -name cpu_reset -value 1' not in prefix or
                'reset -name cpu_reset -value 0' in prefix or '5529600' not in prefix):
            raise ValueError('Unsupported resident NH hardware bootstrap')
        # Administrator-controlled project, copied into an isolated workdir.
        return prefix + '\nreset -name cpu_reset -value 1\nquery -reset\n'

    def _lock(self, name):
        handle = (self.locks / name).open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            handle.close()
            return None

    def _physical(self):
        with tempfile.TemporaryDirectory(prefix='inventory-', dir=self.root) as work:
            process = subprocess.Popen([self.node.uvhs_shell, '-t', 'runtime', '-d', 'V1', '-workdir', work],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True)
            record = self.root / 'probes' / (uuid.uuid4().hex + '.json')
            try:
                atomic_json(record, dict(pid=process.pid, start_ticks=fingerprint(process.pid)))
                output, _ = process.communicate('query -fpgas -all\nexit\n', timeout=45)
            finally:
                if record.exists():
                    retire_record(record)
                    record.unlink()
                process.wait()
            if process.returncode or platform_error(output.encode()):
                raise RuntimeError('UVHS inventory query failed')
        return parse_fpgas(output)

    def _uart_busy(self, number):
        device = Path(self.node.uart_prefix + str(number))
        if not device.exists():
            return 'UART missing'
        actual = str(device.resolve())
        processes = subprocess.check_output(['ps', '-eo', 'pid=,args='], text=True)
        pattern = r'(?<![\w/])(?:' + re.escape(str(device)) + '|' + re.escape(actual) + r')(?!\w)'
        if re.search(pattern, processes):
            return 'UART reader active'
        for proc in Path('/proc').glob('[0-9]*'):
            if proc.name == str(os.getpid()):
                continue
            try:
                for fd in (proc / 'fd').iterdir():
                    try:
                        if os.readlink(fd) == actual:
                            return 'UART open'
                    except OSError:
                        pass
            except OSError:
                pass
        return None

    def _initialize(self, number):
        state = self.states[number]
        board = number // 4
        if any(p // 4 == board and s['state'] == 'BUSY' for p, s in self.states.items()):
            state['reason'] = 'Waiting for active tasks before board initialization'
            return
        board_lock = self._lock(f'board{board}.lock')
        if board_lock is None:
            state['reason'] = 'Board maintenance active'
            return
        handle = None
        try:
            physical = self._physical()
            for p, info in physical.items():
                if p // 4 == board and (info['owner'] or info['booked']) and p not in self.sessions:
                    state['reason'] = f'Board occupied by unmanaged FPGA{p}; no shared operations allowed'
                    return
            if number in self.sessions:
                self.sessions.pop(number).close()
                # Confirm unload retired ownership before any full rebuild.
                physical = self._physical()
                if physical[number]['owner'] or physical[number]['booked']:
                    state['reason'] = 'Previous session ownership has not retired'
                    return
            info = physical[number]
            if info['power'] != 'on':
                state['reason'] = 'FPGA power off'
                return
            reason = self._uart_busy(number)
            if reason:
                state['reason'] = reason
                return
            handle = self._lock(f'fpga{number}.lock')
            if handle is None:
                state['reason'] = 'Port lock held by another executor'
                return
            directory = self.root / 'ports' / str(number)
            directory.mkdir(parents=True, exist_ok=True)
            database = directory / 'hw.dat'
            if not database.exists():
                database.symlink_to((self.project / 'hw.dat').resolve())
            session = Session(self.node, number, directory, handle, **self.options)
            handle = None  # Ownership transferred, including failures.
            try:
                session.start(self.bootstraps[number])
            except BaseException:
                session.close()
                raise
            self.sessions[number] = session
            state.update(state='READY', reason='', attempts=0)
        except Exception as error:
            state['attempts'] += 1
            state.update(state='QUARANTINED' if state['attempts'] >= self.recovery_attempts else 'RECOVERING',
                         reason=f'Initialization failed: {error}',
                         next_retry=time.monotonic() + self.backoff * 2 ** (state['attempts'] - 1))
        finally:
            if handle:
                handle.close()
            board_lock.close()

    def probe(self, node):
        if node != self.node.id:
            raise ValueError('Unknown local node')
        with self.guard:
            if not self.stopped:
                for number, state in self.states.items():
                    if state['state'] == 'RECOVERING' and time.monotonic() >= state['next_retry']:
                        self._initialize(number)
                    session = self.sessions.get(number)
                    if session and state['state'] == 'READY' and session.process.poll() is not None:
                        state.update(state='RECOVERING', reason='Resident UVHS exited', next_retry=time.monotonic() + self.backoff)
            return [dict(number=p, available=s['state'] == 'READY' and not self.stopped,
                         state=s['state'], reasons=[] if s['state'] == 'READY' else [s['reason'] or s['state']],
                         session=self.sessions[p].epoch if p in self.sessions else None,
                         downloads=self.sessions[p].downloads if p in self.sessions else 0)
                    for p, s in self.states.items()]

    def _check_storage(self, reserve=0):
        used = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file() and not p.is_symlink())
        reserve += sum(self.storage_reserved.values())
        if used + reserve > self.max_storage // 2 or shutil.disk_usage(self.root).free < reserve + 16777216:
            raise OSError('Resident storage quota exceeded; retained active inputs are protected')

    def prepare(self, payload, node, image):
        if node != self.node.id:
            raise ValueError('Unknown node')
        reserve = (max(1048576, (image.stat().st_size + 1048575) // 1048576 * 1048576) * 2 +
                   self.options.get('max_job_log_bytes', 1048576) * 2)
        if hashlib.sha256(image.read_bytes()).hexdigest() != payload['sha256']:
            raise ValueError('Input checksum mismatch')
        with self.guard:
            self._check_storage(reserve)
            self.storage_reserved[payload['id']] = reserve
            self.inputs[payload['id']] = image

    def run(self, payload, node, port):
        with self.guard:
            if node != self.node.id or port not in self.states:
                raise ValueError('Port outside whitelist')
            image = self.inputs.pop(payload['id'])
            if self.stopped or self.states[port]['state'] != 'READY':
                self.storage_reserved.pop(payload['id'], None)
                return dict(exit_code=BUSY_EXIT, output=BUSY_TOKEN, seconds=0)
            session = self.sessions[port]
            self.states[port].update(state='BUSY', reason='Task executing')
        try:
            result, healthy = session.execute(payload, image, self.root / 'jobs' / payload['id'])
        except BaseException:
            with self.guard:
                self.storage_reserved.pop(payload['id'], None)
                self.states[port].update(state='QUARANTINED', reason='Result publication failed; inspect hardware')
            raise
        with self.guard:
            self.storage_reserved.pop(payload['id'], None)
            if healthy:
                self.states[port].update(state='READY', reason='')
            else:
                # Full rebuild waits until the other local tasks on this board
                # finish. A reset alone does not establish peripheral safety.
                self.states[port].update(state='RECOVERING', reason='Task fault; full reinitialization required',
                                         next_retry=time.monotonic() + self.backoff)
        return result

    def recover(self, job):
        path = self.root / 'jobs' / job['id'] / 'result.json'
        if not path.is_file():
            return None
        try:
            saved = json.loads(path.read_text())
            payload = json.loads(job['payload'])
            result = saved['result']
            if (saved['id'] != job['id'] or saved['sha256'] != payload['sha256'] or
                    result['node'] != job['node'] or result['details']['fpga'] != job['fpga'] or
                    type(result['exit_code']) is not int or not isinstance(result['output'], str)):
                return None
            return result
        except (KeyError, ValueError, TypeError):
            return None

    def cleanup(self, before):
        # Queue retention runs first; image directories are the authoritative
        # protection set for queued, running and retained terminal jobs.
        images = self.root.parent / 'images'
        for directory in (self.root / 'jobs').glob('*'):
            if (re.fullmatch('[0-9a-f]{32}', directory.name) and not (images / directory.name).exists()
                    and directory.stat().st_mtime < before):
                shutil.rmtree(directory)

    def close(self):
        with self.guard:
            self.stopped = True
            for session in self.sessions.values():
                session.close()
            self.sessions.clear()
