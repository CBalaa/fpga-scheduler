"""Small standard-library SDK. IDs survive waiting/transport failures."""
from dataclasses import dataclass, field
import hashlib
from http.client import HTTPException
import json
import math
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from .models import ACTIVE, MAX_IMAGE, task_id, validate


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


class Client:
    def __init__(self, endpoint='http://127.0.0.1:8765', token=None, *, token_file=None, request_timeout=30):
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

    def _request(self, path, payload=None, method='GET', *, raw=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = Request(self.endpoint + path, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/octet-stream' if raw is not None else 'application/json'})
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
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
            try:
                self._request('/jobs/' + ident + '/image', method='PUT', raw=content)
            except SchedulerError as error:
                # A submitted ID has frozen input. POST below determines whether
                # this is an identical retry, or a conflicting use of that ID.
                if error.status != 409:
                    raise
            try:
                return Task.from_dict(self._request('/jobs', payload, 'POST'))
            except SchedulerError as error:
                if error.status is not None:
                    raise
                # POST alone is idempotent; never create a new ID automatically.
                return Task.from_dict(self._request('/jobs', payload, 'POST'))
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

    def run_test(self, image, timeout=600, idle_seconds=60, queue_timeout=3600, show=True):
        task = self.submit(image, timeout=timeout, idle_seconds=idle_seconds, queue_timeout=queue_timeout)
        print(f'[FPGA] Task {task.id}', flush=True)
        try:
            task = self.wait(task.id, timeout=queue_timeout + timeout + 300,
                             on_update=(lambda t: print(f'[FPGA] {t.state} position={t.position} node={t.node} port={t.fpga}', flush=True)) if show else None)
        except KeyboardInterrupt:
            try:
                self.cancel(task.id)
                print(f'[FPGA] Cancelled queued task {task.id}', flush=True)
            except SchedulerError:
                print(f'[FPGA] Query task {task.id}; it may already be running', flush=True)
            raise
        if task.result is None:
            raise SchedulerError(task.error or f'Task {task.state}', ident=task.id)
        if show:
            print(task.result.output)
        return task.result
