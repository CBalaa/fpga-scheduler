"""Configuration defaults for service and daemon commands."""
from contextlib import redirect_stderr
import io
import os
import unittest
from unittest.mock import patch

from fpga_scheduler.cli import parser


CONFIG_COMMANDS = (('serve',), *(('daemon', action) for action in ('start', 'status', 'stop', '_supervise')))


class ConfigArgumentTests(unittest.TestCase):
    def test_environment_provides_config_for_all_service_commands(self):
        with patch.dict(os.environ, {'FPGA_SCHEDULER_CONFIG': '/example/default.toml'}):
            for command in CONFIG_COMMANDS:
                with self.subTest(command=command):
                    self.assertEqual(parser().parse_args(command).config, '/example/default.toml')

    def test_explicit_config_overrides_environment(self):
        for value in ('', '/example/default.toml'):
            with patch.dict(os.environ, {'FPGA_SCHEDULER_CONFIG': value}):
                for command in CONFIG_COMMANDS:
                    with self.subTest(environment=value, command=command):
                        args = parser().parse_args([*command, '--config', '/example/override.toml'])
                        self.assertEqual(args.config, '/example/override.toml')

    def test_missing_or_empty_environment_still_requires_config(self):
        for environment in ({}, {'FPGA_SCHEDULER_CONFIG': ''}):
            with patch.dict(os.environ, environment, clear=True):
                for command in CONFIG_COMMANDS:
                    with self.subTest(environment=environment, command=command):
                        stderr = io.StringIO()
                        with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                            parser().parse_args(command)
                        self.assertEqual(error.exception.code, 2)
                        self.assertIn('--config', stderr.getvalue())

    def test_config_default_does_not_change_client_commands(self):
        with patch.dict(os.environ, {'FPGA_SCHEDULER_CONFIG': '/nonexistent/config.toml'}):
            self.assertEqual(parser().parse_args(['health']).command, 'health')
