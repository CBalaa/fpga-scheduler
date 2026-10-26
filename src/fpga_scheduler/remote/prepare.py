#!/usr/bin/env python3
"""Prepare a new runtime from a server's existing FPGA project (see NOTICE).

Run as the platform account, with no jobs active. The destination must not exist.
The source project and hardware database are never modified.
"""
import argparse
from pathlib import Path
import re
import shutil


def bind_port(source, number):
    # allocate followed by load_db -release loses the selected port. Bind the
    # database itself, otherwise UVHS can silently download onto B0.F0.
    source = re.sub(r'^\s*allocate -board[^\n]*\n', '', source, flags=re.M)
    source, count = re.subn(
        r'^load_db -db \./hw\.dat[^\n]*$',
        'load_db -db ./hw.dat -to {b%d.f%d}' % divmod(number, 4),
        source, flags=re.M)
    releases = list(re.finditer(r'^\s*load_db\s+-release\s*$', source, re.M))
    loads = list(re.finditer(r'^\s*load_db\s+-db\b', source, re.M))
    if count != 1 or len(loads) != 1 or len(releases) != 1 or releases[0].start() >= loads[0].start():
        raise ValueError(f'FPGA{number}: expected exactly one hardware database load')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--run-script', type=Path, help='Legacy runner; omit for strict UART v1 completion')
    parser.add_argument('--scheduler', type=Path, required=True)
    parser.add_argument('--ports', default='4,5,6,7,0,1,2,3')
    parser.add_argument('--destination', type=Path, default=Path.home() / 'fpga-platform')
    args = parser.parse_args()
    ports = tuple(int(p) for p in args.ports.split(','))
    if not ports or len(set(ports)) != len(ports) or any(p not in range(8) for p in ports):
        raise ValueError('ports must be distinct numbers in 0..7')
    scripts = {n: bind_port((args.project / 'user_script/hw_run' / f'hw_run{n}.tcl').read_text(), n)
               for n in ports}
    for path in (args.project / 'Makefile', args.project / 'hw.dat', args.scheduler):
        if not path.exists(): raise ValueError(f'missing runtime input: {path}')
    runner = Path(__file__).with_name('runner.py')
    if args.run_script is not None and not args.run_script.is_file():
        raise ValueError('missing run script')
    if args.run_script is None and not runner.is_file():
        raise ValueError('runner.py must be next to prepare.py')
    args.destination = args.destination.resolve()
    args.destination.mkdir()  # Never overwrite an existing deployment or jobs.
    shutil.copy2(args.project / 'Makefile', args.destination / 'Makefile')
    shutil.copytree(args.project / 'user_script', args.destination / 'user_script')
    for n, source in scripts.items():
        (args.destination / 'user_script/hw_run' / f'hw_run{n}.tcl').write_text(source)
    (args.destination / 'hw.dat').symlink_to((args.project / 'hw.dat').resolve())
    if args.run_script is None:
        shutil.copy2(runner, args.destination / 'runner.py')
        (args.destination / 'run.sh').write_text(
            '#!/bin/bash\nset -euo pipefail\n'
            'exec python3 "$(dirname -- "$0")/runner.py" "$@"\n')
    else:
        shutil.copy2(args.run_script, args.destination / 'run.sh')
    (args.destination / 'run.sh').chmod(0o755)
    shutil.copy2(args.scheduler, args.destination / 'scheduler.py')
    print(f'Prepared {args.destination}; explicitly bound ports: {ports}')


if __name__ == '__main__':
    main()
