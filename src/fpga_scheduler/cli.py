"""Command-line entry point for clients and the service."""
import argparse
from dataclasses import asdict, is_dataclass
import json
import logging
import os
from pathlib import Path
import secrets
import sys

from .client import Client, SchedulerError
from .display import capacity_details, color_enabled, paint, render


def _command(parent, *args, **kwargs):
    command = parent.add_parser(*args, **kwargs)
    command.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                         help='Emit machine-readable JSON instead of readable text')
    command.add_argument('--color', choices=('auto', 'always', 'never'), default=argparse.SUPPRESS,
                         help='Terminal colors (default: auto; respects NO_COLOR)')
    return command


def parser():
    p = argparse.ArgumentParser(prog='fpga-scheduler')
    default_config = os.environ.get('FPGA_SCHEDULER_CONFIG') or None
    p.add_argument('--endpoint', default=os.environ.get('FPGA_SCHEDULER_URL', 'http://127.0.0.1:8765'))
    p.add_argument('--token-file', default=os.environ.get('FPGA_SCHEDULER_TOKEN_FILE'))
    p.add_argument('--json', action='store_true', help='Emit machine-readable JSON')
    p.add_argument('--color', choices=('auto', 'always', 'never'), default='auto',
                   help='Terminal colors (default: auto; respects NO_COLOR)')
    commands = p.add_subparsers(dest='command', required=True)
    serve = _command(commands, 'serve')
    serve.add_argument('--config', default=default_config, required=default_config is None,
                       help='Configuration file (default: FPGA_SCHEDULER_CONFIG)')
    daemon = _command(commands, 'daemon')
    actions = daemon.add_subparsers(dest='action', required=True)
    for action in ('start', 'status', 'stop', '_supervise'):
        action_parser = _command(actions, action)
        action_parser.add_argument('--config', default=default_config, required=default_config is None,
                                   help='Configuration file (default: FPGA_SCHEDULER_CONFIG)')
    token = _command(commands, 'init-token')
    token.add_argument('path')
    for name in ('run', 'submit'):
        submit = _command(commands, name, help='Run in foreground; Ctrl+C cancels (submit is an alias)')
        submit.add_argument('image')
        submit.add_argument('--id', dest='ident')
        submit.add_argument('--timeout', type=int, default=600)
        submit.add_argument('--idle-seconds', type=int, default=60)
        submit.add_argument('--queue-timeout', type=int, default=3600)
    for name in ('job', 'cancel', 'wait'):
        sub = _command(commands, name)
        sub.add_argument('id')
        if name == 'wait':
            sub.add_argument('--timeout', type=float)
            sub.add_argument('--poll-interval', type=float, default=2)
    jobs = _command(commands, 'jobs')
    jobs.add_argument('--state', default='queued,running,cancelling')
    jobs.add_argument('--limit', type=int, default=100)
    jobs.add_argument('--offset', type=int, default=0)
    _command(commands, 'devices')
    _command(commands, 'health')
    artifacts = _command(commands, 'artifacts', help='List saved DDR outputs')
    artifacts.add_argument('id')
    download = _command(commands, 'download', help='Download a saved DDR output with SHA256 verification')
    download.add_argument('id')
    download.add_argument('name')
    download.add_argument('destination')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    color = not args.json and color_enabled(args.color)
    error_color = not args.json and color_enabled(args.color, sys.stderr)
    client = None
    try:
        if args.command == 'init-token':
            path = Path(args.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(secrets.token_urlsafe(32) + '\n')
            print(json.dumps({'token_file': str(path)}) if args.json else f'Created token file: {path}')
            return 0
        if args.command == 'daemon':
            from . import daemon
            if args.action == '_supervise':
                daemon.supervise(args.config)
                return 0
            data = getattr(daemon, args.action)(args.config)
            print(json.dumps(data, ensure_ascii=False) if args.json else render('daemon', data, action=args.action, color=color))
            return 0
        if args.command == 'serve':
            from .config import load
            from .service import Application
            from logging.handlers import RotatingFileHandler
            config = load(args.config)
            config.state_dir.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                                handlers=[RotatingFileHandler(config.state_dir / 'service.log', maxBytes=1048576, backupCount=3)])
            Application(config).serve()
            return 0
        client = Client(args.endpoint, token_file=args.token_file)
        if args.command == 'artifacts':
            result = dict(id=args.id, artifacts=client.artifacts(args.id))
        elif args.command == 'download':
            path = client.download_artifact(args.id, args.name, args.destination)
            result = dict(id=args.id, name=args.name, path=str(path))
        elif args.command in ('run', 'submit'):
            task = client.submit(args.image, ident=args.ident, timeout=args.timeout,
                                 idle_seconds=args.idle_seconds, queue_timeout=args.queue_timeout)
            print(f'[FPGA] Task {task.id} (Ctrl+C cancels)', file=sys.stderr, flush=True)
            last_chunk = ''
            with client.stream_output(task.id, timeout=args.queue_timeout + args.timeout + 300) as stream:
                def update(current):
                    if current.state == 'queued' and current.waiting_reason:
                        print(paint(f'[FPGA] Queued #{current.position}: {current.waiting_reason}',
                                    'yellow', color=error_color), file=sys.stderr, flush=True)
                        for line in capacity_details(current.waiting_details):
                            print('[FPGA] ' + line, file=sys.stderr, flush=True)
                stream.on_update = update
                for chunk in stream:
                    if not args.json:
                        print(chunk, end='', flush=True)
                        last_chunk = chunk
                result = stream.task
            if last_chunk and not last_chunk.endswith('\n'):
                print()
        elif args.command == 'job':
            result = client.job(args.id)
        elif args.command == 'cancel':
            result = client.cancel(args.id)
        elif args.command == 'wait':
            result = client.wait(args.id, timeout=args.timeout, poll_interval=args.poll_interval)
        elif args.command == 'jobs':
            result = client.jobs(tuple(args.state.split(',')), limit=args.limit, offset=args.offset)
        else:
            result = getattr(client, args.command)()
        data = asdict(result) if is_dataclass(result) else result
        print(json.dumps(data, ensure_ascii=False) if args.json else render(args.command, data, color=color))
        if args.command in ('run', 'submit', 'wait') and result.state != 'completed':
            return 1
        return 0
    except (SchedulerError, ValueError, OSError, RuntimeError) as error:
        data = {'error': str(error)}
        if isinstance(error, SchedulerError) and error.task_id:
            data['id'] = error.task_id
        if isinstance(error, SchedulerError) and error.details:
            data['error_details'] = error.details
        print(json.dumps(data, ensure_ascii=False) if args.json else paint(str(error), 'red', color=error_color), file=sys.stderr)
        if not args.json:
            for line in capacity_details(data.get('error_details')):
                print(line, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(paint('Interrupted; cancelling this client\'s unfinished tasks', 'yellow', color=error_color), file=sys.stderr)
        return 130
    finally:
        if client is not None:
            client.close()
