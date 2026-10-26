#!/usr/bin/env python3
"""Run NH-only resident acceptance against a locally deployed test daemon.

Run on the FPGA server; only the daemon's configured ports are used. All inputs
are explicit and no board configuration or service lifecycle is changed here.
"""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time


def submit_process(endpoint, token_file, image):
    from fpga_scheduler import Client
    # This process exits immediately after durable submission.
    return Client(endpoint, token_file=token_file).submit(image, timeout=120).id


def main():
    from fpga_scheduler import Client
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:18765')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--hello', type=Path, required=True)
    parser.add_argument('--mark-a', type=Path)
    parser.add_argument('--mark-b', type=Path)
    parser.add_argument('--count-per-port', type=int, default=50)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    c = Client(args.endpoint, token_file=args.token_file, request_timeout=360)
    devices = c.devices()
    ports = {d['number'] for d in devices['devices']}
    if ports != {4, 5} or any(not d['available'] for d in devices['devices']):
        raise RuntimeError('Acceptance requires exactly ports 4,5 READY')
    sessions = {d['number']: d['session'] for d in devices['devices']}
    records = []

    def collect(ident, stage, marker=None):
        task = c.wait(ident, timeout=600, poll_interval=.1)
        data = asdict(task)
        (args.output / (ident + '.json')).write_text(json.dumps(data, indent=2) + '\n')
        result = task.result
        if (task.state != 'completed' or not result or not result.ok or result.app_exit_code != 0
                or 'verify hello: PASS' not in result.uart_output
                or not result.details.get('ddr_readback_matches') or not result.details.get('reset_confirmed')
                or result.details['session'] != sessions[task.fpga]):
            raise RuntimeError(f'Acceptance task failed: {ident}: {data}')
        if marker and marker not in result.uart_output:
            raise RuntimeError(f'Build marker missing for {ident}')
        records.append(dict(id=ident, stage=stage, created=task.created,
                            started=task.started, finished=task.finished, **result.details))
        print(json.dumps(dict(id=ident, stage=stage, fpga=task.fpga, ok=result.ok)), flush=True)
        return task

    if args.mark_a and args.mark_b:
        # Two tickets per stage exercise A/B/A on both ports without exposing a
        # public port-pinning API. Assert the observed assignments explicitly.
        for image, marker in ((args.mark_a, 'resident-build-A'), (args.mark_b, 'resident-build-B'), (args.mark_a, 'resident-build-A')):
            ids = [c.submit(image, timeout=120).id for _ in range(2)]
            observed = {collect(i, 'A-B-A', marker).fpga for i in ids}
            if observed != ports:
                raise RuntimeError('A/B/A did not exercise both ports')
    counts = Counter()
    while min((counts[p] for p in ports), default=0) < args.count_per_port:
        # More independent clients than available slots, including durable
        # jobs whose submitting process has already retired.
        with ProcessPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(submit_process, [args.endpoint] * 6, [args.token_file] * 6, [str(args.hello)] * 6))
        (args.output / 'latest-submitted.json').write_text(json.dumps(ids))
        for ident in ids:
            counts[collect(ident, 'health').fpga] += 1
    after = c.devices()
    if {d['number']: d['session'] for d in after['devices']} != sessions:
        raise RuntimeError('Session changed during healthy acceptance')
    intervals = sorted((r['started'], r['finished'], r['fpga']) for r in records if r['stage'] == 'health')
    parallel = any(a[2] != b[2] and b[0] < a[1] for i,a in enumerate(intervals) for b in intervals[i+1:])
    if not parallel:
        raise RuntimeError('No overlapping task intervals observed')
    manifest = dict(hello_sha256=hashlib.sha256(args.hello.read_bytes()).hexdigest(),
                    counts=dict(counts), sessions=sessions, before=devices, after=after,
                    parallel=parallel, records=records)
    (args.output / 'summary.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(dict(counts=dict(counts), parallel=parallel, sessions=sessions)), flush=True)


if __name__ == '__main__':
    main()
