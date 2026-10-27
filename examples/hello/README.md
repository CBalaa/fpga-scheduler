# 独立构建并运行 NH hello

本目录包含从 Buddy `examples/BOSCAMEFPGA/hello` 移植的裸机程序、Makefile 和链接
脚本，以及调用调度服务的 Python 示例。启动代码、UART 驱动和完成协议位于本仓库
的 [runtime/bosc](../../runtime/bosc/README.md)，构建所需源码均在本仓库内。

需要 GNU make、Python 3.11+，以及一套 RISC-V 裸机交叉工具链：

- GNU：`riscv64-unknown-elf-gcc`（调用 GNU assembler）、`riscv64-unknown-elf-ld`、
  `riscv64-unknown-elf-objcopy`；反汇编使用 `riscv64-unknown-elf-objdump`。
- LLVM（默认，可选）：`clang`、`ld.lld`、`llvm-objcopy`、`llvm-objdump`。

`TOOLCHAIN=gcc` 的编译、汇编、链接和 bin 生成全部使用 GNU 工具，不需要 LLVM、
libc 或外部 Buddy 源码。安装及用户目录部署方法见 [工具链说明](../../docs/toolchains.md)。

## 一条命令构建并上板

在仓库根目录执行：

```bash
./examples/hello/run.sh --toolchain gcc
```

也可以从任意目录使用脚本的绝对路径，无需激活虚拟环境或手动设置令牌。
脚本优先使用仓库的 `.venv/bin/python`，否则使用 Python 3.11+ 的 `python3`。

脚本默认连接 `http://127.0.0.1:8765`，读取仓库的 `secrets/service.token`，
先构建本目录的 `build/hello.bin`，再上传、排队并等待结果。FPGA 端口由服务端分配；
当前常驻配置使用 4、5。输出包括任务 ID、排队/运行状态、UART 和两个退出码。
成功时脚本返回 0，任务失败时返回 1；Ctrl-C 退出会取消未结束任务，仍可查询收尾结果。

每次调用默认示例都会运行 make，修改 `hello.c` 后可以直接再次执行脚本。
省略 `--toolchain` 时沿用 LLVM，也可通过环境变量 `TOOLCHAIN=gcc` 设置默认选择。
使用 `--image` 时直接提交指定 bin。程序通过
[UART 完成协议](../../docs/completion.md) 报告 main 的返回值和异常。

成功输出的末尾应为：

```text
执行状态: exited
程序退出码 (app_exit_code): 0
执行器退出码 (exit_code): 0
```

`app_exit_code` 是 FPGA 上 main 的原始返回值；`exit_code` 是 Linux 执行器状态。
单次执行预算为 240 秒，等待预算为 4200 秒，包含排队与回收。

可选参数：

```bash
./examples/hello/run.sh --image /path/to/hello.bin
./examples/hello/run.sh --toolchain gcc --gcc-bin /opt/riscv/bin
./examples/hello/run.sh --toolchain gcc --gcc-prefix /opt/riscv/bin/riscv64-unknown-elf-
./examples/hello/run.sh --toolchain llvm --llvm-bin /path/to/llvm/bin
./examples/hello/run.sh --endpoint http://127.0.0.1:8765 --token-file /path/to/service.token
./examples/hello/run.sh --help
```

服务地址和令牌路径也可以用 `FPGA_SCHEDULER_URL`、`FPGA_SCHEDULER_TOKEN_FILE` 设置。
相对参数路径以调用脚本时的当前目录为准；默认路径以仓库根目录为准。

调度服务需要提前启动；本地真机环境可在另一个终端执行
`.venv/bin/fpga-scheduler serve --config config.local.toml`。完整配置见
[真机接入说明](../../docs/live-setup.md)。

## 只构建，不上板

在仓库根目录执行：

```bash
make -C examples/hello TOOLCHAIN=gcc
make -C examples/hello TOOLCHAIN=gcc size dump
```

也可以在本目录直接执行 `make TOOLCHAIN=gcc all size`。产物为 `build/hello.elf` 和
`build/hello.bin`，入口为 `0x80000000`，只启动 NH 单核。默认系统时钟为
5.5296 MHz，UART 为 115200 8N1，UART 除数为 3。
两套工具链均使用 `-march=rv64gc -mabi=lp64d -mcmodel=medany`。启动代码初始化
寄存器、FPU、gp 和 1 MiB 栈，清零 BSS/TBSS，再进入 C 运行时调用 main。
`main()` 打印 `Hello, World!`，校验 `40 + 2` 得到 `0000002A` 后返回 0。
该表达式由编译器常量折叠，验证的是启动、打印、校验和完成上报链路。

