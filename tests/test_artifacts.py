"""DDR output contract, real Tcl/PTY capture, authenticated download and recovery."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from fpga_scheduler import Client, SchedulerError
from fpga_scheduler.artifacts import validate_artifacts
from fpga_scheduler.cli import main
from fpga_scheduler.config import load
from fpga_scheduler.remote.runner import (
    Completion, MAX_OUTPUT_BYTES, MAX_OUTPUTS, ProtocolError, output_declaration)

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
from fpga_sim_support.runtime import create_application
from fpga_sim_support.scenarios import create_environment, write_image

START = b'@@FPGA_START {"version":1}\n'
RESULT = b'@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":0}\n'


def declaration(**changes):
    return {**dict(version=1, name='C', address='0x80200404', nbytes=24, dtype='<i4',
                   shape=[2, 3]), **changes}


def record(value):
    return ('@@FPGA_OUTPUT ' + json.dumps(value) + '\n').encode()


class DeclarationTests(unittest.TestCase):
    def test_sdk_import_does_not_require_hardware_or_posix_uart_modules(self):
        code = '''
import sys
class WithoutUART:
    def find_spec(self, name, *args):
        if name in ('fcntl', 'pty', 'termios'):
            raise ImportError('POSIX UART unavailable')
sys.meta_path.insert(0, WithoutUART())
from fpga_scheduler import Client
from fpga_scheduler.artifacts import validate_artifacts
assert validate_artifacts([]) == []
'''
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)

    def test_admin_ddr_window_and_aggregate_configuration_bounds(self):
        base = (PROJECT / 'configs/resident.example.toml').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            init = Path(tmp) / 'init'
            init.mkdir()
            for number in (4, 5, 6, 7):
                (init / f'init{number}.tcl').write_text('# configuration fixture\n')
            path.write_text(base)
            resident = load(path).resident
            self.assertEqual(resident['output_ddr_base'], 0x80000000)
            self.assertEqual(resident['output_ddr_bytes'], 4194304)
            self.assertEqual(resident['output_ddr_read_mode'], 'prefix')
            self.assertIs(resident['verify_readback'], True)
            self.assertEqual(resident['load_timeout'], 600)
            self.assertEqual(resident['max_output_bytes'], 16777216)
            # C3 bounds: nonzero/any-64-aligned base inside 4 GiB, 64 B..2 GiB span,
            # prefix|window mode, bool readback, load_timeout 0<t<=86400 and a
            # 256 MiB max_output_bytes deployment cap.
            for before, after, expected in (
                    ('output_ddr_base = 0x80000000', 'output_ddr_base = 0x80000040',
                     {'output_ddr_base': 0x80000040}),
                    ('output_ddr_base = 0x80000000', 'output_ddr_base = 0x0',
                     {'output_ddr_base': 0}),
                    ('output_ddr_base = 0x80000000', 'output_ddr_base = 0xa6e00000',
                     {'output_ddr_base': 0xa6e00000}),  # covers the 87.3 MiB logits address
                    ('output_ddr_base = 0x80000000', 'output_ddr_base = 0xffc00000',
                     {'output_ddr_base': 0xffc00000}),  # base + span == 4 GiB exactly
                    ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 64',
                     {'output_ddr_bytes': 64}),
                    ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 268435520',
                     {'output_ddr_bytes': 268435520}),  # 256 MiB + 64, within the 2 GiB span
                    ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 652214464',
                     {'output_ddr_bytes': 652214464}),  # covers physical 0xa6e00080 from base
                    ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 2147483648',
                     {'output_ddr_bytes': 2147483648}),  # 2 GiB span; base ends at exactly 4 GiB
                    ('output_ddr_read_mode = "prefix"', 'output_ddr_read_mode = "window"',
                     {'output_ddr_read_mode': 'window'}),
                    ('verify_readback = true', 'verify_readback = false',
                     {'verify_readback': False}),
                    ('load_timeout = 600', 'load_timeout = 86400', {'load_timeout': 86400}),
                    ('load_timeout = 600', 'load_timeout = 0.5', {'load_timeout': 0.5}),
                    ('max_output_bytes = 16777216', 'max_output_bytes = 268435456',
                     {'max_output_bytes': 268435456})):
                with self.subTest(after=after):
                    path.write_text(base.replace(before, after))
                    configured = load(path).resident
                    self.assertEqual({key: configured[key] for key in expected}, expected)
            for before, after in (('output_ddr_base = 0x80000000', 'output_ddr_base = true'),
                                  ('output_ddr_base = 0x80000000', 'output_ddr_base = true_value'),
                                  ('output_ddr_base = 0x80000000', 'output_ddr_base = 65'),
                                  ('output_ddr_base = 0x80000000', 'output_ddr_base = 1'),
                                  ('output_ddr_base = 0x80000000', 'output_ddr_base = -64'),
                                  ('output_ddr_base = 0x80000000', 'output_ddr_base = 0x100000000'),
                                  ('output_ddr_base = 0x80000000', 'output_ddr_base = 0xffc00040'),
                                  ('output_ddr_base = 0x80000000', 'output_ddr_base = 0x7fffffffc0'),
                                  ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 0'),
                                  ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 32'),
                                  ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 65'),
                                  ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 2147483712'),
                                  ('output_ddr_bytes = 4194304', 'output_ddr_bytes = 64.0'),
                                  ('output_ddr_bytes = 4194304', 'output_ddr_bytes = "4194304"'),
                                  ('output_ddr_bytes = 4194304', 'output_ddr_bytes = true'),
                                  ('output_ddr_read_mode = "prefix"', 'output_ddr_read_mode = "slice"'),
                                  ('output_ddr_read_mode = "prefix"', 'output_ddr_read_mode = "PREFIX"'),
                                  ('output_ddr_read_mode = "prefix"', 'output_ddr_read_mode = "window "'),
                                  ('output_ddr_read_mode = "prefix"', 'output_ddr_read_mode = 1'),
                                  ('verify_readback = true', 'verify_readback = 0'),
                                  ('verify_readback = true', 'verify_readback = 1'),
                                  ('verify_readback = true', 'verify_readback = "true"'),
                                  ('load_timeout = 600', 'load_timeout = 0'),
                                  ('load_timeout = 600', 'load_timeout = -1'),
                                  ('load_timeout = 600', 'load_timeout = 86401'),
                                  ('load_timeout = 600', 'load_timeout = "600"'),
                                  ('load_timeout = 600', 'load_timeout = true'),
                                  ('max_output_bytes = 16777216', 'max_output_bytes = 0'),
                                  ('max_output_bytes = 16777216', 'max_output_bytes = -1'),
                                  ('max_output_bytes = 16777216', 'max_output_bytes = 268435457'),
                                  ('max_output_bytes = 16777216', 'max_output_bytes = 16777216.0'),
                                  ('max_output_bytes = 16777216', 'max_output_bytes = true')):
                with self.subTest(after=after):
                    path.write_text(base.replace(before, after))
                    with self.assertRaises(ValueError):
                        load(path)
            # 2 GiB span plus a nonzero base exceeds the 4 GiB address space.
            path.write_text(base.replace('output_ddr_base = 0x80000000', 'output_ddr_base = 0x80000040')
                            .replace('output_ddr_bytes = 4194304', 'output_ddr_bytes = 2147483648'))
            with self.assertRaisesRegex(ValueError, '4 GiB'):
                load(path)

    def test_fragmentation_and_strict_record_order(self):
        parser = Completion()
        data = START + record(declaration()) + RESULT
        for byte in data:
            parser.feed(bytes([byte]))
        parser.finish()
        self.assertEqual(parser.outputs, [declaration()])
        for data in (record(declaration()), START + RESULT + record(declaration()),
                     START + record(declaration()) * 2, START + b'@@FPGA_OUTPUT {"version":1,"version":1}\n'):
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                Completion().feed(data)
        parser = Completion()
        parser.feed(START + record(declaration())[:-1])
        with self.assertRaises(ProtocolError):
            parser.finish()

    def test_reject_invalid_addresses_names_shapes_and_limits(self):
        for changes in (dict(name='../C'), dict(name='C.bin'), dict(name=''),
                        dict(address='0x10000000000000000'), dict(address='0xffffffffffffffff'),
                        dict(nbytes=True), dict(nbytes=-4), dict(dtype='object'),
                        dict(shape=[2, True]), dict(shape=[]), dict(shape=[2, 4]),
                        dict(version=True), dict(extra=1)):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                output_declaration(declaration(**changes))

    @unittest.skipUnless(shutil.which('cc'), 'host C compiler required')
    def test_c_helper_emits_real_descriptor_address_and_rejects_strides(self):
        source = r'''
#include <stdio.h>
#include <stdint.h>
#define FPGA_OUTPUT_PUTS(s) fputs((s), stdout)
#include "fpga_output.h"
typedef struct { void *allocated, *aligned; int64_t offset, sizes[2], strides[2]; } MemRef2;
int main(void) {
  void *p = (void *)(uintptr_t)0x80200400;
  MemRef2 c = {p, p, 1, {2,3}, {3,1}};
  if (fpga_export_memref2_i32("C", &c)) return 1;
  c.strides[0] = 4;
  if (fpga_export_memref2_i32("invalid", &c) != -1) return 2;
  c.strides[0] = 3;
  if (fpga_export_memref2_i32("../bad", &c) != -1) return 3;
  return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'main.c').write_text(source)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-I' + str(PROJECT / 'runtime/include'), str(root / 'main.c'),
                            '-o', str(root / 'main')], check=True, capture_output=True)
            data = subprocess.check_output([str(root / 'main')])
        parser = Completion()
        parser.feed(START + data + RESULT)
        parser.finish()
        self.assertEqual(parser.outputs, [declaration()])


class ProtocolLimitTests(unittest.TestCase):
    """C3: the UART protocol ceiling is 256 MiB; the deployment cap may be lower."""

    @staticmethod
    def output(index=0, nbytes=4, dtype='|u1', shape=None):
        shape = [nbytes] if shape is None else shape
        return dict(version=1, name=f'O{index}', address=hex(0x80000000 + index * 0x10000000),
                    nbytes=nbytes, dtype=dtype, shape=shape)

    @classmethod
    def record(cls, index=0, **fields):
        return ('@@FPGA_OUTPUT ' + json.dumps(cls.output(index, **fields)) + '\n').encode()

    @staticmethod
    def artifact(index, nbytes):
        value = ProtocolLimitTests.output(index, nbytes=nbytes)
        value.pop('version')
        value['sha256'] = '0' * 64
        return value

    def test_named_protocol_ceiling_is_256_mib_and_output_count_stays_16(self):
        self.assertEqual(MAX_OUTPUT_BYTES, 256 * 1024 * 1024)
        self.assertEqual(MAX_OUTPUTS, 16)

    def test_single_declaration_beyond_the_old_16_mib_limit_is_accepted(self):
        ceiling = MAX_OUTPUT_BYTES
        for size in (16 * 1024 * 1024 + 1, 64 * 1024 * 1024, ceiling - 1, ceiling):
            with self.subTest(size=size):
                self.assertEqual(output_declaration(self.output(nbytes=size))['nbytes'], size)
        for changes in (dict(nbytes=ceiling + 1), dict(nbytes=ceiling + 64),
                        dict(nbytes=8, dtype='<f8', shape=[ceiling // 8 + 1])):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                output_declaration(self.output(**changes))

    def test_shape_dimension_ceiling_matches_the_byte_ceiling(self):
        self.assertEqual(output_declaration(self.output(nbytes=MAX_OUTPUT_BYTES))['shape'],
                         [MAX_OUTPUT_BYTES])
        # 64 MiB of <f8 is 8,388,608 elements: well past the old 16 MiB element bound.
        value = self.output(nbytes=64 * 1024 * 1024, dtype='<f8', shape=[8 * 1024 * 1024])
        self.assertEqual(output_declaration(value)['nbytes'], 64 * 1024 * 1024)
        for shape in ([MAX_OUTPUT_BYTES + 1], [2 ** 40]):
            with self.subTest(shape=shape), self.assertRaises(ProtocolError):
                output_declaration(self.output(nbytes=4, shape=shape))

    def test_completion_accepts_256_mib_aggregate_and_rejects_one_byte_more(self):
        start = b'@@FPGA_START {"version":1}\n'
        half = MAX_OUTPUT_BYTES // 2
        parser = Completion()
        parser.feed(start + self.record(0, nbytes=half) + self.record(1, nbytes=half))
        self.assertEqual([item['nbytes'] for item in parser.outputs], [half, half])
        parser = Completion()
        parser.feed(start + self.record(0, nbytes=MAX_OUTPUT_BYTES))
        self.assertEqual(parser.outputs[0]['nbytes'], MAX_OUTPUT_BYTES)
        parser = Completion()
        with self.assertRaisesRegex(ProtocolError, 'Output count/total byte limit exceeded'):
            parser.feed(start + self.record(0, nbytes=half) + self.record(1, nbytes=half)
                        + self.record(2, nbytes=1))

    def test_completion_keeps_the_16_output_limit(self):
        start = b'@@FPGA_START {"version":1}\n'
        data = start + b''.join(self.record(index, nbytes=1) for index in range(MAX_OUTPUTS))
        parser = Completion()
        parser.feed(data)
        self.assertEqual(len(parser.outputs), MAX_OUTPUTS)
        parser = Completion()
        with self.assertRaisesRegex(ProtocolError, 'Output count/total byte limit exceeded'):
            parser.feed(data + self.record(MAX_OUTPUTS, nbytes=1))

    def test_artifact_manifest_uses_the_same_ceiling(self):
        half = MAX_OUTPUT_BYTES // 2
        items = [self.artifact(0, half), self.artifact(1, half)]
        self.assertIs(validate_artifacts(items), items)
        self.assertEqual(validate_artifacts([self.artifact(0, 16 * 1024 * 1024 + 4)])[0]['nbytes'],
                         16 * 1024 * 1024 + 4)
        self.assertEqual(validate_artifacts([self.artifact(0, MAX_OUTPUT_BYTES)])[0]['nbytes'],
                         MAX_OUTPUT_BYTES)
        with self.assertRaisesRegex(ValueError, 'Artifact total byte limit exceeded'):
            validate_artifacts(items + [self.artifact(2, 1)])
        with self.assertRaises(ValueError):
            validate_artifacts([self.artifact(0, MAX_OUTPUT_BYTES + 1)])
        with self.assertRaisesRegex(ValueError, 'Invalid artifact manifest'):
            validate_artifacts([self.artifact(index, 1) for index in range(MAX_OUTPUTS + 1)])


@unittest.skipUnless(shutil.which('tclsh'), 'Tcl required for offline DDR capture')
class ArtifactHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        create_environment(self.root / 'sim', ports=(4, 5))
        self.app = create_application(self.root / 'sim', port=0)
        self.endpoint = 'http://127.0.0.1:' + str(self.app.server.server_address[1])
        self.client = Client(self.endpoint, token_file=self.root / 'sim/service.token', request_timeout=3)
        self.app.queue.start()
        self.thread = threading.Thread(target=self.app.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.index = 0

    def tearDown(self):
        self.client.close()
        self.app.server.shutdown()
        self.thread.join()
        self.app.close()

    def image(self, values=(1, -2, 3, -2147483648, 2147483647, 0), **options):
        self.index += 1
        data = struct.pack('<6i', *values)
        path = self.root / f'{self.index}.bin'
        write_image(path, outputs=[dict(declaration(), data=data.hex())], duration=.04, **options)
        return path, data

    def run_image(self, path):
        with self.client.run_test(path, timeout=2, poll_interval=.01) as stream:
            list(stream)
        return stream

    def test_two_slots_same_name_exact_bytes_immutable_download_cli_and_recovery(self):
        first, a = self.image()
        second, b = self.image(values=(7, 8, 9, -10, 11, -12))
        tasks = [self.client.submit(path, timeout=2) for path in (first, second)]
        done = [self.client.wait(task.id, timeout=5, poll_interval=.01) for task in tasks]
        self.assertEqual({t.fpga for t in done}, {4, 5})
        for task, data in zip(done, (a, b)):
            self.assertTrue(task.result.ok, task.result)
            self.assertEqual(task.result.artifacts[0]['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(task.result.artifacts, self.client.artifacts(task.id))
            local = self.client.download_artifact(task.id, 'C', self.root / f'{task.id}.bin')
            self.assertEqual(local.read_bytes(), data)
            row = dict(self.app.queue.db.execute('SELECT * FROM jobs WHERE id=?', (task.id,)).fetchone())
            self.assertEqual(self.app.backend.recover(row)['artifacts'], task.result.artifacts)
        # A later job writes the same DDR address. Earlier downloads stay exact.
        third, _ = self.image(values=(3,) * 6)
        self.assertTrue(self.run_image(third).result.ok)
        target = self.client.download_artifact(tasks[0].id, 'C', self.root / 'again.bin')
        self.assertEqual(target.read_bytes(), a)
        with redirect_stdout(io.StringIO()) as output:
            status = main(['--endpoint', self.endpoint, '--token-file', str(self.root / 'sim/service.token'),
                           'download', tasks[0].id, 'C', str(self.root / 'cli.bin')])
        self.assertEqual(status, 0, output.getvalue())
        self.assertIn('SHA256 verified', output.getvalue())
        self.assertEqual((self.root / 'cli.bin').read_bytes(), a)

    def test_completion_gate_auth_path_isolation_and_missing_files(self):
        image, _ = self.image(mode='hang')
        task = self.client.submit(image, timeout=1)
        with self.assertRaises(SchedulerError) as error:
            self.client.artifacts(task.id)
        self.assertEqual(error.exception.status, 409)
        finished = self.client.wait(task.id, timeout=4, poll_interval=.01)
        self.assertEqual(finished.result.execution_status, 'timeout')
        self.assertEqual(self.client.artifacts(task.id), [])
        self.assertFalse((self.app.backend.root / 'jobs' / task.id / 'artifacts').exists())
        opener = build_opener(ProxyHandler({}))
        for suffix in ('artifacts', 'artifacts/C'):
            with self.assertRaises(HTTPError) as error:
                opener.open(Request(self.endpoint + '/jobs/' + task.id + '/' + suffix,
                                    headers={'Authorization': 'Bearer wrong'}), timeout=2)
            self.assertEqual(error.exception.code, 401)
        with self.assertRaises(ValueError):
            self.client.download_artifact(task.id, '../C', self.root / 'bad.bin')
        with self.assertRaises(SchedulerError) as error:
            self.client.download_artifact(task.id, 'C', self.root / 'none.bin')
        self.assertEqual(error.exception.status, 404)

    def test_corrupt_saved_file_detected_destination_preserved_and_recovery_rejected(self):
        path, data = self.image()
        stream = self.run_image(path)
        artifact = self.app.backend.artifact_path(stream.id, 'C')
        artifact.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        target = self.root / 'keep.bin'
        target.write_bytes(b'original')
        before = set(self.root.iterdir())
        with self.assertRaisesRegex(SchedulerError, 'SHA256'):
            self.client.download_artifact(stream.id, 'C', target)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(before, set(self.root.iterdir()))
        row = dict(self.app.queue.db.execute('SELECT * FROM jobs WHERE id=?', (stream.id,)).fetchone())
        self.assertIsNone(self.app.backend.recover(row))

    def test_multiple_outputs_cannot_collide_with_capture_temporary_files(self):
        path = self.root / 'multiple.bin'
        write_image(path, duration=.04, outputs=[
            dict(declaration(name='snapshot'), data=struct.pack('<6i', *range(6)).hex()),
            dict(declaration(name='manifest', address='0x80200500'), data=struct.pack('<6i', *range(7,13)).hex()),
        ])
        stream = self.run_image(path)
        self.assertTrue(stream.result.ok, stream.result)
        for name, values in (('snapshot', range(6)), ('manifest', range(7,13))):
            output = self.client.download_artifact(stream.id, name, self.root / (name + '.bin'))
            self.assertEqual(output.read_bytes(), struct.pack('<6i', *values))

    def test_outside_ddr_limits_and_capture_failure_cannot_report_success(self):
        path, _ = self.image()
        # Application emits an invalid physical range; no UVHS read may occur.
        injected = self.root / 'outside.bin'
        write_image(injected, output=record(declaration(address='0x1000')).decode(), duration=.03)
        stream = self.run_image(injected)
        self.assertFalse(stream.result.ok)
        self.assertEqual(stream.result.app_exit_code, 0)
        self.assertIn('DDR window', stream.result.details['artifact_error'])
        time.sleep(.15)
        self.app.backend.probe(self.app.backend.node.id)
        # A known captured DDR snapshot fails; app success stays separate.
        for session in self.app.backend.sessions.values():
            session.command('rename readmem original_readmem\nproc readmem {args} {'
                            'if {$::released} {error "injected output read failure"}; original_readmem {*}$args}')
        stream = self.run_image(path)
        self.assertFalse(stream.result.ok)
        self.assertEqual(stream.result.app_exit_code, 0)
        self.assertEqual(stream.result.artifacts, [])
        self.assertIn('artifact_error', stream.result.details)

    def test_retention_keeps_outputs_with_task_then_removes_them(self):
        path, _ = self.image()
        stream = self.run_image(path)
        artifact = self.app.backend.artifact_path(stream.id, 'C')
        self.app.backend.cleanup(time.time() + 100)
        self.assertTrue(artifact.exists())
        with self.app.queue.lock, self.app.queue.db:
            self.app.queue.db.execute('DELETE FROM jobs WHERE id=?', (stream.id,))
        self.app.queue.images.cleanup(time.time() + 100)
        self.app.backend.cleanup(time.time() + 100)
        self.assertFalse(artifact.exists())


@unittest.skipUnless(shutil.which('tclsh'), 'Tcl required for offline DDR capture')
class SimDDRReadmemTests(unittest.TestCase):
    """The software UVHS stores a 64-byte RTL element per i_ddr index; both read
    modes (prefix i_ddr[hi:0] and window i_ddr[hi:lo]) must slice that image."""

    SCRIPT = PROJECT / 'tools/fpga_sim_support/uvhs.tcl'

    def readmem(self, rtl, elements=8):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = bytes((index * 37 + 11) % 256 for index in range(elements * 64))
            (root / 'sim-ddr.bin').write_bytes(image)
            target = root / 'read.bin'
            script = '\n'.join((
                'set loaded {' + str(root / 'image.bin') + '}',
                'set released 1',
                'readmem -rtl {' + rtl + '} -file {' + str(target) + '} -file_type bin',
            )) + '\n'
            process = subprocess.run(['tclsh', str(self.SCRIPT), '-workdir', str(root)],
                                     input=script, text=True, capture_output=True, timeout=30)
            data = target.read_bytes() if target.is_file() else None
            return process, data, image

    def test_prefix_i_ddr_hi_0_reads_elements_from_the_start(self):
        process, data, image = self.readmem('nanhu_xilinx.i_ddr[2:0]')
        self.assertEqual(process.stderr, '')
        self.assertEqual(data, image[:3 * 64])

    def test_window_i_ddr_hi_lo_slices_stored_elements(self):
        for high, low in ((2, 1), (7, 4), (5, 5)):
            with self.subTest(high=high, low=low):
                process, data, image = self.readmem(
                    f'nanhu_xilinx.i_ddr[{high}:{low}]')
                self.assertEqual(process.stderr, '')
                self.assertEqual(data, image[low * 64:(high + 1) * 64])

    def test_invalid_memory_range_reports_an_error_without_writing(self):
        process, data, _ = self.readmem('nanhu_xilinx.i_ddr[2]')
        self.assertIn('invalid memory range', process.stderr)
        self.assertIsNone(data)


class DeploymentCapTests(unittest.TestCase):
    """256 MiB is only the protocol ceiling; resident.max_output_bytes caps exports."""

    def session(self, directory, **options):
        from fpga_scheduler.remote.uvhs_session import Session
        return Session(None, 4, directory, object(), **options)

    def test_capture_enforces_the_configured_deployment_cap(self):
        declaration = dict(version=1, name='C', address='0x80000000',
                           nbytes=16 * 1024 * 1024 + 1, dtype='|u1',
                           shape=[16 * 1024 * 1024 + 1])
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            session = self.session(directory, max_output_bytes=16 * 1024 * 1024)
            try:
                self.assertEqual(session.max_output, 16 * 1024 * 1024)
                with self.assertRaisesRegex(ValueError, 'max_output_bytes'):
                    session.capture_outputs([declaration], directory)
            finally:
                session.control.close()

    def test_unconfigured_session_defaults_to_the_protocol_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            session = self.session(directory)
            try:
                self.assertEqual(session.max_output, MAX_OUTPUT_BYTES)
            finally:
                session.control.close()
