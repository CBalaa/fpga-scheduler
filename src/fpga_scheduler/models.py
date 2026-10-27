"""Wire-level validation and state constants shared by service and client."""
import re
import threading

MAX_IMAGE = 16 * 1024 * 1024
STATES = ('queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled')
ACTIVE = ('queued', 'running', 'cancelling')
BUSY_EXIT = 75
BUSY_TOKEN = '[scheduler] RETRY_BEFORE_LOAD'


class Conflict(ValueError):
    """An operation conflicts with an existing immutable input or task."""


class CancelledError(Exception):
    """Cooperative cancellation at a worker's serial hardware safe point."""


class Cancellation:
    """Serialize cancellation with CPU release and terminal-outcome decisions.

    The guard protects only short state changes / the control-channel write,
    never a command acknowledgement or other blocking hardware operation.
    """
    def __init__(self):
        self.guard = threading.RLock()
        self.event = threading.Event()
        self.reason = None
        self.finished = False

    def request(self, reason):
        with self.guard:
            if self.finished:
                return False
            if self.reason is None:
                self.reason = reason
                self.event.set()
            return True

    def check(self):
        with self.guard:
            if self.reason is not None:
                raise CancelledError(self.reason)

    def finish(self):
        with self.guard:
            self.finished = True
            return self.reason


def cancelled_result(reason, *, node='', seconds=0):
    return dict(output='', uart_output='', node=node, seconds=seconds,
                exit_code=130, execution_status='cancelled', app_exit_code=None,
                details=dict(cancel_reason=reason, hardware_started=False))


def task_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('Task ID must be 32 lowercase hexadecimal characters')
    return value


def validate(payload):
    if not isinstance(payload, dict) or set(payload) - {'id', 'sha256', 'timeout', 'idle_seconds', 'queue_timeout'}:
        raise ValueError('Invalid task fields')
    task_id(payload.get('id'))
    digest = payload.get('sha256')
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('Invalid SHA256 digest')
    result = dict(payload)
    for key, default, maximum in (('timeout', 600, 600), ('idle_seconds', 60, 120), ('queue_timeout', 3600, 86400)):
        value = result.setdefault(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'{key} must be an integer between 1 and {maximum}')
    return result
