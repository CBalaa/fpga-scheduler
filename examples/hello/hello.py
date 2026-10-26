#!/usr/bin/env python3
"""Submit hello.bin, wait for the FPGA, and print its UART and exit codes."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
# The SDK uses only the standard library; the example also works without pip install.
sys.path.insert(0, str(ROOT / 'src'))
from fpga_scheduler import Client, SchedulerError


def show_progress(task):
    if task.state == 'queued':
        print(f'排队中，位置: {task.position}，原因: {task.waiting_reason or "等待分配设备"}')
    elif task.state == 'running':
        print(f'运行中: {task.node} / FPGA {task.fpga}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default=os.environ.get('FPGA_SCHEDULER_URL',
                                                            'http://127.0.0.1:8765'))
    parser.add_argument('--token-file', type=Path,
                        default=Path(os.environ.get('FPGA_SCHEDULER_TOKEN_FILE',
                                                    str(ROOT / 'secrets/service.token'))))
    parser.add_argument('--image', type=Path, help='Use an existing bin instead of the default hello')
    parser.add_argument('--toolchain', choices=('gcc', 'llvm'),
                        default=os.environ.get('TOOLCHAIN', 'llvm'))
    parser.add_argument('--gcc-bin', type=Path, help='Directory containing the GNU cross tools')
    parser.add_argument('--gcc-prefix', help='GNU tool prefix, optionally including its directory')
    parser.add_argument('--llvm-bin', type=Path, help='Optional LLVM tool directory; defaults to PATH')
    args = parser.parse_args()

    project = ROOT / 'examples/hello'
    image = args.image if args.image is not None else project / 'build/hello.bin'
    client = Client(args.endpoint, token_file=args.token_file)
    # Report a stopped/unreachable service before spending time building the image.
    client.health()
    if args.image is None:
        print('构建 hello.bin ...')
        command = ['make', '-C', str(project), 'TOOLCHAIN=' + args.toolchain, 'VARIANT=hello',
                   'BUILD_DIR=' + str(project / 'build'),
                   'UART_FREQ_HZ=5529600', 'UART_BAUD=115200', 'all']
        if args.llvm_bin is not None:
            command.append('LLVM_BIN=' + str(args.llvm_bin.resolve()))
        if args.gcc_bin is not None:
            command.append('GCC_BIN=' + str(args.gcc_bin.expanduser().resolve()))
        if args.gcc_prefix is not None:
            command.append('GCC_PREFIX=' + os.path.expanduser(args.gcc_prefix))
        subprocess.run(command, check=True)

    print(f'提交: {image}')
    task = client.submit(image, timeout=240)
    print(f'任务 ID: {task.id}', flush=True)
    task = client.wait(task.id, timeout=4200, on_update=show_progress)
    print(f'任务状态: {task.state}')
    if task.result is None:
        raise RuntimeError(task.error or task.state)

    result = task.result
    print('\n串口输出:')
    print(result.uart_output if result.uart_output is not None else result.output)
    print(f'执行状态: {result.execution_status}')
    print(f'程序退出码 (app_exit_code): {result.app_exit_code}')
    print(f'执行器退出码 (exit_code): {result.exit_code}')
    if task.error:
        print(f'任务错误: {task.error}', file=sys.stderr)
    if not result.ok:
        print('\n完整运行日志:', file=sys.stderr)
        print(result.output, file=sys.stderr)
    return 0 if task.state == 'completed' and result.ok else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (SchedulerError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'执行失败: {error}', file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print('\n停止等待；已提交任务仍可用上方任务 ID 查询。', file=sys.stderr)
        raise SystemExit(130)
