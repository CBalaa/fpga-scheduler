# 计算 C，并下载到主机验证

NH 计算 `C[4,8]` 的 32 个 INT32 值 `C[i,j] = i*100 - j*7`，写回 DDR，
通过 `fpga_export_memref2_i32("C", &c)` 声明输出。daemon 保存 `C.bin`，
Python 在调用机器下载并逐元素精确验证。示例使用非零 memref offset，输出为 128 字节。

先按仓库 README 部署并启动更新后的 `bosc-resident` daemon，并准备该服务 token 的副本。
在有 RISC-V 工具链的主机、仓库根目录执行：

```bash
make -C examples/ddr_output TOOLCHAIN=gcc
export FPGA_SCHEDULER_URL="http://127.0.0.1:18765"
export FPGA_SCHEDULER_TOKEN_FILE="/path/to/client.token"
python examples/ddr_output/run.py --output examples/ddr_output/build/C.bin
```

也支持 `TOOLCHAIN=llvm` 和 hello 示例相同的工具链路径选项。
通过局域网使用时，将 URL 改为服务内网地址；bin 上传和 C.bin 下载均由 SDK 完成。
成功末尾显示 `Host validation: PASS (32 exact INT32 values)`。

已执行任务可单独下载：

```bash
fpga-scheduler artifacts <任务ID>
fpga-scheduler download <任务ID> C examples/ddr_output/build/C.bin
```

[output.c](output.c) 包含 NH 缓存写回示例，[run.py](run.py) 包含流式 UART、下载和验证。
应用须保证实际 DDR 数据已经就绪；导出函数本身只打印元数据。
完整协议、配置和 NH/E6 接入边界见 [DDR 输出说明](../../docs/outputs.md)。
