#!/usr/bin/env python3
"""One command hello: python run.py hello.bin --token-file client.token."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from fpga_scheduler import Client


def submit(endpoint, token_file, image):
    with Client(endpoint, token_file=token_file) as client:
        task = client.submit(image, timeout=120)
        done = client.wait(task.id)
        return task.id, done.fpga, done.result.ok if done.result else done.error


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('image')
    p.add_argument('--endpoint', default='http://127.0.0.1:8765')
    p.add_argument('--token-file', required=True)
    p.add_argument('--mode', choices=('sync', 'stream', 'submit', 'multiprocess'), default='stream')
    args = p.parse_args()
    c = Client(args.endpoint, token_file=args.token_file)
    if args.mode == 'sync':
        r = c.run_test(args.image, timeout=120, stream=False)
        print(r.execution_status, r.app_exit_code, r.ok)
    elif args.mode == 'stream':
        with c.run_test(args.image, timeout=120) as stream:
            for chunk in stream:
                print(chunk, end='', flush=True)
            r = stream.result
        print(r.execution_status, r.app_exit_code, r.ok)
    elif args.mode == 'submit':
        task = c.submit(args.image, timeout=120)
        print(task.id, flush=True)
        # Nonblocking submit still belongs to this process. Keep it alive.
        done = c.wait(task.id)
        print(done.state, done.result)
    else:
        with ProcessPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, [args.endpoint] * 6, [args.token_file] * 6, [args.image] * 6))
        for result in results:
            print(*result)
    c.close()


if __name__ == '__main__':
    main()
