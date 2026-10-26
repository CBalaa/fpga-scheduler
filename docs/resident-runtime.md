# 本机 daemon 与常驻 UVHS（已实现）

2026-10-26：新增 bosc-resident，首版仅验收 NH-only hello。Simulator 和原 BOSC SSH 后端保留。
服务端与 SDK 均需 Python 3.11+。一个 daemon 管理当前服务器，用户 Python 经本机 HTTP 上传 bin，
复用鉴权、SQLite FIFO、输入校验和结果类型，无需 SSH 回本机。用户不选择端口或启动 UVHS。
配置见 [resident.example.toml](../configs/resident.example.toml)。

## 安装与启动

先排空并正常停止旧入口；所有能操作同一端口的部署使用相同 lock_dir。
仍检查实际 UVHS owner/booked、UART 占用与 TIOCEXCL，遇到外部会话等待，不抢占。
白名单修改必须用旧配置正常停止后进行。共享板级初始化和恢复等待同板受管任务 drain，
不受管会话存在时不执行共享操作。这里只管理白名单内端口。

远端系统 Python 3.8 不满足要求。管理员选用与目标 glibc 兼容的 Python 3.11+ 独立发行包，
从可信发布者 HTTPS 清单独立取得 SHA256，再安装到新目录；不修改系统 Python 或厂商安装。
本次使用经发布清单校验的 python-build-standalone，来源与摘要保存在忽略的验收目录。
按团队策略维护解释器版本。

~~~bash
python3.11 tools/install_verified_python.py python-install-only.tar.gz \
  --sha256 <publisher-sha256> --source <publisher-https-release-url> \
  --destination ./python-runtime
python3.11 -m pip wheel --no-deps . -w ./wheels
python3.11 tools/deploy_resident.py \
  --python ./python-runtime/python/bin/python3 \
  --wheel ./wheels/fpga_scheduler-0.1.0-py3-none-any.whl \
  --project /path/to/existing-hardware-project \
  --uvhs /path/to/vendor/bin/uvhs2_shell \
  --lock-dir /path/to/shared/scheduler-locks \
  --destination ./resident-deploy --ports 4,5 --port 8765
./resident-deploy/venv/bin/fpga-scheduler daemon start --config ./resident-deploy/config.local.toml
./resident-deploy/venv/bin/fpga-scheduler daemon status --config ./resident-deploy/config.local.toml
./resident-deploy/venv/bin/fpga-scheduler daemon stop --config ./resident-deploy/config.local.toml
~~~

也可在管理服务的 shell 中指定默认配置，后续命令省略 --config：

~~~bash
export FPGA_SCHEDULER_CONFIG="$PWD/resident-deploy/config.local.toml"
./resident-deploy/venv/bin/fpga-scheduler daemon start
./resident-deploy/venv/bin/fpga-scheduler daemon status
./resident-deploy/venv/bin/fpga-scheduler daemon stop
~~~

serve 同样读取此变量。显式 --config 优先；变量未设置或为空时，仍必须传 --config。
建议使用绝对路径，以免切换工作目录后指向不同配置；若需新终端自动生效，
将 export 写入用户自己的 shell 启动文件。

部署目录必须不存在。工具不修改原 Makefile、Tcl、hw.dat 或 systemd 设置。
同样支持在 fpga113 本机部署，先确认该机路径、白名单和 UART；本次不在该机板测。
从加载厂商环境的登录 shell 启动，supervisor 继承环境，SSH 断开后继续运行。
start 等待 API 就绪；UVHS 首次初始化在 devices 或首个任务时进行，约一分钟/端口。
首次 Client.devices 可提高 request_timeout=360。status 初始化期间可能显示查询超时。
stop 停止接收和调度，等待运行任务及卸载，保留 queued 并抑制重启。
supervisor 回收自己登记的 UVHS 进程组，退避重启异常退出的 daemon；不保证主机重启、
supervisor 被杀或系统清理用户进程后自动启动。可选 [systemd 模板](../deploy/fpga-scheduler.service.in)
运行 serve 前台模式，由管理员显式安装，避免叠加两层 supervisor。

## Python 调用

管理员安全分发同一团队令牌，各用户保存在自己的 mode 600 文件；不开放服务账号原始 token 文件。
SDK 读取用户自己的 bin 并上传内容，daemon 不访问其私有目录。

