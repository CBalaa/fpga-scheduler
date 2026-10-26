#!/usr/bin/env python3
"""Explicit live acceptance through HTTP. Runs only the configured device pool."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import time

from fpga_scheduler import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://127.0.0.1:8765')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--images', type=Path, default=Path('build/hello'))
    parser.add_argument('--output', type=Path, default=Path('build/acceptance'))
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--ports', default='4,5,6,7')
    args = parser.parse_args()
    expected_ports = {int(port) for port in args.ports.split(',')}
    if args.jobs <= len(expected_ports):
        parser.error('--jobs must exceed the port count to test queueing')
    args.output.mkdir(parents=True, exist_ok=True)
    client = Client(args.endpoint, token_file=args.token_file, request_timeout=90)
    inventory = client.devices()
    (args.output / 'devices-before.json').write_text(json.dumps(inventory, indent=2))
    actual_ports = {device['number'] for device in inventory['devices']}
    assert actual_ports == expected_ports, inventory
    assert all(device['available'] for device in inventory['devices']), inventory
    hello = args.images / 'hello/hello.bin'
    report = dict(started=time.time(), image_sha256=hashlib.sha256(hello.read_bytes()).hexdigest(),
                  jobs=[], faults=[], checks={})

    def submit(_):
        return client.submit(hello, timeout=240, queue_timeout=1800)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        submitted = list(pool.map(submit, range(args.jobs)))
    (args.output / 'submitted.json').write_text(json.dumps([asdict(task) for task in submitted], indent=2))
    cancelled = client.submit(hello)
    cancelled = client.cancel(cancelled.id)
    assert cancelled.state == 'cancelled', cancelled
    report['cancelled'] = asdict(cancelled)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    maximum_running = 0
    queued_while_full = False
    deadline = time.monotonic() + 900
    previous = None
    with (args.output / 'snapshots.jsonl').open('w') as snapshots:
        while time.monotonic() < deadline:
            tasks = [client.job(task.id) for task in submitted]
            counts = Counter(task.state for task in tasks)
            maximum_running = max(maximum_running, counts['running'])
            queued_while_full |= counts['running'] == len(expected_ports) and counts['queued'] > 0
            snapshot = dict(time=time.time(), counts=dict(counts), jobs=[
                dict(id=task.id, state=task.state, node=task.node, fpga=task.fpga,
                     position=task.position, started=task.started, finished=task.finished) for task in tasks])
            snapshots.write(json.dumps(snapshot) + '\n')
            snapshots.flush()
            if dict(counts) != previous:
                print(json.dumps(dict(phase='parallel', **snapshot)), flush=True)
                previous = dict(counts)
            if not counts['running'] and not counts['queued']:
                break
            time.sleep(2)
        else:
            raise TimeoutError('Acceptance waiting deadline exceeded; query submitted IDs')
    for task in tasks:
        record = asdict(task)
        report['jobs'].append(record)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2))
        if task.result:
            (args.output / (task.id + '.log')).write_text(task.result.output)
            (args.output / (task.id + '.uart.log')).write_text(task.result.uart_output or '')
        assert task.state == 'completed' and task.result.ok, record
        assert task.result.execution_status == 'exited' and task.result.app_exit_code == 0, record
        assert task.result.details['ddr_readback_matches'], record
        uart = task.result.uart_output or ''
        (args.output / (task.id + '.uart.log')).write_text(uart)
        assert 'verify hello: PASS' in uart and '=== Hello Done ===' in uart, task.id
        # Verify physical mapping using actual UVHS download evidence as well as
        # API assignment; a Tcl -to declaration alone is insufficient.
        physical = 'B%d.F%d' % divmod(task.fpga, 4)
        targets = re.findall(r'download -target dut -board P0\.(B[01]\.F[0-3])[^\n]*success',
                             task.result.output)
        assert targets == [physical], 'Unexpected physical download targets: ' + str(targets)
    used_ports = {task.fpga for task in tasks}
    assert used_ports == expected_ports, used_ports
    for port in used_ports:
        sequence = sorted((task for task in tasks if task.fpga == port), key=lambda task: task.started)
        assert all(a.finished <= b.started for a, b in zip(sequence, sequence[1:])), sequence
    ordered = sorted(tasks, key=lambda task: task.created)
    assert [task.id for task in ordered] == [task.id for task in sorted(tasks, key=lambda task: task.started)], 'FIFO violation'
    assert queued_while_full and maximum_running == len(expected_ports), 'No observed full pool with queue'
    report['checks'].update(fifo=True, exclusive_ports=True, all_ports=True,
                            maximum_running=maximum_running, queued_while_full=queued_while_full,
                            queued_cancel=True)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))

    for variant, expected, code, timeout in (('exit7', 'exited', 7, 240),
                                           ('trap', 'trap', None, 240),
                                           ('hang', 'timeout', None, 90)):
        task = client.submit(args.images / variant / 'hello.bin', timeout=timeout)
        print(json.dumps(dict(phase=variant, id=task.id)), flush=True)
        task = client.wait(task.id, timeout=timeout + 240, poll_interval=2)
        record = asdict(task)
        report['faults'].append(record)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2))
        if task.result:
            (args.output / (task.id + '.log')).write_text(task.result.output)
        assert task.state == 'failed' and task.result is not None, record
        assert task.result.execution_status == expected and task.result.app_exit_code == code, record
        assert task.result.details['application_started'] and task.result.details['uvhs_prompt_seen'], record
        print(json.dumps(dict(phase=variant, execution_status=task.result.execution_status,
                              app_exit_code=task.result.app_exit_code, exit_code=task.result.exit_code)), flush=True)
    # Successful work after faults proves resources can be reused.
    task = client.submit(hello, timeout=240)
    task = client.wait(task.id, timeout=480, poll_interval=2)
    report['recovery'] = asdict(task)
    assert task.state == 'completed' and task.result.app_exit_code == 0, asdict(task)
    (args.output / (task.id + '.log')).write_text(task.result.output)
    inventory = client.devices()
    (args.output / 'devices-after.json').write_text(json.dumps(inventory, indent=2))
    assert not inventory['errors'] and all(d['available'] for d in inventory['devices']), inventory
    report['checks'].update(faults=True, recovery=True, resources_released=True)
    report['finished'] = time.time()
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(report=str(args.output / 'report.json'), checks=report['checks'])), flush=True)


if __name__ == '__main__':
    main()
