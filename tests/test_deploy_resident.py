"""Deployment preparation copies initialization files without editing hardware Tcl."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fpga_scheduler.config import load


SPEC = importlib.util.spec_from_file_location(
    'deploy_resident', Path(__file__).resolve().parents[1] / 'tools/deploy_resident.py')
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)


class ResidentDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.project / 'hw.dat').mkdir()
        self.wheel = self.root / 'fpga_scheduler.whl'
        self.wheel.write_bytes(b'wheel fixture')
        self.uvhs = self.root / 'uvhs'
        self.uvhs.write_text('vendor fixture')
        self.source = self.root / 'prepared4.tcl'
        self.content = (b'# Source host: fpga113\r\n# Source SHA-256: reference-fixture\r\n'
                        b'allocate -board {b1.f0}\r\nload_db -release\r\nload_db -db ./hw.dat\r\n'
                        b'download\r\ninitialize\r\nreset -name cpu_reset -value 1\r\nquery -reset\r\n')
        self.source.write_bytes(self.content)
        self.destination = self.root / 'deployment'
        self.argv = ['deploy_resident.py', '--python', sys.executable, '--wheel', str(self.wheel),
                     '--project', str(self.project), '--uvhs', str(self.uvhs),
                     '--lock-dir', str(self.root / 'locks'), '--destination', str(self.destination),
                     '--ports', '4', '--init-script', '4=' + str(self.source)]

    def execute(self, argv=None):
        with patch.object(sys, 'argv', self.argv if argv is None else argv), \
                patch.object(DEPLOY.subprocess, 'check_output', return_value='True\n'), \
                patch.object(DEPLOY.subprocess, 'run') as calls, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            DEPLOY.main()
            return calls

    def test_script_copied_verbatim_configured_and_recorded(self):
        self.execute()
        target = self.destination / 'init/init4.tcl'
        self.assertEqual(target.read_bytes(), self.content)
        self.assertEqual(self.source.read_bytes(), self.content)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load(self.destination / 'config.local.toml').resident['init_scripts'], {4: str(target)})
        self.assertNotIn('explicit_allocation', (self.destination / 'config.local.toml').read_text())
        self.assertFalse((self.project / 'user_script').exists())
        manifest = json.loads((self.destination / 'installation.json').read_text())
        self.assertEqual(manifest['init_scripts']['4'], dict(
            source=str(self.source), sha256=hashlib.sha256(self.content).hexdigest(), deployed='init/init4.tcl'))

    def test_repeated_options_for_multiple_physical_ports(self):
        source = self.root / 'init43.tcl'
        source.write_bytes(self.content.replace(b'b1.f0', b'b10.f3'))
        argv = self.argv.copy()
        argv[argv.index('--ports') + 1] = '4,43'
        argv += ['--init-script', '43=' + str(source)]
        self.execute(argv)
        self.assertEqual((self.destination / 'init/init43.tcl').read_bytes(), source.read_bytes())
        config = load(self.destination / 'config.local.toml')
        self.assertEqual(config.nodes[0].ports, (4, 43))
        self.assertEqual(set(config.resident['init_scripts']), {4, 43})

    def test_inactive_registered_scripts_are_copied_configured_and_recorded(self):
        argv = self.argv.copy()
        argv[argv.index('--ports') + 1] = '6'
        contents = {4: self.content}
        # Register out of order; deployment files and manifests stay deterministic.
        for number in (7, 5, 6):
            source = self.root / f'prepared{number}.tcl'
            contents[number] = self.content.replace(b'b1.f0', f'b1.f{number - 4}'.encode())
            source.write_bytes(contents[number])
            argv += ['--init-script', f'{number}={source}']
        calls = self.execute(argv)
        config = load(self.destination / 'config.local.toml')
        self.assertEqual(config.nodes[0].ports, (6,))
        self.assertEqual(set(config.resident['init_scripts']), {4, 5, 6, 7})
        manifest = json.loads((self.destination / 'installation.json').read_text())
        self.assertEqual(manifest['ports'], [6])
        self.assertEqual(list(manifest['init_scripts']), ['4', '5', '6', '7'])
        for number in (4, 5, 6, 7):
            target = self.destination / f'init/init{number}.tcl'
            self.assertEqual(target.read_bytes(), contents[number])
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(manifest['init_scripts'][str(number)]['sha256'],
                             hashlib.sha256(contents[number]).hexdigest())
        # Preparation installs software only; it never runs a hardware command.
        self.assertEqual(len(calls.call_args_list), 2)
        self.assertEqual(calls.call_args_list[0].args[0][1:3], ['-m', 'venv'])
        self.assertEqual(calls.call_args_list[1].args[0][1:4], ['-m', 'pip', 'install'])

    def test_missing_enabled_script_is_rejected_even_with_other_registered_scripts(self):
        argv = self.argv.copy()
        argv[argv.index('--ports') + 1] = '7'
        with self.assertRaises(SystemExit):
            self.execute(argv)
        self.assertFalse(self.destination.exists())

    def test_invalid_or_missing_mapping_fails_before_creating_deployment(self):
        entries = ('43=' + str(self.source), '04=' + str(self.source), '-1=' + str(self.source),
                   '4=', '4=' + str(self.root / 'missing.tcl'), '4=' + str(self.project))
        for entry in entries:
            argv = self.argv.copy()
            argv[-1] = entry
            with self.subTest(entry=entry), self.assertRaises(SystemExit):
                self.execute(argv)
            self.assertFalse(self.destination.exists())
        with self.assertRaises(SystemExit):
            self.execute(self.argv + ['--init-script', '4=' + str(self.source)])
        self.assertFalse(self.destination.exists())
        with self.assertRaises(SystemExit):
            self.execute(self.argv[:-2])
        self.assertFalse(self.destination.exists())

    def test_existing_destination_is_never_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / 'existing'
        marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(marker.read_text(), 'keep')
