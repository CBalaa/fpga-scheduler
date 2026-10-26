# BOSC/UVHS 目标接入

本仓库提供 UVHS 执行器与 UART v1 完成协议。离线单元测试不会访问真实设备；
显式真机验收通过 tools/acceptance.py 执行，见 [完整接入步骤](live-setup.md)。
服务本机需要 OpenSSH 客户端；远端为 Linux，建议 Python 3.8+，有 UVHS、/proc
访问能力和平台账号对 UART 的适当权限。远端脚本独立使用标准库，不安装 SDK。

## 外部工程输入

每台服务器必须有本机可用的硬件工程，包含：

- Makefile、hw.dat（可为数据库目录或链接）、user_script/hw_run/hw_runN.tcl。
- 本仓库提供的 runner.py/run.sh，接受 --fpga=N、--workdir=DIR、test=PATH。
- Makefile/调用链尊重 FPGA_STARTUP_TIMEOUT、UV_RUN_OUT_ROOT、
  UV_RUN_WORKDIR，并在选定端口使用统一 5.5296 MHz 时钟，正常退出时清理 UVHS/UART。
- shell 的非零退出码正确向上传递，UART 必须使用硬件/驱动支持的独占打开。

Makefile、厂商数据库、UVHS 与 user_script 属于外部 FPGA 工程；本仓库不会替换
这些厂商输入。不能用旧网站根目录的 run.sh 替代本仓库的完成协议执行器。
执行器将 bin 补齐至 1 MiB 的整数倍，最少 1 MiB；原输入文件保持不变。

在目标服务器先确保旧队列和硬件任务均已结束，将 remote/runtime.py、prepare.py
及 runner.py 单独复制到管理目录。以下操作由管理员显式执行（目标目录必须不存在）：

~~~bash
python3 prepare.py --project /path/to/local/hardware-project \
  --scheduler /path/to/runtime.py \
  --ports 4,5,6,7 \
  --destination /path/to/new/fpga-platform
~~~

可用 --ports 4,5 限定池。准备程序保留原工程，只在新目录复制 Makefile、运行脚本
与 user_script；hw.dat 链接到本机原数据库。新目录内 scheduler.py 即远端执行器。
每个端口脚本必须在 load_db -release 之后精确执行：

~~~text
load_db -db ./hw.dat -to {b1.f0}
~~~

其中 b1.f0 是 FPGA4。FPGA0–7 映射 B0.F0–B1.F3。准备工具移除旧 allocate -board，
重写唯一 load_db 目标；缺失、重复或 release 顺序错误会拒绝准备。

## SSH 与配置

复制 configs/bosc.example.toml 到 config.local.toml，调整本地 state_dir/token_file
和每节点的 id、ports、ssh_host、root、python、uvhs_shell、uart_prefix。
root、UVHS、UART 前缀必须是远端绝对路径；本地 ssh_config 相对于 TOML 文件。
使用 id 记录设备，不需要将 IP 用作节点身份。只支持实际 UVHS 八端口的子集。

ssh_host 是 OpenSSH alias。账号、私钥、known_hosts 和 ProxyJump 配在独立 SSH
配置中，固定已核验的主机密钥。适配器强制 BatchMode 和 StrictHostKeyChecking；
命令通过 argv/引号传递，不把用户输入当跳板启动代码。没有运行后的重连重放。

可直接通过网络/VPN 访问目标；需要跳板时在 alias 配置 ProxyJump。
网络接入由管理员在本项目之外配置，本仓库不提供 QEMU/UniVPN 托管脚本。

如果必须经现有 VM 的命令转发入口，部署 remote/relay.py，并显式传入 JSON 配置：

~~~json
{"nodes":{"bosc-a":"fpga-a","bosc-b":"fpga-b"},"ssh_config":"/path/to/vm-ssh-config"}
~~~

relay_command 是在服务本机运行的 argv 数组，例如 ["ssh", "access-vm",
"python3 /opt/fpga/relay.py --config /etc/fpga/relay.json"]。传入 JSON stdin，
节点必须在 relay 白名单内；必要时可将这条命令放入原 VM connect.sh 的入口。
优先通过 OpenSSH 配置选择可用路由；不在硬件命令发送后尝试备用路由。

## 执行与回收

服务先验证并冻结本地输入，再将其复制到目标 jobs/<id>/input.bin；临时上传通过
目标机 SHA256 检查后原子替换。失败的传输不触发 run。任务获取 scheduler-locks/
fpgaN.lock，二次检查实时 UVHS、UART 和板卡共享占用，核验 SHA256 后运行。

端口锁目录必须与所有并行使用者共享。若原平台/TReX 仍存在，应指向同一运行
目录的 scheduler-locks；首版切换仍要求同一资源池只有一个 FIFO 队列入口。
探测读取全部八单元，其他账号或同账号未持有协调锁的会话占用整板时整板禁用。
串口检查受 /proc 权限限制，无法替代 run.sh/驱动的 UART 独占机制。

每任务独立 input_padded.bin、runtime/ 和 output/，避免覆盖公共工程日志。
runner 仅从独占 UART 读取 v1 START/RESULT 记录，不把 UVHS 控制台的 PASS 文本
或静默作为成功。总执行期限包含加载；idle_seconds 为旧接口兼容字段，原生执行器
不靠它判定完成。终态后校验 CPU 释放前的 DDR readback，向 UVHS 发送 exit，
等待卸载并检查工具退出状态，再写 output/result.json。调度器返回应用原始退出码、
执行状态及完整日志。协议格式见 [完成协议](completion.md)。
超时或信号先 SIGTERM 本任务进程组，最多等待 45 秒，再 SIGKILL；正常父进程
退出也检查后台子进程。持有端口锁直到进程组回收。运行/回收失败不会自动重放。
探测只读；清理由单独 maintenance 命令执行，按终态标记或陈旧上传目录过期，
并持有 active.lock，避免清除执行中任务。默认 7 天，定期维护会在无排队时运行。

## 真机验收门槛

1. /devices 中验证每台服务器 UVHS 八单元解析正常，配置端口池和 UART 对应正确。
2. 对每个启用端口运行已知正确 bin，检查 UVHS 实际 download 的 B/F 目标，
   串口 PASS/数值输出与任务记录一致。退出码零不代替物理映射和数值检查。
3. 提交数超过可用端口数，保存任务列表、每节点输出和分配时间；验证有持续排队，
   同一 (node, fpga) 不重叠，完成后 UVHS/UART 均释放。
4. 故障注入：单机离线时健康节点继续；端口被别人使用时不抢占；队列可取消。
5. 服务/SSH 中断后核验未自动重放；检查不确定任务的硬件进程和证据，再恢复服务。

真机验收报告保存任务记录、完整日志和队列快照；上线时以目标环境的报告为准。
