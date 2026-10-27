"""Standard-library SDK with client-owned leases and independent UART readers."""
from dataclasses import dataclass, field
import atexit
import base64
import codecs
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from .models import ACTIVE, MAX_IMAGE, task_id, validate


_clients = set()
_clients_lock = threading.Lock()


def _after_fork():
    global _clients_lock
    # Other threads disappear in the child; an inherited lock may stay locked.
    _clients_lock = threading.Lock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def _close_clients():
    # No __del__: abandoning a stream / dropping a local variable is not exit.
    with _clients_lock:
        clients = list(_clients)
    for client in clients:
        if client._pid == os.getpid():
            try:
                client.close()
            except Exception:
                pass  # The server lease is the crash/network-failure fallback.


atexit.register(_close_clients)


class SchedulerError(RuntimeError):
    def __init__(self, message, status=None, ident=None):
        super().__init__(message)
        self.status = status
        self.task_id = ident


@dataclass
class Result:
    output: str
    node: str
    exit_code: int
    seconds: float
    execution_status: str = 'legacy'
    app_exit_code: int | None = None
    details: dict = field(default_factory=dict)
    uart_output: str | None = None

    @property
    def ok(self):
        return self.exit_code == 0


@dataclass
class Task:
    id: str
    state: str
    created: float
    started: float | None = None
    finished: float | None = None
    node: str | None = None
    fpga: int | None = None
    error: str | None = None
    position: int | None = None
    waiting_reason: str | None = None
    result: Result | None = None

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        if value.get('result'):
            value['result'] = Result(**{k: v for k, v in value['result'].items()
                                       if k in Result.__dataclass_fields__})
        return cls(**value)


class OutputStream:
    """Incremental UTF-8 UART text; final Result is available after iteration.

    Uses bounded HTTP polling, so slow or disconnected readers never block the
    FPGA worker. Closing a reader does not cancel its task.
    """

    def __init__(self, client, ident, *, offset=0, timeout=None, poll_interval=.2, on_update=None):
        self.id = task_id(ident)
        self._validate(offset, timeout, poll_interval)
        self.client, self._offset = client, offset
        self.result = self.task = None
        self.poll_interval, self.on_update = poll_interval, on_update
        self.deadline = None if timeout is None else time.monotonic() + timeout
        self.closed = False
        self._finished = False
        self._previous = None
        self._decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._iterator = self._chunks()

    @staticmethod
    def _validate(offset, timeout, poll_interval):
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be a nonnegative byte count')
        if (isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float))
                or not math.isfinite(poll_interval) or poll_interval <= 0):
            raise ValueError('poll_interval must be positive')
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('stream timeout must be nonnegative')

    def __iter__(self):
        return self

    @property
    def offset(self):
        """Resume after the last decoded text, including any buffered UTF-8 bytes."""
        return self._offset - len(self._decoder.getstate()[0])

    def __next__(self):
        return next(self._iterator)

    def close(self):
        self.closed = True
        self._iterator.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _chunks(self):
        try:
            while not self.closed:
                value = self.client._request('/jobs/' + self.id + '/output?' +
                                             urlencode(dict(offset=self._offset, limit=65536)))
                raw = base64.b64decode(value['data'], validate=True)
                if value['offset'] != self._offset or value['next_offset'] != self._offset + len(raw):
                    raise SchedulerError('Invalid output cursor', ident=self.id)
                self.task = Task.from_dict(value['task'])
                current = (self.task.state, self.task.position, self.task.node,
                           self.task.fpga, self.task.waiting_reason)
                if self.on_update and current != self._previous:
                    self.on_update(self.task)
                self._previous = current
                self._offset = value['next_offset']
                self._finished = value['done']
                if self._finished:
                    self.result = self.task.result
                text = self._decoder.decode(raw, final=self._finished)
                if text:
                    yield text
                if self._finished:
                    if self.result is None:
                        raise SchedulerError(self.task.error or f'Task {self.task.state}', ident=self.id)
                    return
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    raise SchedulerError(f'Stream timed out; task remains queryable: {self.id}', ident=self.id)
                # Drain backlog immediately; otherwise poll at a modest rate.
                if len(raw) < 65536:
                    delay = self.poll_interval
                    if self.deadline is not None:
                        delay = min(delay, max(0, self.deadline - time.monotonic()))
                    time.sleep(delay)
        except SchedulerError as error:
            if error.task_id is None:
                error.task_id = self.id
                error.args = (f'{error}; task ID: {self.id}',)
            raise
        finally:
            self.closed = True


