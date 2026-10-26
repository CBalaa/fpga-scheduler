#!/usr/bin/env python3
"""Keep UniVPN connected; systemd supplies credentials and restarts the worker."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

import pexpect

CLIENT = os.environ.get('FPGA_VPN_CLIENT', 'UniVPNCS')
PATTERNS = [
    r'(?im)^Please input[^\r\n]*(?:user\s*name|username|login name|account)[^\r\n]*',
    r'(?im)^Please input[^\r\n]*password[^\r\n]*',
    r'(?i)(?:continue|proceed|accept)[^\r\n]*(?:y/n|yes/no|\(y|\[y)',
    r'(?i)(?:connect.*success|successful login|login success)',
    r'(?i)(?:using the initial password|change it before using services|authentication failed|login failed|incorrect password|login failure)',
    r'(?i)(?:failed to connect|connect failed)',
    pexpect.EOF, pexpect.TIMEOUT,
]


class CredentialsError(Exception):
    """Stop retries until an administrator repairs credentials/certificates."""


def tunnel_present():
    addresses = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr'], text=True))
    return any(a['ifname'] == 'cnem_vnic' and a.get('addr_info') for a in addresses)


def reachable(hosts):
    # One server going offline must not tear down the other server's VPN jobs.
    for host in hosts:
        try:
            with socket.create_connection((host, 22), timeout=5):
                return True
        except OSError:
            pass
    return False


def connect(child, cfg):
    child.expect(re.escape(str(cfg['profile_index']) + ':' + cfg['profile']))
    child.sendline(str(cfg['profile_index']))
    child.expect('1:Connect')
    child.sendline('1')
    sent_user = sent_pass = False
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        idx = child.expect(PATTERNS, timeout=5)
        if idx == 0:
            if sent_user:
                raise CredentialsError('Repeated username prompt')
            child.sendline(cfg['username'])
            sent_user = True
        elif idx == 1:
            if sent_pass:
                raise CredentialsError('Repeated password prompt')
            child.sendline(cfg['password'])
            sent_pass = True
        elif idx == 2:
            raise CredentialsError('Certificate confirmation requires administrator review')
        elif idx == 4:
            raise CredentialsError('VPN authentication failed or password change required')
        elif idx in (5, 6):
            raise RuntimeError('VPN connection failed')
        # A transport "Connect success" appears before credentials; only the
        # assigned tunnel plus a TCP probe establish backend readiness.
        if sent_pass and tunnel_present() and reachable(cfg['probe_hosts']):
            return
    raise RuntimeError('VPN connection timed out')


def watch(child, hosts):
    failures = 0
    next_probe = time.monotonic() + 30
    while True:
        try:
            child.read_nonblocking(4096, timeout=5)
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            raise RuntimeError('VPN client exited')
        # Drop all client output, including login tokens and session IDs.
        if time.monotonic() >= next_probe:
            failures = 0 if tunnel_present() and reachable(hosts) else failures + 1
            next_probe = time.monotonic() + 30
            if failures >= 3:
                raise RuntimeError('VPN/backend health failed three consecutive probes')


def main():
    cfg = json.loads((Path(os.environ['CREDENTIALS_DIRECTORY']) / 'vpn.json').read_text())
    validate_config(cfg)
    client = shutil.which(CLIENT)
    if not client:
        raise RuntimeError('VPN client executable not found')
    child = pexpect.spawn(client, cwd=str(Path(client).parent), encoding='utf-8',
                          codec_errors='replace', echo=False, timeout=15)

    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        connect(child, cfg)
        print('VPN connected; backend SSH port reachable', flush=True)
        watch(child, cfg['probe_hosts'])
    finally:
        if child.isalive():
            child.close(force=True)


def validate_config(cfg):
    if not isinstance(cfg, dict) or any(not isinstance(cfg.get(k), str) or not cfg[k]
                                        or '\n' in cfg[k] or '\r' in cfg[k]
                                        for k in ('username', 'password', 'profile')):
        raise CredentialsError('Missing or invalid VPN credentials/profile')
    if type(cfg.get('profile_index')) is not int or cfg['profile_index'] < 1:
        raise CredentialsError('VPN profile menu index is required')
    hosts = cfg.get('probe_hosts')
    if (not isinstance(hosts, list) or not hosts or
            any(not isinstance(host, str) or not host or any(c.isspace() for c in host)
                for host in hosts)):
        raise CredentialsError('VPN backend probe hosts are required')


if __name__ == '__main__':
    try:
        main()
    except (CredentialsError, KeyError, ValueError, FileNotFoundError):
        print('VPN credentials/certificate need administrator review; automatic login retries stopped', flush=True)
        sys.exit(78)
    except Exception as exc:
        # Pexpect exceptions contain child output; never print exception text.
        print(f'VPN worker failed ({type(exc).__name__}); systemd will retry', flush=True)
        sys.exit(1)
