"""Spurious UART readiness is retryable; EOF and real I/O errors stay fatal."""
import errno
import hashlib
import itertools
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fpga_scheduler.config import Node
from fpga_scheduler.remote.uvhs_session import Session


MODULE = 'fpga_scheduler.remote.uvhs_session'
COMPLETION = (b'@@FPGA_START {"version":1}\n'
              b'@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":0}\n')


def again():
    return BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable')


class NonblockingUartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = Session(Node('local', (4,)), 4, self.root / 'session', None,
                               verify_readback=False, uart_quiet_seconds=0)
        self.addCleanup(self.session.control.close)
        self.session.serial, self.session.master = 101, 102
        self.session.process = SimpleNamespace(pid=1234, poll=Mock(return_value=None))
        self.image = self.root / 'input.bin'
        self.image.write_bytes(b'program')
        self.payload = dict(id='1' * 32, sha256=hashlib.sha256(b'program').hexdigest(), timeout=1)

    def execute(self, reads, *, control_ready=False):
        def ready(fds, *_):
            return (fds if fds == [self.session.serial] or control_ready else [], [], [])

        self.session.reset = Mock(return_value=True)
        self.session.boundary = Mock(return_value=0)
        self.session.memory_command = Mock()
        self.session.command = Mock(return_value=b'')
        with patch(MODULE + '.select.select', side_effect=ready), \
                patch(MODULE + '.os.read', side_effect=reads) as read:
            result, healthy = self.session.execute(self.payload, self.image, self.root / 'job')
        return result, healthy, read

    def test_read_helper_only_retries_would_block(self):
        for code in (errno.EAGAIN, errno.EWOULDBLOCK):
            with self.subTest(errno=code), patch(MODULE + '.os.read', side_effect=OSError(code, 'retry')):
                self.assertIsNone(self.session._read_uart())
        for code in (errno.EIO, errno.EBADF, errno.EINTR):
            failure = OSError(code, 'real fault')
            with self.subTest(errno=code), patch(MODULE + '.os.read', side_effect=failure):
                with self.assertRaises(OSError) as raised:
                    self.session._read_uart()
                self.assertIs(raised.exception, failure)
        with patch(MODULE + '.os.read', return_value=b''):
            with self.assertRaisesRegex(RuntimeError, 'UART disconnected'):
                self.session._read_uart()

    def test_quiet_boundary_completes_despite_repeated_spurious_readiness(self):
        self.session.quiet = .03
        chunks = iter([b'stale task bytes'])

        def read(*_):
            chunk = next(chunks, None)
            if chunk is None:
                raise again()
            return chunk

        ticks = itertools.count(0, .005)
        with patch(MODULE + '.select.select', return_value=([101], [], [])), \
                patch(MODULE + '.os.read', side_effect=read) as uart_read, \
                patch(MODULE + '.time.monotonic', side_effect=lambda: next(ticks)), \
                patch(MODULE + '.termios.tcflush') as flush:
            self.assertEqual(self.session.boundary(), len(b'stale task bytes'))
        self.assertGreater(uart_read.call_count, 1)
        self.assertLess(uart_read.call_count, 20)
        flush.assert_called_once()

    def test_boundary_preserves_disconnect_and_other_read_errors(self):
        for failure in (b'', OSError(errno.EIO, 'device fault')):
            with self.subTest(failure=failure), \
                    patch(MODULE + '.select.select', return_value=([101], [], [])), \
                    patch(MODULE + '.os.read', side_effect=[failure]), \
                    patch(MODULE + '.termios.tcflush') as flush:
                with self.assertRaises((OSError, RuntimeError)):
                    self.session.boundary()
                flush.assert_not_called()

    def test_execution_resumes_after_eagain_and_cleanup_reaches_quiet_boundary(self):
        result, healthy, read = self.execute([again(), COMPLETION, again()])
        self.assertEqual(result['execution_status'], 'exited', result)
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(healthy)
        self.assertEqual(result['uart_output'], COMPLETION.decode())
        self.assertEqual(read.call_count, 3)
        self.assertEqual(self.session.boundary.call_count, 3)
        self.assertEqual(self.session.process.poll.call_count, 2)
        self.assertNotIn('cleanup_error', result['details'])

    def test_eagain_does_not_skip_control_channel_fault_checks(self):
        self.session._tool = Mock(return_value=b'ERROR: link failed\n')
        result, healthy, _ = self.execute([again(), again()], control_ready=True)
        self.assertEqual(result['execution_status'], 'platform_error')
        self.assertEqual(result['details']['error'], 'UVHS error during execution')
        self.assertFalse(healthy)
        self.session._tool.assert_called_once_with(0)

    def test_repeated_eagain_still_obeys_application_deadline(self):
        self.payload['timeout'] = .005

        def unavailable(*_):
            raise again()

        result, healthy, read = self.execute(unavailable)
        self.assertEqual(result['execution_status'], 'timeout', result)
        self.assertEqual(result['exit_code'], 124)
        self.assertTrue(healthy)
        self.assertGreater(read.call_count, 1)
        self.assertNotIn('cleanup_error', result['details'])

    def test_real_execution_fault_is_not_changed_to_a_retry(self):
        for failure in (b'', OSError(errno.EIO, 'device fault')):
            with self.subTest(failure=failure):
                result, healthy, _ = self.execute([failure, again()])
                self.assertEqual(result['execution_status'], 'platform_error')
                self.assertFalse(healthy)
                self.assertIn('UART disconnected' if failure == b'' else 'device fault',
                              result['details']['error'])

    def test_real_cleanup_fault_overrides_success(self):
        for failure in (b'', OSError(errno.EIO, 'device fault')):
            with self.subTest(failure=failure):
                result, healthy, _ = self.execute([COMPLETION, failure])
                self.assertEqual(result['execution_status'], 'platform_error')
                self.assertEqual(result['details']['interrupted_status'], 'exited')
                self.assertFalse(healthy)
                self.assertIn('UART disconnected' if failure == b'' else 'device fault',
                              result['details']['cleanup_error'])


if __name__ == '__main__':
    unittest.main()