~~~python
from fpga_scheduler import Client
client = Client("http://127.0.0.1:8765", token_file="client.token")
result = client.run_test("hello.bin", timeout=120)
print(result.uart_output)
print(result.execution_status, result.app_exit_code, result.ok)
task = client.submit("hello.bin", timeout=120)
print(task.id)  # 保存后可以直接退出，之后 job/wait 查询
~~~

一条命令运行与多进程示例见 [run.py](../examples/resident/run.py)：

~~~bash
python examples/resident/run.py hello.bin --token-file client.token
python examples/resident/run.py hello.bin --token-file client.token --mode submit
python examples/resident/run.py hello.bin --token-file client.token --mode multiprocess
~~~

所有客户端共用权限。客户端退出、Ctrl-C、等待超时或断连不取消任务；cancel 只取消 queued。
服务独立执行期限。无运行中取消、个人权限或裸机内存隔离，仅面向受信任团队。

## 执行与恢复

初始化严格校验原 Tcl 的唯一 load/download/initialize 和写镜像边界，复制前缀到独立工作目录，
绑定实际物理端口并保持 cpu_reset=1。UVHS 命令通过随机 session + 递增序号的 Tcl catch 回应确认，
同时检查 ERROR/FATAL；hspRun> 不是成功依据。复位后必须 query -reset 确认 cpu_reset=1。

每任务保留 1 MiB 补齐、DDR 64 字节元素写入/读回逐字节比较、入口 0x80000000、
rv64gc/lp64d、5.5296 MHz / 115200 UART。释放 CPU 前 fsync start intent，
收尾复位、UART 静默边界确认后原子保存结果，再释放执行槽。普通 PASS 不表示任务成功。
正常退出（含非零应用码）保留会话；trap/timeout/平台/协议故障等待同板安全条件后重建。
重试有上限和指数退避，最终隔离至管理员停服检查。应用返回 0 而收尾失败仍 ok=False。

UART v1 沿用现有 hello；复位期间排空旧字节，每任务独立解析器，拒绝重复 START/终态等。
协议无任务 nonce，边界依赖已确认复位的 NH-only profile；不承诺任意硬件延迟输出的隔离。
DMA/E6 连续复用未开放，服务不能从原始 bin 自动判别是否使用 DMA，只接受团队认可的 NH 程序。

重启先核对 PID+启动时刻、回收已登记旧执行者，再允许初始化调度。
running 有匹配 id/hash/node/port 的可靠终态则恢复，否则 failed/outcome unknown，不自动重放；
queued 按原序继续。SQLite WAL+FULL，状态目录单实例；端口 flock 覆盖 session 并由 UVHS 子进程继承。
启动到登记之间的极小崩溃窗口保守保持实际占用，不抢占无法归属的进程。
物理执行与主机持久化不能提供严格 exactly-once。

## 存储、验收与限制

state_dir 保存 SQLite、images、resident/jobs/<id> 的 UART、镜像/readback、start intent 和结果；
resident/ports/<port> 保存 session 身份和控制日志。输入与 resident 工件各使用 max_storage_bytes
一半预算，任务准备预留空间，不通过删除活动输入腾空间。UART 有上限，超限失败；
控制日志保留当前及上一份，service/supervisor 日志轮转。厂商自身日志计入 resident 配额，
但由厂商写入；准入检查不能替代文件系统硬配额，管理员仍需监控空间。
每小时清理过期终态；保留记录及活动任务输入受保护。

离线：PYTHONPATH=src python3 -m unittest discover -s tests -v。
真机工具 [resident_acceptance.py](../tools/resident_acceptance.py) 要求恰好 4、5 READY，
执行 A/B/A、每端口至少 50 次、多独立 Python 进程超额提交与并行区间校验。
2026-10-26 已验证 FPGA4/5 基准 hello、A/B/A、每端口 51 次健康任务；健康阶段每端口
session 不变，download/initialize 各一次，DDR 读回、UART PASS、exit=0、复位确认均通过。
故障、supervisor 和最终停止证据见忽略的 build/resident-acceptance/ 汇总。
未在 fpga113、第二 Linux 账号或 DMA/E6 镜像上验收。源码及配置示例不提交机器路径、凭据或原始日志。

真机额外验证 exit7/trap/hang 的失败语义、超时后复位与重建、再次 hello；
supervisor 监护的 daemon 被 SIGKILL 后，两个有 start intent 的任务标记结果未知且未重放，
queued 在正常停止/重启后完成。验收结束正常 stop，确认 supervisor/API 停止且 UART4/5 无占用。
