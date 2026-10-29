# FPGA Scheduler

在 FPGA 服务器运行 daemon，通过 CLI 或 Python 上传 bin，自动排队、分配端口并返回 UART 和退出码。

前提：构建机、服务器及客户端已有 Python 3.11+（下文以 `python3.11` 为例）。服务器已配置厂商环境、UVHS 和硬件工程，运行账号能访问 UART。当前常驻模式支持 NH-only UART v1 程序，管理显式配置的端口白名单；不要与旧上板脚本同时管理这些端口。所有 `/path/to/...` 和示例 IP 都需替换。

## 1. 在其他机器构建 wheel，上传并安装

在**非 FPGA 机器**执行（已有仓库则直接进入目录；`fpga` 为服务器 SSH 地址或别名）：

~~~bash
git clone git@github.com:CBalaa/fpga-scheduler.git
cd fpga-scheduler
python3.11 -m pip wheel --no-deps . --wheel-dir build/wheels
ssh fpga 'mkdir -p ~/fpga-scheduler-dist'
scp build/wheels/fpga_scheduler-0.1.0-py3-none-any.whl fpga:~/fpga-scheduler-dist/
~~~

登录 FPGA 服务器（`ssh fpga`），首次安装到独立目录；服务器无需仓库源码：

~~~bash
export FPGA_DEPLOY_DIR="$HOME/fpga-resident-deploy"
umask 077
mkdir -p "$FPGA_DEPLOY_DIR"
python3.11 -m venv "$FPGA_DEPLOY_DIR/venv"
. "$FPGA_DEPLOY_DIR/venv/bin/activate"
python -m pip install --no-deps "$HOME/fpga-scheduler-dist/fpga_scheduler-0.1.0-py3-none-any.whl"
fpga-scheduler init-token "$FPGA_DEPLOY_DIR/service.token"
~~~

`init-token` 首次生成随机共享 token，保存在服务器的 `~/fpga-resident-deploy/service.token`（权限 `600`）。daemon 和客户端必须使用相同的 token 内容；后文的 `client.token` 是它的副本，不是重新生成的 token。已有部署直接使用原文件，不要重新生成。

## 2. 准备初始化 Tcl、配置和管理 daemon

仍在选定 FPGA 服务器执行。硬件工程须包含 `hw.dat`；共享锁目录须与其他硬件入口一致且运行账号可写。初始化文件属于 scheduler 部署配置，须由部署者在启动 daemon 前准备：

1. 找到**该机器、该 FPGA 已验证可用**的 `~/Desktop/fpga-tester/user_script/hw_run/hw_runN.tcl`，只读检查当前内容及 SHA-256。
2. 在部署目录创建 `init/initN.tcl`。对当前参考脚本，人工复制 `source ./user_script/hw_run/write_image.tcl` **之前**的初始化部分，不修改原硬件工程。
3. 保留该机资源分配、数据库加载、连接器、时钟、download、initialize 的原指令、参数及顺序。不同机器可能分别使用 `load_db -db ./hw.dat -to {b1.f0}`，或 `allocate -board {b1.f0}` → `load_db -release` → `load_db -db ./hw.dat`；必须按实测原文件准备，不能拼成统一模板。
4. 文件结束时保持 `cpu_reset=1` 并用 `query -reset` 确认；删除应用写入/读回、`source write_image.tcl`、释放 CPU 复位、`exit` 等运行阶段内容。
5. 在文件头注释记录来源服务器、原文件绝对路径和来源版本或 SHA-256，例如 `# Source host: fpga113`、`# Source path: /home/USER/Desktop/fpga-tester/user_script/hw_run/hw_run4.tcl`、`# Source SHA-256: ...`。硬件项目升级后须重新核对并正常重建会话。

以上是**部署准备步骤**，scheduler 不会自动裁剪 `hw_runN.tcl` 或改写初始化文件。可提前为所有计划使用的端口准备并登记初始化文件；下例登记 FPGA4–7，只启用 FPGA6。旧的单端口映射配置仍可使用。

