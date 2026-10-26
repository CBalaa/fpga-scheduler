"""Atomic immutable job inputs; active jobs are excluded from retention."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading

from .models import Conflict, MAX_IMAGE, task_id


class Images:
    def __init__(self, root, max_bytes=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.max_bytes = max_bytes

    def path(self, ident):
        return self.root / task_id(ident) / 'input.bin'

    def put(self, ident, content):
        path = self.path(ident)
        if not 0 < len(content) <= MAX_IMAGE:
            raise ValueError('Image must contain between 1 byte and 16 MiB')
        with self.lock:
            if (path.parent / 'sha256').exists():
                raise Conflict('Submitted input is immutable')
            if self.max_bytes is not None:
                used = sum(p.stat().st_size for p in self.root.rglob('input.bin'))
                previous = path.stat().st_size if path.exists() else 0
                if used - previous + len(content) > self.max_bytes:
                    raise OverflowError('Input storage quota exceeded; active inputs are protected')
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
