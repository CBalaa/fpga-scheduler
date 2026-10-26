# FPGA Scheduler

独立的多人 FPGA 上板任务队列，提供 Python SDK 和命令行。使用 SQLite 持久化
FIFO，根据空闲设备自动分配服务器和端口；SDK 不依赖 JupyterHub、Kubernetes
或 FPGA 工具。支持可离线运行的 Simulator，以及 BOSC/UVHS 后端。

代码来自 buddy-caas 的任务调度实现，经独立打包和配置化改造；来源及修改见
[NOTICE](NOTICE)，Apache-2.0 许可证见 [LICENSE](LICENSE)。

## 快速开始（无需 FPGA）

服务端需要 Linux、Python 3.11+；客户端支持 Python 3.11+。
在仓库根目录执行：

~~~bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
fpga-scheduler init-token secrets/service.token
fpga-scheduler serve --config configs/simulator.toml
~~~

另开终端，激活虚拟环境后：

~~~bash
export FPGA_SCHEDULER_TOKEN_FILE="$PWD/secrets/service.token"
mkdir -p build
printf 'simulation input' > build/fpga-example.bin
fpga-scheduler --json submit build/fpga-example.bin
# 将返回的 id 填入下面命令
fpga-scheduler job <id>
fpga-scheduler wait <id> --timeout 30 --poll-interval 0.2
fpga-scheduler jobs
fpga-scheduler jobs --state completed,failed,cancelled
fpga-scheduler devices
fpga-scheduler cancel <id>
~~~

全局选项 --json、--endpoint、--token-file 放在子命令之前。
默认服务地址为 http://127.0.0.1:8765，可通过 FPGA_SCHEDULER_URL 或 --endpoint 修改。
Simulator 输出明确包含 SIMULATED，不代表真实硬件校验。

~~~python
from fpga_scheduler import Client

client = Client(token_file="secrets/service.token")
task = client.submit("build/fpga-example.bin", timeout=600, queue_timeout=3600)
print(task.id)  # 保存 ID，客户端退出后仍可查询
finished = client.wait(task.id, timeout=1200, poll_interval=0.5)
if finished.result:
    print(finished.result.ok, finished.result.output)
else:
    print(finished.state, finished.error)

# Notebook 便捷入口；返回独立 Result 类型
result = client.run_test("build/fpga-example.bin")
~~~

## 真机执行

按 [BOSC 完整接入步骤](docs/live-setup.md) 构建带完成协议的 hello.bin，准备独立
远端运行目录并启动服务。真实设备默认要求 [UART v1 完成协议](docs/completion.md)：
保留程序 main 的返回值，区分正常返回、异常、超时和平台失败；仅下载成功或
串口出现普通 PASS 文本不会报成功。

已在 FPGA 4、5、6、7 完成并发排队、非零退出、异常、超时和重启恢复验证，见
[真机验证记录](docs/validation.md)。

已配置真机服务的环境可以直接运行 [hello 示例](examples/hello/README.md)：
示例自带裸机源码和 Makefile，脚本会构建后提交；支持 GNU RISC-V 裸机 GCC/binutils
和可选的 LLVM 工具链，构建不依赖外部 Buddy 源码。安装见 [工具链说明](docs/toolchains.md)。

~~~bash
./examples/hello/run.sh --toolchain gcc
~~~

~~~python
result = client.run_test("examples/hello/build/hello.bin")
print(result.execution_status, result.app_exit_code, result.exit_code)
# 正常返回 7：exited, 7, 1；超时：timeout, None, 124。
~~~

## 行为与边界

- 按任务 FIFO 分配，跨节点轮转，各节点按配置的 ports 顺序分配。完成顺序可以不同。
- 设备以 (node, fpga) 标识，同号端口可跨主机并行。
- 已接收任务独立于客户端连接；GET 查询、重新连接或等待超时均不会重复上板。
- 提交 ID 为 32 位小写十六进制。同 ID 同输入参数幂等，不同参数冲突。
- 输入最大 16 MiB，SHA256 校验后冻结；已提交输入不可重新上传或修改。
- 排队默认 3600 秒、最大 86400 秒；执行默认/最大 600 秒；idle_seconds 为兼容字段。
- 运行预算从远端执行器启动运行脚本起算；输入传输独立限时，硬件探测和清理另有预算。
- 最多 256 个 queued/running 任务；失败设备冷却 60 秒；终态记录与输入默认保留 7 天。
- 仅 queued 可取消；不提供运行中强杀 API。CLI wait 超时或 Ctrl-C 不取消任务。
  SDK run_test 的 Ctrl-C 会尝试取消排队任务，运行中任务继续完成。
- 仅明确的“尚未加载硬件且端口忙”响应可按原票号重排。实际执行失败不自动重放。
- 服务异常退出后保留 queued；原 running 标为 failed，error 注明结果未知，不自动重放。
- 共享服务令牌，团队成员可查看全局队列、取消任意排队任务。首版无个人配额/用户公平调度。
- 单调度进程，状态目录有进程锁。不同状态目录不能同时调度同一套硬件资源。
- 首版仅支持 bin 上板任务，不包含 TReX、端口租约或网页。

## 配置与部署

配置文件为 TOML，本地路径相对于配置文件解析。示例：
[Simulator](configs/simulator.toml)、[BOSC](configs/bosc.example.toml)。

Simulator 支持 duration、offline_nodes、busy_nodes、failure_nodes、disconnect_nodes、
transfer_failure_nodes，用于离线验证设备不足、节点离线、传输失败和执行后断线。
节点端口可任意配置；BOSC 适配器支持从 UVHS 0–7 中选择子集，仍查询全部单元以判断板级占用。

Docker/Compose、状态备份与恢复见 [运维文档](docs/operations.md)。
目标 FPGA 的外部运行工程要求、SSH 接入和真机验收见 [BOSC 接入](docs/bosc.md)。
HTTP 状态、SDK 和 CLI 契约见 [接口文档](docs/api.md)。
迁移范围与验收映射见 [迁移记录](docs/migration.md)。

## 测试

~~~bash
PYTHONPATH=src NO_PROXY='*' python3 -m unittest discover -s tests -v
~~~

测试仅使用临时目录、回环 HTTP、模拟 SSH/UVHS 和本地子进程；不连接真实 FPGA。
安装交叉工具链时还会在独立临时目录实际构建 hello 四种变体，验证 ELF/ABI、
工具覆盖及 GNU/LLVM 切换；未安装工具链时跳过这些构建测试，CI 的 gcc-build 作业安装 GNU 工具执行。
其中进程组与文件锁测试需要 Linux。CI 执行安装与测试，不依赖 reference 目录。
显式运行 tools/acceptance.py 才会访问真实 FPGA，验证四端口并发、FIFO、排队取消、
应用非零退出、trap、超时及故障后资源恢复。
