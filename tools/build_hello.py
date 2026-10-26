#!/usr/bin/env python3
"""Build this repository's standalone NH hello and optional acceptance variants."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', choices=('gcc', 'llvm'),
                        default=os.environ.get('TOOLCHAIN', 'llvm'))
    parser.add_argument('--gcc-bin', type=Path, help='Directory containing the GNU cross tools')
    parser.add_argument('--gcc-prefix', help='GNU tool prefix, optionally including its directory')
    parser.add_argument('--llvm-bin', type=Path, help='Optional LLVM tool directory; defaults to PATH')
    parser.add_argument('--output', type=Path, default=Path('build/hello'))
    parser.add_argument('--variant', choices=('hello', 'exit7', 'trap', 'hang'), default='hello')
    args = parser.parse_args()
    output = args.output.resolve() / args.variant
    output.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    command = ['make', '-C', str(project / 'examples/hello'),
               'TOOLCHAIN=' + args.toolchain, 'BUILD_DIR=' + str(output), 'VARIANT=' + args.variant,
               'UART_FREQ_HZ=5529600', 'UART_BAUD=115200', 'all']
    if args.llvm_bin is not None:
        command.append('LLVM_BIN=' + str(args.llvm_bin.resolve()))
    if args.gcc_bin is not None:
        command.append('GCC_BIN=' + str(args.gcc_bin.expanduser().resolve()))
    if args.gcc_prefix is not None:
        command.append('GCC_PREFIX=' + os.path.expanduser(args.gcc_prefix))
    subprocess.run(command, check=True)
    image = output / 'hello.bin'
    manifest = dict(variant=args.variant, toolchain=args.toolchain,
                    sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                    bytes=image.stat().st_size, sys_clk_hz=5529600, protocol='uart-v1')
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
