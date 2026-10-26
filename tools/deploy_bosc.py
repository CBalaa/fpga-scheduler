#!/usr/bin/env python3
"""Prepare an isolated runtime on an existing BOSC SSH host; preserve the project."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host', required=True)
    parser.add_argument('--project', required=True, help='Hardware project directory on the SSH host')
    parser.add_argument('--destination', required=True)
    parser.add_argument('--ports', default='4,5,6,7')
    args = parser.parse_args()
    ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ConnectTimeout=8', args.ssh_host]
    def remote(argv):
        return subprocess.run([*ssh, shlex.join(argv)], check=True, capture_output=True,
                              text=True, timeout=90).stdout.strip()
    info = json.loads(remote(['python3', '-c',
        'from pathlib import Path; import json,shutil,sys,tempfile; '
        'p=Path(sys.argv[1]).expanduser().resolve(); d=Path(sys.argv[2]).expanduser().resolve(); '
        'assert p.is_dir() and not d.exists(), "Project missing or destination exists"; '
        'print(json.dumps(dict(project=str(p), root=str(d), stage=tempfile.mkdtemp(prefix="fpga-scheduler-deploy-"), '
        'uvhs=shutil.which("uvhs2_shell"))))', args.project, args.destination]))
    if not info['uvhs']:
        # The adapter and make run in a login shell, matching normal board use.
        info['uvhs'] = remote(['bash', '-lc', 'command -v uvhs2_shell'])
    source = Path(__file__).resolve().parents[1] / 'src/fpga_scheduler/remote'
    subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                    *[str(source / name) for name in ('prepare.py', 'runtime.py', 'runner.py')],
                    args.ssh_host + ':' + info['stage'] + '/'], check=True, timeout=60)
    info['preparation'] = remote(['python3', info['stage'] + '/prepare.py',
        '--project', info['project'], '--scheduler', info['stage'] + '/runtime.py',
        '--ports', args.ports, '--destination', info['root']])
    info['sha256'] = {}
    for local_name, deployed_name in (('runtime.py', 'scheduler.py'), ('runner.py', 'runner.py')):
        expected = hashlib.sha256((source / local_name).read_bytes()).hexdigest()
        actual = remote(['sha256sum', info['root'] + '/' + deployed_name]).split()[0]
        if actual != expected:
            raise RuntimeError('Deployed ' + deployed_name + ' differs from the local source')
        info['sha256'][deployed_name] = actual
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
