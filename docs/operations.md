# 部署、停止与备份

## 本地和内网服务

服务从 --config 或环境变量 FPGA_SCHEDULER_CONFIG 指定的配置启动，显式 --config 优先；
环境变量未设置或为空时，仍必须传 --config。必须存在 owner-only 的令牌文件（600/400），
用 init-token 创建。默认监听本机回环；共享给团队时显式配置内网 bind 地址，
跨不可信网络使用 HTTPS 反向代理。所有调用者共用令牌，具有同等任务权限。

state_dir 必须在服务账号可写的本地文件系统上，保存 service.lock、jobs.sqlite3、
images/。使用同一状态目录的第二个服务会在访问数据库前退出；不提供多进程、
多副本或跨主机 HA。不要通过新建状态目录绕过同一设备池的单实例约束。

SIGTERM/SIGINT 停止接收请求和分配新任务，等待已接受 HTTP 处理及运行任务完成；
queued 留在数据库待重启，关联客户端租约重启时仍须有效，过期则取消。
daemon 正常停止不会主动取消；提交客户端自身退出或 Client.close() 才请求取消其任务。
必须保留充足停机时间，BOSC 默认最长执行约 10 分钟，
另有输入、探测和回收时间。强杀后 running 标为结果未知，不自动重放。

GET /health 只表明服务和调度线程存活；GET /devices 才会探测硬件。
日志保留任务 ID 和错误，不记录请求令牌或上传文件正文；SDK/CLI 输出可保存为证据。

## 服务器本机部署

按 [README](../README.md) 在 FPGA 服务器的用户目录准备 Python 3.11+、虚拟环境、
配置和 token，使用 bosc-resident 后端直接管理本机硬件。当前部署不依赖容器或 VM/VPN
管理脚本；网络接入和厂商环境由管理员单独准备。

使用 README 默认部署路径时，在加载厂商环境的服务器 shell 中执行：

~~~bash
. "$HOME/fpga-resident-deploy/env.sh"
fpga-scheduler daemon start
fpga-scheduler health
fpga-scheduler daemon status
# 停止时等待运行任务和硬件清理，保留 queued
fpga-scheduler daemon stop
~~~

构建部署、CLI/Python 执行和局域网访问见 README，硬件初始化与限制见
[常驻运行时](resident-runtime.md)，卸载步骤见本文。
可选的 systemd 模板仅用于管理员另行托管前台 serve，不与 daemon supervisor 同时使用。

`daemon status` 分别报告 API、队列和端口。硬件初始化或故障恢复可能超过端口查询的
2 秒等待期限；此时仍显示 API 和队列信息，端口显示 `Ports: unavailable` 及原因，
不代表 daemon 已退出。先用 `fpga-scheduler health` 检查 API，等待恢复后重查 status；
不要仅因端口查询超时就重启服务或重复提交 bin。

## 卸载

使用原部署账号和配置正常停止，确认 status 显示 `Daemon: STOPPED`、硬件清理完成后再移走部署：

~~~bash
. "$HOME/fpga-resident-deploy/env.sh"
fpga-scheduler daemon stop
fpga-scheduler daemon status
# 确认已停止后，保留一份包含队列、结果、token 的备份
FPGA_RETIRED_DIR="$FPGA_DEPLOY_DIR.retired-$(date +%Y%m%d-%H%M%S)"
mv -- "$FPGA_DEPLOY_DIR" "$FPGA_RETIRED_DIR"
~~~

如果停止失败或仍在清理，不执行移动或删除。移除自己 shell 启动文件中的 env.sh 加载行，
重新打开终端。确认不需要历史队列和结果后，可删除刚保留的目录：

~~~bash
rm -rf -- "$FPGA_RETIRED_DIR"
~~~

不要删除共享锁目录、硬件工程、厂商软件或其他部署依赖的 Python。
如果另行安装了 systemd 单元，应先按实际部署方式停止、禁用并移除单元。

## 备份、恢复与回滚

第一版使用新的数据库，不导入旧平台历史任务。首次切换：停止旧入口接受提交，
等待 queued/running/upload 全部完成，核验远端硬件进程结束；备份旧代码、SQLite
和输入，再启用新服务。不要同时打开两个 FIFO 服务入口。

备份新服务：停止接受新提交，查询 jobs 确认排空，然后正常停止服务；一起复制
整个 state_dir（数据库和 images）以及配置。令牌、SSH 密钥分开备份，保持权限。
离线复制时无需 service.lock 内容，仅需恢复目录和数据库/输入的完整集合。
如果必须中断运行任务，备份/恢复前先核验远端执行结果和占用，避免重复提交。

恢复：服务关闭状态下恢复完整状态目录和匹配版本代码，修复目录所有权，配置
相同稳定 node ID 和实际设备映射，然后启动。queued 在原会话租约有效时继续排队；历史 running/cancelling
将失败并注明结果未知。恢复队列之前，确认旧服务实例不再存活。

回滚：排空新服务并核验硬件释放，再恢复旧服务及其匹配数据库。不要把本服务
数据库交给旧 buddy-caas，也不要自动重放任何结果未知的任务。

## 保留期

每小时清理超过 retention_days 的终态记录及相应本地输入；所有仍有记录的任务
输入都受保护。未提交的孤立上传以目录年龄清理。BOSC 远端独立清理陈旧运行和
上传目录，执行中目录持有 active.lock 不删除。可配置 retention_days；真实环境
需预留输入、readback 和日志空间。设置短保留期会缩短查询与幂等去重窗口。

## 本机常驻模式

新增 bosc-resident，使用 daemon start/status/stop 管理用户态 supervisor；
配置路径由 --config 或 FPGA_SCHEDULER_CONFIG 指定。
存储准入不足时保持 queued 并退避重试，不占 FPGA 端口、不记硬件冷却；job/jobs 显示容量诊断。
max_storage_bytes 默认 1 GiB，输入和 resident 文件各占一半；磁盘剩余空间另行检查。
默认在可靠结果保存、DDR 校验匹配及复位确认后回收 image.bin/readback.bin，
历史安全任务在升级启动时同样回收；keep_load_files=true 可保留用于调试。
原始输入、UART、结果与 artifacts 仍按保留期保存，不能通过手工批量删除活动目录释放空间。
完整安装、共享令牌分发、端口锁、板级恢复与限制见 [常驻运行时](resident-runtime.md)。
正常 stop 会 drain 并卸载；更换白名单之前用旧配置正常停止。
使用 systemd 时运行 serve，避免叠加 supervisor。SQLite WAL 与主库须一起备份，
推荐停服后复制完整 state_dir；不导入旧系统历史任务。

客户端租约默认 2 秒心跳、15 秒失联；配置 client_heartbeat_seconds / client_lease_seconds。
租约过期会取消关联任务，断网也适用。cancelling 仍占用硬件，等待 worker 复位、清理 UART、
持久化结果后才结束。NH-only 取消/超时收尾成功保留会话且不冷却；失败端口隔离，
devices.reasons 给出原因，不自动重建。管理员检查后用原配置正常停止再启动。
升级需同时更新 daemon 与 SDK：新 HTTP 提交要求客户端会话；旧 SSH 后端明确拒绝新租约提交。
