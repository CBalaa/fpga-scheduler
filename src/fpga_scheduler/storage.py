"""Atomic immutable job inputs; active jobs are excluded from retention."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .models import Conflict, MAX_IMAGE, task_id


class StorageQuotaExceeded(OverflowError):
    """Capacity failure with diagnostics safe to expose through the API."""
    def __init__(self, resource, *, used_bytes, reserved_bytes=0, requested_bytes,
                 limit_bytes, disk_free_bytes=None, required_free_bytes=None, sampled_at=None):
        disk_full = (disk_free_bytes is not None and required_free_bytes is not None
                     and disk_free_bytes < required_free_bytes)
        code = 'filesystem_space_exhausted' if disk_full else f'{resource}_storage_quota_exceeded'
        reason = 'Filesystem free space is insufficient' if disk_full else f'{resource.title()} storage quota exceeded'
        message = (f'{reason}: used={used_bytes} B, remaining reservations={reserved_bytes} B, '
                   f'requested={requested_bytes} B, limit={limit_bytes} B')
        if disk_free_bytes is not None:
            message += f', disk free={disk_free_bytes} B'
        action = ('Ask the administrator to free filesystem space' if disk_full else
                  'Ask the administrator to increase resident.max_storage_bytes or shorten retention_days')
        message += f'; hardware was not started. {action}; retained inputs and outputs are protected'
        super().__init__(message)
        self.summary = reason
        self.details = dict(error_code=code, resource=resource, phase='admission', hardware_started=False,
                            used_bytes=used_bytes, reserved_bytes=reserved_bytes,
                            requested_bytes=requested_bytes, limit_bytes=limit_bytes, action=action,
                            sampled_at=time.time() if sampled_at is None else sampled_at)
        if disk_free_bytes is not None:
            self.details.update(disk_free_bytes=disk_free_bytes, required_free_bytes=required_free_bytes)


class Images:
    def __init__(self, root, max_bytes=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.max_bytes = max_bytes
        self._refresh_usage()

    def _refresh_usage(self):
        used = sum(p.stat().st_size for p in self.root.rglob('input.bin'))
        self._usage = dict(used_bytes=used, limit_bytes=self.max_bytes, sampled_at=time.time())
        return used

    def storage_status(self):
        # Health must stay responsive while upload/retention holds the IO lock.
        return dict(self._usage)

    def path(self, ident):
        return self.root / task_id(ident) / 'input.bin'

    def put(self, ident, content):
        path = self.path(ident)
        if not 0 < len(content) <= MAX_IMAGE:
            raise ValueError('Image must contain between 1 byte and 16 MiB')
        with self.lock:
            if (path.parent / 'sha256').exists():
                raise Conflict('Submitted input is immutable')
            used = self._refresh_usage()
            previous = path.stat().st_size if path.exists() else 0
            if self.max_bytes is not None:
                if used - previous + len(content) > self.max_bytes:
                    raise StorageQuotaExceeded('input', used_bytes=used,
                                               requested_bytes=max(0, len(content) - previous), limit_bytes=self.max_bytes)
            path.parent.mkdir(exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(path)
                os.utime(path.parent, None)
                self._usage = dict(used_bytes=used - previous + len(content), limit_bytes=self.max_bytes,
                                   sampled_at=time.time())
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return hashlib.sha256(content).hexdigest()

    def freeze(self, ident, digest):
        with self.lock:
            path = self.path(ident)
            try:
                content = path.read_bytes()
            except OSError:
                raise ValueError('Input image has not been uploaded') from None
            if not 0 < len(content) <= MAX_IMAGE or hashlib.sha256(content).hexdigest() != digest:
                raise ValueError('Input image checksum mismatch')
            (path.parent / 'sha256').write_text(digest)
            os.utime(path.parent, None)
            return path

    def cleanup(self, before, protected=()):
        protected = set(protected)
        with self.lock:
            for directory in self.root.iterdir():
                if (directory.name not in protected and not directory.is_symlink()
                        and directory.is_dir() and len(directory.name) == 32
                        and all(c in '0123456789abcdef' for c in directory.name)
                        and directory.stat().st_mtime < before):
                    shutil.rmtree(directory)
            self._refresh_usage()
