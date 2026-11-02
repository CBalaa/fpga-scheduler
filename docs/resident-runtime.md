# 本机 daemon 与常驻 UVHS（已实现）

2026-10-26：新增 bosc-resident，首版仅验收 NH-only hello。Simulator 和原 BOSC SSH 后端保留。
服务端与 SDK 均需 Python 3.11+。一个 daemon 管理当前服务器，用户 Python 经本机 HTTP 上传 bin，
复用鉴权、SQLite FIFO、输入校验和结果类型，无需 SSH 回本机。用户不选择端口或启动 UVHS。
配置见 [resident.example.toml](../configs/resident.example.toml)。

## 安装与启动

先排空并正常停止旧入口；所有能操作同一端口的部署使用相同 lock_dir。
仍检查目标端口的 UVHS owner/booked、UART 占用与 TIOCEXCL，被占用时等待，不抢占。
白名单修改必须用旧配置正常停止后进行。初始化前只读检查目标和同板占用、物理时钟，
`shared_clock_index`（默认 6）须与硬件工程 sys_clk 的映射一致。同板其他 FPGA 有 owner、
booked 或受管会话时，只有只读确认该板此 index 的实际频率为 NH 的 5529600 Hz，
才允许原样执行初始化 Tcl；频率未知或不匹配则拒绝初始化。含时钟配置/commit 的脚本
还要求同板单元信息完整；完整且同板没有其他活动端口时允许脚本配置时钟。
不含时钟配置的脚本始终要求现有频率已匹配。scheduler 不省略或改写原 Tcl 的
`config -clock` 与 commit；上述频率兼容性检查不保证同频 commit 在硬件上没有瞬态影响。
板级锁串行化初始化，脚本与 NH profile/目标/共享时钟不兼容时说明原因。
每次新建 UVHS 会话都刷新独立工作目录的 `hw.dat` 链接，使用配置工程当前指向的版本。
切换工程版本后需正常停止并重新启动 daemon；已有会话不会在任务之间自动切换 bitstream。

**动态 runtime 分区与物理拓扑发现。** 某些 UVHS 主机的 `query -fpgas -all` 只列出当时已挂接的
runtime 分区；目标缺行不能证明物理设备不存在，也不能证明它空闲。此类主机可在现有
`[resident]` 表中启用显式映射，例如：

```toml
[resident]
topology_boards = {"0" = "TB1", "1" = "TB2"}
```

键是初始化 Tcl 使用的逻辑 B 板号：`B0.F0–F3` 对应端口 0–3，`B1.F0–F3` 对应 4–7；
值是该主机已核验的真实拓扑板号。该示例不是固定偏移公式，不能根据 TB 编号减一或查询行序
生成映射。键使用规范的非负数字字符串，值使用 `TB` 加规范非负数字；表非空、值唯一，
并覆盖所有启用端口所属板。允许提前登记未启用板的映射。

此模式使用 topology 发现和映射信息、`query -fpgas -all -tb` 的物理 Power/Owner/Booked，
再与普通 runtime inventory 交叉核验；按同一显式映射解析 `query -clock -tb` 的共享时钟。
目标分区尚未挂接时仍须取得完整物理状态，任何来源的 owner、预约或占用信息都不能被另一
张表的空字段覆盖。目标及同板检查通过后，才允许原初始化流程分配、装载和挂接分区。
厂商不支持所需查询、目标缺失、信息不完整或映射冲突时阻塞并报告原因，不回退为“空闲”，
也不跳过所有权、预约或共享时钟检查。未配置 `topology_boards` 时继续采用原 runtime
inventory 模式；该模式仍会拒绝查询中缺失的目标。

### 准备部署初始化文件

这一步由人或部署 AI 在启动服务前完成，scheduler 运行时不自动从硬件脚本提取初始化部分。

