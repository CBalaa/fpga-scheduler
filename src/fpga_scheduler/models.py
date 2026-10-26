"""Wire-level validation and state constants shared by service and client."""
import re

MAX_IMAGE = 16 * 1024 * 1024
STATES = ('queued', 'running', 'completed', 'failed', 'cancelled')
ACTIVE = ('queued', 'running')
BUSY_EXIT = 75
BUSY_TOKEN = '[scheduler] RETRY_BEFORE_LOAD'


class Conflict(ValueError):
    """An operation conflicts with an existing immutable input or task."""


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
