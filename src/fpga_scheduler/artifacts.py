"""Portable validation for immutable, task-scoped DDR output files."""
import re

from .remote.runner import MAX_OUTPUT_BYTES, MAX_OUTPUTS, output_declaration, output_name


def validate_artifacts(items):
    if not isinstance(items, list) or len(items) > MAX_OUTPUTS:
        raise ValueError('Invalid artifact manifest')
    names, total = set(), 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {'name', 'address', 'nbytes', 'dtype', 'shape', 'sha256'}:
            raise ValueError('Invalid artifact metadata')
        declaration = {key: value for key, value in item.items() if key != 'sha256'}
        output_declaration(dict(declaration, version=1))
        digest = item['sha256']
        if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
            raise ValueError('Invalid artifact SHA256')
        if item['name'] in names:
            raise ValueError('Repeated artifact name')
        names.add(item['name'])
        total += item['nbytes']
    if total > MAX_OUTPUT_BYTES:
        raise ValueError('Artifact total byte limit exceeded')
    return items