1. 在目标机器找到对应 FPGA 已验证可运行的 `~/Desktop/fpga-tester/user_script/hw_run/hw_runN.tcl`，重新只读核对内容及来源版本/SHA-256。
2. 为计划使用的各端口提前在 scheduler 部署目录准备新的 `init/initN.tcl`。对当前参考脚本，人工复制 `source ./user_script/hw_run/write_image.tcl` 之前的内容。不要修改原 `hw_runN.tcl`、原硬件工程或供应商目录。
3. 保留资源分配、数据库加载、连接器、时钟配置、download、initialize 的原参数和顺序。`fpga` 的参考顺序是 `load_db -release` 后 `load_db -db ./hw.dat -to {b1.f0}`；`fpga113` 的参考顺序是 `allocate -board {b1.f0}`、`load_db -release`、`load_db -db ./hw.dat`。两者是机器已验证的参考流程，不是通用模板，部署时必须重新确认。
4. 结束时保持 `cpu_reset=1` 并查询确认。不能包含应用 bin 的 writemem/readmem、`source write_image.tcl`、释放 CPU 复位或 `exit`/关闭 shell。
5. 初始化文件头用注释记录来源服务器、原文件路径、版本或 SHA-256；硬件项目升级后重新核对。每个端口分别准备，不将 FPGA4 的目标直接套给 FPGA5。
6. 在配置中使用 `[resident.init_scripts]`，登记所有已准备的脚本；每个 `[[nodes]].ports` 启用端口必须有映射，允许额外登记未启用端口。配置加载检查所有登记项的规范编号、路径和文件可读性，因此未启用端口的文件也必须存在。仅启用端口接受 Tcl 内容、目标编号和 NH profile 的兼容性校验，并在每次会话重建时重新校验；共享时钟及占用检查在实际初始化前执行。不兼容或初始化失败会明确报错，没有旧初始化回退。原有的单端口映射仍兼容。

例如提前登记 FPGA4–7、只启用 FPGA6：

```toml
[resident.init_scripts]
"4" = "./init/init4.tcl"
"5" = "./init/init5.tcl"
"6" = "./init/init6.tcl"
"7" = "./init/init7.tcl"

[[nodes]]
id = "local"
ports = [6]
uvhs_shell = "/path/to/vendor/bin/uvhs2_shell"
```

`ports` 是唯一启用白名单。登记 FPGA4、5、7 的路径不会创建这些端口的会话、打开 UART、
分配硬件、下载 bitstream 或执行 Tcl。切换时保留节点的其他配置，用旧配置正常停止后，
只更改 `ports`；不自动选择目标或改写 Tcl：

```bash
ssh fpga113
cd ~/fpga-resident-deploy
source ./env.sh
fpga-scheduler daemon stop
/bitstream/nanhu-ra/scripts/check_fpga.sh
vim config.local.toml  # 确认目标空闲，仅将 ports = [6] 改为 ports = [7]
fpga-scheduler daemon start
fpga-scheduler daemon status
```

无需改动 `init_scripts` 或 Tcl 文件；最终必须看到 FPGA7 为 `READY`。占用、锁、共享时钟、
命令确认、超时和 CPU 复位检查仍然生效；不能删除时钟命令来绕过共享时钟保护。
已禁用的故障端口若保留 `closed_cleanly=false`，scheduler 仅在记录明确声明进程已退役、
PID/启动时刻身份完整且实际进程组为空时，允许其他启用端口启动。原脏记录保持不变，
不读取该禁用端口的初始化 Tcl、不打开其 UART，也不重新分配硬件；同板共享时钟检查仍把
它视为活跃兄弟端口。身份不完整或仍有活进程时拒绝启动，不能删除记录或手工标记 clean。
以后重新启用该端口仍须通过完整准入和初始化。

初始化文件路径相对 **TOML 配置文件目录**解析（绝对路径也可），支持开头的 `~`，
不展开 `$HOME` 等环境变量。脚本执行 cwd 是常驻会话的隔离工作目录；`./hw.dat` 指向
`hardware_project/hw.dat`，并非初始化文件所在目录。其他脚本依赖资源须使用明确可读路径；
不要依赖原工程的 `./user_script` 运行文件。配置升级时删除 `resident.explicit_allocation`，
包括原值为 `false` 的配置；分配策略和命令顺序完全由初始化文件提供。旧 bootstrap 缓存不再使用，
`remote.prepare`/旧 BOSC 后端仍保留自己的旧流程，不受此 Resident 配置迁移影响。

