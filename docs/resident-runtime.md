# 本机 daemon 与常驻 UVHS（已实现）

2026-10-26：新增 bosc-resident，首版仅验收 NH-only hello。Simulator 和原 BOSC SSH 后端保留。
服务端与 SDK 均需 Python 3.11+。一个 daemon 管理当前服务器，用户 Python 经本机 HTTP 上传 bin，
复用鉴权、SQLite FIFO、输入校验和结果类型，无需 SSH 回本机。用户不选择端口或启动 UVHS。
配置见 [resident.example.toml](../configs/resident.example.toml)。

## 安装与启动

先排空并正常停止旧入口；所有能操作同一端口的部署使用相同 lock_dir。
仍检查目标端口的 UVHS owner/booked、UART 占用与 TIOCEXCL，被占用时等待，不抢占。
白名单修改必须用旧配置正常停止后进行。初始化与恢复不因同板其他 FPGA 有会话而直接阻塞：
先只读查询物理时钟，`shared_clock_index`（默认 6，须与硬件工程 sys_clk 的映射一致）
为 5.5296 MHz 时复用现有时钟，跳过 `config -clock` 和 commit，仅初始化白名单内空闲端口。
时钟不一致或无法确认且同板仍有其他会话时保持 RECOVERING，不重配共享时钟；
同板无其他会话时才允许设置时钟。板级锁仍串行化初始化，不要求运行任务排空。
每次新建 UVHS 会话都刷新独立工作目录的 `hw.dat` 链接，使用配置工程当前指向的版本。
切换工程版本后需正常停止并重新启动 daemon；已有会话不会在任务之间自动切换 bitstream。

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
with client.run_test("hello.bin", timeout=120) as stream:
    for chunk in stream:
        print(chunk, end="", flush=True)
    result = stream.result
print(result.execution_status, result.app_exit_code, result.ok)
task = client.submit("hello.bin", timeout=120)
print(task.id)
finished = client.wait(task.id)  # 提交进程必须继续存活，退出会取消未结束任务
client.close()
~~~

run_test 默认返回可迭代的输出流；同步获取 Result 可用 `stream=False`，
配合 `show=True` 自动实时打印 UART，`show=False` 静默等待。

一条命令运行与多进程示例见 [run.py](../examples/resident/run.py)：

~~~bash
python examples/resident/run.py hello.bin --token-file client.token
python examples/resident/run.py hello.bin --token-file client.token --mode submit
python examples/resident/run.py hello.bin --token-file client.token --mode multiprocess
~~~

所有客户端共用权限，但各 Client 具有独立租约。客户端正常退出、Ctrl+C、未捕获异常退出
尽量主动关闭会话并取消全部未结束任务；崩溃、SIGKILL、断网由租约兜底，默认每 2 秒心跳、
15 秒失联取消。后台 daemon 心跳线程独立于输出读取，不阻止 Python 退出；关闭流或等待超时
本身不取消。服务配置 client_heartbeat_seconds / client_lease_seconds 或 SDK 同名去掉 client_ 前缀
的参数可调整。其他客户端查询、读取输出不会续租。没有 detached 模式。
排队取消不操作硬件；运行取消先进入 cancelling，worker 串行收尾后才完成并释放槽位。
CLI 使用 fpga-scheduler run hello.bin --timeout 120 保持前台，Ctrl+C 取消；submit 命令是前台同义命令。
服务独立执行期限；正常 daemon stop 仍 drain，不因停止服务而主动取消任务。
无个人权限或裸机内存隔离，仅面向受信任团队。

## 执行与恢复

初始化严格校验原 Tcl 的唯一 load/download/initialize 和写镜像边界，复制前缀到独立工作目录，
绑定实际物理端口并保持 cpu_reset=1。UVHS 命令通过随机 session + 递增序号的 Tcl catch 回应确认，
同时检查 ERROR/FATAL；hspRun> 不是成功依据。复位后必须 query -reset 确认 cpu_reset=1。

每任务保留 1 MiB 补齐、DDR 64 字节元素写入/读回逐字节比较、入口 0x80000000、
rv64gc/lp64d、5.5296 MHz / 115200 UART。释放 CPU 前 fsync start intent，
收尾复位、UART 静默边界确认后原子保存结果，再释放执行槽。普通 PASS 不表示任务成功。
正常退出（含非零应用码）保留会话。取消/超时在 CPU 复位读回及 UART 清理均成功后
同样保留 UVHS PID/session、回到 READY，不重新 download/initialize、不进入失败冷却。
取消/超时打断半条 UART 协议记录时，在确认复位与静默边界后记录 `uart_record_interrupted=true`，
不将截断本身当作收尾失败；完整记录的格式、顺序和重复检查仍有效。
取消/超时期间控制通道或收尾失败则 QUARANTINED，记录原因，不自动完整重建。
装载取消等待当前 writemem/readmem 确认结束后收尾；等待受 command_timeout 约束。
UVHS 命令没有确认时不再并发发送 reset，按平台失败隔离，禁止释放 CPU 和写入下一任务。
其余 trap/平台/协议故障按同一共享时钟检查重建目标端口，不等待同频的其他 FPGA 空闲。
重试有上限和指数退避，最终隔离至管理员停服检查。应用返回 0 而收尾失败仍 ok=False。

