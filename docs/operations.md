# 部署、停止与备份

## 本地和内网服务

服务从显式 --config 启动，必须存在 owner-only 的令牌文件（600/400），
用 init-token 创建。默认监听本机回环；共享给团队时显式配置内网 bind 地址，
跨不可信网络使用 HTTPS 反向代理。所有调用者共用令牌，具有同等任务权限。

state_dir 必须在服务账号可写的本地文件系统上，保存 service.lock、jobs.sqlite3、
images/。使用同一状态目录的第二个服务会在访问数据库前退出；不提供多进程、
多副本或跨主机 HA。不要通过新建状态目录绕过同一设备池的单实例约束。

SIGTERM/SIGINT 停止接收请求和分配新任务，等待已接受 HTTP 处理及运行任务完成；
queued 留在数据库待重启。必须保留充足停机时间，BOSC 默认最长执行约 10 分钟，
另有输入、探测和回收时间。强杀后 running 标为结果未知，不自动重放。

GET /health 只表明服务和调度线程存活；GET /devices 才会探测硬件。
日志保留任务 ID 和错误，不记录请求令牌或上传文件正文；SDK/CLI 输出可保存为证据。

## Compose

在仓库根目录、激活虚拟环境后执行：

~~~bash
fpga-scheduler init-token secrets/service.token
mkdir -p state
export FPGA_UID=$(id -u)
export FPGA_GID=$(id -g)
docker compose -f deploy/compose.yml config -q
docker compose -f deploy/compose.yml up -d --build
fpga-scheduler --token-file secrets/service.token health
~~~

Compose 默认仅映射 127.0.0.1:8765，使用 simulator.toml；容器镜像默认账号 10001。
FPGA_UID/FPGA_GID 指定当前宿主账号，让 bind mount 的 state 和 600 token 可访问。
请勿将权限扩大到全局可读。FPGA_PORT、FPGA_STATE_DIR、FPGA_CONFIG、FPGA_TOKEN_FILE
可覆盖默认路径；相对 bind mount 路径以 deploy/compose.yml 所在目录为基准。

BOSC 使用覆盖配置挂载 SSH 配置/私钥/known_hosts 到固定容器路径，TOML 的 ssh_config
指向容器内路径。配置本地路径必须使用容器内绝对值，例如 state_dir=/var/lib/fpga-scheduler、
token_file=/run/secrets/service.token。默认 Compose 不包含任何 SSH 凭据或 VPN 登录。

stop_grace_period 为 20 分钟。容器健康检查带认证调用 /health；不因硬件离线反复
重启服务。Docker 守护进程不可用时，仍可用本地 Python 完成全部离线验收。

## 备份、恢复与回滚

第一版使用新的数据库，不导入旧平台历史任务。首次切换：停止旧入口接受提交，
等待 queued/running/upload 全部完成，核验远端硬件进程结束；备份旧代码、SQLite
和输入，再启用新服务。不要同时打开两个 FIFO 服务入口。

备份新服务：停止接受新提交，查询 jobs 确认排空，然后正常停止服务；一起复制
整个 state_dir（数据库和 images）以及配置。令牌、SSH 密钥分开备份，保持权限。
离线复制时无需 service.lock 内容，仅需恢复目录和数据库/输入的完整集合。
如果必须中断运行任务，备份/恢复前先核验远端执行结果和占用，避免重复提交。

恢复：服务关闭状态下恢复完整状态目录和匹配版本代码，修复目录所有权，配置
相同稳定 node ID 和实际设备映射，然后启动。queued 继续排队；历史 running
将失败并注明结果未知。恢复队列之前，确认旧服务实例不再存活。

回滚：排空新服务并核验硬件释放，再恢复旧服务及其匹配数据库。不要把本服务
数据库交给旧 buddy-caas，也不要自动重放任何结果未知的任务。

## 保留期

每小时清理超过 retention_days 的终态记录及相应本地输入；所有仍有记录的任务
输入都受保护。未提交的孤立上传以目录年龄清理。BOSC 远端独立清理陈旧运行和
上传目录，执行中目录持有 active.lock 不删除。可配置 retention_days；真实环境
需预留输入、readback 和日志空间。设置短保留期会缩短查询与幂等去重窗口。
