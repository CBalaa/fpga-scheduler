"""Validate optional access templates without starting a VM or VPN client."""
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import types
import unittest
from unittest.mock import Mock, call, patch


PROJECT = Path(__file__).resolve().parents[1]


class LegacyAccessConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.pexpect = types.SimpleNamespace(EOF=EOFError, TIMEOUT=TimeoutError, spawn=Mock())
        spec = importlib.util.spec_from_file_location('access_vpn',
                                                      PROJECT / 'deploy/legacy-access/vpn.py')
        self.vpn = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'pexpect': self.pexpect}):
            spec.loader.exec_module(self.vpn)
        self.config = dict(username='example-user', password=secrets.token_hex(16),
                           profile='example.profile', profile_index=2,
                           probe_hosts=['fpga-a.example.invalid', 'fpga-b.example.invalid'])

    def test_profile_and_probe_hosts_must_be_explicit_and_valid(self):
        self.vpn.validate_config(self.config)
        for change in (dict(profile=''), dict(profile='line\nbreak'), dict(profile_index=0),
                       dict(profile_index=True), dict(probe_hosts=[]), dict(probe_hosts=None),
                       dict(probe_hosts=['']), dict(probe_hosts=['two hosts'])):
            with self.subTest(change=change), self.assertRaises(self.vpn.CredentialsError):
                self.vpn.validate_config(dict(self.config, **change))
        for missing in ('username', 'password', 'profile', 'profile_index', 'probe_hosts'):
            cfg = dict(self.config)
            del cfg[missing]
            with self.subTest(missing=missing), self.assertRaises(self.vpn.CredentialsError):
                self.vpn.validate_config(cfg)

    def test_missing_deployment_config_never_launches_vpn_login(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = dict(self.config)
            del cfg['probe_hosts']
            (Path(directory) / 'vpn.json').write_text(json.dumps(cfg))
            with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': directory}):
                with self.assertRaises(self.vpn.CredentialsError):
                    self.vpn.main()
        self.pexpect.spawn.assert_not_called()

    def test_reachability_probes_only_configured_hosts_with_failover(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        with patch.object(self.vpn.socket, 'create_connection',
                          side_effect=[OSError('offline'), connection]) as connect:
            self.assertTrue(self.vpn.reachable(self.config['probe_hosts']))
        self.assertEqual(connect.call_args_list,
                         [call((host, 22), timeout=5) for host in self.config['probe_hosts']])

    def test_login_uses_configured_profile_and_never_logs_credentials(self):
        child = Mock()
        child.expect.side_effect = [None, None, 0, 1]
        with patch.object(self.vpn, 'tunnel_present', return_value=True), \
             patch.object(self.vpn, 'reachable', return_value=True) as reachable, \
             patch('builtins.print') as output:
            self.vpn.connect(child, self.config)
        self.assertEqual(child.expect.call_args_list[0], call(re.escape('2:example.profile')))
        self.assertEqual(child.sendline.call_args_list,
                         [call('2'), call('1'), call(self.config['username']),
                          call(self.config['password'])])
        reachable.assert_called_once_with(self.config['probe_hosts'])
        output.assert_not_called()
