# 将 FPGA 的 DDR 结果下载到主机

`bosc-resident` 支持应用声明输出区域，daemon 在应用成功完成并确认 CPU 复位后、
释放执行槽前读取 DDR，保存为每个任务独立的 `.bin`。主机通过 HTTP、CLI 或 Python SDK
下载并验证数据。可运行的最小示例见 [examples/ddr_output](../examples/ddr_output/README.md)。

## 应用端：声明 C memref

将本仓库 `runtime/include` 加入编译器 include 路径，在 NH 代码中使用：

```c
#include "uart.h"
#include "fpga_output.h"

/* c 是应用已有的 MemRef2，包含 aligned、offset、sizes、strides。 */
/* 先等待所有写入完成，并将脏缓存写回 DDR。 */
if (fpga_export_memref2_i32("C", &c) != 0)
  return 1;
return 0;  /* runtime 随后打印 @@FPGA_RESULT */
```

头文件默认调用 `print_uart`；使用其他 NH UART 函数时，在 include 前定义
`FPGA_OUTPUT_PUTS`。函数只打印元数据，返回 0 表示声明成功。
它不复制矩阵、不刷新缓存、不等待 DMA，也不会在 FPGA 上创建文件。

`fpga_export_memref2_i32` 接受二维、连续、按行存储的 INT32 memref，要求
`strides == {sizes[1], 1}`。导出物理起始地址为
`(uintptr_t)c.aligned + c.offset * 4`，长度为 `sizes[0] * sizes[1] * 4`。
offset 和 stride 的单位都是元素。无效名称、布局、负数或溢出返回 -1，不打印声明。
输出名称须匹配 `[A-Za-z][A-Za-z0-9_-]{0,63}`，例如 `C`，不带 `.bin`。

地址必须是实际 DDR 物理地址；使用虚拟地址的程序应先转换。输出数据在声明后到任务
结束期间必须保持不变。数据仍在 NH/E6 缓存或 SPM 时，须先通过平台验证过的写回协议
送入 DDR；`volatile` 或 `fence` 本身不等于缓存写回。

对于 NH/E6 matmul，由 E6 完成计算、DMA 回写和完成屏障，再通知 NH。NH 拿到成功终态后，
用相同 C 缓冲区的物理地址和形状重建 descriptor 并声明输出，然后报告成功。
不要让 NH 和 E6 同时打印协议记录。当前运行 profile 仍是 `nh-only`；导出功能不扩展
DMA/E6 连续执行的安全承诺。

## 主机端：下载并验证

daemon 和客户端都需升级到包含本功能的版本。URL 和 token 与执行 bin 时相同：

```python
from fpga_scheduler import Client

with Client("http://127.0.0.1:18765", token_file="client.token") as client:
    task = client.submit("output.bin", timeout=120)
    finished = client.wait(task.id)
    if finished.result is None or not finished.result.ok:
        raise RuntimeError(finished)
    print(finished.result.artifacts)
    path = client.download_artifact(task.id, "C", "C.bin")
```

`Result` 不含任务 ID，应保存 `stream.id` 或 `submit()` 返回的 `task.id`。完整流式写法：

```python
with Client("http://127.0.0.1:18765", token_file="client.token") as client:
    with client.run_test("output.bin", timeout=120) as stream:
        for chunk in stream:
            print(chunk, end="", flush=True)
    if not stream.result.ok:
        raise RuntimeError(stream.result.details)
    path = client.download_artifact(stream.id, "C", "C.bin")

# 按声明的 dtype/shape 解释原始字节；该示例的 C 为 4×8 INT32。
import struct
values = struct.unpack("<32i", path.read_bytes())
expected = tuple(i * 100 - j * 7 for i in range(4) for j in range(8))
assert values == expected
# 有 NumPy 时也可用 np.fromfile(path, dtype="<i4").reshape(4, 8)。
```

CLI 使用已有任务 ID，不会重新运行程序：

```bash
fpga-scheduler artifacts <任务ID>
fpga-scheduler download <任务ID> C ./C.bin
```

下载检查声明长度和 SHA256，写入临时文件后原子替换目标文件。下载失败保留原文件，
可用同一 ID 重试。每个任务的文件独立保存，后续任务覆盖同一 DDR 地址不改变历史文件。
任务按服务端保留策略清理后，相应文件也会删除。所有下载接口均需团队 token。

## 边界与服务配置

仅在应用 `exited`、`app_exit_code=0` 且硬件收尾确认成功后抓取。
取消、超时、trap 或收尾失败不产生可下载输出；抓取失败使整个任务失败，
即使 `app_exit_code=0`。`details.artifact_error` 记录原因。
传输校验保证收到的是保存的字节，数值正确性仍由主机检查。

`[resident]` 的配置默认如下；修改需正常停止 daemon、修改配置后重新启动：

```toml
output_ddr_base = 0x80000000
output_ddr_bytes = 4194304
max_output_bytes = 16777216
```

物理基地址固定为 `0x80000000`；默认允许前 4 MiB。`output_ddr_bytes` 必须为
64 的倍数，上限 256 MiB，管理员应按实际硬件映射和应用布局设定。
每任务最多 16 个输出，累计最多 `max_output_bytes`（上限 16 MiB）。
daemon 使用已验证的 `i_ddr[N:0]` 前缀读回，随后按声明地址精确切片；
读取量为 DDR 基地址至最高输出末尾、向上对齐 64 字节。因此远离基地址的小输出
也会增加抓取时间和临时存储。存储限额为抓取预留快照及输出空间，不删除活动任务输入。

UART 声明格式如下，位于 START 与 RESULT 之间，以换行结束：

```text
@@FPGA_OUTPUT {"version":1,"name":"C","address":"0x80002004","nbytes":128,"dtype":"<i4","shape":[4,8]}
```

协议要求字段精确匹配、名称唯一、shape 与 nbytes 一致。通用声明支持 1–8 维，
dtype 为 `|i1`、`|u1`、`<i2`、`<u2`、`<i4`、`<u4`、`<i8`、`<u8`、`<f4`、`<f8`。
`.bin` 只保存原始数据；形状、dtype、物理地址、长度和 SHA256 在输出清单中。
旧 SSH 执行器不支持抓取，遇到输出声明会明确失败。
