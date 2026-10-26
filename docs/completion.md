# 裸机完成协议 UART v1

服务器无法直接读取裸机 main 的返回值。板上运行时必须捕获返回值并通过独占
UART 报告；服务器执行器验证终态、清理硬件会话后，再发布调度结果。

串口使用 115200 8N1；当前 BOSC 池统一配置 5.5296 MHz。应用必须使用匹配的
UART 除数，示例构建使用可配置的驱动，除数为 3。

## 线格式

记录必须单独成行，LF 或 CRLF 结束，UTF-8 JSON，每行最长 4096 字节：

```text
@@FPGA_START {"version":1}
...应用日志...
@@FPGA_RESULT {"version":1,"status":"exited","app_exit_code":7}
```

START 在 UART 初始化之后、应用代码之前发出。RESULT 在全部同步/异步工作、
应用检查与报告完成之后发出，随后应用停机。一个会话只允许一条 START 和一条
RESULT；缺少 START、重复、损坏、版本错误均失败，不通过日志中的普通 PASS 猜测结果。
app_exit_code 为原始有符号 32 位整数，0 表示成功。程序必须等待 DMA/AME 完成；
协议本身不能证明应用正确使用硬件或校验数值。

不可恢复异常由运行时输出：

```text
@@FPGA_RESULT {"version":1,"status":"trap","mcause":"0x2","mepc":"0x80000100","mtval":"0x0"}
```

trap 不包含 app_exit_code。主机等待总预算到期仍未收到终态时，报告 timeout，
app_exit_code=null；静默时间不触发成功。协议用于受信任团队程序，不是安全隔离。

## 程序侧接入

[runtime/bosc/uart_protocol.c](../runtime/bosc/uart_protocol.c) 提供三个公共 UART hook：
bare_runtime_report_start、
bare_runtime_report_exit、bare_runtime_report_trap。本仓库的
[公共单核运行时](../runtime/bosc/bare_runtime.c)
调用 start，捕获 main() 的 int 返回值，在 after_main 报告结束后调用 exit；
致命异常处理输出 trap。默认 weak hook 为空，其他应用可以选择自己的传输实现。

hello 已独立移植到 [examples/hello](../examples/hello/README.md)，依赖的 CRT、
运行时和 UART 驱动均位于 `runtime/bosc`。Makefile 默认链接完成协议，
`tools/build_hello.py` 使用本仓库源码生成普通或故障变体及其 manifest。
原 Buddy 工程的 hook 接入补丁保存在 `patches/buddy-hello-uart.patch`，供接入其他
工作区时参考；本仓库构建直接使用已经包含这些 hook 的本地运行时。

构建默认 hello 时只链接公共协议实现。验收用的返回 7、非法指令、等待不退出包装
位于 [tests/fixtures/bosc_main_faults.c](../tests/fixtures/bosc_main_faults.c)，仅在
显式选择 exit7、trap 或 hang 变体时编译，并通过 `--wrap=main` 链接。
用户调用示例位于 [examples/hello](../examples/hello/README.md)。

NH/E6 双核应用也应由 NH 在拿到 E6 最终结果、完成报告之后发出同样的 UART
记录。旧 SYSCTRL 的 PASS/FAIL 映射会丢失原始非零值；要返回具体 E6 int 值，
还需在经过验证的跨核通道中传递它。当前 hello 验收是 NH 单核，未宣称验证双核协议。

## 主机与 API

runner 先独占并清空旧串口缓冲，再执行 make uv_runN。仅 UART 来源的记录参与
应用判定；UVHS 工具日志单独检查。拿到终态后还要求加载达到 hspRun>、DDR
读回匹配、UVHS 正常卸载和退出，才可判定整个任务成功。终态不是卸载完成的标志。

| 场景 | execution_status | app_exit_code | exit_code |
|---|---|---|---|
| main 返回 0，平台正常 | exited | 0 | 0 |
| main 返回 7 | exited | 7 | 1 |
| 致命异常 | trap | null | 1 |
| 超过总运行期限 | timeout | null | 124 |
| 完成协议损坏 | protocol_error | null | 1 |
| 加载/读回/卸载失败 | platform_error | null 或已收到的值 | 非零 |
| 收到终止信号 | interrupted | null | 130 |
| SSH 断开、没有可靠结果 | result=null，error 说明结果未知 | — | — |

应用返回值不直接用于 SSH 退出状态，避免 255 被解释为连接失败，以及与超时码
混淆。API 始终通过 app_exit_code 保留完整值，通过 exit_code/ok 表示整个运行是否成功。
output/result.json 在远端清理后原子写入，agent 汇总并通过 SSH 返回；uart_output
单独返回 output/uart.raw.log 的串口文本，避免工具控制台插入应用行。客户端查询
不会触发硬件重放。
