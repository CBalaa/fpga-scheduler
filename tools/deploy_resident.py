#!/usr/bin/env python3
"""Prepare an isolated local deployment; run this ON the selected FPGA server.

Requires an existing verified Python 3.11+ executable and a built wheel. Never
modifies system Python, vendor installs, hardware sources or systemd settings.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess


def quote(value):
    return json.dumps(str(value))  # TOML basic strings share JSON string escaping.


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--python', type=Path, required=True)
    p.add_argument('--wheel', type=Path, required=True)
    p.add_argument('--project', type=Path, required=True)
    p.add_argument('--uvhs', type=Path, required=True)
    p.add_argument('--lock-dir', type=Path, required=True)
    p.add_argument('--destination', type=Path, required=True)
    p.add_argument('--ports', default='4,5')
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args()
    ports = [int(n) for n in args.ports.split(',')]
    if not ports or len(set(ports)) != len(ports) or any(n not in range(8) for n in ports):
        p.error('ports must be distinct integers in 0..7')
    if not 1 <= args.port <= 65535:
        p.error('invalid listening port')
    args.python, args.wheel, args.project, args.uvhs, args.lock_dir = (
        path.expanduser().resolve() for path in (args.python, args.wheel, args.project, args.uvhs, args.lock_dir))
    version = subprocess.check_output([str(args.python), '-c', 'import sys;print(sys.version_info >= (3,11))'], text=True).strip()
    if version != 'True':
        p.error('Python 3.11+ is required')
    if not args.wheel.is_file() or not args.uvhs.is_file() or not (args.project / 'hw.dat').exists():
        p.error('wheel, UVHS executable or hardware database missing')
    for number in ports:
        if not (args.project / 'user_script/hw_run' / f'hw_run{number}.tcl').is_file():
            p.error('allowlisted hardware script missing')
    destination = args.destination.expanduser().resolve()
    destination.mkdir(mode=0o700)  # Refuse to overwrite an existing deployment.
    subprocess.run([str(args.python), '-m', 'venv', str(destination / 'venv')], check=True)
    subprocess.run([str(destination / 'venv/bin/python'), '-m', 'pip', 'install', '--no-deps', str(args.wheel)], check=True)
    token = destination / 'service.token'
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(secrets.token_urlsafe(32) + '\n')
    config = destination / 'config.local.toml'
    config.write_text(f'''backend = "bosc-resident"
state_dir = "./state"
token_file = "./service.token"
bind = "127.0.0.1"
port = {args.port}
[resident]
profile = "nh-only"
hardware_project = {quote(args.project)}
lock_dir = {quote(args.lock_dir)}
[[nodes]]
id = "local"
ports = {json.dumps(ports)}
uvhs_shell = {quote(args.uvhs)}
''')
    (destination / 'installation.json').write_text(json.dumps(dict(
        wheel=args.wheel.name, sha256=hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        python=str(args.python), ports=ports), indent=2) + '\n')
    print(f'{destination}/venv/bin/fpga-scheduler daemon start --config {config}')


if __name__ == '__main__':
    main()
