# hello 裸机交叉工具链

hello 的应用、CRT、UART 驱动、完成协议和链接脚本均来自本仓库。
构建不访问外部 Buddy 工作区，也不需要 newlib/libc。构建机需要 GNU make；
调度客户端需要 Python 3.11+。FPGA 服务器继续使用既有运行工程和 bitstream，
不需要安装交叉编译器。

## GNU GCC / binutils

```bash
make -C examples/hello TOOLCHAIN=gcc
make -C examples/hello TOOLCHAIN=gcc dump
./examples/hello/run.sh --toolchain gcc
```

编译及预处理汇编由 `riscv64-unknown-elf-gcc` 完成，gcc 调用 GNU `as`。
链接直接调用 `riscv64-unknown-elf-ld -m elf64lriscv`，生成 bin 使用
`riscv64-unknown-elf-objcopy`，反汇编使用 `riscv64-unknown-elf-objdump`。
该路径不调用 clang、LLVM 集成汇编器、ld.lld 或 llvm-objcopy。

工具默认从 PATH 查找。Ubuntu/Debian 可以安装发行版的
`gcc-riscv64-unknown-elf`、`binutils-riscv64-unknown-elf` 包。
如需在用户目录安装，可在与构建机发行版匹配的 apt 源上下载并解包：

```bash
gcc_root="$HOME/.local/share/fpga-scheduler/toolchains/local"
mkdir -p "$gcc_root/packages"
(
  cd "$gcc_root/packages"
  apt download gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf
  for package in ./*.deb; do
    dpkg-deb -x "$package" "$gcc_root"
  done
)
# Ubuntu Jammy 的 GCC 解包迁移后需要补齐相对的交叉 binutils 搜索目录。
if [ -d "$gcc_root/usr/lib/riscv64-unknown-elf" ] &&
   [ ! -e "$gcc_root/usr/riscv64-unknown-elf" ]; then
  ln -s lib/riscv64-unknown-elf "$gcc_root/usr/riscv64-unknown-elf"
fi
export PATH="$gcc_root/usr/bin:$PATH"
riscv64-unknown-elf-gcc --version
riscv64-unknown-elf-gcc -print-prog-name=as
./examples/hello/run.sh --toolchain gcc
```

`-print-prog-name=as` 应指向这套交叉工具的 assembler。若指向宿主机 `as`，
需修正工具链安装布局，否则汇编时会出现宿主机不识别 `-march=rv64gc` 的错误。
使用下载包时仍依赖构建机提供其宿主运行库，应选择匹配的发行版包。

本次已在用户目录准备 Ubuntu Jammy 的 **GCC 10.2.0 / binutils 2.35.1**，
存储于 `$HOME/.local/share/fpga-scheduler/toolchains/ubuntu-jammy-gcc-10.2.0`。
包来自 Ubuntu 官方 HTTPS 源，SHA256 已与 apt 索引核对；包、校验记录和安装信息
位于该目录的 `packages/`、`installation.json`。常用工具已链接到
`$HOME/.local/bin`，无需修改系统安装。也可显式使用此安装：

```bash
./examples/hello/run.sh --toolchain gcc --gcc-bin \
  "$HOME/.local/share/fpga-scheduler/toolchains/ubuntu-jammy-gcc-10.2.0/usr/bin"
```

## 选择工具目录及前缀

| Make 变量 | 默认值 / 作用 | 脚本参数 |
|---|---|---|
| `TOOLCHAIN` | `llvm`；指定 `gcc` 使用 GNU | `--toolchain gcc` / `--toolchain llvm` |
| `GCC_BIN` | GNU 工具所在目录；默认 PATH | `--gcc-bin /opt/riscv/bin` |
| `GCC_PREFIX` | `riscv64-unknown-elf-`，可含完整目录 | `--gcc-prefix riscv64-none-elf-` |
| `CROSS_COMPILE` | `GCC_PREFIX` 未显式指定时使用的兼容前缀 | 继承同名环境变量 |
| `LLVM_BIN` | LLVM 工具目录；默认 PATH | `--llvm-bin /opt/llvm/bin` |
| `RISCV_CC` / `RISCV_LD` / `RISCV_OBJCOPY` / `RISCV_OBJDUMP` | 直接覆盖各工具命令，优先于工具链选择 | 继承同名环境变量 |

`GCC_BIN` 与不含目录的前缀配合，或者单独传包含目录的完整 `GCC_PREFIX`：

```bash
make -C examples/hello TOOLCHAIN=gcc GCC_BIN=/opt/riscv/bin GCC_PREFIX=riscv64-none-elf-
make -C examples/hello TOOLCHAIN=gcc GCC_PREFIX=/opt/riscv/bin/riscv64-unknown-elf-
make -C examples/hello TOOLCHAIN=gcc CROSS_COMPILE=/opt/riscv/bin/riscv64-unknown-elf-
```

这些选项也适用于 `tools/build_hello.py --toolchain gcc`。
命令行选项优先于同名环境变量。使用 `RISCV_CC` 覆盖时需包含编译器所需的目标选项：
GNU gcc 自带 RISC-V 目标，clang 则需要 `--target=riscv64-unknown-elf`。
直接覆盖链接器命令时保留 `-m elf64lriscv`。

## LLVM 及共同的板端约定

LLVM 沿用默认选择；可执行 `make -C examples/hello TOOLCHAIN=llvm` 或
`./examples/hello/run.sh --toolchain llvm`，并可设置 `LLVM_BIN`。
GCC 和 LLVM 都使用 `rv64gc/lp64d`、`medany`、freestanding、无默认库的构建方式。
GCC 命令不会收到 clang 的 `--target` 参数。

两者共用同一启动汇编、链接脚本和完成上报实现：入口 `0x80000000`，1 MiB 栈，
启动时设置 gp/sp、FPU 和 trap 向量并清零 BSS/TBSS；系统时钟 5.5296 MHz，
UART 115200、除数 3。`main()` 返回值通过 UART v1 上报，不按 UART 普通 PASS
文本或 UVHS 提示符判断退出。详见 [完成协议](completion.md)。

工具、编译参数或变体改变时，构建配置戳触发重编译，可以复用同一个 BUILD_DIR。
不同编译器或版本可以产生不同 bin；数值校验和退出码约定相同。
本次 GNU 实测及日志见 [GCC 验证记录](gcc-validation.md)。
