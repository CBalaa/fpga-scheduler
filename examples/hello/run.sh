#!/usr/bin/env bash
set -euo pipefail

hello_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
hello_root="$(cd -- "$hello_dir/../.." && pwd)"
hello_python="$hello_root/.venv/bin/python"
if [[ ! -x "$hello_python" ]]; then
    hello_python=python3
fi

exec "$hello_python" -u "$hello_dir/hello.py" "$@"
