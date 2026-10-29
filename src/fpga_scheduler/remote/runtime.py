#!/usr/bin/env python3
"""Standalone BOSC agent, adapted from buddy-caas bosc_runtime.py; see NOTICE."""
import argparse
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent
UVHS = os.environ.get('FPGA_UVHS') or shutil.which('uvhs2_shell') or 'uvhs2_shell'
UART_PREFIX = '/dev/FPGA'
CLEANUP_GRACE = 45
ORDER = (4, 5, 6, 7, 0, 1, 2, 3)
BUSY_EXIT = 75
BUSY_TOKEN = '[scheduler] RETRY_BEFORE_LOAD'


def parse_fpgas(output):
    """Return the devices UVHS actually reports, preserving physical IDs.

    A host need not expose both boards (or all four DUTs on a board). Missing
    devices are not free devices; callers must check their configured targets.
    """
    ports = {}
    for line in output.splitlines():
        fields = line.split()
        if not fields or fields[0] != 'uvhs-0':
            continue
        if len(fields) < 2 or not fields[1].startswith('B'):
            continue
        if (len(fields) not in (7, 8) or not re.fullmatch(r'B\d+', fields[1])
                or not re.fullmatch(r'F[0-3]', fields[2])
                or fields[3] not in ('on', 'off') or fields[4] != 'link'
                or fields[5] not in ('up', 'down') or fields[-1] not in ('true', 'false')):
            raise RuntimeError('UVHS returned an invalid FPGA row: ' + line.strip())
        number = int(fields[1][1:]) * 4 + int(fields[2][1])
        if number in ports:
            raise RuntimeError('UVHS returned a duplicate FPGA')
        owner = fields[6] if len(fields) == 8 else ''
        ports[number] = dict(number=number, board=f'B{number // 4}', fpga=fields[2],
                             owner=owner, power=fields[3], booked=fields[-1] != 'false')
    if not ports:
        raise RuntimeError('UVHS did not return any FPGA units')
    return ports


