# 真机验证记录

2026-10-26，在单个 BOSC 节点上完成调度系统验证。仅使用 FPGA **4、5、6、7**，
对应 UVHS 的 **B1.F0、B1.F1、B1.F2、B1.F3**；运行既有 NH patch13 平台，
系统时钟 5.5296 MHz、UART 115200。远端执行器部署在独立目录，复用现有运行工程
和 bitstream。接入与复现命令见 [真机接入](live-setup.md)。

## 构建与自动验证

- 从 Buddy 的 `examples/BOSCAMEFPGA/hello` 构建 NH 单核 hello，加入
  [UART v1 完成协议](completion.md)；分别构建返回 7、非法指令、等待不退出三个故障变体。
- 正常 hello.bin 大小为 1884 字节，SHA256 为
  `004100b613d61f5443b9980c998e9e84a85b875e725011bf91739dafdd4f6a0e`。
  上板时补齐至 1 MiB，补齐后 SHA256 为
  `ca790f9ceaff4c53d718838330e2bad3a560e531f269e7015cbeb2e3a1440ec0`。
- 71 项自动测试全部通过，覆盖队列、HTTP/SDK/CLI、SSH、UART 分帧、退出码、
  读回校验、进程组清理、重启恢复和历史结果兼容。
- 成功构建 wheel，并在仓库外的新虚拟环境中安装，用 SDK 查询真实任务，确认
  原始应用退出码和独立 UART 输出可用；依赖检查通过。

## 并发与排队

8 个客户端并发提交正常 hello，8 个任务全部成功，每个端口执行两次。快照记录了
4 个任务运行、其余任务排队；最大同时运行数为 4。核验按入队顺序分配，同一设备
执行时间无重叠；额外提交的第 9 个排队任务取消成功且未执行。

逐任务核对 UVHS 实际 download 目标与分配端口一致、DDR 读回与补齐后输入逐字节
一致，以及独立 UART 输出中的 hello 校验结果和完整完成协议。
`result.uart_output` 保留纯串口文本；`result.output` 同时包含运行工具日志。

## 退出、异常与恢复

| 真机用例 | 任务状态 | execution_status | app_exit_code | exit_code |
|---|---|---|---|---|
| 正常 hello | completed | exited | 0 | 0 |
| main 返回 7 | failed | exited | 7 | 1 |
| 非法指令 | failed | trap | null | 1 |
| 启动后等待不退出 | failed | timeout | null | 124 |

`app_exit_code` 是裸机程序的原始返回值；`exit_code` 是 Linux 执行器的状态，含义见
[完成协议](completion.md)。非法指令用例记录了 `mcause=2` 及 mepc、mtval。
超时用例已收到启动协议并打印普通 `PASS`，随后进入等待循环且不发终态；调度系统
在 90 秒总预算耗尽后报告 timeout，没有把普通打印文本判为成功。加载和资源回收
均占用实际墙钟时间，回收可以使最终结果晚于执行预算返回。

故障后再次执行正常 hello 成功，4 个端口均恢复可用。随后正常停止并重启调度服务，
确认完整历史结果保留、已结束任务未被重新执行；重启后又在曾超时的 FPGA 4 上
执行 hello 成功。最终队列中无 queued/running 任务，设备查询无节点错误。

## 本地证据与范围

以下文件由本次验证生成，位于 Git 忽略的 build/ 目录，保留在执行验证的机器上：

- [完整验收报告](../build/acceptance/report.json)：任务结果及全部验收检查。
- [队列快照](../build/acceptance/snapshots.jsonl)：并发运行和排队证据。
- [验收控制台日志](../build/acceptance-console-final.log)及
  [自动测试日志](../build/unit-tests.log)。
- [重启后 FPGA 4 的结果](../build/acceptance/post-restart.json)和
  [纯 UART 日志](../build/acceptance/post-restart.uart.log)。

本次板测范围为 NH hello、明确的故障变体及调度链路；DMA、E6 和其他算法程序尚未
在本次验收中验证。其他 bin 需要接入相同完成协议，才能获得可信的应用退出码。

## hello 独立源码移植后的验证

应用、Makefile、链接脚本及依赖的 CRT/运行时和 UART 驱动已移植到本仓库。
将 `examples/hello` 的构建文件、`runtime/bosc` 和故障 fixture 复制到临时目录，
使用 PATH 中的 LLVM 20.1.8 完成四种变体的独立构建，验证 ELF 为 RV64、入口为
`0x80000000`，且只有故障变体包含 main 包装。增量构建、改变时钟参数及复用同一
构建目录切换变体均通过。使用原编译器重建时，四种 bin 与移植前逐字节一致。

PATH 工具链生成的普通 hello 为 1924 字节，SHA256 为
`f05a0e5473c97d169866960eee7ccd8b2c6e82536d6f683232ead4a74baccc99`。
从仓库外直接执行 `examples/hello/run.sh`，在 FPGA 4 / B1.F0 上成功完成，
UART 显示 `verify hello: PASS`，`app_exit_code=0`、`exit_code=0`，DDR 读回一致。
编译器版本不同会影响 bin 内容；上述 1884 字节的原编译器产物仍可复现。

新增本地证据位于 Git 忽略的 build/standalone-migration/：

- [原编译器的四种 bin 比较](../build/standalone-migration/golden-comparison.json)。
- [独立构建、ELF 和配置切换验证](../build/standalone-migration/isolated-report.json)。
- [移植后的真机结果](../build/standalone-migration/live-summary.json)和
  [UART 日志](../build/standalone-migration/live.uart.log)。

## GNU RISC-V GCC 接入

standalone hello 新增 `TOOLCHAIN=gcc`，编译、汇编、链接和 bin 生成全程使用
GNU 工具。GCC 10.2.0 / binutils 2.35.1 的实际构建、板测及资源回收记录见
[GCC 验证记录](gcc-validation.md)。该记录与上述 LLVM 产物分别保存，按数值校验
及应用退出语义比较，不要求不同编译器输出相同 bin。
