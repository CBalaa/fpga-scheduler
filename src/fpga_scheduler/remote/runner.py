#!/usr/bin/env python3
"""Run one UVHS task; only a UART v1 terminal record can report application exit.

Standalone Python 3.8+, deployed alongside scheduler.py. stdout contains logs;
output/result.json is the machine-readable result, written after session cleanup.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time

START = b'@@FPGA_START '
RESULT = b'@@FPGA_RESULT '
OUTPUT = b'@@FPGA_OUTPUT '
MAX_OUTPUTS = 16
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
OUTPUT_DTYPES = {'|i1': 1, '|u1': 1, '<i2': 2, '<u2': 2, '<i4': 4,
                 '<u4': 4, '<i8': 8, '<u8': 8, '<f4': 4, '<f8': 8}


class ProtocolError(ValueError):
    pass


def output_name(name):
    if not isinstance(name, str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name) is None:
        raise ValueError('Output name must match [A-Za-z][A-Za-z0-9_-]{0,63}')
    return name


def output_declaration(value):
    fields = {'version', 'name', 'address', 'nbytes', 'dtype', 'shape'}
    if not isinstance(value, dict) or set(value) != fields or type(value['version']) is not int or value['version'] != 1:
        raise ProtocolError('Invalid output declaration fields/version')
    try:
        output_name(value['name'])
    except ValueError as error:
        raise ProtocolError(str(error)) from None
    address, size, shape = value['address'], value['nbytes'], value['shape']
    if not isinstance(address, str) or re.fullmatch(r'0x[0-9a-fA-F]{1,16}', address) is None:
        raise ProtocolError('Invalid output physical address')
    if type(size) is not int or not 1 <= size <= MAX_OUTPUT_BYTES or int(address, 16) + size > 2 ** 64:
        raise ProtocolError('Invalid output byte count/range')
    dtype = value['dtype']
    if not isinstance(dtype, str) or dtype not in OUTPUT_DTYPES:
        raise ProtocolError('Unsupported output dtype')
    if not isinstance(shape, list) or not 1 <= len(shape) <= 8:
        raise ProtocolError('Invalid output shape')
    expected = OUTPUT_DTYPES[dtype]
    for dimension in shape:
        if type(dimension) is not int or not 1 <= dimension <= MAX_OUTPUT_BYTES:
            raise ProtocolError('Invalid output dimension')
        expected *= dimension
    if expected != size:
        raise ProtocolError('Output shape/dtype does not match byte count')
    return dict(value, shape=list(shape))


class Completion:
    """Incremental, UART-only framing. Ordinary logs are never success evidence."""
    def __init__(self):
        self.pending = b''
        self.started = False
        self.result = None
        self.outputs = []

    def feed(self, data):
        self.pending += data
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            line = line.strip(b'\r')
            if len(line) > 4096:
                raise ProtocolError('UART line exceeds 4096 bytes')
            if line.startswith(START):
                if self.started:
                    raise ProtocolError('Repeated application start (unexpected restart)')
                value = self.decode(line[len(START):])
                if value != {'version': 1} or type(value['version']) is not int:
                    raise ProtocolError('Unsupported start record')
                self.started = True
            elif line.startswith(OUTPUT):
                if not self.started or self.result is not None:
                    raise ProtocolError('Output declaration must be between START and RESULT')
                value = output_declaration(self.decode(line[len(OUTPUT):]))
                if any(item['name'] == value['name'] for item in self.outputs):
                    raise ProtocolError('Repeated output name')
                if (len(self.outputs) >= MAX_OUTPUTS or
                        sum(item['nbytes'] for item in self.outputs) + value['nbytes'] > MAX_OUTPUT_BYTES):
                    raise ProtocolError('Output count/total byte limit exceeded')
                self.outputs.append(value)
            elif line.startswith(RESULT):
                if not self.started:
                    raise ProtocolError('Terminal record without application start')
                if self.result is not None:
                    raise ProtocolError('Repeated terminal record')
                value = self.decode(line[len(RESULT):])
                if type(value.get('version')) is not int or value['version'] != 1:
                    raise ProtocolError('Unsupported result version')
                status = value.get('status')
                if status == 'exited':
                    code = value.get('app_exit_code')
                    if (set(value) != {'version', 'status', 'app_exit_code'} or
                            type(code) is not int or not -(2 ** 31) <= code < 2 ** 31):
                        raise ProtocolError('Invalid application exit code')
                elif status == 'trap':
                    if (set(value) != {'version', 'status', 'mcause', 'mepc', 'mtval'} or
                            any(not isinstance(value.get(key), str) or
                                not re.fullmatch(r'0x[0-9a-fA-F]{1,16}', value[key])
                                for key in ('mcause', 'mepc', 'mtval'))):
                        raise ProtocolError('Invalid trap record')
                else:
                    raise ProtocolError('Unsupported application status')
                self.result = value
        if len(self.pending) > 4096:
            raise ProtocolError('UART line exceeds 4096 bytes')

    def finish(self):
        if self.pending.startswith((START, RESULT, OUTPUT)):
            raise ProtocolError('Incomplete UART protocol record')

    @staticmethod
    def decode(data):
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ProtocolError('Duplicate result field')
                value[key] = item
            return value
        try:
            value = json.loads(data, object_pairs_hook=unique)
        except (ValueError, UnicodeError) as error:
            raise ProtocolError('Malformed UART protocol record') from error
        if not isinstance(value, dict):
            raise ProtocolError('Protocol record must be an object')
        return value


def platform_error(data):
    for line in data.replace(b'\r', b'').split(b'\n'):
        summary = re.search(rb'Total (?:ERROR|FATAL):\s*(\d+)', line)
        if summary:
            if int(summary[1]):
                return True
        elif re.search(rb'\b(?:ERROR|FATAL):|^STACK TRACE:', line):
            return True
    return False


def emit(data):
    # Keep UART and tool logs available even when the SSH client stops reading.
    try:
        os.write(1, data)
    except (BrokenPipeError, OSError):
        pass


def run(args):
    # Completion/declaration validation is also imported by the portable SDK.
    # Only the hardware runner needs POSIX UART and terminal support.
    import fcntl
    import pty
    import termios

    directory = Path(args.workdir).resolve()
    output = Path(os.environ.get('UV_RUN_OUT_ROOT', directory / 'output'))
    output.mkdir(parents=True, exist_ok=True)
    result = dict(execution_status='platform_error', app_exit_code=None,
                  exit_code=1, details={})
    process = None
    master = slave = serial = None
    saved_attrs = None
    interrupted = [False]
    protocol = Completion()
    old_signals = {}
    tool_tail = b''
    tool_pending = b''
    prompt = False
    terminal_at = None
    started = time.monotonic()

    def stop(signum, frame):
        interrupted[0] = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        old_signals[sig] = signal.signal(sig, stop)
    try:
        with (output / 'uart.raw.log').open('wb') as uart_log, (output / 'uvhs.log').open('wb') as tool_log:
            serial = os.open(os.environ.get('FPGA_UART_PREFIX', '/dev/FPGA') + str(args.fpga),
                             os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            fcntl.ioctl(serial, termios.TIOCEXCL)
            saved_attrs = termios.tcgetattr(serial)
            attrs = termios.tcgetattr(serial)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(serial, termios.TCSANOW, attrs)
            termios.tcflush(serial, termios.TCIFLUSH)
            master, slave = pty.openpty()
            # Inherit the agent's task process group so its final cleanup also
            # reaches UVHS grandchildren if this runner crashes.
            process = subprocess.Popen(['make', 'uv_run' + str(args.fpga), args.test],
                                       cwd=str(directory), stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            slave = None
            readers = [master, serial]
            deadline = started + float(os.environ.get('FPGA_EXECUTION_TIMEOUT', '600'))
            startup_deadline = started + float(os.environ.get('FPGA_STARTUP_TIMEOUT', '300'))
            while True:
                now = time.monotonic()
                if interrupted[0]:
                    result.update(execution_status='interrupted', exit_code=130)
                    break
                if now >= deadline:
                    result.update(execution_status='timeout', exit_code=124)
                    result['details']['error'] = 'Total execution deadline exceeded'
                    break
                if not prompt and now >= startup_deadline:
                    raise RuntimeError('UVHS startup deadline exceeded')
                if protocol.result is not None and prompt and terminal_at is not None and now - terminal_at >= .5:
                    record = protocol.result
                    code = record.get('app_exit_code')
                    result.update(execution_status=record['status'], app_exit_code=code,
                                  exit_code=0 if record['status'] == 'exited' and code == 0 else 1)
                    result['details']['application'] = record
                    break
                for fd in select.select(readers, [], [], .1)[0]:
                    try:
                        data = os.read(fd, 65536)
                    except OSError as error:
                        if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            continue
                        if fd == master and error.errno == errno.EIO:
                            data = b''
                        else:
                            raise
                    if not data:
                        if fd == serial:
                            raise RuntimeError('UART disconnected')
                        readers.remove(fd)
                        continue
                    emit(data)
                    if fd == serial:
                        uart_log.write(data)
                        uart_log.flush()
                        protocol.feed(data)
                        if protocol.result is not None:
                            terminal_at = now
                    else:
                        tool_log.write(data)
                        tool_log.flush()
                        tool_tail = (tool_tail + data)[-8192:]
                        tool_pending += data
                        lines = tool_pending.split(b'\n')
                        tool_pending = lines.pop()[-8192:]
                        if any(platform_error(line) for line in lines):
                            raise RuntimeError('UVHS reported an error; see uvhs.log')
                        prompt |= b'hspRun>' in tool_tail
                status = process.poll()
                if status not in (None, 0):
                    raise RuntimeError('make exited with status ' + str(status))
                if master not in readers:
                    raise ProtocolError('UVHS exited before a complete application session')
            if protocol.result is not None:
                result['details']['application'] = protocol.result
            if protocol.outputs:
                result['details']['outputs_declared'] = protocol.outputs
                raise RuntimeError('DDR output export requires the bosc-resident backend')
            if result['execution_status'] in ('exited', 'trap'):
                # The project captures readback before releasing CPU reset.
                image = Path(args.test.split('=', 1)[1])
                readback = output / 'images' / (image.stem + '.readback.bin')
                if not readback.is_file() or readback.read_bytes() != image.read_bytes():
                    raise RuntimeError('Pre-execution DDR readback is missing or differs from the image')
                result['details']['ddr_readback_matches'] = True
                result['details']['image_sha256'] = hashlib.sha256(image.read_bytes()).hexdigest()
    except ProtocolError as error:
        result.update(execution_status='protocol_error', app_exit_code=None, exit_code=1)
        result['details']['error'] = str(error)
    except (OSError, RuntimeError, ValueError) as error:
        result.update(execution_status='platform_error', app_exit_code=None, exit_code=1)
        result['details']['error'] = str(error)
    finally:
        # Request graceful unload before the outer agent's process-group cleanup.
        if master is not None and process is not None:
            try:
                os.write(master, b'exit\n')
            except OSError:
                pass
            cleanup_deadline = time.monotonic() + 40
            with (output / 'uvhs.log').open('ab') as tool_log:
                while time.monotonic() < cleanup_deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            data = b''
                        if not data:
                            break
                        tool_log.write(data)
                        emit(data)
                        tool_pending += data
                        lines = tool_pending.split(b'\n')
                        tool_pending = lines.pop()[-8192:]
                        if any(platform_error(line) for line in lines):
                            result.update(execution_status='platform_error', exit_code=1)
                            result['details']['error'] = 'UVHS error during session cleanup'
                    if process.poll() is not None:
                        # Drain remaining PTY output before accepting cleanup.
                        if not select.select([master], [], [], 0)[0]:
                            break
                try:
                    status = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    status = None
                if status != 0:
                    result.update(execution_status='platform_error', exit_code=1)
                    result['details']['error'] = 'UVHS did not unload cleanly'
        if serial is not None:
            try:
                if saved_attrs is not None:
                    termios.tcsetattr(serial, termios.TCSANOW, saved_attrs)
                fcntl.ioctl(serial, termios.TIOCNXCL)
            except OSError:
                pass
            os.close(serial)
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
        result['details']['application_started'] = protocol.started
        result['details']['uvhs_prompt_seen'] = prompt
        result['details']['runner_seconds'] = time.monotonic() - started
        pending = output / 'result.json.tmp'
        pending.write_text(json.dumps(result) + '\n')
        pending.replace(output / 'result.json')
    return result['exit_code']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fpga', type=int, choices=range(8), required=True)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('test')
    args = parser.parse_args()
    if not args.test.startswith('test=') or not Path(args.test[5:]).is_absolute():
        parser.error('test must be an absolute bin path')
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
