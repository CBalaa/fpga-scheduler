# 本地 FPGA 模拟测试

模拟器只放在仓库的 `tools/` 和 `tests/`，用于本地开发，不注册 `fpga-scheduler sim` 命令或新的生产 backend，也不随 wheel 安装。需要 Linux、Python 3.11+ 和 PATH 中的 `tclsh`，无需 FPGA 服务器或厂商软件。

## 直接运行测试

在仓库根目录执行，无需安装包：

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_simulation.py -v
```

覆盖镜像切换、DDR 读回、UART 分片与实时输出、双端口排队、取消、客户端失联、超时后复用，以及进程崩溃后未知任务不重放、排队任务恢复。原有 supervisor 测试继续在 `tests/test_daemon.py` 中验证。测试会创建临时目录和虚拟 UART，结束后清理进程和文件。

## 本地手动调试

终端 A，在仓库根目录启动前台模拟服务：

```bash
python tools/fpga_sim.py init build/fpga-sim
python tools/fpga_sim.py serve build/fpga-sim
```

`init` 生成独立配置、token 和场景镜像；目录已存在时拒绝覆盖。默认使用回环地址 127.0.0.1:18766 和虚拟端口 4、5。可在首次初始化时用 `--port 18767 --ports 4` 修改。该工具只接受自身生成的本地隔离配置；不要对这个目录执行生产 `daemon start`，使用上面的 `serve` 即可。

终端 B，仍在仓库根目录，使用现有 Python SDK：

```bash
PYTHONPATH=src python - <<'PY'
from fpga_scheduler import Client

with Client("http://127.0.0.1:18766", token_file="build/fpga-sim/service.token") as client:
    with client.run_test("build/fpga-sim/images/hello.bin", timeout=3) as stream:
        for chunk in stream:
            print(chunk, end="", flush=True)
        print(stream.result.execution_status, stream.result.app_exit_code, stream.result.ok)
PY
```

也可以让现有 CLI 作为普通客户端调用本地 HTTP 服务：

```bash
PYTHONPATH=src python -m fpga_scheduler \
  --endpoint http://127.0.0.1:18766 \
  --token-file build/fpga-sim/service.token \
  run build/fpga-sim/images/hello.bin --timeout 3
```

在终端 A 按 Ctrl+C 正常停止，等待运行任务收尾。再次执行 `serve` 可恢复队列；彻底清理时先停止，再删除 `build/fpga-sim`。所有日志和状态保存在该目录的 `state/` 中。

## 故障场景

模拟镜像是带 `FPGA_SIM_V1` 标记的场景文件，用于描述输出和故障。它们不是 RISC-V 可执行程序，不能提交给真机。模拟器也拒绝真实 bin。

`init` 自带：

| 镜像 | 预期行为 |
| --- | --- |
| `hello.bin` | 流式 UART，app_exit_code=0 |
| `hang.bin` | 输出普通 PASS 但没有完成记录，等待超时/取消 |
| `trap.bin` | 应用 trap |
| `duplicate.bin` / `missing-start.bin` / `malformed.bin` | UART 协议错误 |
| `readback-error.bin` / `command-error.bin` | DDR 核验或命令失败，CPU 不释放 |
| `command-timeout.bin` | 命令缺少及时确认，隔离端口 |
| `cleanup-error.bin` | 应用返回 0，但收尾失败，任务整体失败 |
| `disconnect.bin` / `uvhs-exit.bin` | UART 或控制进程断开 |

自定义构建标记、退出码和分片大小：

```bash
python tools/fpga_sim.py image build/fpga-sim/images/A.bin --label A
python tools/fpga_sim.py image build/fpga-sim/images/B.bin --label B --exit-code -7 --fragment-bytes 1
```

`--mode` 选择表中场景，`--duration` 控制完成前等待时间，`--output` 设置 UART 文本。超时/取消收尾成功后保留 session；控制通道或收尾无法确认时按真实 Resident 规则隔离。断连场景可停止并重新 `serve` 来重建虚拟 UART。

模拟器复用真实 Application、队列、Resident、Session，只在本地入口构建时替换硬件对象。它验证服务和控制流程，不执行 RISC-V 指令，不验证真实 bin 的计算结果、DMA/E6 或 RTL 时序。
