"""Detached user supervisor. A stop drains work and leaves queued tickets on disk."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading

from .client import Client, SchedulerError
from .config import load
from .remote.uvhs_session import atomic_json, fingerprint
from .service import read_token


def _paths(config):
    root = config.state_dir / 'daemon'
    root.mkdir(parents=True, exist_ok=True)
    return root, root / 'supervisor.json'


def _alive(record):
    return bool(record and record.get('pid') and fingerprint(record['pid']) == record.get('start_ticks'))


def _record(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _client(config):
    host = '127.0.0.1' if config.bind == '0.0.0.0' else config.bind
    return Client(f'http://{host}:{config.port}', token=read_token(config.token_file), request_timeout=2)


def status(path):
    config = load(path)
    root, pidfile = _paths(config)
    record = _record(pidfile)
    value = dict(supervisor=record, supervisor_alive=_alive(record), api=None)
    if value['supervisor_alive']:
        try:
            client = _client(config)
            value.update(api=client.health(), queue=client.jobs(limit=1), ports=client.devices())
        except SchedulerError as error:
            value['api_error'] = str(error)
    return value


def start(path, timeout=360):
    path = str(Path(path).resolve())
    config = load(path)
    read_token(config.token_file)
    root, pidfile = _paths(config)
    if _alive(_record(pidfile)):
        raise RuntimeError('Supervisor already running for this state directory')
    # A different serve instance must not masquerade as the new daemon.
    from .service import StateLock
    state = StateLock(config.state_dir)
    state.close()
    with (root / 'launcher.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'fpga_scheduler', 'daemon', '_supervise', '--config', path],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True, close_fds=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = _record(pidfile)
        if record and record.get('pid') == process.pid and _alive(record):
            try:
                health = _client(config).health()
                if not record.get('child') or health.get('pid') != record['child']['pid']:
                    time.sleep(.1)
                    continue
                threading.Thread(target=process.wait, daemon=True).start()
                return dict(supervisor=record, api=health)
            except SchedulerError:
                pass
        if process.poll() is not None:
            raise RuntimeError('Supervisor exited during startup; inspect daemon/launcher.log')
        time.sleep(.1)
    # Cancel startup through the supervisor; it drains any accepted work.
    os.kill(process.pid, signal.SIGTERM)
    threading.Thread(target=process.wait, daemon=True).start()
    raise RuntimeError('Daemon startup deadline exceeded; graceful stop requested')


def stop(path, timeout=720):
    config = load(path)
    root, pidfile = _paths(config)
    record = _record(pidfile)
    if not _alive(record):
        return dict(stopped=True, already_stopped=True)
    if os.stat(f'/proc/{record["pid"]}').st_uid != os.getuid():
        raise RuntimeError('Supervisor belongs to a different user')
    os.kill(record['pid'], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while _alive(record) and time.monotonic() < deadline:
        time.sleep(.1)
    if _alive(record):
        raise RuntimeError('Graceful stop still draining; supervisor will not restart')
    return dict(stopped=True)


def supervise(path):
    config = load(path)
    root, pidfile = _paths(config)
    lock = (root / 'supervisor.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Supervisor lock already held') from None
    handler = RotatingFileHandler(root / 'supervisor.log', maxBytes=1048576, backupCount=3)
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(message)s')
    record = dict(pid=os.getpid(), start_ticks=fingerprint(os.getpid()), config=str(Path(path).resolve()),
                  child=None, restarts=0, stopping=False)
    atomic_json(pidfile, record)
    stopping = False
    child = None

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True
        record['stopping'] = True
        atomic_json(pidfile, record)
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    old = {s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        delay = 1
        while not stopping:
            started = time.monotonic()
            # stdout/stderr are deliberately not an unbounded on-disk pipe.
            # Application installs its own rotating logger; UVHS logs are bounded.
            child = subprocess.Popen([sys.executable, '-m', 'fpga_scheduler', 'serve', '--config', str(Path(path).resolve())],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            record['child'] = dict(pid=child.pid, start_ticks=fingerprint(child.pid))
            atomic_json(pidfile, record)
            while child.poll() is None:
                time.sleep(.1)
            logging.info('Service retired with status %s', child.returncode)
            # The foreground process may have left owned UVHS grandchildren.
            # Exact PID/start-time records avoid touching unrelated sessions.
            from .remote.uvhs_session import retire_record
            records = list((config.state_dir / 'resident/ports').glob('*/session.json'))
            records += list((config.state_dir / 'resident/probes').glob('*.json'))
            for session in records:
                retire_record(session)
            if stopping:
                break
            record['restarts'] += 1
            atomic_json(pidfile, record)
            if time.monotonic() - started > 60:
                delay = 1
            end = time.monotonic() + delay
            while not stopping and time.monotonic() < end:
                time.sleep(.1)
            delay = min(30, delay * 2)
    finally:
        record.update(stopping=True, stopped=True)
        atomic_json(pidfile, record)
        for sig, previous in old.items():
            signal.signal(sig, previous)
        lock.close()
