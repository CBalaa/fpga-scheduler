"""UART framing and real PTY sessions; no hardware is used by these tests."""
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fpga_scheduler.backends.bosc import BOSC
from fpga_scheduler.config import Node
from fpga_scheduler.remote.runner import Completion, ProtocolError, platform_error

PROJECT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT / 'src/fpga_scheduler/remote/runner.py'
START = b'@@FPGA_START {"version":1}\r\n'


def terminal(code=0):
    return ('@@FPGA_RESULT ' + json.dumps(dict(version=1, status='exited', app_exit_code=code)) + '\r\n').encode()


class ProtocolTests(unittest.TestCase):
    def test_fragments_and_full_signed_return_values(self):
        for code in (0, 7, -1, -2147483648, 2147483647):
            parser = Completion()
            for byte in START + b'hello PASS\r\n' + terminal(code):
                parser.feed(bytes([byte]))
            self.assertEqual(parser.result['app_exit_code'], code)

    def test_printed_pass_without_terminal_is_not_success(self):
        parser = Completion()
        parser.feed(b'verify hello: PASS\r\nTotal ERROR: 0\r\nhspRun>')
        self.assertIsNone(parser.result)

    def test_invalid_or_replayed_records(self):
        for data in (terminal(), START + START, START + terminal() + terminal(),
                     START + terminal(True), START + terminal(2 ** 31),
                     START + b'@@FPGA_RESULT {"version":1,"status":"exited"}\n',
                     START + b'@@FPGA_RESULT {"version":1,"version":1}\n',
                     START + b'@@FPGA_RESULT not-json\n',
                     b'@@FPGA_START {"version":true}\n', b'x' * 4097):
            with self.subTest(data=data[:60]), self.assertRaises(ProtocolError):
                Completion().feed(data)

    def test_trap_has_no_application_exit_code(self):
        parser = Completion()
        parser.feed(START + b'@@FPGA_RESULT {"version":1,"status":"trap","mcause":"0x2","mepc":"0x80000000","mtval":"0x0"}\n')
        self.assertEqual(parser.result['status'], 'trap')
        self.assertNotIn('app_exit_code', parser.result)

    def test_zero_error_summaries_are_not_platform_errors(self):
        self.assertFalse(platform_error(b'Total ERROR: 0\nTotal FATAL: 0\n'))
        for text in (b'Total ERROR: 1', b'[12:00] ERROR: load failed', b'STACK TRACE:'):
            self.assertTrue(platform_error(text))


class RunnerSessionTests(unittest.TestCase):
    def session(self, uart, *, readback=True, tool_error=False, deadline=3):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hardware, slave = pty.openpty()
            uart_device = os.ttyname(slave)
            os.close(slave)
            (root / 'uart4').symlink_to(uart_device)
            image = root / 'input_padded.bin'
            image.write_bytes(b'hello input')
            output = root / 'output'
            binary = root / 'bin'
            binary.mkdir()
            make = binary / 'make'
            make.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
output=Path(os.environ['UV_RUN_OUT_ROOT']); (output/'images').mkdir(parents=True)
image=Path(sys.argv[2].split('=',1)[1])
if os.environ['TEST_READBACK']=='1':
    (output/'images'/(image.stem+'.readback.bin')).write_bytes(image.read_bytes())
print('hspRun>', flush=True)
(output/'ready').touch()
for line in sys.stdin:
    if line.strip()=='exit': break
