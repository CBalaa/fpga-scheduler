# GNU RISC-V GCC 验证记录

2026-10-26，基于本仓库的 standalone hello，完成 GNU 工具链构建及 BOSC 真机验证。
编译不依赖 LLVM 或外部 Buddy 源码。工具安装和选择方法见
[工具链说明](toolchains.md)，一条命令上板见 [hello README](../examples/hello/README.md)。

## 工具及构建检查

- 编译器：`riscv64-unknown-elf-gcc` **10.2.0**。
- GNU assembler、ld、objcopy、objdump：**2.35.1**。
- 工具来自 Ubuntu Jammy 官方包，校验 SHA256 后解包到用户目录，无系统安装修改。
  GCC 的 assembler 搜索路径已修正，驱动日志确认调用的是交叉 GNU as。
- 目标为 `rv64gc/lp64d`、`medany`；GCC 不使用 clang 的 `--target` 参数。
  GNU as 接受现有 CSR/FPU 指令、gp 的 `.option norelax` 和 BSS/TBSS 清零循环，
  GNU ld 接受现有 PHDRS、NOLOAD、链接断言和 `--wrap=main`，无需修改启动逻辑或地址布局。
- GNU readelf/objdump 确认 ELF64 little-endian RISC-V，入口和 `_start` 均为
  `0x80000000`，RVC/double-float ABI 标志正确，栈大小为 1 MiB，BSS/TBSS 边界对齐。

四种变体均实际编译、汇编、链接并生成 raw bin：

| 变体 | 大小（字节） | 预期应用行为 |
|---|---|---|
| hello | 1925 | 校验 42，返回 0 |
| exit7 | 1941 | 校验 42，返回 7 |
| trap | 1941 | 校验 42，随后执行非法指令 |
| hang | 1941 | 校验 42，随后等待，不发送完成记录 |

普通 hello 的 SHA256 为
`ec4f053ce89d505cb72ad8f018e3ced5df9364515d4f3a1a2c9a5eaad2bdf6c2`。
上板时补齐为 1 MiB，补齐后的 SHA256 为
`7f3b46fccadea0c37957da5895cc7715c11cfc9b2d0451c7df976b000af039f8`。
各变体的完整 SHA256 见构建报告。与 LLVM 生成的 bin 按数值和退出语义比较，
不要求二进制相同。

77 项自动测试全部通过。其中新增的实际构建测试将源码复制到独立临时目录，
屏蔽 PATH 中的 LLVM 命令，构建四种 GNU 变体，并检查 ELF/ABI、main 包装和栈。
自定义 GNU 目录、完整前缀、`CROSS_COMPILE`、`RISCV_*` 覆盖、构建工具的 manifest，
以及同一构建目录内 GNU/LLVM 切换均通过。可选 LLVM 回归使用 LLVM 20.1.8。
CI 新增 gcc-build 作业，安装 RISC-V GCC/binutils 后执行这些构建测试。

## 实际 FPGA 执行

通过既有 HTTP 调度服务和 SSH 别名 `fpga` 执行。服务设备池严格为 **4、5、6、7**，
本次实际分配到 **4、5**。日志确认 UVHS **2025.06.P3-20260115**、既有
**nr-patch13** RuntimeDB、`sys_clk=5529600`，UART 使用 **115200 8N1 / 除数 3**。
逐任务核验实际 download 目标与分配端口一致。

| 用例 | FPGA | 任务状态 | execution_status | app_exit_code | exit_code |
|---|---|---|---|---|---|
| `run.sh --toolchain gcc` | 4 / B1.F0 | completed | exited | 0 | 0 |
| exit7 | 4 / B1.F0 | failed | exited | 7 | 1 |
| trap | 5 / B1.F1 | failed | trap | null | 1 |
| hang | 4 / B1.F0 | failed | timeout | null | 124 |
| 故障后正常 hello | 4 / B1.F0 | completed | exited | 0 | 0 |

所有用例均收到启动记录，UART 打印 `computed: 40 + 2 = 0000002A` 和
`verify hello: PASS`。正常 hello 的完整终态为：

```text
@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":0}
```

正常退出及 trap 用例的 DDR 读回与补齐输入逐字节一致。trap 报告 `mcause=2`、
`mepc=0x8000054c`、`mtval=0`。hang 已实际启动应用并打印 PASS，随后没有完成记录；
90 秒总预算耗尽后返回 124，含卸载回收约 102 秒后取得结果。超时结果不包含正常
终态路径的 `ddr_readback_matches` 字段，未将普通 PASS 文本解释为程序已退出。

超时后在同一 FPGA4 上重新完整加载并执行正常 hello 成功。最终设备查询无错误，
FPGA **4、5、6、7 全部可用、无 owner、无遗留 UART 或调度锁占用**，队列无活动任务。
中途 FPGA5 存在外部占用时按调度规则等待，确认空闲后才分配该端口。

首次尝试在硬件加载前返回端口忙。SSH 的 locale 警告混入日志，使旧代码将重试
标记误判为失败；现已按独立 stdout 的完整标记识别。仅 exit 75 且 stdout 严格
匹配加载前标记才能按原票号重排，额外 stdout 或仅 stderr 出现标记仍不重放。
新增回归测试覆盖这些边界，首次失败记录也保留在日志目录。

## 证据及复现

以下文件保存在 Git 忽略的 `build/gcc-validation/`，不含服务令牌：

- [工具链版本、路径和包校验记录](../build/gcc-validation/toolchain.json)。
- [GNU assembler 驱动调用](../build/gcc-validation/gcc-driver.log)、
  [ELF 检查](../build/gcc-validation/hello.elf.log)和
  [GNU 反汇编](../build/gcc-validation/hello.disassembly.log)。
- [四种 bin 构建报告](../build/gcc-validation/build-report.json)，以及目录中的
  `hello/exit7/trap/hang.build.log` 和 `images/<变体>/manifest.json`。
- [一条命令运行日志](../build/gcc-validation/hello-run-retry.log)、
  [hello 纯 UART](../build/gcc-validation/hello.uart.log)和
  [完整真机报告](../build/gcc-validation/live-report.json)。报告包含任务 ID、
  分配端口、原始应用退出码、平台日志及检查结果。
- 各用例的 `<变体>.result.json`、`<变体>.platform.log`、`<变体>.uart.log`，
  [初始设备状态](../build/gcc-validation/devices-before.json)和
  [最终设备状态](../build/gcc-validation/devices-after.json)。
- [77 项自动测试日志](../build/gcc-validation/tests.log)。

真机服务已配置时，在仓库根目录执行：

```bash
./examples/hello/run.sh --toolchain gcc
```

单独构建或反汇编：

```bash
make -C examples/hello TOOLCHAIN=gcc
make -C examples/hello TOOLCHAIN=gcc dump
python3 tools/build_hello.py --toolchain gcc --variant exit7
```

安装 GNU 交叉工具后，可离线复现构建测试：

```bash
PYTHONPATH=src python3 -m unittest tests.test_hello_build -v
```

本次验证覆盖 NH hello、故障变体及调度执行链路；硬件计算内容为 hello 的
42 校验，DMA/E6 不在本次验证范围内。
