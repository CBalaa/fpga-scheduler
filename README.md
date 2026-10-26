# FPGA Scheduler

在 FPGA 服务器运行 daemon，通过 CLI 或 Python 上传 bin，自动排队、分配端口并返回 UART 和退出码。

前提：构建机、服务器及客户端已有 Python 3.11+（下文以 `python3.11` 为例）。服务器已配置厂商环境、UVHS 和硬件工程，运行账号能访问 UART。当前常驻模式支持 NH-only UART v1 程序，使用 FPGA4、5；不要与旧上板脚本同时管理这些端口。所有 `/path/to/...` 和示例 IP 都需替换。

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

## 2. 配置和管理 daemon

仍在 FPGA 服务器执行。硬件工程须包含 `hw.dat` 和 `user_script/hw_run/hw_run4.tcl`、`hw_run5.tcl`；共享锁目录须与其他硬件入口一致且运行账号可写。

~~~bash
cat > "$FPGA_DEPLOY_DIR/config.local.toml" <<'TOML'
backend = "bosc-resident"
state_dir = "./state"
token_file = "./service.token"
bind = "127.0.0.1"
port = 18765

[resident]
profile = "nh-only"
hardware_project = "/path/to/hardware-project"
lock_dir = "/path/to/shared/scheduler-locks"

[[nodes]]
id = "local"
ports = [4, 5]
uvhs_shell = "/path/to/vendor/bin/uvhs2_shell"
TOML
~~~

编辑其中三个绝对路径再启动。相对路径以 config 所在目录为基准；TOML 内不展开 `~` 或 `$HOME`。保存环境变量：

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
# 正常停止：等待运行任务及硬件清理，保留排队任务
fpga-scheduler daemon stop
# 再次启动
fpga-scheduler daemon start
~~~

新终端先执行 `. "$HOME/fpga-resident-deploy/env.sh"`，也可将它加入 `~/.bashrc`。`FPGA_SCHEDULER_CONFIG` 指定默认配置，显式 `--config` 优先。修改配置前先用原配置正常停止，再修改并启动。首次硬件初始化可能需要几分钟，期间状态查询可能超时，稍后重试。SSH 断开后服务继续运行，主机重启后需重新启动。

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
TASK_ID="$(fpga-scheduler --json submit "$FPGA_BIN" --timeout 120 | \
  python -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
echo "$TASK_ID"
fpga-scheduler wait "$TASK_ID" --timeout 4200
# 后续查询：fpga-scheduler job "$TASK_ID"
~~~

`submit --timeout 120` 为执行期限，`wait --timeout 4200` 为客户端等待期限（含排队）。成功时 `state=completed`、`app_exit_code=0`、`exit_code=0`，UART 在 `result.uart_output` 中。

**Python：**保存为 `run_bin.py`（后面的局域网调用也使用此脚本）：

~~~python
import os
import sys
from fpga_scheduler import Client

client = Client(
    os.environ["FPGA_SCHEDULER_URL"],
    token_file=os.environ["FPGA_SCHEDULER_TOKEN_FILE"],
    request_timeout=360,
)
result = client.run_test(sys.argv[1], timeout=120, show=False)
print(result.uart_output if result.uart_output is not None else result.output)
print(result.execution_status, result.app_exit_code, result.ok)
raise SystemExit(0 if result.ok else 1)
~~~

~~~bash
python run_bin.py "$FPGA_BIN"
~~~

CLI 自动读取上述环境变量；Python 脚本通过 `os.environ` 显式传入。客户端退出、断连或等待超时不会取消已接收任务。

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
TASK_ID="$(fpga-scheduler --json submit "$FPGA_BIN" --timeout 120 | \
  python -c 'import json, sys; print(json.load(sys.stdin)["id"])')"
fpga-scheduler wait "$TASK_ID" --timeout 4200
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

仅在 stop 成功且 `supervisor_alive` 为 `false` 后卸载；失败时先排查。删除 `~/.bashrc` 中加载 `env.sh` 的语句，关闭当前终端。备份目录包含队列和日志，确认不再需要后再删除；不要删除共享锁目录、硬件工程或厂商工具。

更多选项见 [配置示例](configs/resident.example.toml)、[运维说明](docs/operations.md) 和 [API 文档](docs/api.md)。来源见 [NOTICE](NOTICE)，许可证见 [Apache-2.0](LICENSE)。
