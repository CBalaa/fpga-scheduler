# 迁移记录与验证范围

参考 buddy-caas 提交 34268ca6c0daac3910a3efe411bf0414d090e7be。
本仓库独立运行，reference/ 被 Git 排除，不是运行依赖。
保留 Apache-2.0 的 LICENSE 和来源 NOTICE。

| 原模块 | 独立模块 | 保留/改动 |
|---|---|---|
| fpga_jobs.py | queue.py | FIFO、SQLite、重排、冷却、未知结果不重放；节点/端口配置化 |
| fpga_images.py | storage.py、backends/bosc.py | 校验、冻结、目标上传；改原始 bin PUT API |
| fpga_bridge.py | service.py | 任务 API；新增令牌、列表、进程锁、生命周期；去 shell API |
| device/fpga.py | client.py、cli.py | 独立 Result/Task、等待与取消；移除 K3/Kubernetes、课程路径 |
| bosc_runtime.py | remote/runtime.py | UVHS、flock、隔离运行目录；参数化工具路径、进程组清理 |
| bosc-access/prepare_runtime.py | remote/prepare.py | 不覆盖原工程；校验 release 之后唯一绑定 |
| bosc_hostname.py | remote/relay.py | 可选 stdin JSON 转发；节点白名单和 SSH alias 配置 |
| 新增完成协议 | remote/runner.py、runtime/bosc/uart_protocol.c | UART 分帧、原始返回码、异常/超时、DDR 读回与正常卸载 |
| hello 独立程序 | examples/hello、runtime/bosc、tools/build_hello.py | 移植应用、链接脚本、CRT/运行时与可配置 UART；源码和构建均在本仓库，保留原数值检查 |
| 验收故障变体 | tests/fixtures/bosc_main_faults.c | 独立的返回非零、trap、hang 包装，仅用于显式验收构建 |

原 41 项回归中保留适用的调度与硬件安全语义；旧课程命令兼容、shell 接口、旧上传
协议和旧 SQLite 单机→双机迁移被新 API/独立状态目录替代，不再测试原 wire 协议。
测试覆盖移植的 FIFO/互斥、节点轮转、同号跨机、离线降级、无效清单、幂等、容量、
取消、排队超时、执行失败冷却、显式加载前重排、启动后断线不重放、重启恢复、
校验失败、冻结输入、物理映射、SSH 数据转发及旧目录不覆盖。

新增验证：真实回环 HTTP 与 SDK/CLI、共享令牌、上传 API、分页、并发多客户端、
状态目录跨进程锁、真实进程组超时/后台子进程清理、活跃输入保留、可安装打包。
模拟结果不能代替真实硬件验收，具体要求见 bosc.md。
真机执行、并发、故障与资源恢复的复现流程见 live-setup.md；结果范围见 validation.md。

当前采用 FPGA 服务器本机 daemon 部署，已移除容器部署文件、未接入服务的 VM/VPN
托管模板和外部 Buddy 工程参考补丁；hello 构建直接使用本仓库已接入完成协议的运行时。
仍保留 Simulator、SSH 后端及其测试，用于离线回归、兼容和故障对照。
不迁移 JupyterHub、Keycloak/NestJS/React、课程发布、K3、TReX runtime/算子服务。
第一版是按任务 FIFO，不提供端口时段租约或用户公平性策略；可作为以后扩展的基线。
