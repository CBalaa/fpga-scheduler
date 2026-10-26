# BOSC NH 板端运行时

本目录包含编译进 FPGA 裸机 bin 的公共代码，供本仓库的
[hello](../../examples/hello/README.md) 等程序使用。

| 文件 | 作用 |
|---|---|
| `crt_uart.S`、`encoding.h` | RV64 单核入口、寄存器/FPU/栈初始化、清零 BSS/TBSS、trap 入口 |
| `bare_runtime.c`、`bare_runtime.h` | UART 初始化、调用 main、捕获原始返回值、异常处理和生命周期 hook |
| `uart.c`、`uart.h` | UART MMIO 驱动与字符串、整数输出；按频率和波特率配置除数 |
| `uart_protocol.c` | 启动、退出值和异常的 UART v1 上报 |
| `toolchain.mk` | 通过 TOOLCHAIN 选择 GNU 或 LLVM 工具，支持目录、前缀和 RISCV_* 覆盖 |

默认配置为 NH 单核、系统时钟 5.5296 MHz、UART 115200 8N1、MMIO 基址
`0x310b0000`。链接地址由应用的链接脚本决定；hello 的入口为 `0x80000000`。
完成协议见 [completion.md](../../docs/completion.md)。

构建使用 `rv64gc/lp64d`，GCC 和 GNU as/ld 可直接处理现有启动汇编及链接脚本。
`TOOLCHAIN=gcc` 选择 `riscv64-unknown-elf-gcc`、GNU ld/objcopy/objdump；
`GCC_BIN` 指定目录，`GCC_PREFIX`（默认取 `CROSS_COMPILE`）指定工具前缀。
`TOOLCHAIN=llvm` 保留原有默认行为和 `LLVM_BIN`。各个 `RISCV_*` 覆盖始终优先。
详见 [工具链说明](../../docs/toolchains.md)和 [hello 示例](../../examples/hello/README.md)。

CRT、encoding、bare runtime 来自 Buddy 的 `examples/BOSCAMEFPGA/common/runtime`，
可配置 UART 驱动来自 `runtime/boscame/uart`。本地适配将默认时钟设为调度池的
5.5296 MHz，并使用本仓库的工具链选择和完成协议；程序源码和构建不依赖外部
Buddy 工作区。原许可证头保留，来源说明见 [NOTICE](../../NOTICE)。
