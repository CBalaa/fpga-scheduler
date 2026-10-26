#!/usr/bin/env python3
"""One command hello: python run.py hello.bin --token-file client.token."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from fpga_scheduler import Client


def submit(endpoint, token_file, image):
    return Client(endpoint, token_file=token_file).submit(image, timeout=120).id


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('image')
    p.add_argument('--endpoint', default='http://127.0.0.1:8765')
    p.add_argument('--token-file', required=True)
    p.add_argument('--mode', choices=('sync', 'submit', 'multiprocess'), default='sync')
    args = p.parse_args()
    c = Client(args.endpoint, token_file=args.token_file)
    if args.mode == 'sync':
        r = c.run_test(args.image, timeout=120)
        print(r.execution_status, r.app_exit_code, r.ok)
    elif args.mode == 'submit':
        print(c.submit(args.image, timeout=120).id)
    else:
        with ProcessPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(submit, [args.endpoint] * 6, [args.token_file] * 6, [args.image] * 6))
        for ident in ids:
            task = c.wait(ident)
            print(ident, task.fpga, task.result.ok if task.result else task.error)


if __name__ == '__main__':
    main()