def lock_port(number):
    directory = ROOT / 'scheduler-locks'
    directory.mkdir(exist_ok=True)
    handle = (directory / f'fpga{number}.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def runtime_mapping_valid(script, number):
    """Require the downloaded database to use the scheduler's physical port."""
    loads = [line.strip() for line in script.splitlines()
             if re.match(r'^\s*load_db\s+-db\b', line)]
    target = r'\{b%d\.f%d\}' % divmod(number, 4)
    releases = list(re.finditer(r'^\s*load_db\s+-release\s*$', script, re.M))
    load_positions = list(re.finditer(r'^\s*load_db\s+-db\b', script, re.M))
    if (len(releases) != 1 or len(load_positions) != 1 or len(loads) != 1
            or releases[0].start() >= load_positions[0].start()):
        return False
    return re.fullmatch(r'load_db\s+-db\s+\./hw\.dat\s+-to\s+' + target, loads[0]) is not None


def inventory(held=None):
    with tempfile.TemporaryDirectory(prefix='fpga-scheduler-probe-') as directory:
        query = subprocess.run([UVHS, '-t', 'runtime', '-d', 'V1', '-workdir', directory],
                               input='query -fpgas -all\nexit\n', text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
    if query.returncode or re.search(r'Total (?:ERROR|FATAL):\s*[1-9]', query.stdout):
        raise RuntimeError('UVHS status query failed')
    ports = parse_fpgas(query.stdout)
    discovered = set(ports)
    # Board clocks/connectors are shared. Avoid a board used by another account.
    busy_boards = set()
    username = pwd.getpwuid(os.getuid()).pw_name
    for port in ports.values():
        if not port['owner']: continue
        if port['owner'] != username:
            busy_boards.add(port['board'])
            continue
        # Concurrent platform tasks all use the same fixed clock. An unrelated
        # session under the same login is only considered coordinated if locked.
        if port['number'] == held: continue
        handle = lock_port(port['number'])
        if handle is not None:
            handle.close()
            busy_boards.add(port['board'])
    processes = subprocess.check_output(['ps', '-eo', 'pid=,args='], text=True)
    for number in ORDER:
        if number not in ports:
            board, fpga = divmod(number, 4)
            ports[number] = dict(number=number, board=f'B{board}', fpga=f'F{fpga}',
                                 available=False, reasons=['FPGA missing from UVHS inventory'])
            continue
        port = ports[number]
        device = Path(f'{UART_PREFIX}{number}')
        reasons = []
        if port['power'] != 'on': reasons.append('power off')
        if port['owner']: reasons.append('FPGA occupied')
        if port['booked']: reasons.append('reserved')
        if port['board'] in busy_boards: reasons.append('board in use (shared clock)')
        board_start = number // 4 * 4
        if not set(range(board_start, board_start + 4)) <= discovered:
            # This runner configures shared clocks on every run. Unseen
            # siblings cannot be assumed idle even when the target is free.
            reasons.append('board inventory incomplete')
        if not device.exists():
            reasons.append('UART missing')
        else:
            actual = str(device.resolve())
            # Other accounts' /proc/*/fd are private; inspect their command lines too.
            pattern = r'(?<![\w/])(?:' + re.escape(str(device)) + '|' + re.escape(actual) + r')(?!\w)'
            if re.search(pattern, processes): reasons.append('UART reader active')
            for proc in Path('/proc').glob('[0-9]*'):
                try:
                    for fd in (proc / 'fd').iterdir():
                        try:
                            if os.readlink(fd) == actual:
                                reasons.append('UART open')
                                break
                        except OSError:
                            pass
                except OSError:
                    pass
        script = ROOT / 'user_script' / 'hw_run' / f'hw_run{number}.tcl'
        if not script.exists():
            reasons.append('runtime script missing')
        elif not runtime_mapping_valid(script.read_text(), number):
            reasons.append('runtime FPGA mapping invalid')
        if number != held:
            handle = lock_port(number)
            if handle is None:
                reasons.append('platform task active')
            else:
                handle.close()
        port.update(available=not reasons, reasons=sorted(set(reasons)))
    return [ports[number] for number in ORDER]


def run_job(args):
    handle = lock_port(args.fpga)
    if handle is None:
        print(BUSY_TOKEN, flush=True)
        return BUSY_EXIT
    with handle:
        ports = inventory(held=args.fpga)
        if not next(port for port in ports if port['number'] == args.fpga)['available']:
            print(BUSY_TOKEN, flush=True)
            return BUSY_EXIT
        directory = ROOT / 'jobs' / args.id
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError('job directory missing')
        image = directory / 'input.bin'
        if image.is_symlink() or not image.is_file() or image.stat().st_size > 16 * 1024 * 1024:
            raise RuntimeError('invalid job image')
        content = image.read_bytes()
        if not content or hashlib.sha256(content).hexdigest() != args.sha256:
            raise RuntimeError('uploaded image checksum mismatch')
        # Never mutate a user's input, the shared test directory, or another job's logs.
        padded = directory / 'input_padded.bin'
        size = max(1048576, ((len(content) + 1048575) // 1048576) * 1048576)
        padded.write_bytes(content + bytes(size - len(content)))
        for name in ('Makefile', 'hw.dat', 'user_script'):
            target = directory / name
            if not target.exists(): target.symlink_to(ROOT / name)
        env = {**os.environ, 'FPGA_IDLE_SECONDS': str(args.idle),
               'FPGA_STARTUP_TIMEOUT': str(min(300, args.timeout)),
               'FPGA_EXECUTION_TIMEOUT': str(args.timeout),
               'FPGA_UART_PREFIX': UART_PREFIX,
               'UV_RUN_SYS_CLK_HZ': '5529600',
               'UV_RUN_OUT_ROOT': str(directory / 'output'),
               'UV_RUN_WORKDIR': str(directory / 'runtime')}
        (directory / 'runtime').mkdir(exist_ok=True)
        print(f'[scheduler] Task {args.id}: FPGA{args.fpga}, isolated workdir {directory}', flush=True)
        process = subprocess.Popen(['bash', str(ROOT / 'run.sh'), f'--fpga={args.fpga}',
                                    f'--workdir={directory}', f'test={padded}'], env=env,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        native = (ROOT / 'runner.py').is_file()
        status = supervise(process, args.timeout + 60 if native else args.timeout)
        if native:
            result_file = directory / 'output/result.json'
            if result_file.is_file():
                result = json.loads(result_file.read_text())
                if result.get('exit_code') != status:
                    result.update(execution_status='platform_error', exit_code=status or 1)
                    status = result['exit_code']
            else:
                result = dict(execution_status='timeout' if status == 124 else 'protocol_error',
                              app_exit_code=None, exit_code=status or 1,
                              details={'error': 'Runner did not publish a result'})
                status = result['exit_code']
            uart_log = directory / 'output/uart.raw.log'
            result['uart_output'] = uart_log.read_text(errors='replace') if uart_log.is_file() else ''
            result['id'] = args.id
            print('\n[scheduler-result] ' + json.dumps(result), flush=True)
        return status


def supervise(process, timeout):
    """Hold the port until the entire task process group has retired.

    Even a successful parent may leave a UART child alive. SIGTERM/SIGKILL
    target the session group, regardless of the leader's exit status.
    """
    interrupted = False

    def group_signal(sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def group_alive():
        try:
            os.killpg(process.pid, 0)
            for proc in Path('/proc').glob('[0-9]*'):
                try:
                    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                    if int(fields[2]) == process.pid and fields[0] != 'Z':
                        return True
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, IndexError):
                    return True
            return False
        except ProcessLookupError:
            return False

    def stop(signum, frame):
        nonlocal interrupted
        interrupted = True
        group_signal(signal.SIGTERM)

    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print('[scheduler] Execution timed out; closing UVHS and UART.', flush=True)
            status = 124
        finally:
            group_signal(signal.SIGTERM)
            deadline = time.monotonic() + CLEANUP_GRACE
            while group_alive() and time.monotonic() < deadline:
                process.poll()
                time.sleep(0.05)
            if group_alive():
                group_signal(signal.SIGKILL)
            process.wait()
            deadline = time.monotonic() + 5
            while group_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if group_alive():
                raise RuntimeError('Task processes did not retire; inspect hardware before reuse')
        return 130 if interrupted and status != 124 else status
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def cleanup(before):
    """Delete retired remote job directories only; never clean during probe.

    Input arrives immediately before run. Uploads refresh directory age;
    active runs hold a per-job lock. Never expire an active hardware session.
    """
    jobs = ROOT / 'jobs'
    if not jobs.is_dir():
        return
    for directory in jobs.iterdir():
        if (not re.fullmatch('[0-9a-f]{32}', directory.name) or directory.is_symlink()
                or not directory.is_dir()):
            continue
        marker = directory / 'finished'
        age = marker.stat().st_mtime if marker.is_file() else directory.stat().st_mtime
        if age >= before:
            continue
        with (directory / 'active.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            age = marker.stat().st_mtime if marker.is_file() else directory.stat().st_mtime
            if age < before:
                shutil.rmtree(directory)


def main():
    global ROOT, UVHS, UART_PREFIX, ORDER
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--uvhs', default=UVHS)
    parser.add_argument('--uart-prefix', default=UART_PREFIX)
    parser.add_argument('--ports', default=','.join(map(str, ORDER)))
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('probe')
    retire = commands.add_parser('cleanup')
    retire.add_argument('before', type=float)
    run = commands.add_parser('run')
    run.add_argument('id', type=lambda value: value if re.fullmatch(r'[0-9a-f]{32}', value) else parser.error('invalid id'))
    run.add_argument('fpga', type=int)
    run.add_argument('sha256', type=lambda value: value if re.fullmatch(r'[0-9a-f]{64}', value) else parser.error('invalid hash'))
    run.add_argument('timeout', type=int, choices=range(1, 601))
    run.add_argument('idle', type=int, choices=range(1, 121))
    args = parser.parse_args()
    ROOT = args.root.resolve()
    UVHS, UART_PREFIX = args.uvhs, args.uart_prefix
    ORDER = tuple(int(p) for p in args.ports.split(','))
    if not ORDER or len(set(ORDER)) != len(ORDER) or any(p < 0 for p in ORDER):
        parser.error('ports must be distinct nonnegative integers')
    if args.action == 'run' and args.fpga not in ORDER:
        parser.error('FPGA port is outside configured pool')
    if args.action == 'cleanup':
        cleanup(args.before)
        return 0
    if args.action == 'probe':
        print(json.dumps(inventory()))
        return 0
    directory = ROOT / 'jobs' / args.id
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError('job directory missing')
    with (directory / 'active.lock').open('a') as active:
        fcntl.flock(active, fcntl.LOCK_EX)
        (directory / 'finished').unlink(missing_ok=True)
        try:
            status = run_job(args)
        except BaseException:
            (directory / 'finished').touch()
            raise
        if status != BUSY_EXIT:
            (directory / 'finished').touch()
        return status


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('[scheduler] ' + str(error), flush=True)
        raise SystemExit(1)
