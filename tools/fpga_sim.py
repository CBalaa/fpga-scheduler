#!/usr/bin/env python3
"""Repository-only offline FPGA simulator. Never installed with fpga-scheduler."""
import argparse
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from fpga_sim_support.runtime import create_application
from fpga_sim_support.scenarios import MODES, create_environment, write_image


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    init = actions.add_parser('init', help='Create a new local test directory')
    init.add_argument('directory')
    init.add_argument('--port', type=int, default=18766)
    init.add_argument('--ports', default='4,5', help='Virtual port whitelist')
    serve = actions.add_parser('serve', help='Run in foreground on localhost; Ctrl+C stops')
    serve.add_argument('directory')
    image = actions.add_parser('image', help='Create a simulated scenario image')
    image.add_argument('path')
    image.add_argument('--mode', choices=MODES, default='hello')
    image.add_argument('--label', default='hello')
    image.add_argument('--duration', type=float, default=.5)
    image.add_argument('--exit-code', type=int, default=0)
    image.add_argument('--output', default='SIMULATED FPGA hello: PASS\n')
    image.add_argument('--fragment-bytes', type=int, default=11)
    args = parser.parse_args(argv)
    try:
        if args.action == 'init':
            result = create_environment(args.directory, port=args.port,
                                        ports=tuple(int(p) for p in args.ports.split(',')))
        elif args.action == 'image':
            result = dict(image=str(write_image(args.path, mode=args.mode, label=args.label,
                              duration=args.duration, exit_code=args.exit_code, output=args.output,
                              fragment_bytes=args.fragment_bytes)))
        else:
            app = create_application(args.directory)
            print(json.dumps(dict(endpoint=f'http://127.0.0.1:{app.config.port}', simulated=True)), flush=True)
            app.serve()
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
