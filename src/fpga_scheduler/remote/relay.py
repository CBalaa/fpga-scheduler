#!/usr/bin/env python3
"""Optional JSON-stdin SSH relay, adapted from buddy-caas bosc_hostname.py.

Place on an access VM, invoke over SSH, and configure allowed nodes in JSON.
SSH routes, ProxyJump and pinned known_hosts belong to the VM's OpenSSH config.
No retry/fallback occurs after the target command is sent.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time


def run(request, config):
    if not isinstance(request, dict) or set(request) != {'node', 'command', 'timeout'}:
        raise ValueError('Invalid relay request')
    node, command, timeout = request['node'], request['command'], request['timeout']
    if (not isinstance(node, str) or node not in config['nodes'] or not isinstance(command, str)
            or not command.strip() or '\x00' in command or len(command) > 100000
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 900):
        raise ValueError('Invalid relay node, command or timeout')
    host = config['nodes'][node]
    if not isinstance(host, str) or not host or host.startswith('-') or any(c.isspace() for c in host):
        raise ValueError('Invalid SSH host alias')
    argv = ['ssh', '-T']
    if config.get('ssh_config'):
        argv += ['-F', config['ssh_config']]
    argv += ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
             '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2', host, command]
    start = time.monotonic()
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode == 255:
        return dict(error='ssh', node=node)
    return dict(output=(result.stdout + result.stderr).replace('\r', '').strip(),
                stdout=result.stdout,
                exit_code=result.returncode, node=node, seconds=time.monotonic() - start)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        print(json.dumps(run(json.load(sys.stdin), config)))
    except Exception:
        print(json.dumps({'error': 'Access relay failed; outcome may be unknown'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
