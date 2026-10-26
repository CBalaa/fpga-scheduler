#!/usr/bin/env python3
"""Extract an administrator-verified python-build-standalone install-only archive.

Supply the expected SHA256 from the publisher's HTTPS release manifest using
an independent trusted channel. This does not download or trust an archive's
own checksum, and never changes system Python. The destination must be new.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('archive', type=Path)
    p.add_argument('--sha256', required=True)
    p.add_argument('--source', required=True, help='Publisher HTTPS release URL for audit provenance')
    p.add_argument('--destination', type=Path, required=True)
    args = p.parse_args()
    if not re.fullmatch('[0-9a-f]{64}', args.sha256) or not args.source.startswith('https://'):
        p.error('expected SHA256 and HTTPS provenance URL required')
    digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    if digest != args.sha256:
        p.error('archive checksum mismatch')
    # Restrict both members and symlink targets to the fresh extraction root.
    with tarfile.open(args.archive) as archive:
        base = args.destination.resolve()
        for member in archive.getmembers():
            target = (base / member.name).resolve()
            if not target.is_relative_to(base) or not member.name.startswith('python/') or member.isdev() or member.isfifo():
                p.error('unsafe archive member')
            if member.issym() or member.islnk():
                linked = ((target.parent if member.issym() else base) / member.linkname).resolve()
                if not linked.is_relative_to(base):
                    p.error('unsafe archive link')
        base.mkdir(mode=0o700)
        archive.extractall(base)
    (base / 'provenance.json').write_text(json.dumps(dict(source=args.source, sha256=digest, archive=args.archive.name), indent=2) + '\n')
    print(base / 'python/bin/python3')


if __name__ == '__main__':
    main()