UART v1 沿用现有 hello；复位期间排空旧字节，每任务独立解析器，拒绝重复 START/终态等。
协议无任务 nonce，边界依赖已确认复位的 NH-only profile；不承诺任意硬件延迟输出的隔离。
DMA/E6 连续复用未开放，服务不能从原始 bin 自动判别是否使用 DMA，只接受团队认可的 NH 程序。

重启先核对 PID+启动时刻、回收已登记旧执行者，再允许初始化调度。
running/cancelling 有匹配 id/hash/node/port 的可靠终态则恢复，否则 failed/outcome unknown，不自动重放；
会话租约持久化，queued 在原会话仍有效时按原序继续，过期则取消；晚到心跳不复活任务。
旧版本遗留的无会话 queued 仍按原顺序恢复；新 HTTP 提交不允许无会话。
SQLite WAL+FULL，状态目录单实例；端口 flock 覆盖 session 并由 UVHS 子进程继承。
启动到登记之间的极小崩溃窗口保守保持实际占用，不抢占无法归属的进程。
物理执行与主机持久化不能提供严格 exactly-once。

## 存储、验收与限制

state_dir 保存 SQLite、images、resident/jobs/<id> 的 UART、镜像/readback、start intent 和结果；
resident/ports/<port> 保存 session 身份和控制日志。输入与 resident 工件各使用 max_storage_bytes
一半预算（默认各 512 MiB），与文件系统剩余空间分别检查。
任务分配端口前预留峰值空间；已经落盘的活动文件只占已用空间，对该任务仅保留尚未落盘的预算，
避免双端口并发时重复计账。预算覆盖补齐镜像、DDR 读回、UART 与其 JSON 结果副本、输出快照及 artifacts。
UART 有上限，超限失败；
控制日志保留当前及上一份，service/supervisor 日志轮转。厂商自身日志计入 resident 配额，
但由厂商写入；准入检查不能替代文件系统硬配额，管理员仍需监控空间。

默认 `keep_load_files=false`：任务原子保存结果、确认 DDR 读回匹配和 CPU 复位后，
删除该任务的 `image.bin/readback.bin` 装载副本；取消/超时符合这两个条件也可回收。
校验失败、未确认安全收尾或无可靠终态的任务保留这些文件。
启动升级后的服务时，同样回收符合条件的历史副本；`keep_load_files=true` 可保留至完整任务过期，供调试使用。
原始上传输入、UART、start intent、result.json 和导出 artifacts 仍按 `retention_days` 保留，
每小时清理过期终态，不为容量压力提前删除这些记录或活动任务输入。

resident 配额或文件系统空间不足时，任务保持 `queued`，`started/node/fpga` 为空，
不调用任务 prepare/run，也不对端口施加故障冷却。按 `storage_retry_seconds`（默认 10 秒）重试，
后续任务保持 FIFO；容量恢复后原 ID 自动继续，无需重新提交。
`queue_timeout` 包含这段等待，超时后说明存储等待原因，附带最后一次容量采样，确认硬件未启动。
输入上传本身超配额则 HTTP 503 拒绝，不创建任务；SDK 异常仍携带 ID 和结构化原因。

`fpga-scheduler job <id>` / `jobs` 显示配额等待、已用/剩余预留/请求量、配额上限和磁盘余量；
`daemon status` / `health` 显示两部分配额的缓存采样，查询不等待硬件锁或扫描磁盘。
SDK 使用 `task.waiting_details`、`task.retry_at` 或 `SchedulerError.details` 获取相同信息。
管理员可增大 `[resident].max_storage_bytes`，或减小顶层 `retention_days` 后正常重启服务。
不要因磁盘余量充足就关闭配额，也不要直接删除活动任务目录。

离线：PYTHONPATH=src python3 -m unittest discover -s tests -v。
真机工具 [resident_acceptance.py](../tools/resident_acceptance.py) 要求恰好 4、5 READY，
执行 A/B/A、每端口至少 50 次、多独立 Python 进程超额提交与并行区间校验。
2026-10-26 已验证 FPGA4/5 基准 hello、A/B/A、每端口 51 次健康任务；健康阶段每端口
session 不变，download/initialize 各一次，DDR 读回、UART PASS、exit=0、复位确认均通过。
故障、supervisor 和最终停止证据见忽略的 build/resident-acceptance/ 汇总。
未在 fpga113、第二 Linux 账号或 DMA/E6 镜像上验收。源码及配置示例不提交机器路径、凭据或原始日志。

以下是旧版恢复策略的历史真机证据，不代表新取消/租约策略已经板测：
真机曾额外验证 exit7/trap/hang 的失败语义、超时后复位与重建、再次 hello；
supervisor 监护的 daemon 被 SIGKILL 后，两个有 start intent 的任务标记结果未知且未重放，
queued 在正常停止/重启后完成。验收结束正常 stop，确认 supervisor/API 停止且 UART4/5 无占用。

2026-10-27 新增租约、运行取消及 NH-only 超时复用的离线测试：模拟 UVHS、PTY UART、
独立客户端正常/异常退出、Ctrl+C、SIGKILL、租约到期、双客户端隔离、装载取消及隔离路径。
新策略仍需真机验证：复位和 UART 边界的可靠性、取消/超时后的同 PID/session 下一任务、
控制故障隔离与同板其他任务不受影响。本次未部署、未执行 bin、未操作 FPGA。
