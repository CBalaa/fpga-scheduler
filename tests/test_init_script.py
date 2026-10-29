"""Pure profile validation of deployment initialization Tcl; no UVHS required."""
from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fpga_scheduler.remote.init_script import InitScript, load_init_script


class InitScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'init4.tcl'
        self.base = ('# source host/path/version recorded by deployer\n'
                     'query -user\nquery -fpgas -all\nquery -version\n'
                     'allocate -board {b1.f0}\nload_db -release\nload_db -db ./hw.dat\n'
                     'config -connector\nquery -connector -type fmc\nquery -voltage\n'
                     'reset -name cpu_reset -value 1\nquery -reset\n'
                     'download\nquery -ipinfo\ninitialize\nquery -reset\n')

    def load(self, text=None, number=4):
        self.path.write_text(self.base if text is None else text)
        return load_init_script(self.path, number)

    def test_fpga113_order_and_original_content_are_preserved(self):
        original = self.base.replace('\n', '\r\n').encode('utf-8')
        self.path.write_bytes(original)
        result = load_init_script(self.path, 4)
        self.assertIsInstance(result, InitScript)
        self.assertEqual(result.path, self.path)
        self.assertEqual(result.content, original)
        self.assertEqual(result.sha256, hashlib.sha256(original).hexdigest())
        self.assertFalse(result.configures_clock)
        with self.assertRaises(FrozenInstanceError):
            result.content = b'changed'

    def test_nonallocation_style_and_larger_boards_keep_physical_numbering(self):
        text = self.base.replace('allocate -board {b1.f0}\n', '').replace(
            'load_db -db ./hw.dat', 'load_db -db {./hw.dat} -to {B10.F3}')
        result = self.load(text, 43)
        self.assertEqual(result.content, text.encode())
        self.assertFalse(result.configures_clock)

    def test_allocation_after_release_is_also_preserved(self):
        text = self.base.replace('allocate -board {b1.f0}\nload_db -release',
                                 'load_db -release\nallocate -board b1.f0')
        self.assertEqual(self.load(text).content, text.encode())

    def test_literal_and_variable_clock_forms(self):
        forms = ('config -clock -frequency 5529600 sys_clk\n',
                 'set sys_clk_hz {5529600}\nconfig -clock -frequency $sys_clk_hz sys_clk\n')
        for form in forms:
            text = self.base.replace('reset -name', form + 'config -clock -commit\nquery -clock\nreset -name')
            with self.subTest(form=form):
                loaded = self.load(text)
                self.assertTrue(loaded.configures_clock)
                self.assertEqual(loaded.content, text.encode())

    def test_reference_environment_block_one_or_multiple_lines(self):
        for body in ('if {[info exists ::env(UV_RUN_SYS_CLK_HZ)]} {\n'
                     '  set sys_clk_hz $::env(UV_RUN_SYS_CLK_HZ)\n}\n',
                     'if {[info exists ::env(UV_RUN_SYS_CLK_HZ)]} { set sys_clk_hz $::env(UV_RUN_SYS_CLK_HZ) }\n'):
            clock = ('set sys_clk_hz 5529600\n' + body +
                     'config -clock -frequency $sys_clk_hz sys_clk\nconfig -clock -commit\n')
            text = self.base.replace('reset -name', clock + 'reset -name')
            with self.subTest(body=body), patch.dict(os.environ, {}, clear=True):
                self.assertTrue(self.load(text).configures_clock)
            with patch.dict(os.environ, UV_RUN_SYS_CLK_HZ='5529600'):
                self.assertEqual(self.load(text).content, text.encode())
            for value in ('11059200', '', '5.5296e6', '5529600; reset -name cpu_reset -value 0'):
                with self.subTest(value=value), patch.dict(os.environ, UV_RUN_SYS_CLK_HZ=value):
                    with self.assertRaisesRegex(ValueError, 'UV_RUN_SYS_CLK_HZ=.*incompatible'):
                        self.load(text)

    def test_unreferenced_environment_does_not_override_literal_clock(self):
        text = self.base.replace('reset -name', 'config -clock -frequency 5529600 sys_clk\n'
                                'config -clock -commit\nreset -name')
        with patch.dict(os.environ, UV_RUN_SYS_CLK_HZ='11059200'):
            self.assertTrue(self.load(text).configures_clock)

    def test_forbidden_commands_and_dynamic_tcl_have_location(self):
        forbidden = ('writemem -file image.bin i_ddr', 'readmem -file read.bin i_ddr',
                     'source ./user_script/hw_run/write_image.tcl', 'exit', 'exec uname',
                     'reset -name cpu_reset -value 0', 'eval {query -reset}',
                     'query -reset; reset -name cpu_reset -value 0', 'query -reset [exit]',
                     'set sys_clk_hz $arbitrary', 'if {1} {exit}', 'while {1} {query -reset}',
                     'puts "done"', 'query -reset\\nexit', 'source {anything.tcl}')
        for command in forbidden:
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, r'init4\.tcl:[0-9]+:'):
                self.load(self.base + command + '\n')

    def test_wrong_target_and_combined_allocation_styles_rejected(self):
        variants = (self.base.replace('b1.f0', 'b1.f1'),
                    self.base.replace('b1.f0', '$target'),
                    self.base.replace('load_db -db ./hw.dat', 'load_db -db ./hw.dat -to {b1.f0}'),
                    self.base.replace('allocate -board {b1.f0}\n', ''),
                    self.base.replace('load_db -db ./hw.dat', 'load_db -db /different/hw.dat'))
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_missing_duplicate_and_out_of_order_steps_rejected(self):
        variants = [self.base.replace(command + '\n', '', 1)
                    for command in ('load_db -release', 'load_db -db ./hw.dat',
                                    'reset -name cpu_reset -value 1', 'download', 'initialize')]
        variants += [self.base + command + '\n' for command in
                     ('allocate -board {b1.f0}', 'load_db -release', 'download', 'initialize')]
        variants += [self.base.replace('download\nquery -ipinfo\ninitialize', 'initialize\nquery -ipinfo\ndownload'),
                     self.base.replace('load_db -release\nload_db -db ./hw.dat',
                                       'load_db -db ./hw.dat\nload_db -release')]
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_clock_mismatch_incomplete_pair_and_bad_variable_rejected(self):
        clocks = ('config -clock -frequency 11059200 sys_clk\nconfig -clock -commit\n',
                  'config -clock -frequency 5529600 other_clk\nconfig -clock -commit\n',
                  'config -clock -frequency 5529600 sys_clk\n', 'config -clock -commit\n',
                  'config -clock -commit\nconfig -clock -frequency 5529600 sys_clk\n',
                  'config -clock -frequency $sys_clk_hz sys_clk\nconfig -clock -commit\n',
                  'set sys_clk_hz 5529600\nconfig -clock -frequency {$sys_clk_hz} sys_clk\nconfig -clock -commit\n')
        for clock in clocks:
            text = self.base.replace('reset -name', clock + 'reset -name')
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                self.load(text)

    def test_missing_relative_empty_and_non_utf8_files_rejected(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            load_init_script(Path('init4.tcl'), 4)
        with self.assertRaisesRegex(ValueError, 'readable UTF-8'):
            load_init_script(self.path, 4)
        for content in (b'', b'# comment only\n', b'\xff', self.base.encode() + b'\x00',
                        self.base.encode() + b'\x1c'):
            self.path.write_bytes(content)
            with self.subTest(content=content[-20:]), self.assertRaises(ValueError):
                load_init_script(self.path, 4)

    def test_script_is_reread_for_each_session(self):
        first = self.load()
        second = self.load(self.base + '# new deployment revision\n')
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(first.content, self.base.encode())
