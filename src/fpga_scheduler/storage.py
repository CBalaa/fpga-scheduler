"""Atomic immutable job inputs; active jobs are excluded from retention.

Large images are staged in an append-only 'input.bin.part' file so a dropped
connection can resume at the durably stored offset instead of re-uploading the
whole image. freeze() atomically renames the staged part file to input.bin and
writes the 'sha256' marker that makes the input immutable. The legacy
single-shot put() path keeps writing input.bin directly.
"""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .models import Conflict, MAX_IMAGE, MAX_IMAGE_CHUNK, MAX_IMAGE_LIMIT, task_id

PART_NAME = 'input.bin.part'
_HASH_BLOCK = 1024 * 1024


def _read_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


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
    def __init__(self, root, max_bytes=None, max_image_bytes=MAX_IMAGE):
        if type(max_image_bytes) is not int or not 1 <= max_image_bytes <= MAX_IMAGE_LIMIT:
            raise ValueError(f'max_image_bytes must be an integer within 1..{MAX_IMAGE_LIMIT}')
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.max_bytes = max_bytes
        self.max_image_bytes = max_image_bytes
        self._refresh_usage()

    def _refresh_usage(self):
        # Quota covers both the frozen input and an in-flight staged part file.
        used = sum(p.stat().st_size for p in self.root.rglob('input.bin'))
        used += sum(p.stat().st_size for p in self.root.rglob(PART_NAME))
        self._usage = dict(used_bytes=used, limit_bytes=self.max_bytes, sampled_at=time.time())
        return used

    def storage_status(self):
        # Health must stay responsive while upload/retention holds the IO lock.
        return dict(self._usage)

    @staticmethod
    def _task_dir(root, ident):
        return root / task_id(ident)

    def path(self, ident):
        return self._task_dir(self.root, ident) / 'input.bin'

    def part_path(self, ident):
        return self._task_dir(self.root, ident) / PART_NAME

    def _marker_path(self, ident):
        return self._task_dir(self.root, ident) / 'sha256'

    def put(self, ident, content):
        """Legacy single-shot upload: write input.bin directly, return its SHA256."""
        path = self.path(ident)
        limit = min(MAX_IMAGE, self.max_image_bytes)
        if not 0 < len(content) <= limit:
            raise ValueError(f'Image must contain between 1 byte and {limit} bytes')
        with self.lock:
            if self._marker_path(ident).exists():
                raise Conflict('Submitted input is immutable')
            # A staged part file and a legacy direct write must never coexist:
            # freeze() would prefer the stale staged bytes over this upload.
            if self.part_path(ident).exists():
                raise Conflict('Image upload is already staged for this task ID; '
                               'finish it or submit a new ID')
            used = self._refresh_usage()
            # The guard above means the same task directory holds no part file,
            # so the legacy input is the only usage this write replaces.
            previous = path.stat().st_size if path.exists() else 0
            if self.max_bytes is not None:
                if used - previous + len(content) > self.max_bytes:
                    raise StorageQuotaExceeded('input', used_bytes=used,
                                               requested_bytes=max(0, len(content) - previous),
                                               limit_bytes=self.max_bytes)
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

    def append(self, ident, offset, content):
        """Append one bounded chunk, allowing idempotent prefix retries.

        offset must not exceed the bytes already durable in the staged part
        file. An earlier offset is accepted only when the stored bytes there
        are exactly the stored prefix of content; the missing suffix is then
        appended. Returns dict(offset=..., next_offset=...) where next_offset
        is the number of durably stored bytes.
        """
        ident = task_id(ident)
        if type(offset) is not int or offset < 0:
            raise ValueError('Upload offset must be a nonnegative integer')
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ValueError('Chunk must contain bytes')
        chunk = bytes(content)
        if not 1 <= len(chunk) <= MAX_IMAGE_CHUNK:
            raise ValueError(f'Chunk must contain between 1 byte and {MAX_IMAGE_CHUNK} bytes')
        part = self.part_path(ident)
        with self.lock:
            if self._marker_path(ident).exists():
                raise Conflict('Submitted input is immutable')
            used = self._refresh_usage()
            previous = part.stat().st_size if part.exists() else 0
            if offset > previous:
                raise Conflict(f'Upload offset {offset} is beyond the {previous} bytes already stored')
            if offset < previous:
                with part.open('rb') as handle:
                    handle.seek(offset)
                    stored = handle.read(min(len(chunk), previous - offset))
                if stored != chunk[:len(stored)]:
                    raise Conflict('Retried chunk does not match the bytes already stored at this offset')
                suffix = chunk[len(stored):]
            else:
                suffix = chunk
            next_offset = previous + len(suffix)
            if next_offset > self.max_image_bytes:
                raise ValueError(f'Image must contain between 1 byte and {self.max_image_bytes} bytes')
            if self.max_bytes is not None and used - previous + next_offset > self.max_bytes:
                raise StorageQuotaExceeded('input', used_bytes=used, requested_bytes=len(suffix),
                                           limit_bytes=self.max_bytes)
            if not part.parent.exists():
                part.parent.mkdir(parents=True, exist_ok=True)
                created = True
            else:
                created = not part.exists()
            with part.open('ab') as handle:
                handle.write(suffix)
                handle.flush()
                os.fsync(handle.fileno())
            if created:
                # Make the new directory entry durable together with the bytes.
                try:
                    directory = os.open(part.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError:
                    pass
            os.utime(part.parent, None)
            self._usage = dict(used_bytes=used - previous + next_offset, limit_bytes=self.max_bytes,
                               sampled_at=time.time())
        return dict(offset=offset, next_offset=next_offset)

    def status(self, ident):
        ident = task_id(ident)
        path = self.path(ident)
        part = self.part_path(ident)
        with self.lock:
            complete = self._marker_path(ident).is_file() and path.is_file()
            if complete:
                uploaded = path.stat().st_size
            else:
                uploaded = part.stat().st_size if part.is_file() else 0
        return dict(id=ident, uploaded_bytes=uploaded, max_image_bytes=self.max_image_bytes,
                    complete=complete)

    def freeze(self, ident, digest):
        ident = task_id(ident)
        path = self.path(ident)
        part = self.part_path(ident)
        with self.lock:
            if part.exists():
                size = part.stat().st_size
                if not 0 < size <= self.max_image_bytes or _read_sha256(part) != digest:
                    raise ValueError('Input image checksum mismatch')
                part.replace(path)
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    raise ValueError('Input image has not been uploaded') from None
                if not 0 < size <= self.max_image_bytes or _read_sha256(path) != digest:
                    raise ValueError('Input image checksum mismatch')
            self._marker_path(ident).write_text(digest)
            os.utime(path.parent, None)
            self._refresh_usage()
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