print('Total ERROR: '+os.environ['TEST_TOOL_ERROR'], flush=True)
''')
            make.chmod(0o755)
            env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                   'UV_RUN_OUT_ROOT': str(output), 'FPGA_UART_PREFIX': str(root / 'uart'),
                   'FPGA_EXECUTION_TIMEOUT': str(deadline), 'FPGA_STARTUP_TIMEOUT': '3',
                   'TEST_READBACK': str(int(readback)), 'TEST_TOOL_ERROR': str(int(tool_error))}
            process = subprocess.Popen([sys.executable, str(RUNNER), '--fpga=4',
                                        '--workdir=' + str(root), 'test=' + str(image)],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            errors = []
            def write_uart():
                try:
                    end = time.monotonic() + 4
                    while not (output / 'ready').exists():
                        if process.poll() is not None or time.monotonic() > end:
                            return
                        time.sleep(.01)
                    for offset in range(0, len(uart), 13):
                        os.write(hardware, uart[offset:offset+13])
                        time.sleep(.003)
                except OSError as error:
                    errors.append(str(error))
            writer = threading.Thread(target=write_uart)
            writer.start()
            try:
                stdout, stderr = process.communicate(timeout=8)
                writer.join(timeout=2)
                self.assertFalse(errors, errors)
                self.assertEqual(stderr, b'')
                result = json.loads((output / 'result.json').read_text())
                self.assertEqual(process.returncode, result['exit_code'], stdout)
                self.assertEqual((output / 'uart.raw.log').read_bytes(), uart)
                return result
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                writer.join(timeout=2)
                os.close(hardware)

    def test_clean_exit_and_nonzero_return(self):
        for code in (0, 7, -1):
            result = self.session(START + terminal(code))
            self.assertEqual(result['execution_status'], 'exited')
            self.assertEqual(result['app_exit_code'], code)
            self.assertEqual(result['exit_code'], 0 if code == 0 else 1)
            self.assertTrue(result['details']['ddr_readback_matches'])

    def test_missing_terminal_and_false_pass_time_out(self):
        result = self.session(START + b'PASS\r\n', deadline=.7)
        self.assertEqual(result['execution_status'], 'timeout')
        self.assertIsNone(result['app_exit_code'])

    def test_missing_readback_cannot_pass(self):
        result = self.session(START + terminal(), readback=False)
        self.assertEqual(result['execution_status'], 'platform_error')

    def test_cleanup_failure_overrides_application_success(self):
        result = self.session(START + terminal(), tool_error=True)
        self.assertEqual(result['execution_status'], 'platform_error')
        self.assertEqual(result['app_exit_code'], 0)
        self.assertNotEqual(result['exit_code'], 0)


class MetadataTests(unittest.TestCase):
    def test_only_authenticated_agent_metadata_supplies_application_result(self):
        backend = BOSC((Node('a', (4,), root='/runtime'),))
        payload = dict(id='a' * 32, sha256='0' * 64, timeout=3, idle_seconds=1)
        metadata = dict(id=payload['id'], execution_status='exited', app_exit_code=7,
                        exit_code=1, details={}, uart_output='verify hello: PASS\n')
        with patch.object(backend, 'remote', return_value=dict(exit_code=1, seconds=1, node='a',
                output='logs\n[scheduler-result] ' + json.dumps(metadata))):
            result = backend.run(payload, 'a', 4)
            self.assertEqual(result['app_exit_code'], 7)
            self.assertEqual(result['uart_output'], 'verify hello: PASS\n')
        with patch.object(backend, 'remote', return_value=dict(exit_code=0, seconds=1, node='a', output='PASS')):
            result = backend.run(payload, 'a', 4)
            self.assertEqual(result['execution_status'], 'protocol_error')
            self.assertEqual(result['exit_code'], 1)

    def test_transport_success_cannot_hide_a_nonzero_application_code(self):
        backend = BOSC((Node('a', (4,), root='/runtime'),))
        payload = dict(id='a' * 32, sha256='0' * 64, timeout=3, idle_seconds=1)
        metadata = dict(id=payload['id'], execution_status='exited', app_exit_code=7,
                        exit_code=0, details={})
        with patch.object(backend, 'remote', return_value=dict(exit_code=0, seconds=1, node='a',
                output='[scheduler-result] ' + json.dumps(metadata))):
            with self.assertRaises(ValueError):
                backend.run(payload, 'a', 4)


if __name__ == '__main__':
    unittest.main()
