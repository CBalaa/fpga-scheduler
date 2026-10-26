"""BOSC/UVHS transport. Route selection precedes execution; never replay run."""
import base64
import hashlib
import json
from pathlib import PurePosixPath
import shlex
import subprocess
import time

from ..models import BUSY_EXIT, BUSY_TOKEN


class BOSC:
    def __init__(self, nodes, transfer_timeout=120):
        self.nodes = tuple(nodes)
        self.by_id = {n.id: n for n in nodes}
        self.transfer_timeout = transfer_timeout

    def remote(self, node, command, timeout):
        cfg = self.by_id[node]
        try:
            if cfg.relay_command:
                response = subprocess.run(list(cfg.relay_command),
                                          input=json.dumps(dict(node=node, command=command, timeout=timeout)),
                                          text=True, capture_output=True, timeout=timeout + 10)
                if response.returncode:
                    raise OSError('Access relay failed')
                result = json.loads(response.stdout)
                if not isinstance(result, dict):
                    raise ValueError('Invalid relay response')
                return result
            argv = ['ssh', '-T']
            if cfg.ssh_config:
                argv += ['-F', cfg.ssh_config]
            argv += ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                     '-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=10',
                     '-o', 'ServerAliveCountMax=2', cfg.ssh_host, command]
            start = time.monotonic()
            response = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                      text=True, encoding='utf-8', errors='replace', timeout=timeout)
            if response.returncode == 255:
                raise OSError('SSH failed; remote outcome may be unknown')
            return dict(output=(response.stdout + response.stderr).replace('\r', '').strip(),
                        stdout=response.stdout,
                        exit_code=response.returncode, node=node, seconds=time.monotonic() - start)
        except (OSError, subprocess.TimeoutExpired):
            # TimeoutExpired embeds the command, which may contain a bin chunk.
            # Do not expose payload bytes through worker exception logging.
            raise OSError('SSH/relay unavailable or timed out; execution outcome may be unknown') from None

    def runtime(self, node, *args):
        cfg = self.by_id[node]
        argv = [cfg.python, str(PurePosixPath(cfg.root) / 'scheduler.py'),
                '--root', cfg.root, '--uvhs', cfg.uvhs_shell, '--uart-prefix', cfg.uart_prefix,
                '--ports', ','.join(map(str, cfg.ports)), *map(str, args)]
        # A login shell loads the vendor environment on existing FPGA hosts.
        return 'bash -lc ' + shlex.quote(shlex.join(argv))

    def probe(self, node):
        response = self.remote(node, self.runtime(node, 'probe'), 60)
        if response.get('error') or response.get('exit_code') != 0:
            raise OSError('FPGA probe failed')
        return json.loads(response.get('stdout', response['output']))

    def prepare(self, payload, node, image):
        content = image.read_bytes()
        if hashlib.sha256(content).hexdigest() != payload['sha256']:
            raise ValueError('Input checksum mismatch')
        # Structured data reaches a fixed helper. Unlike run, this command can
        # only create/replace the not-yet-executed job input, never load hardware.
        directory = str(PurePosixPath(self.by_id[node].root) / 'jobs' / payload['id'])
        code = """import base64,fcntl,hashlib,json,os,sys
from pathlib import Path
p=Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True)
if p.is_symlink(): raise ValueError('job directory is a symlink')
lock=(p/'active.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX)
os.utime(p,None)
(p/'finished').unlink(missing_ok=True)
dst=p/'input.bin'; temp=p/'upload.tmp'
if dst.is_symlink() or temp.is_symlink(): raise ValueError('invalid input path')
if sys.argv[2]=='start': temp.write_bytes(b'')
elif sys.argv[2]=='chunk':
    with temp.open('ab') as f: f.write(base64.b64decode(sys.argv[3],validate=True))
elif sys.argv[2]=='finish':
    if hashlib.sha256(temp.read_bytes()).hexdigest()!=sys.argv[3]: raise ValueError('checksum mismatch')
    with temp.open('rb') as f: os.fsync(f.fileno())
    temp.replace(dst)
"""
        deadline = time.monotonic() + self.transfer_timeout

        def upload(action, value=''):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Input transfer deadline exceeded')
            argv = [self.by_id[node].python, '-c', code, directory, action, value]
            response = self.remote(node, shlex.join(argv), min(60, remaining))
            if response.get('error') or response.get('exit_code') != 0:
                raise OSError('Input transfer failed before hardware execution')

        upload('start')
        for offset in range(0, len(content), 32768):
            upload('chunk', base64.b64encode(content[offset:offset + 32768]).decode())
        upload('finish', payload['sha256'])

    def run(self, payload, node, port):
        result = self.remote(node, self.runtime(node, 'run', payload['id'], port, payload['sha256'],
                                               payload['timeout'], payload['idle_seconds']), payload['timeout'] + 180)
        stdout = result.pop('stdout', result['output'])
        # Only the agent's final record is metadata; UART/tool text stays a log.
        lines = result['output'].splitlines()
        # Login-shell diagnostics may arrive on stderr after stdout is captured.
        records = [line for line in lines if line.startswith('[scheduler-result] ')]
        if records:
            record = json.loads(records[-1][len('[scheduler-result] '):])
            if (record.get('id') != payload['id'] or record.get('exit_code') != result['exit_code'] or
                    record.get('execution_status') not in
                    ('exited', 'trap', 'timeout', 'protocol_error', 'platform_error', 'interrupted') or
                    not isinstance(record.get('details'), dict)):
                raise ValueError('Invalid remote execution metadata')
            code = record.get('app_exit_code')
            if code is not None and (type(code) is not int or not -(2 ** 31) <= code < 2 ** 31):
                raise ValueError('Invalid application exit code')
            if record['execution_status'] == 'exited' and code is None:
                raise ValueError('Application exit record has no return value')
            if result['exit_code'] == 0 and (record['execution_status'] != 'exited' or code != 0):
                raise ValueError('Success requires a zero application exit and clean session')
            result.update({key: record[key] for key in ('execution_status', 'app_exit_code', 'details')})
            uart = record.get('uart_output', '')
            if not isinstance(uart, str):
                raise ValueError('Invalid UART output')
            result['uart_output'] = uart
        else:
            if result['exit_code'] == BUSY_EXIT and stdout.strip() == BUSY_TOKEN:
                # The agent prints this exact stdout only before loading hardware.
                # SSH login diagnostics on stderr must not turn a safe retry into
                # a failed application. Extra stdout still forbids replay.
                result['output'] = BUSY_TOKEN
                return result
            transport_code = result['exit_code']
            result.update(execution_status='protocol_error' if transport_code == 0 else 'platform_error',
                          app_exit_code=None, exit_code=transport_code or 1,
                          details={'error': 'Remote agent did not publish execution metadata',
                                   'transport_exit_code': transport_code})
        return result

    def cleanup(self, before):
        for node in self.by_id:
            response = self.remote(node, self.runtime(node, 'cleanup', before), 60)
            if response.get('error') or response.get('exit_code') != 0:
                raise OSError('Remote retention cleanup failed')