可覆盖构建目录、工具链和时钟：

```bash
make -C examples/hello TOOLCHAIN=gcc GCC_BIN=/opt/riscv/bin
make -C examples/hello TOOLCHAIN=gcc GCC_BIN=/opt/riscv/bin GCC_PREFIX=riscv64-none-elf-
make -C examples/hello TOOLCHAIN=gcc GCC_PREFIX=/opt/riscv/bin/riscv64-unknown-elf-
make -C examples/hello TOOLCHAIN=gcc CROSS_COMPILE=/opt/riscv/bin/riscv64-unknown-elf-
make -C examples/hello TOOLCHAIN=llvm LLVM_BIN=/path/to/llvm/bin
make -C examples/hello TOOLCHAIN=gcc BUILD_DIR=/tmp/hello-build
make -C examples/hello TOOLCHAIN=gcc UART_FREQ_HZ=5529600 UART_BAUD=115200
```

Makefile 保留 `RISCV_CC`、`RISCV_LD`、`RISCV_OBJCOPY`、`RISCV_OBJDUMP` 和
`EXTRA_OBJECTS`、`EXTRA_CFLAGS`、`EXTRA_LDFLAGS` 覆盖入口。时钟设置须与目标
FPGA 平台一致。工具链、编译参数或变体变化时会自动重新构建。
`GCC_PREFIX` 默认是 `riscv64-unknown-elf-`，也接受包含目录的完整前缀；
`CROSS_COMPILE` 是它的兼容入口，显式 `GCC_PREFIX` 优先。`GCC_BIN` 用于工具在
PATH 外、但前缀不含目录的情况。单独的 `RISCV_*` 命令覆盖优先于上述选择，例：

```bash
make -C examples/hello TOOLCHAIN=gcc \
  RISCV_CC=/opt/riscv/bin/riscv64-unknown-elf-gcc \
  RISCV_LD='/opt/riscv/bin/riscv64-unknown-elf-ld -m elf64lriscv' \
  RISCV_OBJCOPY=/opt/riscv/bin/riscv64-unknown-elf-objcopy \
  RISCV_OBJDUMP=/opt/riscv/bin/riscv64-unknown-elf-objdump
```

脚本同样继承导出的 `GCC_BIN`、`GCC_PREFIX`、`CROSS_COMPILE` 和 `RISCV_*` 环境
变量；`--gcc-bin`、`--gcc-prefix` 则显式传给 make。

验收工具需要的四种 bin 可以通过以下命令生成到仓库的 `build/hello/<变体>/`，
同时生成 SHA256 manifest：

```bash
python3 tools/build_hello.py --toolchain gcc
python3 tools/build_hello.py --toolchain gcc --variant exit7
python3 tools/build_hello.py --toolchain gcc --variant trap
python3 tools/build_hello.py --toolchain gcc --variant hang
```

构建工具同样支持 `--gcc-bin`、`--gcc-prefix`、`--llvm-bin`；manifest 记录工具链
种类、变体和实际 bin 的 SHA256。不同编译器的 bin 无须逐字节相同。

也可直接用 `make -C examples/hello TOOLCHAIN=gcc VARIANT=exit7`，结果在本目录的
`build/exit7/`。故障包装仅由这些显式验收变体链接，源码位于
[tests/fixtures/bosc_main_faults.c](../../tests/fixtures/bosc_main_faults.c)。

| 变体 | 数值校验 | execution_status | app_exit_code | exit_code |
|---|---|---|---|---|
| hello | 42 / PASS | exited | 0 | 0 |
| exit7 | 42 / PASS | exited | 7 | 1 |
| trap | 42 / PASS 后触发非法指令 | trap | null | 1 |
| hang | 42 / PASS 后等待，不发送完成上报 | timeout | null | 124 |

GCC 版本及实际 FPGA 验证证据见 [GCC 验证记录](../../docs/gcc-validation.md)。

## 文件

| 文件 | 作用 |
|---|---|
| [hello.c](hello.c) | FPGA 上的裸机应用和日志 hook |
| [makefile](makefile) | 编译、链接、生成 bin、反汇编和显式故障变体 |
| [platform/hello_nr.ld](platform/hello_nr.ld) | NH 入口、DDR 段、BSS/TBSS 和 1 MiB 栈 |
| [hello.py](hello.py) | 构建后通过 SDK 提交、等待并打印结果 |
| [run.sh](run.sh) | 从任意目录直接运行示例 |

源代码沿用 Apache-2.0 许可证，来源及适配说明见 [NOTICE](../../NOTICE)。