当前 NH 初始化校验器只接受可静态核对的简单 Tcl：逐条的已知初始化命令、注释、
字面量变量赋值，以及参考脚本已有的 `UV_RUN_SYS_CLK_HZ` 环境覆盖块。环境覆盖的
实际值也必须符合 NH 的 5529600 Hz。动态目标、任意嵌套 source、过程/循环或其他无法
证明符合目标与共享时钟要求的结构会说明原因并拒绝执行，须由部署者人工核对；
校验器不通过重写脚本来支持这些结构，也不把 Tcl 静态检查视为通用沙箱。

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
  --init-script 4=/path/to/prepared/init4.tcl \
  --init-script 5=/path/to/prepared/init5.tcl \
  --init-script 6=/path/to/prepared/init6.tcl \
  --init-script 7=/path/to/prepared/init7.tcl \
  --destination ./resident-deploy --ports 6 --port 8765
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

部署目录必须不存在。工具要求为每个启用端口显式传入 `--init-script PORT=PATH`，
也允许传入其他未启用端口的脚本。工具检查所有输入脚本可读且非空，按端口号排序原样复制到
新部署的 `init/initN.tcl` 并登记，配置使用相对路径；`installation.json` 记录全部输入脚本路径及
SHA-256，`ports` 单独记录启用白名单。工具不执行硬件命令，也不自动裁剪、生成或改写 Tcl。
只管理 FPGA4 时仍可使用 `--ports 4` 和一个 `--init-script 4=...`。
工具不修改原 Makefile、Tcl、hw.dat 或 systemd 设置。同样支持在 fpga113 本机部署，
先确认该机路径、白名单和 UART。已有部署升级须保留原 token/state、正常停止服务，
更换 wheel 和明确迁移配置，再启动；不要用此首次安装工具覆盖已有部署。
从加载厂商环境的登录 shell 启动，supervisor 继承环境，SSH 断开后继续运行。
start 等待 API 就绪；Resident 的后台维护线程随后初始化白名单端口，即使没有排队任务也会进行。
首次服务构造失败会立即返回具体原因并停止重试，诊断保存在
`state/daemon/service-stderr.log` 的有界轮转日志中；已运行服务的异常退出仍按退避策略恢复。
`devices` / `daemon status` 只读最近一次设备快照，不触发 UVHS 命令，也不等待初始化或硬件锁。
首次采样完成前显示 `RECOVERING` / `Hardware initialization pending`；响应中的 `refreshing`
表示后台查询正在进行，`sampled_at` 标明最近完成采样的时间。初始化完成后显示 `READY` 或
具体拒绝/失败原因。设备快照可能暂时滞后，任务调度仍使用新查询结果和端口预留检查。
stop 停止接收和调度，等待运行任务及卸载，保留 queued 并抑制重启。
supervisor 回收本配置启用端口登记的 UVHS 进程组和探测进程，退避重启异常退出的 daemon；不保证主机重启、
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
的参数可调整。其他客户端查询、读取输出不会续租。

镜像分片上传的每个 PUT/GET 可带 `X-FPGA-Session`：daemon 校验会话仍有效并顺带续租，
所以即使大镜像上传期间独立心跳请求被延迟，租约也不会到期；会话确实过期后分片请求
返回 409，需要新建 Client。分片上传是断点续传的，传输失败只重试未落盘部分。慢速链路
或超大镜像建议把服务端 client_heartbeat_seconds 调大（如 5）、client_lease_seconds
调大（如 120），并相应调大客户端 request_timeout（单个分片请求的超时）。没有 detached 模式。
排队取消不操作硬件；运行取消先进入 cancelling，worker 串行收尾后才完成并释放槽位。
CLI 使用 fpga-scheduler run hello.bin --timeout 120 保持前台，Ctrl+C 取消；submit 命令是前台同义命令。
服务独立执行期限；正常 daemon stop 仍 drain，不因停止服务而主动取消任务。
无个人权限或裸机内存隔离，仅面向受信任团队。