~~~bash
cat > "$FPGA_DEPLOY_DIR/config.local.toml" <<'TOML'
backend = "bosc-resident"
state_dir = "./state"
token_file = "./service.token"
bind = "127.0.0.1"
port = 18765
client_heartbeat_seconds = 2
client_lease_seconds = 15

[resident]
profile = "nh-only"
hardware_project = "/path/to/hardware-project"
lock_dir = "/path/to/shared/scheduler-locks"

[resident.init_scripts]
"4" = "./init/init4.tcl"
"5" = "./init/init5.tcl"
"6" = "./init/init6.tcl"
"7" = "./init/init7.tcl"

[[nodes]]
id = "local"
ports = [6]
uvhs_shell = "/path/to/vendor/bin/uvhs2_shell"
TOML
~~~

编辑其中三个绝对路径和初始化文件路径再启动。`[resident.init_scripts]` 的键是带引号的物理端口号，须覆盖 `ports` 的每个启用端口，允许额外登记未启用端口。初始化文件相对路径以 **config 所在目录**为基准，支持开头的 `~`，不展开 `$HOME` 等环境变量；其他配置路径仍以 config 所在目录为基准，不展开 `~`。

加载配置时检查所有登记项的编号、路径及文件是否可读；所有文件都必须存在。Resident 只为 `ports` 中的端口校验 Tcl 内容、目标编号、NH profile 与共享时钟要求，并在每次重建会话时重新校验。`ports` 是唯一启用白名单：未启用端口不会创建会话、打开 UART、分配硬件、下载 bitstream 或执行脚本。缺少启用端口的映射、启用脚本不兼容或初始化失败时明确报错，不会回退到旧流程。

脚本在每个常驻会话的**隔离工作目录**执行，`./hw.dat` 是指向 `hardware_project/hw.dat` 的链接；初始化文件自身所在目录不作为 cwd。不要假设 `./user_script/...` 指向原硬件工程；脚本需要的其他资源请使用明确可读的路径。scheduler 原样 `source` 配置脚本，保留 allocate、load_db、`-to`、时钟参数和顺序。`[resident] shared_clock_index` 默认 6，须与工程的 sys_clk 映射一致；共享时钟检查不能通过删改脚本绕过。旧 `resident.explicit_allocation`（包括 `false`）须删除，原分配逻辑移入部署初始化 Tcl；旧 bootstrap 不再使用。

同板其他 FPGA 有 owner、booked 或受管会话时，必须只读确认该板 `shared_clock_index` 的实际频率为 NH 要求的 5529600 Hz，才允许原样执行初始化 Tcl；频率未知或不匹配时拒绝初始化。含时钟配置/commit 的脚本还要求同板 FPGA 信息完整；同板完整且没有其他活动端口时，允许脚本配置时钟。未含时钟配置的脚本始终要求现有频率已匹配。这些是频率兼容性检查，scheduler 不省略或改写脚本中的时钟命令，也不据此保证同频 commit 在硬件上没有瞬态影响。

设备发现按 UVHS 实际返回的单元数量工作，不要求固定八个 FPGA。`ports` 是本机要管理的端口白名单，例如 `[4]`；编号保持 `B0.F0–F3 → 0–3`、`B1.F0–F3 → 4–7`，后续板依此递增，不按查询行顺序重新编号。配置的目标未出现在查询结果中时会明确报告缺失；同板信息不完整时不能据此修改共享时钟。每个配置端口仍须有对应 UART 和独立初始化 Tcl。

一次初始化后保留同一 UVHS shell：每任务确认 CPU 复位、写入 bin、DDR 回读校验、释放复位、收集输出并判断完成、再次确认 CPU 复位。连续正常任务不重新下载 bitstream，也不执行完整 `hw_runN.tcl`。正常停止按清理流程释放资源并退出 shell；故障恢复需要重建会话时才重新执行配置的初始化 Tcl。

登记好所有初始化文件后，切换端口只需用**旧配置先停止** daemon、确认目标空闲、修改 `ports` 后启动。例如在 fpga113 从 FPGA6 切到 FPGA7：

