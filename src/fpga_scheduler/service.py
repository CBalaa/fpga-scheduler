"""Authenticated internal HTTP API and single-process lifecycle."""
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import stat
import threading
from urllib.parse import parse_qs, urlsplit

from .backends.simulator import Simulator
from .models import ACTIVE, MAX_IMAGE, Conflict
from .queue import JobQueue
from .storage import Images

LOG = logging.getLogger(__name__)


class StateLock:
    def __init__(self, root):
        Path(root).mkdir(parents=True, exist_ok=True)
        self.handle = (Path(root) / 'service.lock').open('a')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise RuntimeError('Another scheduler owns this state directory') from None

    def close(self):
        self.handle.close()


def read_token(path):
    path = Path(path)
    with path.open() as handle:
        mode = os.fstat(handle.fileno()).st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError('Token file must be a regular file readable only by its owner (mode 600/400)')
        token = handle.read().strip()
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('Service token must be at least 32 ASCII characters without whitespace')
    return token


def make_server(address, queue, images, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_):
            # Do not log authorization headers, request bodies, or query strings.
            pass

        def reply(self, status, data):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Durable task state is independent of this connection.

        def body(self, maximum):
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('Chunked request bodies are unsupported; supply Content-Length')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1:
                raise ValueError('Exactly one Content-Length is required')
            size = int(lengths[0])
            if not 0 < size <= maximum:
                raise ValueError('Invalid request size')
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError('Incomplete request body')
            return body

        def handle_api(self):
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                self.reply(401, {'error': 'Bearer token required'})
                return
            try:
                path = urlsplit(self.path)
                route = path.path
                match = re.fullmatch(r'/jobs/([0-9a-f]{32})(/image)?', route)
                if self.command == 'GET' and route == '/health':
                    alive = queue.dispatcher is not None and queue.dispatcher.is_alive()
                    ok = alive and not queue.stopping.is_set()
                    self.reply(200 if ok else 503, dict(service='fpga-scheduler', pid=os.getpid(), backend=type(queue.backend).__name__,
                                                      dispatcher_alive=alive, stopping=queue.stopping.is_set(),
                                                      hardware_checked=False, scheduler_error=queue.status_error))
                elif self.command == 'GET' and route == '/devices':
                    self.reply(200, queue.inventory())
                elif self.command == 'GET' and route == '/jobs':
                    query = parse_qs(path.query, keep_blank_values=True)
                    if set(query) - {'state', 'limit', 'offset'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('Invalid query parameters')
                    states = tuple(query['state'][0].split(',')) if 'state' in query else ACTIVE
                    self.reply(200, queue.list(states, int(query.get('limit', ['100'])[0]), int(query.get('offset', ['0'])[0])))
                elif self.command == 'GET' and match and not match[2]:
                    self.reply(200, queue.get(match[1]))
                elif self.command == 'DELETE' and match and not match[2]:
                    self.reply(200, queue.cancel(match[1]))
                elif self.command == 'PUT' and match and match[2]:
                    digest = images.put(match[1], self.body(MAX_IMAGE))
                    self.reply(200, dict(id=match[1], sha256=digest))
                elif self.command == 'POST' and route == '/jobs':
                    self.reply(202, queue.submit(json.loads(self.body(131072))))
                else:
                    self.reply(404, {'error': 'Unknown endpoint'})
            except KeyError:
                self.reply(404, {'error': 'Task not found'})
            except Conflict as error:
                self.reply(409, {'error': str(error)})
            except (ValueError, UnicodeError) as error:
                self.reply(400, {'error': str(error)})
            except OverflowError as error:
                self.reply(503, {'error': str(error)})
            except (TimeoutError, socket.timeout):
                self.reply(408, {'error': 'Request body timeout'})
            except Exception:
                LOG.exception('HTTP operation failed')
                self.reply(503, {'error': 'Service temporarily unavailable'})

        do_GET = handle_api
        do_POST = handle_api
        do_PUT = handle_api
        do_DELETE = handle_api

    class Server(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True
        request_queue_size = 128

    return Server(address, Handler)


class Application:
    def __init__(self, config):
        self.config = config
        self.token = read_token(config.token_file)
        self.state_lock = StateLock(config.state_dir)
        self.queue = None
        self.backend = None
        self.server = None
        self.closed = False
        try:
            images = Images(config.state_dir / 'images',
                            max_bytes=config.resident.get('max_storage_bytes', 1073741824) // 2
                            if config.backend == 'bosc-resident' else None)
            if config.backend == 'simulator':
                backend = Simulator(config.nodes, **config.simulator)
            elif config.backend == 'bosc-resident':
                from .backends.resident import Resident
                backend = Resident(config.nodes, config.state_dir, **config.resident)
            else:
                from .backends.bosc import BOSC
                backend = BOSC(config.nodes, transfer_timeout=config.transfer_timeout)
            self.backend = backend
            self.queue = JobQueue(config.state_dir / 'jobs.sqlite3', backend, images,
                                  interval=config.poll_interval, capacity=config.queue_capacity,
                                  cooldown=config.cooldown_seconds, retention=config.retention_days * 86400)
            self.server = make_server((config.bind, config.port), self.queue, images, self.token)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Caller must stop serve_forever first. Stop accepting requests and
        # finish handlers before closing SQLite; drain running hardware work.
        if self.queue:
            self.queue.stopping.set()
            self.queue.wake.set()
        if self.server:
            self.server.server_close()
        if self.queue:
            self.queue.close()
        elif self.backend is not None and hasattr(self.backend, 'close'):
            self.backend.close()
        self.state_lock.close()

    def serve(self):
        self.queue.start()
        stopped = threading.Event()

        def stop(signum, frame):
            if not stopped.is_set():
                stopped.set()
                self.queue.stopping.set()
                self.queue.wake.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            LOG.info('Listening on %s:%s (%s backend)', *self.server.server_address, self.config.backend)
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