class Client:
    def __init__(self, endpoint='http://127.0.0.1:8765', token=None, *, token_file=None, request_timeout=30,
                 heartbeat_seconds=None, lease_seconds=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('endpoint must be an HTTP(S) base URL')
        self.endpoint = endpoint.rstrip('/')
        if token_file is not None:
            if token is not None:
                raise ValueError('Specify token or token_file, not both')
            token = Path(token_file).read_text().strip()
        if not isinstance(token, str) or not token or any(c.isspace() for c in token) or not token.isascii():
            raise ValueError('A service token is required')
        self.token = token
        self.request_timeout = request_timeout
        for name, value in (('heartbeat_seconds', heartbeat_seconds), ('lease_seconds', lease_seconds)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not .01 <= value <= 3600):
                raise ValueError(f'{name} must be within 0.01..3600')
        if heartbeat_seconds is not None and lease_seconds is not None and heartbeat_seconds >= lease_seconds:
            raise ValueError('heartbeat_seconds must be less than lease_seconds')
        self._session_options = {k: v for k, v in dict(heartbeat_seconds=heartbeat_seconds,
                                                      lease_seconds=lease_seconds).items() if v is not None}
        self._init_session_state()

    def _init_session_state(self):
        self._pid = os.getpid()
        self._session_id = uuid.uuid4().hex
        self._session_lock = threading.RLock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._session_open = False
        self._closed = False
        self._session_error = None

    def _ensure_session(self):
        # A forked child must never heartbeat or close its parent's session.
        if self._pid != os.getpid():
            self._init_session_state()
        with self._session_lock:
            if self._closed:
                raise SchedulerError('Client is closed')
            if self._session_error:
                raise SchedulerError(self._session_error, status=409)
            if not self._session_open:
                try:
                    value = self._request('/sessions/' + self._session_id, self._session_options, 'PUT')
                except SchedulerError as error:
                    if error.status == 404:
                        raise SchedulerError('Daemon does not support client leases; upgrade the daemon before submitting', 409) from None
                    raise
                self._heartbeat_interval = value['heartbeat_seconds']
                self._heartbeat_timeout = min(self.request_timeout, value['lease_seconds'] / 3, 2)
                self._session_open = True
                with _clients_lock:
                    _clients.add(self)
                self._heartbeat_thread = threading.Thread(target=self._heartbeat, name='fpga-client-heartbeat', daemon=True)
                self._heartbeat_thread.start()
            return self._session_id

    def _heartbeat(self):
        while not self._heartbeat_stop.wait(self._heartbeat_interval):
            try:
                self._request('/sessions/' + self._session_id + '/heartbeat', {}, 'POST',
                              _timeout=self._heartbeat_timeout)
            except SchedulerError as error:
                if error.status in (404, 409):
                    self._session_error = 'Client session expired or ended; create a new Client to submit new work'
                    return
                # A single failed request is not process death. Keep trying
                # this same identity; expired sessions can never be reopened.

    def close(self):
        """Cancel this Client's unfinished tasks; the worker finishes cleanup."""
        if self._pid != os.getpid():
            return
        with self._session_lock:
            if self._closed:
                return
            self._closed = True
            self._heartbeat_stop.set()
            try:
                if self._session_open:
                    self._request('/sessions/' + self._session_id, method='DELETE',
                                  _timeout=min(2, self.request_timeout))
            except SchedulerError:
                pass  # Unreachable daemon: lease expiry still cancels the tasks.
            finally:
                with _clients_lock:
                    _clients.discard(self)
        if self._heartbeat_thread is not None and self._heartbeat_thread is not threading.current_thread():
            self._heartbeat_thread.join(timeout=min(2, self.request_timeout))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, path, payload=None, method='GET', *, raw=None, session=None, _timeout=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = Request(self.endpoint + path, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'})
        if session is not None:
            request.add_header('X-FPGA-Session', session)
        try:
            with urlopen(request, timeout=self.request_timeout if _timeout is None else _timeout) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                detail = json.load(error).get('error', f'HTTP {error.code}')
            except (ValueError, OSError):
                detail = f'HTTP {error.code}'
            raise SchedulerError(detail, error.code) from None
        except (URLError, TimeoutError, OSError, ValueError, HTTPException) as error:
            raise SchedulerError(f'Connection or response failed ({type(error).__name__}); query the same task ID before resubmitting') from None

    def submit(self, image, *, ident=None, timeout=600, idle_seconds=60, queue_timeout=3600):
        ident = uuid.uuid4().hex if ident is None else ident
        content = Path(image).read_bytes()
        if not 0 < len(content) <= MAX_IMAGE:
            raise ValueError('Image must be nonempty and at most 16 MiB')
        payload = validate(dict(id=ident, sha256=hashlib.sha256(content).hexdigest(), timeout=timeout,
                                idle_seconds=idle_seconds, queue_timeout=queue_timeout))
        try:
            session = self._ensure_session()
            try:
                self._request('/jobs/' + ident + '/image', method='PUT', raw=content)
            except SchedulerError as error:
                # A submitted ID has frozen input. POST below determines whether
                # this is an identical retry, or a conflicting use of that ID.
                if error.status != 409:
                    raise
            try:
                return Task.from_dict(self._request('/jobs', payload, 'POST', session=session))
            except SchedulerError as error:
                if error.status is not None:
                    raise
                # POST alone is idempotent; never create a new ID automatically.
                return Task.from_dict(self._request('/jobs', payload, 'POST', session=session))
        except SchedulerError as error:
            error.task_id = ident
            error.args = (f'{error}; task ID: {ident}',)
            raise

    def job(self, ident):
        return Task.from_dict(self._request('/jobs/' + task_id(ident)))

    def jobs(self, states=ACTIVE, *, limit=100, offset=0):
        return self._request('/jobs?' + urlencode(dict(state=','.join(states), limit=limit, offset=offset)))

    def cancel(self, ident):
        return Task.from_dict(self._request('/jobs/' + task_id(ident), method='DELETE'))

    def devices(self):
        return self._request('/devices')

    def health(self):
        return self._request('/health')

    def wait(self, ident, *, timeout=None, poll_interval=2, on_update=None):
        task_id(ident)
        if (not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool)
                or not math.isfinite(poll_interval) or poll_interval <= 0):
            raise ValueError('poll_interval must be positive')
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('wait timeout must be nonnegative')
        deadline = None if timeout is None else time.monotonic() + timeout
        previous = None
        while True:
            try:
                task = self.job(ident)
            except SchedulerError as error:
                error.task_id = ident
                error.args = (f'{error}; task ID: {ident}',)
                raise
            current = task.state, task.position, task.node, task.fpga, task.waiting_reason
            if on_update and current != previous:
                on_update(task)
            previous = current
            if task.state not in ACTIVE:
                return task
            if deadline is not None and time.monotonic() >= deadline:
                raise SchedulerError(f'Wait timed out; task remains queryable: {ident}', ident=ident)
            time.sleep(poll_interval if deadline is None else min(poll_interval, max(0, deadline - time.monotonic())))

    def stream_output(self, ident, *, offset=0, timeout=None, poll_interval=.2):
        return OutputStream(self, ident, offset=offset, timeout=timeout, poll_interval=poll_interval)

    def run_test(self, image, timeout=600, idle_seconds=60, queue_timeout=3600, show=True, *,
                 stream=True, poll_interval=.2):
        """Submit and return an OutputStream by default.

        Iterate the stream to read UART text, then inspect stream.result.
        Set stream=False to wait and return Result directly; show controls
        automatic printing only in that synchronous mode.
        """
        # Validate stream options before submitting any work.
        if type(stream) is not bool:
            raise ValueError('stream must be a boolean')
        OutputStream._validate(0, None, poll_interval)
        task = self.submit(image, timeout=timeout, idle_seconds=idle_seconds, queue_timeout=queue_timeout)
        print(f'[FPGA] Task {task.id}', flush=True)
        reader = OutputStream(self, task.id, timeout=queue_timeout + timeout + 300,
                              poll_interval=poll_interval)
        if stream:
            return reader
        try:
            if show:
                reader.on_update = lambda t: print(
                    f'[FPGA] {t.state} position={t.position} node={t.node} port={t.fpga}', flush=True)
                try:
                    for text in reader:
                        print(text, end='', flush=True)
                except SchedulerError as error:
                    # Older daemons do not expose /output. Retain SDK compatibility.
                    if error.status != 404:
                        raise
                    task = self.wait(task.id, timeout=queue_timeout + timeout + 300)
                    if task.result is not None:
                        print(task.result.output)
                    return self._result(task)
                return reader.result
            task = self.wait(task.id, timeout=queue_timeout + timeout + 300)
        finally:
            reader.close()
        return self._result(task)

    @staticmethod
    def _result(task):
        if task.result is None:
            raise SchedulerError(task.error or f'Task {task.state}', ident=task.id)
        return task.result