~~~bash
ssh fpga113
cd ~/fpga-resident-deploy
source ./env.sh
fpga-scheduler daemon stop
/bitstream/nanhu-ra/scripts/check_fpga.sh
vim config.local.toml  # 仅将 ports = [6] 改为 ports = [7]
fpga-scheduler daemon start
fpga-scheduler daemon status
~~~

`init_scripts` 和 Tcl 文件无需随切换修改。最终须确认所选端口为 `READY`；目标占用、共享时钟不兼容或初始化错误仍会拒绝初始化，不能删除时钟命令绕过保护。

保存环境变量：

~~~bash
cat > "$FPGA_DEPLOY_DIR/env.sh" <<'SH'
export FPGA_DEPLOY_DIR="$HOME/fpga-resident-deploy"
export FPGA_SCHEDULER_CONFIG="$FPGA_DEPLOY_DIR/config.local.toml"
export FPGA_SCHEDULER_URL="http://127.0.0.1:18765"
export FPGA_SCHEDULER_TOKEN_FILE="$FPGA_DEPLOY_DIR/service.token"
export PATH="$FPGA_DEPLOY_DIR/venv/bin:$PATH"
SH
. "$HOME/fpga-resident-deploy/env.sh"

fpga-scheduler daemon start
fpga-scheduler daemon status
fpga-scheduler health
fpga-scheduler devices  # 端口状态、可用性和不可用原因
fpga-scheduler jobs     # 排队、运行和取消中的任务
# 正常停止：等待运行任务及硬件清理，保留排队任务
fpga-scheduler daemon stop
# 再次启动
fpga-scheduler daemon start
~~~

新终端先执行 `. "$HOME/fpga-resident-deploy/env.sh"`，也可将它加入 `~/.bashrc`。`FPGA_SCHEDULER_CONFIG` 指定默认配置，显式 `--config` 优先。修改配置前先用原配置正常停止，再修改并启动。首次硬件初始化可能需要几分钟，期间状态查询可能超时，稍后重试。SSH 断开后服务继续运行，主机重启后需重新启动。

CLI 默认显示状态摘要和表格；脚本读取原始数据时加上 `--json`，例如 `fpga-scheduler daemon status --json` 或 `fpga-scheduler jobs --json`。

