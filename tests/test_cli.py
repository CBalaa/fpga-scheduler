"""CLI defaults, readable command output, and machine-readable compatibility."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import re
import unittest
from unittest.mock import patch

from fpga_scheduler.cli import main, parser
from fpga_scheduler.client import Result, SchedulerError, Task
from fpga_scheduler.display import render


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


class OutputTests(unittest.TestCase):
    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_flag_before_and_after_commands(self):
        with patch.dict(os.environ, {'FPGA_SCHEDULER_CONFIG': '/example/config.toml'}):
            for args in (['health'], ['jobs'], ['devices'], ['job', 'a' * 32],
                         ['cancel', 'a' * 32], ['wait', 'a' * 32], ['run', 'hello.bin'],
                         ['submit', 'hello.bin'], ['init-token', 'token'],
                         ['daemon', 'start'], ['daemon', 'status'], ['daemon', 'stop']):
                for argv in (['--json', *args], [*args, '--json']):
                    with self.subTest(argv=argv):
                        self.assertTrue(parser().parse_args(argv).json)
                self.assertFalse(parser().parse_args(args).json)
            self.assertTrue(parser().parse_args(['daemon', '--json', 'status']).json)

    @patch('fpga_scheduler.cli.Client')
    def test_jobs_table_pagination_and_empty_list(self, factory):
        value = dict(total=3, offset=1, limit=2, jobs=[
            dict(id='a' * 32, state='queued', position=2, waiting_reason='Waiting for an available FPGA'),
            dict(id='b' * 32, state='failed', node='local', fpga=0,
                 result=dict(seconds=1.25, execution_status='exited', app_exit_code=-2147483648))])
        factory.return_value.jobs.return_value = value
        code, stdout, stderr = self.invoke('jobs', '--offset', '1', '--limit', '2')
        self.assertEqual((code, stderr), (0, ''))
        self.assertIn('3 matching | showing 2 | offset 1', stdout)
        for expected in ('STATE', 'APP EXIT', 'a' * 32, 'queued', 'local/FPGA0',
                         '-2147483648', '1.25s', 'Waiting for an available FPGA'):
            self.assertIn(expected, stdout)
        factory.return_value.jobs.assert_called_once_with(('queued', 'running', 'cancelling'), limit=2, offset=1)
        _, stdout, _ = self.invoke('jobs', '--json')
        self.assertEqual(json.loads(stdout), value)
        factory.return_value.jobs.return_value = dict(total=0, jobs=[], offset=0)
        _, stdout, _ = self.invoke('jobs')
        self.assertIn('No matching jobs.', stdout)

    @patch('fpga_scheduler.cli.Client')
    def test_devices_show_quarantine_reasons_and_node_errors(self, factory):
        value = dict(devices=[dict(node='local', number=4, state='READY', available=False,
                                   reasons=['platform task active'], session='c' * 32, downloads=1),
                              dict(node='local', number=5, state='QUARANTINED', available=False,
                                   reasons=['Abort cleanup failed: reset query failed'], downloads=1)],
                     errors={'other': 'Device probe unavailable or invalid'})
        factory.return_value.devices.return_value = value
        code, stdout, stderr = self.invoke('devices')
        self.assertEqual((code, stderr), (0, ''))
        for expected in ('AVAILABLE', 'QUARANTINED', 'DOWNLOADS', 'c' * 32,
                         'platform task active', 'reset query failed', 'Node other: Device probe unavailable'):
            self.assertIn(expected, stdout)
        _, stdout, _ = self.invoke('--json', 'devices')
        self.assertEqual(json.loads(stdout), value)

    @patch('fpga_scheduler.daemon.status')
    def test_status_keeps_healthy_api_when_port_query_times_out(self, status):
        value = dict(supervisor=dict(pid=123, config='/example/config.toml', restarts=2),
                     supervisor_alive=True, api=dict(pid=124, backend='Resident', dispatcher_alive=True),
                     queue=dict(total=3, jobs=[]), ports_error='Connection or response failed (TimeoutError)',
                     ports_note='Initialization or recovery may still be in progress.')
        status.return_value = value
        code, stdout, stderr = self.invoke('daemon', 'status', '--config', '/example/config.toml')
        self.assertEqual((code, stderr), (0, ''))
        for expected in ('Daemon: RUNNING', 'Supervisor: PID 123', 'restarts 2', 'API: OK',
                         'Queue: 3 active jobs', 'Ports: unavailable', 'TimeoutError', 'recovery'):
            self.assertIn(expected, stdout)
        _, stdout, _ = self.invoke('daemon', 'status', '--config', '/example/config.toml', '--json')
        self.assertEqual(json.loads(stdout), value)
        value.update(api=None, api_error='Connection refused')
        _, stdout, _ = self.invoke('daemon', 'status', '--config', '/example/config.toml')
        self.assertIn('Daemon: DEGRADED', stdout)
        self.assertIn('API: UNREACHABLE | Connection refused', stdout)
        value.update(supervisor_alive=False, api_error=None)
        _, stdout, _ = self.invoke('daemon', 'status', '--config', '/example/config.toml')
        self.assertIn('Daemon: STOPPED', stdout)

    @patch('fpga_scheduler.cli.Client')
    def test_task_details_keep_application_and_cleanup_failures(self, factory):
        result = Result('hello\n', 'local', 1, .25, execution_status='platform_error', app_exit_code=0,
                        details=dict(cleanup_error='CPU reset assertion was not confirmed', reset_confirmed=False))
        factory.return_value.job.return_value = Task('a' * 32, 'failed', 0, node='local', fpga=4, result=result)
        code, stdout, stderr = self.invoke('job', 'a' * 32)
        self.assertEqual((code, stderr), (0, ''))
        for expected in ('State: failed', 'App exit code: 0', 'Platform exit code: 1',
                         'Cleanup error: CPU reset assertion was not confirmed', 'Reset confirmed: no', 'UART:\nhello'):
            self.assertIn(expected, stdout)

    def test_start_and_stop_have_readable_summaries(self):
        value = dict(supervisor=dict(pid=123, child={'pid': 124}, restarts=0, config='/example/config.toml'),
                     api=dict(pid=124, backend='Resident', dispatcher_alive=True, stopping=False))
        with patch('fpga_scheduler.daemon.start', return_value=value):
            code, stdout, stderr = self.invoke('daemon', 'start', '--config', '/example/config.toml')
        self.assertEqual((code, stderr), (0, ''))
        self.assertIn('Daemon: RUNNING', stdout)
        self.assertIn('Service: PID 124 | backend Resident', stdout)
        with patch('fpga_scheduler.daemon.stop', return_value=dict(stopped=True, already_stopped=True)):
            code, stdout, stderr = self.invoke('daemon', 'stop', '--config', '/example/config.toml')
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(stdout, 'Daemon: STOPPED (already stopped)\n')

    @patch('fpga_scheduler.cli.Client')
    def test_run_streams_uart_once_then_summary_and_json_stays_parseable(self, factory):
        client = factory.return_value
        task = Task('a' * 32, 'completed', 0, node='local', fpga=4,
                    result=Result('hello', 'local', 0, .25, execution_status='exited', app_exit_code=0))
        client.submit.return_value = task
        stream = client.stream_output.return_value.__enter__.return_value
        stream.__iter__.side_effect = lambda: iter(['hel', 'lo'])
        stream.task = task
        for command in ('run', 'submit'):
            code, stdout, stderr = self.invoke(command, 'hello.bin')
            self.assertEqual(code, 0, stderr)
            self.assertTrue(stdout.startswith('hello\nTask: '), stdout)
            self.assertEqual(stdout.count('hello'), 1)
            self.assertIn('App exit code: 0', stdout)
            self.assertIn('Task ' + task.id, stderr)
        code, stdout, stderr = self.invoke('run', 'hello.bin', '--json')
        self.assertEqual(code, 0, stderr)
        value = json.loads(stdout)
        self.assertEqual(value['result']['output'], 'hello')
        self.assertEqual(value['state'], 'completed')

    @patch('fpga_scheduler.cli.Client')
    def test_wait_exit_code_and_json_errors_are_preserved(self, factory):
        client = factory.return_value
        client.wait.return_value = Task('a' * 32, 'failed', 0, error='Outcome unknown; not replayed')
        code, stdout, stderr = self.invoke('wait', 'a' * 32)
        self.assertEqual((code, stderr), (1, ''))
        self.assertIn('Outcome unknown; not replayed', stdout)
        client.health.side_effect = SchedulerError('Connection refused')
        code, stdout, stderr = self.invoke('health', '--json')
        self.assertEqual((code, stdout), (1, ''))
        self.assertEqual(json.loads(stderr), {'error': 'Connection refused'})
    def test_color_option_before_and_after_commands(self):
        with patch.dict(os.environ, {'FPGA_SCHEDULER_CONFIG': '/example/config.toml'}):
            for command in (['devices'], ['jobs'], ['daemon', 'status']):
                for argv in (['--color', 'always', *command], [*command, '--color', 'always']):
                    with self.subTest(argv=argv):
                        self.assertEqual(parser().parse_args(argv).color, 'always')
                self.assertEqual(parser().parse_args(command).color, 'auto')

    @patch('fpga_scheduler.cli.Client')
    def test_auto_color_detects_terminal_and_respects_no_color(self, factory):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        factory.return_value.health.return_value = dict(dispatcher_alive=True, stopping=False)
        for env, flags, expected in (({'TERM': 'xterm-256color'}, [], True),
                                     ({'TERM': 'dumb'}, [], False),
                                     ({'TERM': 'xterm', 'NO_COLOR': ''}, [], False),
                                     ({'TERM': 'xterm'}, ['--color', 'never'], False),
                                     ({'TERM': 'dumb', 'NO_COLOR': '1'}, ['--color', 'always'], True)):
            with self.subTest(env=env, flags=flags), patch.dict(os.environ, env, clear=True):
                stdout = Terminal()
                with redirect_stdout(stdout):
                    self.assertEqual(main(['health', *flags]), 0)
                self.assertEqual('\033[' in stdout.getvalue(), expected)
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
            _, stdout, _ = self.invoke('health')
            self.assertNotIn('\033[', stdout)

    def test_storage_wait_views_explain_quota_separately_from_disk(self):
        details = dict(error_code='resident_storage_quota_exceeded', phase='admission', hardware_started=False,
                       used_bytes=494092257, reserved_bytes=16777216, requested_bytes=38862848,
                       limit_bytes=536870912, disk_free_bytes=244 * 1024**3,
                       action='Ask the administrator to increase resident.max_storage_bytes or shorten retention_days')
        queued = dict(id='a' * 32, state='queued', position=1, retry_at=1,
                      waiting_reason='Resident storage quota exceeded; retrying in 10s; hardware was not started',
                      waiting_details=details)
        for command, data in (('job', queued), ('jobs', dict(total=1, jobs=[queued]))):
            text = render(command, data)
            for expected in ('471.2 MiB used', '512.0 MiB quota', '244.0 GiB', 'max_storage_bytes'):
                self.assertIn(expected, text)
        text = render('job', queued)
        self.assertIn('Hardware started: no', text)
        self.assertIn('Next retry:', text)
        health = dict(dispatcher_alive=True, storage=dict(resident={k: v for k, v in details.items()
                                                                   if k.endswith('_bytes')}))
        status = render('daemon', dict(supervisor_alive=True, api=health, queue=dict(total=1, jobs=[queued])),
                        action='status')
        self.assertIn('Resident storage: 471.2 MiB used', status)
        self.assertIn('Filesystem free: 244.0 GiB', status)
        self.assertIn('storage quota exceeded', status)

    def test_colored_tables_keep_same_text_alignment_and_full_ids(self):
        values = {
            'jobs': dict(total=2, jobs=[dict(id='a' * 32, state='running', node='sim', fpga=4),
                                       dict(id='b' * 32, state='failed', node='sim', fpga=5,
                                            result=dict(execution_status='exited', app_exit_code=-7))]),
            'devices': dict(devices=[dict(node='sim', number=4, state='READY', available=True, session='c' * 32),
                                    dict(node='sim', number=5, state='QUARANTINED', available=False,
                                         reasons=['CPU reset not confirmed'])]),
        }
        for command, data in values.items():
            with self.subTest(command=command):
                colored = render(command, data, color=True)
                plain = render(command, data)
                self.assertIn('\033[', colored)
                self.assertEqual(re.sub(r'\x1b\[[0-9;]*m', '', colored), plain)
        self.assertIn('\033[32mREADY\033[0m', render('devices', values['devices'], color=True))
        self.assertIn('\033[31mQUARANTINED\033[0m', render('devices', values['devices'], color=True))
        self.assertIn('\033[36mrunning\033[0m', render('jobs', values['jobs'], color=True))

    @patch('fpga_scheduler.cli.Client')
    def test_forced_color_never_colors_json_or_streamed_uart(self, factory):
        client = factory.return_value
        task = Task('a' * 32, 'completed', 0, node='sim', fpga=4,
                    result=Result('PASS\n', 'sim', 0, .1, execution_status='exited', app_exit_code=0))
        client.submit.return_value = task
        stream = client.stream_output.return_value.__enter__.return_value
        stream.__iter__.side_effect = lambda: iter(['PA', 'SS\n'])
        stream.task = task
        code, stdout, _ = self.invoke('run', 'hello.bin', '--color', 'always')
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith('PASS\nTask: '), stdout)
        self.assertEqual(stdout.count('PASS'), 1)
        self.assertIn('State: \033[32mcompleted\033[0m', stdout)
        code, stdout, stderr = self.invoke('run', 'hello.bin', '--color', 'always', '--json')
        self.assertEqual(code, 0)
        self.assertNotIn('\033[', stdout + stderr)
        self.assertEqual(json.loads(stdout)['result']['output'], 'PASS\n')

    @patch('fpga_scheduler.cli.Client')
    def test_errors_use_stderr_color_policy_and_json_remains_clean(self, factory):
        factory.return_value.health.side_effect = SchedulerError('Connection refused')
        code, stdout, stderr = self.invoke('health', '--color', 'always')
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('\033[31mConnection refused\033[0m', stderr)
        code, stdout, stderr = self.invoke('health', '--color', 'always', '--json')
        self.assertNotIn('\033[', stderr)
        self.assertEqual(json.loads(stderr), {'error': 'Connection refused'})
