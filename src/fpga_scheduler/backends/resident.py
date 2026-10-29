"""Local, allowlisted NH-only resident workers; never SSH back to the host."""
import fcntl
import hashlib
import json
import logging
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
from ..remote.init_script import load_init_script
from ..remote.runner import platform_error
from ..remote.runtime import parse_fpgas
from ..remote.uvhs_session import Session, atomic_json, fingerprint, retire_record
from ..artifacts import validate_artifacts
from ..storage import StorageQuotaExceeded


NH_CLOCK_HZ = 5529600
LOG = logging.getLogger(__name__)


class Resident:
    supports_cancellation = True
    background_inventory = True
    def __init__(self, nodes, state_dir, *, hardware_project, lock_dir,
                 recovery_attempts=3, recovery_backoff=10, max_storage_bytes=1073741824,
                 shared_clock_index=6, init_scripts=None, keep_load_files=False,
                 storage_retry_seconds=10, **options):
        if len(nodes) != 1:
            raise ValueError('Resident backend manages exactly one local node')
        self.nodes, self.node = nodes, nodes[0]
        self.root = Path(state_dir) / 'resident'
        self.root.mkdir(parents=True, exist_ok=True)
        self.project, self.locks = Path(hardware_project), Path(lock_dir)
        self.locks.mkdir(parents=True, exist_ok=True)
        self.options = options
        if type(shared_clock_index) is not int or not 0 <= shared_clock_index <= 23:
            raise ValueError('shared_clock_index must be an integer in 0..23')
        self.shared_clock_index = shared_clock_index
        if 'explicit_allocation' in options:
            raise ValueError('resident.explicit_allocation is no longer supported; prepare per-FPGA '
                             'initialization Tcl and configure resident.init_scripts')
        if (not isinstance(init_scripts, dict)
                or any(type(p) is not int or p < 0 for p in init_scripts)):
            raise ValueError('resident.init_scripts must map nonnegative FPGA port numbers to initialization Tcl paths')
        missing = sorted(set(self.node.ports) - set(init_scripts))
        if missing:
            raise ValueError('resident.init_scripts must cover every enabled FPGA port '
                             f'(missing={missing})')
        self.init_scripts = {p: Path(value) for p, value in init_scripts.items()}
        # Config loading resolves/checks every registered path. Only enabled
        # ports require Tcl/profile/target validation here, before retiring old
        # sessions; disabled registrations never initialize hardware. Rebuilds
        # re-read and validate the selected port's file.
        for p in self.node.ports:
            load_init_script(self.init_scripts[p], p)
        self.recovery_attempts, self.backoff = recovery_attempts, recovery_backoff
        self.max_storage = max_storage_bytes
        self.keep_load_files = keep_load_files
        self.storage_retry_seconds = storage_retry_seconds
        self.guard = threading.RLock()
        self.sessions = {}
        self.states = {p: dict(state='RECOVERING', attempts=0, next_retry=0, reason='Not initialized') for p in self.node.ports}
        self.inputs = {}
        self.storage_reserved = {}
        self.stopped = False
        self.initialization_stopping = threading.Event()
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
        # Only reliable terminal records with verified readback and reset allow
        # removal of old load copies. This also reclaims pre-upgrade scratch.
        self._prune_load_files()
        self._storage_usage()

    def _lock(self, name):
        handle = (self.locks / name).open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            handle.close()
            return None

    def _hardware_query(self, commands):
        with tempfile.TemporaryDirectory(prefix='inventory-', dir=self.root) as work:
            process = subprocess.Popen([self.node.uvhs_shell, '-t', 'runtime', '-d', 'V1', '-workdir', work],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True)
            record = self.root / 'probes' / (uuid.uuid4().hex + '.json')
            try:
                atomic_json(record, dict(pid=process.pid, start_ticks=fingerprint(process.pid)))
                output, _ = process.communicate(commands + '\nexit\n', timeout=45)
            finally:
                if record.exists():
                    retire_record(record)
                    record.unlink()
                process.wait()
            if process.returncode or platform_error(output.encode()):
                raise RuntimeError('UVHS hardware query failed')
        return output

    def _physical(self):
        return parse_fpgas(self._hardware_query('query -fpgas -all'))

    def _clocks(self):
        # With no DB loaded this reports live board clocks, not the database's
        # sign-off defaults. -board can still return every board; filter by ID.
        output = self._hardware_query('set_option runtime.database.remapping_enable true\nquery -clock')
        clocks = {}
        for line in output.splitlines():
            match = re.fullmatch(r'\s*B(\d+)\s+(\d+)\s+(\d+(?:\.\d+)?)\s+\S+\s+PLL\s*', line)
            if match:
                key = int(match[1]), int(match[2])
                if key in clocks:
                    raise RuntimeError('UVHS returned a duplicate board clock')
                clocks[key] = float(match[3]) * 1000000
        return clocks

    def _initialization_script(self, number, physical):
        # Re-read for each rebuild; live sessions retain their original snapshot.
        script = load_init_script(self.init_scripts[number], number)
        board = number // 4
        active = sorted(p for p, info in physical.items() if p != number and p // 4 == board
                        and (info['owner'] or info['booked'] or p in self.sessions))
        frequency = self._clocks().get((board, self.shared_clock_index))
        if script.configures_clock:
            # Require full occupancy information before running clock commands.
            # An active sibling forbids changing the shared frequency, rather
            # than forbidding the deployment's verified initializer altogether.
            # Execute its original commands, including a same-frequency commit;
            # this is a frequency compatibility check, not a no-op guarantee.
            missing = sorted(set(range(board * 4, board * 4 + 4)) - physical.keys())
            if missing:
                self.states[number].update(
                    reason=f'Initialization Tcl configures shared B{board}.GCLK{self.shared_clock_index}; '
                           + 'FPGA inventory missing ' + ', '.join(f'FPGA{p}' for p in missing),
                    next_retry=time.monotonic() + self.backoff)
                return None
        if (active or not script.configures_clock) and (
                frequency is None or abs(frequency - NH_CLOCK_HZ) > .5):
            actual = 'unknown' if frequency is None else f'{frequency:g} Hz'
            constraint = (', '.join(f'FPGA{p}' for p in active) + ' active; '
                          'refusing to change a shared clock' if active else
                          'initialization Tcl has no clock configuration')
            self.states[number].update(
                reason=f'Shared B{board}.GCLK{self.shared_clock_index} is {actual}; expected {NH_CLOCK_HZ} Hz; '
                       + constraint,
                next_retry=time.monotonic() + self.backoff)
            return None
        return script

    @staticmethod
    def _target(physical, number):
        if number not in physical:
            board, fpga = divmod(number, 4)
            raise RuntimeError(f'FPGA{number} (B{board}.F{fpga}) missing from UVHS inventory; '
                               + 'reported FPGA units: ' + ', '.join(map(str, sorted(physical))))
        return physical[number]

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
        if self.initialization_stopping.is_set():
            state['reason'] = 'Service is stopping'
            return
        board = number // 4
        board_lock = self._lock(f'board{board}.lock')
        if board_lock is None:
            state['reason'] = 'Board maintenance active'
            return
        handle = None
        try:
            physical = self._physical()
            # A sibling FPGA is an independent execution slot. Only ownership
            # of this target prevents allocation; shared clocks are checked
            # separately before any initialization commands are submitted.
            info = self._target(physical, number)
            if (info['owner'] or info['booked']) and number not in self.sessions:
                state['reason'] = f'FPGA{number} occupied by another session'
                return
            if number in self.sessions:
                self.sessions.pop(number).close()
                # Confirm unload retired ownership before any full rebuild.
                physical = self._physical()
                info = self._target(physical, number)
                if info['owner'] or info['booked']:
                    state['reason'] = 'Previous session ownership has not retired'
                    return
            if info['power'] != 'on':
                state['reason'] = 'FPGA power off'
                return
            reason = self._uart_busy(number)
            if reason:
                state['reason'] = reason
                return
            bootstrap = self._initialization_script(number, physical)
            if bootstrap is None:
                return
            # Inventory/clock queries can take minutes. A graceful stop may
            # have arrived while they ran; do not allocate a new session now.
            if self.initialization_stopping.is_set():
                state['reason'] = 'Service is stopping'
                return
            handle = self._lock(f'fpga{number}.lock')
            if handle is None:
                state['reason'] = 'Port lock held by another executor'
                return
            directory = self.root / 'ports' / str(number)
            directory.mkdir(parents=True, exist_ok=True)
            database = directory / 'hw.dat'
            target = (self.project / 'hw.dat').resolve(strict=True)
            if not target.is_dir():
                raise RuntimeError('Hardware project hw.dat must be a database directory')
            if database.exists() and not database.is_symlink():
                raise RuntimeError('Resident hw.dat is not a symlink; refusing to replace local data')
            if not database.is_symlink() or database.resolve() != target:
                # Pin the current project version for this session. Refresh old
                # deployment links only after its previous session has retired.
                temporary = directory / ('hw.dat-' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.symlink_to(target)
                    temporary.replace(database)
                finally:
                    temporary.unlink(missing_ok=True)
            session = Session(self.node, number, directory, handle, **self.options)
            handle = None  # Ownership transferred, including failures.
            try:
                session.start(bootstrap)
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
                    if (not self.initialization_stopping.is_set()
                            and state['state'] == 'RECOVERING' and time.monotonic() >= state['next_retry']):
                        self._initialize(number)
                    session = self.sessions.get(number)
                    if session and state['state'] == 'READY' and session.process.poll() is not None:
                        state.update(state='RECOVERING', reason='Resident UVHS exited', next_retry=time.monotonic() + self.backoff)
            return [dict(number=p, available=s['state'] == 'READY' and not self.stopped,
                         state=s['state'], reasons=[] if s['state'] == 'READY' else [s['reason'] or s['state']],
                         session=self.sessions[p].epoch if p in self.sessions else None,
                         downloads=self.sessions[p].downloads if p in self.sessions else 0)
                    for p, s in self.states.items()]

    def _storage_usage(self):
        used, materialized = 0, {}
        for path in self.root.rglob('*'):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
            except FileNotFoundError:
                continue  # A snapshot or atomically replaced file just retired.
            used += size
            relative = path.relative_to(self.root).parts
            if len(relative) >= 3 and relative[0] == 'jobs':
                materialized[relative[1]] = materialized.get(relative[1], 0) + size
        # Files and reservations must use the same sample. Each active job is
        # charged max(its peak budget, its materialized files), never both.
        remaining = sum(max(0, budget - materialized.get(ident, 0))
                        for ident, budget in self.storage_reserved.items())
        self._usage = dict(used_bytes=used, reserved_bytes=remaining,
                           limit_bytes=self.max_storage // 2,
                           disk_free_bytes=shutil.disk_usage(self.root).free, sampled_at=time.time())
        return self._usage

    def storage_status(self):
        # No hardware guard or filesystem scan on the HTTP health path.
        return dict(self._usage)

    def _check_storage(self, reserve=0):
        usage = self._storage_usage()
        required_free = usage['reserved_bytes'] + reserve + 16777216
        if (usage['used_bytes'] + usage['reserved_bytes'] + reserve > usage['limit_bytes']
                or usage['disk_free_bytes'] < required_free):
            raise StorageQuotaExceeded('resident', requested_bytes=reserve, required_free_bytes=required_free, **usage)

    def reserve_storage(self, payload, image):
        """Admission before the queue assigns a port or marks a job running."""
        with self.guard:
            if payload['id'] in self.storage_reserved:
                return
            reserve = (max(1048576, (image.stat().st_size + 1048575) // 1048576 * 1048576) * 2 +
                       self.options.get('max_job_log_bytes', 1048576) * 13 + 65536 +
                       self.options.get('output_ddr_bytes', 4194304) +
                       self.options.get('max_output_bytes', 16777216))
            # Raw UART plus the two JSON-escaped output copies in result.json
            # can require 13 times the UART limit, plus bounded metadata.
            self._check_storage(reserve)
            self.storage_reserved[payload['id']] = reserve
            self._usage = {**self._usage, 'reserved_bytes': self._usage['reserved_bytes'] + reserve}

    def prepare(self, payload, node, image, *, cancellation=None):
        if cancellation:
            cancellation.check()
        if node != self.node.id:
            raise ValueError('Unknown node')
        if hashlib.sha256(image.read_bytes()).hexdigest() != payload['sha256']:
            raise ValueError('Input checksum mismatch')
        with self.guard:
            self.reserve_storage(payload, image)
            self.inputs[payload['id']] = image

    def release_prepared(self, ident):
        with self.guard:
            reserved = self.storage_reserved.pop(ident, None)
            self.inputs.pop(ident, None)
            if reserved is not None:
                self._storage_usage()

    def run(self, payload, node, port, *, cancellation=None):
        with self.guard:
            if node != self.node.id or port not in self.states:
                raise ValueError('Port outside whitelist')
            image = self.inputs.pop(payload['id'])
            if self.stopped or self.states[port]['state'] != 'READY':
                self.release_prepared(payload['id'])
                return dict(exit_code=BUSY_EXIT, output=BUSY_TOKEN, seconds=0)
            session = self.sessions[port]
            self.states[port].update(state='BUSY', reason='Task executing')
        try:
            result, healthy = session.execute(payload, image, self.root / 'jobs' / payload['id'],
                                              cancellation=cancellation)
        except BaseException:
            with self.guard:
                self.release_prepared(payload['id'])
                self.states[port].update(state='QUARANTINED', reason='Result publication failed; inspect hardware')
            raise
        with self.guard:
            self._prune_job_load_files(self.root / 'jobs' / payload['id'], result)
            self.release_prepared(payload['id'])
            if healthy:
                self.states[port].update(state='READY', reason='')
            else:
                aborted = (result['execution_status'] in ('timeout', 'cancelled') or
                           result['details'].get('interrupted_status') in ('timeout', 'cancelled'))
                if aborted:
                    reason = result['details'].get('cleanup_error') or result['details'].get('error') or 'Unconfirmed cleanup'
                    self.states[port].update(state='QUARANTINED', reason='Abort cleanup failed: ' + reason)
                    return result
                # Rebuild only this slot; initialization checks shared clocks.
                # A reset alone does not establish peripheral safety.
                self.states[port].update(state='RECOVERING', reason='Task fault; full reinitialization required',
                                         next_retry=time.monotonic() + self.backoff)
        return result

    def read_output(self, ident, offset, limit):
        # Session.execute flushes this per-job file after every UART read.
        # No session lock: reading output must not wait for execution to finish.
        path = self.root / 'jobs' / ident / 'uart.raw.log'
        try:
            with path.open('rb') as handle:
                if offset > os.fstat(handle.fileno()).st_size:
                    raise ValueError('Output offset exceeds available bytes')
                handle.seek(offset)
                return handle.read(limit)
        except FileNotFoundError:
            return None

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
            for item in validate_artifacts(result.get('artifacts', [])):
                artifact = path.parent / 'artifacts' / (item['name'] + '.bin')
                if (artifact.stat().st_size != item['nbytes'] or
                        hashlib.sha256(artifact.read_bytes()).hexdigest() != item['sha256']):
                    return None
            return result
        except (KeyError, ValueError, TypeError, OSError):
            return None

    def artifact_path(self, ident, name):
        from ..models import task_id
        from ..remote.runner import output_name
        return self.root / 'jobs' / task_id(ident) / 'artifacts' / (output_name(name) + '.bin')

    def _prune_job_load_files(self, directory, result):
        details = result.get('details') or {}
        if (self.keep_load_files or not isinstance(details, dict) or details.get('reset_confirmed') is not True
                or details.get('ddr_readback_matches') is not True):
            return
        paths = [directory / name for name in ('image.bin', 'readback.bin')]
        if not any(path.exists() or path.is_symlink() for path in paths):
            return
        try:
            timestamp = directory.stat()
        except OSError:
            LOG.warning('Could not inspect load scratch for task %s', directory.name, exc_info=True)
            return
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # The result is already durable. A cleanup failure must not
                # turn a known application result into an unknown outcome.
                LOG.warning('Could not remove load scratch for task %s', directory.name, exc_info=True)
        try:
            # Removing legacy scratch must not make an expired job look recent
            # to retention, which uses the directory's last modification time.
            os.utime(directory, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
        except OSError:
            LOG.warning('Could not preserve retention time for task %s', directory.name, exc_info=True)

    def _prune_load_files(self):
        if self.keep_load_files:
            return
        for directory in (self.root / 'jobs').glob('*'):
            if (not re.fullmatch('[0-9a-f]{32}', directory.name) or directory.is_symlink()
                    or directory.name in self.storage_reserved or directory.name in self.inputs):
                continue
            if not any((directory / name).exists() or (directory / name).is_symlink()
                       for name in ('image.bin', 'readback.bin')):
                continue
            try:
                saved = json.loads((directory / 'result.json').read_text())
                if (saved['id'] == directory.name and re.fullmatch('[0-9a-f]{64}', saved['sha256'])
                        and isinstance(saved['result'], dict)):
                    self._prune_job_load_files(directory, saved['result'])
            except (OSError, ValueError, KeyError, TypeError):
                continue  # An active/unknown task or damaged record is protected.

    def cleanup(self, before):
        # Queue retention runs first; image directories are the authoritative
        # protection set for queued, running and retained terminal jobs.
        with self.guard:
            self._prune_load_files()
        images = self.root.parent / 'images'
        for directory in (self.root / 'jobs').glob('*'):
            if (re.fullmatch('[0-9a-f]{32}', directory.name) and not (images / directory.name).exists()
                    and directory.stat().st_mtime < before):
                shutil.rmtree(directory)
        with self.guard:
            self._storage_usage()

    def stop_initialization(self):
        # Never wait for guard: an in-flight initialization may own it. Running
        # tasks retain their sessions until the queue finishes draining.
        self.initialization_stopping.set()

    def close(self):
        self.stop_initialization()
        with self.guard:
            self.stopped = True
            for session in self.sessions.values():
                session.close()
            self.sessions.clear()
