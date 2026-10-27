#!/usr/bin/env python3
"""Run output.bin, download C.bin, and verify every INT32 value on the host."""
import argparse
import os
from pathlib import Path
import struct

from fpga_scheduler import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', nargs='?', default=str(Path(__file__).parent / 'build/output.bin'))
    parser.add_argument('--output', default='C.bin')
    parser.add_argument('--endpoint', default=os.environ.get('FPGA_SCHEDULER_URL', 'http://127.0.0.1:18765'))
    parser.add_argument('--token-file', default=os.environ.get('FPGA_SCHEDULER_TOKEN_FILE'))
    args = parser.parse_args()
    with Client(args.endpoint, token_file=args.token_file, request_timeout=120) as client:
        with client.run_test(args.image, timeout=120) as stream:
            for chunk in stream:
                print(chunk, end='', flush=True)
        if not stream.result.ok:
            raise SystemExit(f'FPGA task failed: {stream.result.execution_status} {stream.result.details}')
        path = client.download_artifact(stream.id, 'C', args.output)
    got = struct.unpack('<32i', path.read_bytes())
    expected = tuple(i * 100 - j * 7 for i in range(4) for j in range(8))
    if got != expected:
        raise SystemExit('Host validation: FAIL')
    print(f'Host validation: PASS (32 exact INT32 values); saved {path}')


if __name__ == '__main__':
    main()