## 执行与恢复

启动每个常驻 UVHS shell 后，原样 source 为该 FPGA 配置的部署初始化 Tcl，校验其与 NH profile、
目标及共享时钟要求相容；不使用 bind_port、不按 write_image 边界自动切片，也不按主机名或开关
组合 allocate/load_db。初始化成功后仍须独立确认 cpu_reset=1，不能只因 source 返回而设为 READY。
UVHS 命令通过随机 session + 递增序号的 Tcl catch 回应确认，
同时检查 ERROR/FATAL；hspRun> 不是成功依据。复位后必须 query -reset 确认 cpu_reset=1。
初始化失败、超时或复位未确认均不可调度。日志保留 script 的来源/摘要及命令输出，供追踪失败。
同一 shell 反复执行任务，正常任务不重复初始化或下载；正常 stop 清理资源并退出该 shell，
故障恢复若需重建会话，重新执行配置的初始化文件。

每任务保留 1 MiB 补齐、入口 0x80000000、rv64gc/lp64d、5.5296 MHz / 115200 UART。
任务期限分为两段：装载阶段（复位确认、补齐镜像、`writemem`、可选 `readmem` 逐字节比较、
UART 静默边界）由 `[resident].load_timeout` 单独限制，默认 600 秒、上限 86400 秒；
应用执行期限（`timeout`，1–86400 秒）从释放 CPU 复位开始到收到终态 UART 记录，不包含
装载时间。`verify_readback = false` 可跳过装载后的全量读回以缩短大镜像装载时间，
结果记录 `ddr_readback_skipped=true`；首次运行新镜像建议保持开启。
释放 CPU 前 fsync start intent，收尾复位、UART 静默边界确认后原子保存结果，
再释放执行槽。普通 PASS 不表示任务成功。
正常退出（含非零应用码）保留会话。取消/超时在 CPU 复位读回及 UART 清理均成功后
同样保留 UVHS PID/session、回到 READY，不重新 download/initialize、不进入失败冷却。
取消/超时打断半条 UART 协议记录时，在确认复位与静默边界后记录 `uart_record_interrupted=true`，
不将截断本身当作收尾失败；完整记录的格式、顺序和重复检查仍有效。
装载期间收到取消不会中断正在等待确认的 writemem/readmem：该命令只受装载期限约束，
确认返回后在下一个安全检查点按取消收尾。装载期间单条 writemem/readmem 的 Tcl 确认超时
仍按"内存确认超时"处理：
任务以平台失败结束（`details.memory_ack_timeout`），端口进入 RECOVERING 并走重建流程，
因为未确认的控制通道不能安全复用；只有装载总期限在安全检查点被超过时才按
执行器 timeout / exit_code 124 结束。镜像 writemem/readmem 或输出 readmem 确认超时，
当前任务按平台失败结束，不自动重放 bin；
端口进入 RECOVERING，禁止在未确认结束的 shell 中继续发送 reset、释放 CPU 或写入下一任务。
恢复只退休本端口已登记的 UVHS 进程组，确认其全部退出且硬件占用已释放后，
重新通过端口锁、占用及共享时钟检查，启动新 shell 并执行原初始化 Tcl；确认 CPU 复位后才可 READY。
内存确认超时的连续恢复次数独立累计，初始化成功不清零；只有完整成功任务清零。
最多自动重建 recovery_attempts 次（默认 5 次）；用尽次数后再次发生内存确认超时，或无法确认旧进程组退出、
无法完成安全重建时隔离并记录原因。
普通取消/应用超时的 CPU 复位或 UART 收尾失败仍 QUARANTINED，不据此自动重建。
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

## 大镜像与大模型运行

整模型 bin 通常超过旧的 16 MiB 单次上传上限，且执行时间可能远超 600 秒。建议按
[configs/resident-llm.example.toml](../configs/resident-llm.example.toml) 调整部署：