存储配额不足时任务保持排队，`jobs/job` 显示原因及用量，容量恢复后自动继续；超过 `--queue-timeout` 才失败。`daemon status` 可查看配额，管理员可调整 `[resident].max_storage_bytes`（默认 1 GiB，输入与运行文件各占一半）或顶层 `retention_days` 后正常重启。安全收尾后的临时镜像/readback 默认自动回收，输入、UART 和导出文件仍按保留期保存，详见[存储说明](docs/resident-runtime.md#存储验收与限制)。

终端自动启用颜色：绿色表示正常，青色表示执行中，黄色表示等待/恢复/取消，红色表示失败/隔离。重定向和 JSON 默认不加颜色；可用 `--color always` 强制开启、`--color never` 或环境变量 `NO_COLOR=1` 关闭。

## 3. 通过 127.0.0.1 执行 bin

以下在 **FPGA 服务器**执行，以部署 daemon 的同一 Linux 账号为例，bin 路径属于当前调用用户。

**先准备 token（回环访问也需要认证）：**从第 1 节生成的服务端 `service.token` 复制一份，作为客户端的 `~/client.token`；然后将客户端环境变量指向这份副本。不要对 `client.token` 执行 `init-token`。

~~~bash
. "$HOME/fpga-resident-deploy/env.sh"
install -m 600 "$FPGA_DEPLOY_DIR/service.token" "$HOME/client.token"
export FPGA_SCHEDULER_URL="http://127.0.0.1:18765"
export FPGA_SCHEDULER_TOKEN_FILE="$HOME/client.token"
~~~

其他 Linux 账号调用时，由管理员将相同 token 的副本交给该用户，存为该用户自己的 `~/client.token` 并设为 `600`；用户在自己安装 SDK 的 Python 环境中设置上述两个客户端变量，无需访问服务账号的部署目录。每次调用前确认变量指向正确的文件；Bash 和 Python 共用它。

**Bash：**

~~~bash
export FPGA_BIN="/path/to/hello.bin"
fpga-scheduler run "$FPGA_BIN" --timeout 120
# 运行时打印任务 ID；另一个终端可查询或取消：
# fpga-scheduler job <任务ID>
# fpga-scheduler cancel <任务ID>
~~~

`run --timeout 120` 为执行期限；命令保持前台心跳、实时打印 UART，Ctrl+C 取消。`submit` 命令现在也是前台执行，`--json run` 输出最终 JSON。成功时 `state=completed`、`app_exit_code=0`、`exit_code=0`。

**Python：**保存为 `run_bin.py`（后面的局域网调用也使用此脚本）：

~~~python
import os
import sys
from fpga_scheduler import Client

with Client(
    os.environ["FPGA_SCHEDULER_URL"],
    token_file=os.environ["FPGA_SCHEDULER_TOKEN_FILE"],
    request_timeout=360,
) as client:
    with client.run_test(sys.argv[1], timeout=120) as stream:
        for chunk in stream:
            print(chunk, end="", flush=True)
        result = stream.result
print(result.execution_status, result.app_exit_code, result.ok)
raise SystemExit(0 if result.ok else 1)
~~~

~~~bash
python run_bin.py "$FPGA_BIN"
~~~

`run_test()` 默认 `stream=True`，提交后返回流对象，由 `for` 持续读取输出；读完后从 `stream.result` 取得最终结果。需要同步等待并直接返回 `Result` 时，显式设置 `stream=False`：

~~~python
with Client("http://127.0.0.1:18765", token_file="client.token") as client:
    result = client.run_test("hello.bin", timeout=120, stream=False, show=True)
print(result.execution_status, result.app_exit_code, result.ok)
~~~

流模式下由调用者打印，`show` 不自动打印；同步模式下 `show=True` 自动实时打印，`show=False` 静默等待。文本块不一定是一整行。HTTP 默认每 0.2 秒增量读取 UART（可设 `poll_interval`），回环和局域网用法相同。升级需同时更新 daemon 与 SDK，再正常重启服务。Simulator 在任务结束时提供输出；旧 SSH 后端不支持新租约提交，会明确报错。

CLI 自动读取上述环境变量；Python 脚本通过 `os.environ` 显式传入。**提交客户端退出即取消其未结束任务**：正常退出、Ctrl+C、未捕获异常尽量主动通知；崩溃、SIGKILL 或断网由租约到期兜底。默认每 2 秒心跳、15 秒失联取消；服务端配置如第 2 节，SDK 也可传 `heartbeat_seconds=2, lease_seconds=15`。心跳在后台自动运行，不依赖读取输出；关闭流或等待超时本身不取消，超过租约的断网会取消。其他客户端查询、读输出不会续租。

`submit()` 只提交、不等待，适合同一进程批量提交；脚本必须继续运行并等待，直接退出会取消。`run_test()` 默认返回输出流；`Client.close()` 或退出 `with Client(...)` 会取消该 Client 的未结束任务。没有脱离客户端运行模式：

~~~python
with Client("http://127.0.0.1:18765", token_file="client.token") as client:
    tasks = [client.submit(path, timeout=120) for path in ("a.bin", "b.bin")]
    for task in tasks:
        finished = client.wait(task.id)
        print(task.id, finished.state, finished.result)
~~~

排队任务直接取消；已分配任务先显示 `cancelling`，由 worker 收尾后才结束并释放端口。NH-only 取消或超时在确认 CPU 复位、UART 清理成功后保留 UVHS PID/session，不重新 download/initialize、不冷却；收尾或控制通道失败则隔离端口，不自动重建。CPU 复位不能证明 DMA/E6 已停止，仍只接受团队认可的 NH-only 程序。

## 4. 通过局域网执行 bin

**服务器**：正常停止，将监听地址改为 `0.0.0.0`，再启动；防火墙需允许所需内网来源访问 TCP 18765。

~~~bash
. "$HOME/fpga-resident-deploy/env.sh"
fpga-scheduler daemon stop
sed -i 's/^bind = "127.0.0.1"/bind = "0.0.0.0"/' "$FPGA_SCHEDULER_CONFIG"
fpga-scheduler daemon start
~~~

**客户端先取得 token：**`client.token` 来自服务器第 1 节生成的 `~/fpga-resident-deploy/service.token`，由管理员安全分发副本。已经获准使用部署账号 SSH 登录的客户端，可直接通过 SCP 加密复制（将 `DEPLOY_USER` 换成 daemon 的部署账号，IP 换成服务器内网 IP）：

~~~bash
umask 077
scp DEPLOY_USER@192.168.1.100:~/fpga-resident-deploy/service.token ./client.token
chmod 600 ./client.token
~~~

没有部署账号 SSH 权限时，请管理员通过 SCP/SFTP 等安全渠道交付这份副本，保存为当前目录的 `client.token`。客户端不需要取得部署账号的登录权限；不要重新运行 `init-token`，也不要将 token 内容发到公开聊天或提交到 Git。

取得第 1 节生成的 wheel 后，在同一客户端目录安装并配置；`FPGA_SCHEDULER_TOKEN_FILE` 指向刚收到的文件，Bash 和 Python 都使用它：

~~~bash
python3.11 -m venv .venv-fpga
. .venv-fpga/bin/activate
python -m pip install --no-deps ./fpga_scheduler-0.1.0-py3-none-any.whl
export FPGA_SCHEDULER_URL="http://192.168.1.100:18765"  # 替换为服务器内网 IP
export FPGA_SCHEDULER_TOKEN_FILE="$PWD/client.token"
chmod 600 "$FPGA_SCHEDULER_TOKEN_FILE"
fpga-scheduler health
~~~

**Bash**（上传客户端本地 bin）：

~~~bash
export FPGA_BIN="/path/to/hello.bin"
fpga-scheduler run "$FPGA_BIN" --timeout 120  # 保持前台；Ctrl+C 取消
~~~

**Python**（将第 3 节的 `run_bin.py` 保存到客户端）：

~~~bash
python run_bin.py "$FPGA_BIN"
~~~

客户端只需 wheel 和 token，无需 FPGA 工具或服务器 config。新终端需激活客户端虚拟环境并重新设置 URL 和 token 环境变量。当前为团队共享 token，所有持有者权限相同；HTTP 不加密，仅用于可信内网，需要加密时使用 HTTPS 反向代理或 SSH 隧道。

## 5. 卸载 daemon

在 FPGA 服务器用原部署账号执行。先确认停止完成，再移走整个部署目录，保留队列和日志备份：

~~~bash
. "$HOME/fpga-resident-deploy/env.sh"
FPGA_BACKUP_DIR="$FPGA_DEPLOY_DIR.backup-$(date +%Y%m%d-%H%M%S)"
fpga-scheduler daemon stop &&
  fpga-scheduler daemon status &&
  mv -- "$FPGA_DEPLOY_DIR" "$FPGA_BACKUP_DIR"
# 确认无需历史队列和日志后，彻底删除：
# rm -rf -- "$FPGA_BACKUP_DIR"
~~~

仅在 stop 成功且 status 显示 `Daemon: STOPPED` 后卸载；失败时先排查。删除 `~/.bashrc` 中加载 `env.sh` 的语句，关闭当前终端。备份目录包含队列和日志，确认不再需要后再删除；不要删除共享锁目录、硬件工程或厂商工具。

更多选项见 [配置示例](configs/resident.example.toml)、[运维说明](docs/operations.md) 和 [API 文档](docs/api.md)。来源见 [NOTICE](NOTICE)，许可证见 [Apache-2.0](LICENSE)。

需要把 FPGA 的计算结果写入 DDR 并下载到主机验证时，参见 [DDR 输出示例](examples/ddr_output/README.md)。


本地开发的离线 FPGA 模拟测试见 [开发工具说明](docs/simulator.md)，无需 FPGA 服务器或硬件。
