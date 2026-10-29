"""Resident UVHS control channel. Only numbered, caught Tcl replies acknowledge commands."""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import subprocess
import termios
import time
import uuid

from .runner import Completion, ProtocolError, platform_error, MAX_OUTPUT_BYTES
from ..models import Cancellation, CancelledError


class CommandTimeout(RuntimeError):
    """An unacknowledged command cannot establish a reusable control channel."""


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def tcl(value):
    # A quoted Tcl word, including protection against command/variable substitution.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('[', '\\[').replace('\n', '\\n').replace('\r', '\\r') + '"'


def fingerprint(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else None
    except (FileNotFoundError, ProcessLookupError):
        return None


def group_members(record):
    """Match the recorded session, even if its shell leader has already exited."""
    pid = record.get('pid')
    ticks = record.get('start_ticks')
    if not pid or not ticks:
        return []
    live_leader = fingerprint(pid)
    if live_leader is not None and live_leader != ticks:
        raise RuntimeError('Recorded UVHS PID was reused; refusing to signal it')
    members = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) != pid or int(fields[3]) != pid or fields[0] == 'Z':
                continue
            if proc.stat().st_uid != os.getuid() or int(fields[19]) < int(ticks):
                raise RuntimeError('Recorded UVHS process group identity cannot be verified')
            members.append(int(proc.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return members


def retire_record(path):
    """Retire only the exact UVHS process group this deployment recorded."""
    record = json.loads(Path(path).read_text())
    if group_members(record):
        os.killpg(record['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while group_members(record) and time.monotonic() < deadline:
            time.sleep(.05)
        if group_members(record):
            os.killpg(record['pid'], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while group_members(record) and time.monotonic() < deadline:
            time.sleep(.05)
        if group_members(record):
            raise RuntimeError('Recorded UVHS descendants did not retire')
    return record


class BoundedLog:
    def __init__(self, path, maximum, rotate=False):
        self.path, self.maximum, self.rotate = Path(path), maximum, rotate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('ab')
        self.size = self.handle.tell()

    def write(self, data):
        if self.rotate and self.size + len(data) > self.maximum:
            self.handle.close()
            self.path.replace(self.path.with_suffix(self.path.suffix + '.1'))
            self.handle = self.path.open('wb')
            self.size = 0
        if self.size + len(data) > self.maximum:
            raise RuntimeError('Task log limit exceeded')
        self.handle.write(data)
        self.handle.flush()
        self.size += len(data)

    def close(self):
        self.handle.close()


class Session:
    def __init__(self, node, number, directory, port_lock, *, startup_timeout=300,
                 command_timeout=60, uart_quiet_seconds=.2, max_job_log_bytes=1048576,
                 max_session_log_bytes=4194304, output_ddr_base=0x80000000,
                 output_ddr_bytes=4194304, max_output_bytes=MAX_OUTPUT_BYTES, **_):
        self.node, self.number = node, number
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.port_lock = port_lock
        self.startup_timeout, self.command_timeout = startup_timeout, command_timeout
        self.quiet, self.max_job_log = uart_quiet_seconds, max_job_log_bytes
        self.output_base, self.output_span = output_ddr_base, output_ddr_bytes
        self.max_output = max_output_bytes
        self.epoch, self.seq = uuid.uuid4().hex, 0
        self.process = None
        self.master = self.serial = None
        self.saved_attrs = None
        self.pending = b''
        self.command_pending = False
        self.control = BoundedLog(self.directory / 'control.log', max_session_log_bytes, rotate=True)
        self.downloads = 0
        self.record = self.directory / 'session.json'

    def start(self, initialization):
        # Execute the validated deployment file verbatim from an immutable-for-
        # this-session snapshot. Relative Tcl paths use the isolated workdir.
        snapshot = self.directory / 'init.tcl'
        temporary = snapshot.with_suffix('.tmp')
        temporary.write_bytes(initialization.content)
        temporary.replace(snapshot)
        origin = dict(source=str(initialization.path), sha256=initialization.sha256,
                      snapshot=str(snapshot), session=self.epoch)
        atomic_json(self.directory / 'init-script.json', origin)
        self.control.write(('Scheduler initialization: ' + json.dumps(origin) + '\n').encode())
        self.serial = os.open(self.node.uart_prefix + str(self.number), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        fcntl.ioctl(self.serial, termios.TIOCEXCL)
        self.saved_attrs = termios.tcgetattr(self.serial)
        attrs = termios.tcgetattr(self.serial)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.serial, termios.TCSANOW, attrs)
        termios.tcflush(self.serial, termios.TCIFLUSH)
        self.master, slave = pty.openpty()
        # No terminal echo: echoed command text must never impersonate a reply.
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        # UVHS and every descendant inherit the session port lock. A daemon crash
        # cannot make the port appear free while an orphan still controls it.
        try:
            self.process = subprocess.Popen([self.node.uvhs_shell, '-t', 'runtime', '-d', 'V1',
                                             '-workdir', str(self.directory)], cwd=self.directory,
                                            stdin=slave, stdout=slave, stderr=slave,
                                            start_new_session=True, pass_fds=(self.port_lock.fileno(),))
        finally:
            os.close(slave)
        atomic_json(self.record, dict(epoch=self.epoch, pid=self.process.pid,
                                     start_ticks=fingerprint(self.process.pid), number=self.number, dirty=True))
        self.command('source ' + tcl(snapshot), timeout=self.startup_timeout)
        self.downloads = 1
        # Do not repair a script that leaves CPU reset deasserted and then call
        # it ready. The script itself must satisfy its reset contract first.
        self.confirm_reset(self.command('query -reset'))
        self.reset()
        self.boundary()

    def _tool(self, timeout):
        if self.process.poll() is not None:
            raise RuntimeError('UVHS process exited')
        if not select.select([self.master], [], [], max(0, timeout))[0]:
            return b''
        try:
            data = os.read(self.master, 65536)
        except OSError as error:
            if error.errno == errno.EIO:
                raise RuntimeError('UVHS control channel closed') from None
            raise
        if not data:
            raise RuntimeError('UVHS control channel closed')
        self.control.write(data)
        return data

    def command(self, script, timeout=None, *, cancellation=None):
        if self.command_pending:
            raise RuntimeError('Previous UVHS command is unacknowledged; refusing another hardware command')
        self.seq += 1
        marker = f'@@CONTROL_{self.epoch}_{self.seq} '
        diagnostics = ('if {$__scheduler_rc != 0} {puts stderr "Scheduler Tcl error: $__scheduler_value"; '
                       'if {[dict exists $__scheduler_options -errorinfo]} '
                       '{puts stderr [dict get $__scheduler_options -errorinfo]}; flush stderr}; ')
        command = ('set __scheduler_rc [catch ' + tcl(script) + ' __scheduler_value __scheduler_options]; '
                   + diagnostics +
                   'puts "' + marker + '$__scheduler_rc"; flush stdout\n').encode()
        # Long writes can exceed the canonical PTY input buffer; scripts use source.
        if len(command) > 3500:
            path = self.directory / 'command.tcl'
            path.write_text(script + '\n')
            command = ('set __scheduler_rc [catch {source ' + tcl(path) + '} __scheduler_value __scheduler_options]; '
                       + diagnostics +
                       'puts "' + marker + '$__scheduler_rc"; flush stdout\n').encode()
        if cancellation is None:
            self.command_pending = True
            os.write(self.master, command)
        else:
            # Acceptance of cancellation and the CPU-release write are ordered.
            # Never hold this guard while awaiting UVHS's response.
            with cancellation.guard:
                cancellation.check()
                self.command_pending = True
                os.write(self.master, command)
        deadline = time.monotonic() + (self.command_timeout if timeout is None else timeout)
        output = b''
        while time.monotonic() < deadline:
            data = self._tool(min(.1, deadline - time.monotonic()))
            output = (output + data)[-262144:]
            # Vendor diagnostics may report failure without raising Tcl errors.
            if platform_error(output):
                raise RuntimeError('UVHS command reported an error')
            match = re.search(rb'(?:^|\n)' + re.escape(marker.encode()) + rb'([0-9]+)\r*\n', output)
            if match:
                self.command_pending = False
                if int(match[1]):
                    raise RuntimeError('UVHS Tcl command failed')
                return output[:match.start()]
        raise CommandTimeout('UVHS command acknowledgement deadline exceeded')

    def reset(self):
        output = self.command('reset -name cpu_reset -value 1\nquery -reset')
        return self.confirm_reset(output)

    @staticmethod
    def confirm_reset(output):
        # Require the queried value; a command acknowledgement alone is not proof.
        if re.search(rb'(?m)^[ \t]*(?:[0-9]+[ \t]+[01][ \t]+)?cpu_reset[ \t]+1[ \t]*\r*$', output, re.I) is None:
            raise RuntimeError('CPU reset assertion was not confirmed by query -reset')
        return True

    def boundary(self):
        """Require a quiet UART while reset is asserted; discard complete old bytes."""
        deadline, quiet_since = time.monotonic() + max(2, self.quiet * 5), time.monotonic()
        discarded = 0
        while time.monotonic() < deadline:
            if select.select([self.serial], [], [], min(.05, self.quiet))[0]:
                data = os.read(self.serial, 65536)
                if not data:
                    raise RuntimeError('UART disconnected')
                discarded += len(data)
                quiet_since = time.monotonic()
                if discarded > self.max_job_log:
                    raise ProtocolError('UART cannot establish a task boundary')
            elif time.monotonic() - quiet_since >= self.quiet:
                termios.tcflush(self.serial, termios.TCIFLUSH)
                return discarded
        raise ProtocolError('UART did not become quiet while reset was asserted')

    def capture_outputs(self, outputs, directory):
        """Read one bounded DDR prefix, then extract exact byte ranges.

        Prefix reads deliberately use the same vendor memory ordering as the
        established image readback. Arbitrary nonzero RTL slices are not assumed.
        Caller owns the slot and has confirmed reset and application completion.
        """
        if not outputs:
            return []
        if sum(item['nbytes'] for item in outputs) > self.max_output:
            raise ValueError('Output total exceeds resident max_output_bytes')
        end = self.output_base
        for item in outputs:
            address = int(item['address'], 16)
            if not self.output_base <= address < address + item['nbytes'] <= self.output_base + self.output_span:
                raise ValueError('Output lies outside the configured physical DDR window')
            end = max(end, address + item['nbytes'])
        size = (end - self.output_base + 63) // 64 * 64
        directory = Path(directory) / 'artifacts'
        directory.mkdir(exist_ok=True)
        snapshot = directory / '.snapshot.tmp'
        written, manifest = [], []
        try:
            memory = f'nanhu_xilinx.i_ddr[{size // 64 - 1}:0]'
            self.command('readmem -rtl ' + tcl(memory) + ' -file ' + tcl(snapshot) + ' -file_type bin')
            if snapshot.stat().st_size != size:
                raise RuntimeError('Output DDR snapshot length mismatch')
            with snapshot.open('rb') as source:
                for item in outputs:
                    source.seek(int(item['address'], 16) - self.output_base)
                    data = source.read(item['nbytes'])
                    if len(data) != item['nbytes']:
                        raise RuntimeError('Output DDR data is truncated')
                    path = directory / (item['name'] + '.bin')
                    temporary = directory / (item['name'] + '.tmp')
                    written.extend((temporary, path))
                    with temporary.open('wb') as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary.replace(path)
                    manifest.append({**{k: v for k, v in item.items() if k != 'version'},
                                     'sha256': hashlib.sha256(data).hexdigest()})
            atomic_json(directory / 'manifest.json', manifest)
            return manifest
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        finally:
            snapshot.unlink(missing_ok=True)

    def execute(self, payload, image, directory, *, cancellation=None):
        cancellation = cancellation or Cancellation()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        deadline = started + payload['timeout']
        result = dict(output='', uart_output='', node=self.node.id, seconds=0,
                      execution_status='platform_error', app_exit_code=None, exit_code=1,
                      details=dict(session=self.epoch, uvhs_pid=self.process.pid, fpga=self.number,
                                   downloads=self.downloads, profile='nh-only', image_sha256=payload['sha256']))
        protocol, uart = Completion(), bytearray()
        reset_ok = False
        log = BoundedLog(directory / 'uart.raw.log', self.max_job_log)
        try:
            cancellation.check()
            self.reset()
            result['details']['discarded_uart_bytes'] = self.boundary()
            cancellation.check()
            content = image.read_bytes()
            if hashlib.sha256(content).hexdigest() != payload['sha256']:
                raise RuntimeError('Input checksum mismatch')
            size = max(1048576, (len(content) + 1048575) // 1048576 * 1048576)
            padded, readback = directory / 'image.bin', directory / 'readback.bin'
            padded.write_bytes(content + bytes(size - len(content)))
            memory = f'nanhu_xilinx.i_ddr[{size // 64 - 1}:0]'
            cancellation.check()
            self.command('writemem -rtl ' + tcl(memory) + ' -file ' + tcl(padded) + ' -file_type bin\n'
                         'readmem -rtl ' + tcl(memory) + ' -file ' + tcl(readback) + ' -file_type bin',
                         timeout=max(.01, min(self.command_timeout, deadline - time.monotonic())))
            cancellation.check()
            if readback.read_bytes() != padded.read_bytes():
                raise RuntimeError('DDR readback differs from padded input')
            result['details']['ddr_readback_matches'] = True
            self.boundary()
            cancellation.check()
            if time.monotonic() >= deadline:
                raise TimeoutError('Execution deadline exceeded before CPU release')
            atomic_json(directory / 'start-intent.json', dict(id=payload['id'], sha256=payload['sha256'],
                        node=self.node.id, fpga=self.number, session=self.epoch, time=time.time()))
            self.command('reset -name cpu_reset -value 0', timeout=max(.01, deadline - time.monotonic()),
                         cancellation=cancellation)
            terminal_at = None
            while time.monotonic() < deadline:
                cancellation.check()
                if select.select([self.serial], [], [], .02)[0]:
                    data = os.read(self.serial, 65536)
                    if not data:
                        raise RuntimeError('UART disconnected')
                    log.write(data)
                    uart.extend(data)
                    protocol.feed(data)
                    if protocol.result is not None and terminal_at is None:
                        terminal_at = time.monotonic()
                if select.select([self.master], [], [], 0)[0]:
                    if platform_error(self._tool(0)):
                        raise RuntimeError('UVHS error during execution')
                if self.process.poll() is not None:
                    raise RuntimeError('UVHS exited during execution')
                if terminal_at and time.monotonic() - terminal_at >= self.quiet:
                    break
            else:
                raise TimeoutError('Execution deadline exceeded')
            cancellation.check()
            if cancellation.finish():
                cancellation.check()
            record = protocol.result
            result.update(execution_status=record['status'], app_exit_code=record.get('app_exit_code'),
                          exit_code=0 if record['status'] == 'exited' and record['app_exit_code'] == 0 else 1)
            result['details']['application'] = record
        except CancelledError as error:
            result.update(execution_status='cancelled', exit_code=130)
            result['details']['cancel_reason'] = str(error)
        except TimeoutError as error:
            reason = cancellation.finish()
            result.update(execution_status='cancelled' if reason else 'timeout', exit_code=130 if reason else 124)
            if reason:
                result['details']['cancel_reason'] = reason
            result['details']['error'] = str(error)
        except ProtocolError as error:
            result.update(execution_status='protocol_error', exit_code=1)
            result['details']['error'] = str(error)
        except (OSError, RuntimeError, ValueError) as error:
            result.update(execution_status='platform_error', exit_code=1)
            result['details']['error'] = str(error)
            if isinstance(error, CommandTimeout):
                result['details']['interrupted_status'] = 'timeout'
        finally:
            reason = cancellation.finish()
            if reason:
                result['details']['cancel_reason'] = reason
                if result['execution_status'] not in ('cancelled', 'exited'):
                    result['details']['interrupted_status'] = 'cancelled'
            try:
                self.reset()
                # Parse bytes arriving during reset too: late duplicate terminal
                # records cannot silently become a successful next task.
                while select.select([self.serial], [], [], 0)[0]:
                    data = os.read(self.serial, 65536)
                    if not data:
                        raise RuntimeError('UART disconnected')
                    log.write(data)
                    uart.extend(data)
                    protocol.feed(data)
                self.boundary()
                try:
                    protocol.finish()
                except ProtocolError:
                    if result['execution_status'] not in ('timeout', 'cancelled'):
                        raise
                    # Reset can stop UART in the middle of a START/RESULT
                    # record. After verified reset and a quiet boundary this
                    # is expected on abort, not a failure to reclaim the slot.
                    # Complete malformed/duplicate records still fail feed().
                    result['details']['uart_record_interrupted'] = True
                reset_ok = True
            except (OSError, RuntimeError, ValueError) as error:
                result['details'].setdefault('interrupted_status', result['execution_status'])
                result.update(execution_status='platform_error', exit_code=1)
                result['details']['cleanup_error'] = str(error)
            if protocol.outputs:
                result['details']['outputs_declared'] = protocol.outputs
                if reset_ok and result['execution_status'] == 'exited' and result['app_exit_code'] == 0:
                    try:
                        result['artifacts'] = self.capture_outputs(protocol.outputs, directory)
                    except (OSError, RuntimeError, ValueError) as error:
                        result.update(execution_status='platform_error', exit_code=1)
                        result['details']['artifact_error'] = str(error)
                        if isinstance(error, CommandTimeout):
                            result['details']['interrupted_status'] = 'timeout'
                else:
                    result['details']['artifacts_skipped'] = 'Application completion or hardware cleanup was not successful'
            log.close()
            result['details'].update(reset_confirmed=reset_ok, application_started=protocol.started)
            result['uart_output'] = result['output'] = uart.decode(errors='replace')
            result['seconds'] = time.monotonic() - started
            atomic_json(directory / 'result.json', dict(id=payload['id'], sha256=payload['sha256'], result=result))
        # NH-only: CPU reset + verified quiet UART is the abort reuse boundary.
        # This does not establish DMA/E6 peripheral quiescence.
        healthy = reset_ok and result['execution_status'] in ('exited', 'timeout', 'cancelled')
        return result, healthy

    def close(self):
        clean = self.process is None
        if self.process is not None:
            try:
                self.reset()
                os.write(self.master, b'exit\n')
                deadline = time.monotonic() + 40
                while self.process.poll() is None and time.monotonic() < deadline:
                    try:
                        data = self._tool(.1)
                        if platform_error(data):
                            raise RuntimeError('UVHS unload reported an error')
                    except RuntimeError:
                        if self.process.poll() is None:
                            raise
                        break
                clean = self.process.wait(timeout=2) == 0
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                clean = False
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
        if self.record.exists():
            try:
                retire_record(self.record)
            except (OSError, RuntimeError):
                clean = False
        if self.serial is not None:
            try:
                if self.saved_attrs:
                    termios.tcsetattr(self.serial, termios.TCSANOW, self.saved_attrs)
                fcntl.ioctl(self.serial, termios.TIOCNXCL)
            except (OSError, termios.error):
                clean = False
            os.close(self.serial)
        if self.master is not None:
            os.close(self.master)
        self.control.close()
        self.port_lock.close()
        if self.record.exists():
            record = json.loads(self.record.read_text())
            record['closed_cleanly'] = clean
            atomic_json(self.record, record)
        return clean