| 配置 | 用途 |
|---|---|
| 顶层 `max_image_bytes` | 允许的单个镜像字节数，默认 16 MiB；按模型 bin 大小设置并留出余量 |
| 顶层 `client_heartbeat_seconds` / `client_lease_seconds` | 慢速上传时的租约；可设 5 / 120 |
| 顶层 `upload_timeout` | 单个镜像请求的 socket 读超时，默认 120 秒 |
| `[resident] load_timeout` | 装载阶段总期限（复位+DDR 写入+可选读回），大镜像需按实测放大 |
| `[resident] verify_readback` | 首次运行新镜像保持 true；稳定后可关闭以缩短装载 |
| `[resident] output_ddr_base` / `output_ddr_bytes` | 输出物理窗口；必须覆盖 logits 等输出地址，上限 2 GiB span |
| `[resident] output_ddr_read_mode` | 远地址输出用 `window`（需一次性 A/B 硬件校验，见 [DDR 输出](outputs.md)）；未校验前用 `prefix` 并放大窗口与存储 |
| `[resident] max_output_bytes` | 每任务导出总量，默认 16 MiB，上限 256 MiB |
| `[resident] max_storage_bytes` | 输入与 resident 工件各占一半；大镜像+读回+快照需整体放大 |

客户端无需特殊参数，`Client.submit`/`run_test` 会自动分片并流式计算 SHA256；
可用 `upload_chunk_bytes`（默认 4 MiB）和 `request_timeout` 适配链路。示例：

~~~python
from fpga_scheduler import Client

with Client("http://127.0.0.1:18765", token_file="client.token",
            request_timeout=120) as client:
    task = client.submit("model.bin", timeout=7200)   # 仅执行期限，不含装载
    finished = client.wait(task.id, timeout=None, poll_interval=5)
    print(finished.state, finished.result.execution_status)
    for artifact in finished.result.artifacts:
        client.download_artifact(task.id, artifact["name"], artifact["name"] + ".bin")
~~~

升级已有部署的步骤：正常 `daemon stop` 排空任务 → 替换已安装 wheel（用同一
运行时重建的 wheel，勿只覆盖旧部署）→ 修改 config.local.toml → `daemon start` →
`devices` 看到目标端口 READY → 先跑小 bin 验证装载/读回/复位，再跑大模型。
daemon 升级前已保存的任务记录和输入按原保留策略继续可用；旧 daemon 不支持分片上传，
必须先升级 daemon 再上传超过 16 MiB 的镜像。

## 存储、验收与限制

state_dir 保存 SQLite、images、resident/jobs/<id> 的 UART、镜像/readback、start intent 和结果；
resident/ports/<port> 保存 session 身份和控制日志。输入与 resident 工件各使用 max_storage_bytes
一半预算（默认各 512 MiB），与文件系统剩余空间分别检查。
任务分配端口前预留峰值空间；已经落盘的活动文件只占已用空间，对该任务仅保留尚未落盘的预算，
避免双端口并发时重复计账。预算覆盖补齐镜像、DDR 读回、UART 与其 JSON 结果副本、输出快照及
artifacts；输出快照按读取模式估算（prefix 为整个 `output_ddr_bytes` 窗口，window 为
`max_output_bytes` 级），所以远地址小输出应优先使用 window 模式，否则需要为 622 MiB 级
快照预留存储。输入上限由顶层 `max_image_bytes` 控制（默认 16 MiB，可配到 4 GiB），
应与 `max_storage_bytes` 一半的输入配额匹配。
UART 有上限，超限失败；
控制日志保留当前及上一份，service/supervisor 日志轮转。厂商自身日志计入 resident 配额，
但由厂商写入；准入检查不能替代文件系统硬配额，管理员仍需监控空间。

默认 `keep_load_files=false`：任务原子保存结果、确认 DDR 读回匹配（或读回已按
`verify_readback=false` 跳过，即 `ddr_readback_skipped=true`）和 CPU 复位后，
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
