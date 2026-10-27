# BOSC 真机接入与复现

本页为旧 SSH 后端的历史部署/验收说明。该后端保留传输和执行器回归，
但尚不支持安全运行中取消，当前 SDK 的租约提交会明确返回 409。
新部署和前台 CLI/Python 执行请按 [README](../README.md) 使用 bosc-resident；
以下旧版提交与验收命令不能作为新版 SDK 的可执行教程。

以下命令在调度仓库根目录执行。SSH 主机、远端路径、令牌和部署记录保存在本地
配置或 build/ 中，不提交个人环境值。设备池示例仅使用 4、5、6、7。

## 构建与部署

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

python tools/build_hello.py --toolchain gcc
python tools/build_hello.py --toolchain gcc --variant exit7
python tools/build_hello.py --toolchain gcc --variant trap
python tools/build_hello.py --toolchain gcc --variant hang

python tools/deploy_bosc.py --ssh-host <SSH别名> \
  --project /path/to/hardware-project \
  --destination '~/fpga-scheduler-runtime' --ports 4,5,6,7
```

hello 的源码和链接脚本位于本仓库 `examples/hello`，公共启动代码和 UART 驱动
位于 `runtime/bosc`，已接入 [完成协议](completion.md)。需要 GNU make 以及
RISC-V 裸机 GCC/binutils；`--toolchain gcc` 全程使用 GNU 工具。也可选
`--toolchain llvm`，使用 clang、ld.lld、llvm-objcopy（默认工具链）。默认从 PATH
查找，或通过 `--gcc-bin`、`--gcc-prefix`、`--llvm-bin` 指定工具，安装见
[工具链说明](toolchains.md)。上述命令构建到本仓库 build/hello/ 下。exit7/trap/hang 是同一 hello 的
显式故障变体：分别改变返回值、触发非法指令、在 hello 打印完成后进入等待循环。
每个变体有独立 SHA256 manifest，不冒充原始 hello。
公共 UART 上报实现位于 `runtime/bosc/uart_protocol.c`；故障包装位于
`tests/fixtures/bosc_main_faults.c`，仅故障变体会编译、链接该测试代码。
只构建普通 hello 也可以执行 `make -C examples/hello TOOLCHAIN=gcc all size`，输出为
`examples/hello/build/hello.bin`；`./examples/hello/run.sh --toolchain gcc` 会自动构建并提交该产物。

部署工具使用现有非交互 SSH，将 runtime.py、prepare.py、runner.py 上传到独立
临时目录，再准备不存在的新运行目录。复制 Makefile/user_script，链接已有
hw.dat；不修改源工程或切换 bitstream。要求选定板卡空闲、无其他队列使用同一池。
后续升级须排空队列后执行，勿覆盖正在运行的目录。

创建 config.local.toml（已忽略），root、uvhs_shell 填部署命令输出的绝对路径：

```toml
backend = "bosc"
state_dir = "state"
token_file = "secrets/service.token"
bind = "127.0.0.1"
port = 8765
poll_interval = 3.0

[[nodes]]
id = "lab"
ssh_host = "<SSH别名>"
root = "/absolute/remote/fpga-scheduler-runtime"
uvhs_shell = "/absolute/vendor/bin/uvhs2_shell"
ports = [4,5,6,7]
```

```bash
# 令牌只创建一次；若已存在则跳过。
fpga-scheduler init-token secrets/service.token
fpga-scheduler serve --config config.local.toml
```

另一个终端激活虚拟环境后：

```bash
export FPGA_SCHEDULER_TOKEN_FILE="$PWD/secrets/service.token"
fpga-scheduler devices
fpga-scheduler run build/hello/hello/hello.bin
```

用户连接调度器 HTTP 地址；服务器 SSH 凭据只在服务端使用。其他机器可通过 SSH
转发访问回环服务，或者由管理员配置内网监听/HTTPS。一次 submit 对应一次完整运行。

## 真机验收

```bash
python -u tools/acceptance.py --token-file secrets/service.token \
  --ports 4,5,6,7 --jobs 8 --output build/acceptance
```

该命令会实际执行硬件任务：8 个客户端并发提交 hello、取消一个排队任务，核验
4 个端口同时运行且仍有等待者、FIFO、相同设备无重叠、UART PASS、原始返回值、
DDR 读回和 UVHS 实际 download 目标；再运行 exit7、trap、hang，最后正常 hello
验证故障后的资源恢复。完整日志、任务结果、队列快照和设备前后状态保存在输出目录。

默认 hello/exit7/trap 总预算为 240 秒；hang 预算为 90 秒，包含硬件加载。若目标
机器加载本身就超过 90 秒，必须增加验收脚本中的预算，并确认 application_started
为 true，才能证明测试到了应用卡住的场景。超时任务不会自动重放。

服务通过 SIGTERM 正常停止并等待运行任务排空；保持现有 state_dir 可继续查询
历史结果并恢复排队任务。进程托管、备份和共享访问见 [运维文档](operations.md)。
