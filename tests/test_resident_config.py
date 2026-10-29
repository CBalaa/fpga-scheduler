"""Deployment-owned initialization paths; these tests do not launch UVHS."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fpga_scheduler.config import load


class ResidentInitializationConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'init').mkdir()
        for number in (4, 5, 6, 7, 43):
            (self.root / 'init' / f'init{number}.tcl').write_text('# deployment script\n')
        self.path = self.root / 'config.toml'
        self.base = '''backend="bosc-resident"
state_dir="state"
token_file="token"
[resident]
hardware_project="project"
lock_dir="locks"
[resident.init_scripts]
"4"="init/init4.tcl"
"5"="init/init5.tcl"
[[nodes]]
id="local"
ports=[4,5]
uvhs_shell="/vendor/uvhs"
'''

    def config(self, text=None):
        self.path.write_text(self.base if text is None else text)
        return load(self.path)

    def all_scripts(self, port=6):
        return self.base.replace('"5"="init/init5.tcl"',
                                 '"5"="init/init5.tcl"\n"6"="init/init6.tcl"\n"7"="init/init7.tcl"'
                                 ).replace('ports=[4,5]', f'ports=[{port}]')

    def test_paths_relative_to_config_even_from_different_cwd(self):
        old = Path.cwd()
        try:
            os.chdir('/')
            config = self.config()
        finally:
            os.chdir(old)
        self.assertEqual(config.resident['init_scripts'], {
            4: str(self.root / 'init/init4.tcl'), 5: str(self.root / 'init/init5.tcl')})

    def test_absolute_home_and_symlink_paths_resolve(self):
        link = self.root / 'linked.tcl'
        link.symlink_to(self.root / 'init/init4.tcl')
        text = self.base.replace('init/init4.tcl', str(link)).replace('init/init5.tcl', '~/init/init5.tcl')
        with patch.dict(os.environ, {'HOME': str(self.root)}):
            config = self.config(text)
        self.assertEqual(config.resident['init_scripts'][4], str(self.root / 'init/init4.tcl'))
        self.assertEqual(config.resident['init_scripts'][5], str(self.root / 'init/init5.tcl'))

    def test_port_number_beyond_two_boards_remains_physical(self):
        text = self.base.replace('"4"="init/init4.tcl"', '"43"="init/init43.tcl"').replace('ports=[4,5]', 'ports=[43,5]')
        config = self.config(text)
        self.assertEqual(config.nodes[0].ports, (43, 5))
        self.assertEqual(set(config.resident['init_scripts']), {43, 5})

    def test_missing_table_has_migration_guidance(self):
        text = self.base.replace('[resident.init_scripts]\n"4"="init/init4.tcl"\n"5"="init/init5.tcl"\n', '')
        with self.assertRaisesRegex(ValueError, 'legacy hw_runN.tcl extraction is no longer used'):
            self.config(text)

    def test_all_ports_can_be_registered_with_only_one_enabled(self):
        config = self.config(self.all_scripts())
        self.assertEqual(config.nodes[0].ports, (6,))
        self.assertEqual(config.resident['init_scripts'], {
            port: str(self.root / f'init/init{port}.tcl') for port in (4, 5, 6, 7)})

    def test_switching_only_ports_keeps_registered_scripts(self):
        text = self.all_scripts()
        before = self.config(text)
        after = self.config(text.replace('ports=[6]', 'ports=[7]'))
        self.assertEqual(before.nodes[0].ports, (6,))
        self.assertEqual(after.nodes[0].ports, (7,))
        self.assertEqual(before.resident['init_scripts'], after.resident['init_scripts'])

    def test_single_port_mapping_remains_supported(self):
        text = self.base.replace('"5"="init/init5.tcl"\n', '').replace('ports=[4,5]', 'ports=[4]')
        config = self.config(text)
        self.assertEqual(config.nodes[0].ports, (4,))
        self.assertEqual(config.resident['init_scripts'], {4: str(self.root / 'init/init4.tcl')})

    def test_missing_enabled_script_reports_port(self):
        text = self.all_scripts().replace('"6"="init/init6.tcl"\n', '')
        with self.assertRaisesRegex(ValueError, r'include every enabled port.*missing=\[6\]'):
            self.config(text)

    def test_missing_enabled_scripts_and_invalid_keys_are_rejected(self):
        cases = (
            self.base.replace('"5"="init/init5.tcl"\n', ''),
            self.base.replace('"5"="init/init5.tcl"', '"43"="init/init43.tcl"'),
            self.base.replace('"4"=', '"04"='),
            self.base.replace('"4"=', '"-1"='),
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.config(text)

    def test_disabled_script_paths_are_validated_at_config_load(self):
        for value in ('missing.tcl', 'init', '$HOME/init/init4.tcl'):
            text = self.all_scripts().replace('init/init4.tcl', value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, r'init_scripts\.4'):
                self.config(text)

    def test_disabled_script_readability_is_validated_at_config_load(self):
        real_open = Path.open

        def blocked(path, *args, **kwargs):
            if path.name == 'init4.tcl':
                raise PermissionError('denied')
            return real_open(path, *args, **kwargs)

        with patch.object(Path, 'open', blocked):
            with self.assertRaisesRegex(ValueError, r'init_scripts\.4.*readable file.*denied'):
                self.config(self.all_scripts())

    def test_config_load_does_not_execute_or_parse_registered_tcl(self):
        # Content and target checks happen in Resident for enabled ports only.
        (self.root / 'init/init4.tcl').write_text('this is not Tcl initialization\n')
        config = self.config(self.all_scripts())
        self.assertEqual(config.nodes[0].ports, (6,))
        self.assertIn(4, config.resident['init_scripts'])

    def test_invalid_paths_and_tables_are_rejected(self):
        cases = (
            self.base.replace('init/init4.tcl', 'missing.tcl'),
            self.base.replace('init/init4.tcl', 'init'),
            self.base.replace('init/init4.tcl', '$HOME/init/init4.tcl'),
            self.base.replace('"init/init4.tcl"', 'true'),
            self.base.replace('"init/init4.tcl"', '""'),
            self.base.replace('[resident.init_scripts]\n"4"="init/init4.tcl"\n"5"="init/init5.tcl"',
                              'init_scripts = []'),
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.config(text)

    def test_unreadable_script_has_port_and_path_error(self):
        real_open = Path.open

        def blocked(path, *args, **kwargs):
            if path.name == 'init4.tcl':
                raise PermissionError('denied')
            return real_open(path, *args, **kwargs)

        with patch.object(Path, 'open', blocked):
            with self.assertRaisesRegex(ValueError, r'init_scripts\.4.*readable file.*denied'):
                self.config()

    def test_explicit_allocation_including_false_requires_migration(self):
        for value in ('true', 'false', '1'):
            text = self.base.replace('[resident]\n', '[resident]\nexplicit_allocation=' + value + '\n')
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'remove it and provide'):
                self.config(text)

    def test_legacy_bosc_options_remain_supported(self):
        text = self.base.replace('"bosc-resident"', '"bosc"').replace('[[nodes]]',
            '[[nodes]]').replace('id="local"', 'id="local"\nssh_host="fpga"\nroot="/runtime"')
        text = text.replace('[resident.init_scripts]\n"4"="init/init4.tcl"\n"5"="init/init5.tcl"\n', '')
        text = text.replace('[resident]\n', '[resident]\nexplicit_allocation=true\n')
        self.assertTrue(self.config(text).resident['explicit_allocation'])
