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


def parser():
    p = argparse.ArgumentParser(prog='fpga-scheduler')
    default_config = os.environ.get('FPGA_SCHEDULER_CONFIG') or None
    p.add_argument('--endpoint', default=os.environ.get('FPGA_SCHEDULER_URL', 'http://127.0.0.1:8765'))
    p.add_argument('--token-file', default=os.environ.get('FPGA_SCHEDULER_TOKEN_FILE'))
    p.add_argument('--json', action='store_true', help='Emit machine-readable JSON')
    commands = p.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--config', default=default_config, required=default_config is None,
                       help='Configuration file (default: FPGA_SCHEDULER_CONFIG)')
    daemon = commands.add_parser('daemon')
    actions = daemon.add_subparsers(dest='action', required=True)
    for action in ('start', 'status', 'stop', '_supervise'):
        action_parser = actions.add_parser(action)
        action_parser.add_argument('--config', default=default_config, required=default_config is None,
                                   help='Configuration file (default: FPGA_SCHEDULER_CONFIG)')
    token = commands.add_parser('init-token')
    token.add_argument('path')
    submit = commands.add_parser('submit')
    submit.add_argument('image')
    submit.add_argument('--id', dest='ident')
    submit.add_argument('--timeout', type=int, default=600)
    submit.add_argument('--idle-seconds', type=int, default=60)
    submit.add_argument('--queue-timeout', type=int, default=3600)
    for name in ('job', 'cancel', 'wait'):
        sub = commands.add_parser(name)
        sub.add_argument('id')
        if name == 'wait':
            sub.add_argument('--timeout', type=float)
            sub.add_argument('--poll-interval', type=float, default=2)
    jobs = commands.add_parser('jobs')
    jobs.add_argument('--state', default='queued,running')
    jobs.add_argument('--limit', type=int, default=100)
    jobs.add_argument('--offset', type=int, default=0)
    commands.add_parser('devices')
    commands.add_parser('health')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
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
            print(json.dumps(getattr(daemon, args.action)(args.config), ensure_ascii=False, indent=2))
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
        if args.command == 'submit':
            result = client.submit(args.image, ident=args.ident, timeout=args.timeout,
                                   idle_seconds=args.idle_seconds, queue_timeout=args.queue_timeout)
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
        print(json.dumps(data, ensure_ascii=False, indent=None if args.json else 2))
        if args.command == 'wait' and result.state != 'completed':
            return 1
        return 0
    except (SchedulerError, ValueError, OSError, RuntimeError) as error:
        data = {'error': str(error)}
        if isinstance(error, SchedulerError) and error.task_id:
            data['id'] = error.task_id
        print(json.dumps(data, ensure_ascii=False) if args.json else str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted; submitted tasks remain queryable by ID', file=sys.stderr)
        return 130
