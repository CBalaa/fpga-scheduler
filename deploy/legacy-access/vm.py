#!/usr/bin/env python3
"""Run the existing access VM in the foreground under a user systemd service."""
import datetime
import fcntl
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(os.environ.get('FPGA_ACCESS_VM_ROOT',
                          Path.home() / '.local/share/fpga-scheduler/access-vm')).expanduser().resolve()


def monitor(command=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(ROOT / 'monitor.sock'))
        if command:
            sock.sendall((command + '\n').encode())
            sock.recv(4096)


def vm_alive():
    try:
        pid = int((ROOT / 'vm.pid').read_text())
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return (b'fpga-vpn' in argv and
                any(arg.startswith(b'file=disk.qcow2,') for arg in argv))
    except (OSError, ValueError):
        return False


def start():
    os.chdir(ROOT)
    lock = os.open(ROOT / 'service.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock, True)
    if vm_alive():
        raise RuntimeError('Existing VM is running; refuse a second instance')
    try:
        monitor()
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    else:
        raise RuntimeError('Existing VM monitor is live; refuse a second instance')
    for name in ('disk.qcow2', 'seed.iso'):
        if not (ROOT / name).is_file():
            raise RuntimeError(f'Missing {name}')
    for name in ('monitor.sock', 'vm.pid'):
        (ROOT / name).unlink(missing_ok=True)
    # Preserve each boot's console rather than truncating evidence on restart.
    console = ROOT / 'console.log'
    if console.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        console.rename(ROOT / f'console-{stamp}.log')
        for old in sorted(ROOT.glob('console-*.log'))[:-20]:
            old.unlink()
    argv = ['qemu-system-x86_64', '-name', 'fpga-vpn', '-enable-kvm',
            '-cpu', 'host', '-smp', '2', '-m', '4096',
            '-drive', 'file=disk.qcow2,format=qcow2,if=virtio',
            '-drive', 'file=seed.iso,format=raw,media=cdrom,readonly=on',
            '-nic', 'user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:22222-:22',
            '-display', 'none', '-serial', 'file:console.log',
            '-monitor', 'unix:monitor.sock,server=on,wait=off', '-pidfile', 'vm.pid']
    process = subprocess.Popen(argv, pass_fds=(lock,))
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f'{stamp} QEMU started pid={process.pid}', flush=True)
    status = process.wait()
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f'{stamp} QEMU exited status={status} (negative means signal)', flush=True)
    return 0 if status == 0 else 1


def stop():
    if not vm_alive():
        return
    monitor('system_powerdown')
    deadline = time.monotonic() + 60
    while vm_alive() and time.monotonic() < deadline:
        time.sleep(1)
    if vm_alive():
        raise RuntimeError('Guest shutdown timed out; systemd will terminate QEMU')


if __name__ == '__main__':
    if sys.argv[1:] == ['start']:
        sys.exit(start())
    elif sys.argv[1:] == ['stop']:
        stop()
    else:
        raise SystemExit('Usage: vm.py start|stop')
