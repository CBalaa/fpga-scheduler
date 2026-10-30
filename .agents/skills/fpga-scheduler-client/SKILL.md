---
name: fpga-scheduler-client
description: 作为 fpga-scheduler 的客户端使用调度器：上传并执行 FPGA bin 镜像、实时读取 UART、查询/等待/取消任务、下载 DDR 输出；引导用户配置 endpoint URL、SSH 隧道和 token 文件；获授权直连 FPGA 主机时只读查看 daemon 与端口状态。仅限客户端操作，不包含也不允许部署或修改 daemon、服务端配置、初始化 Tcl 与 FPGA 硬件。
---

# fpga-scheduler 客户端使用

用于把已构建好的 FPGA bin 提交给 fpga-scheduler daemon，并拿到 UART、退出码与 DDR 输出。本 skill 只讲客户端视角，**部署、配置与修复 daemon 不在范围内**。

文中 `<host>`、`<ssh-target>`、`<token-file>`、`<image>` 等均为占位符，必须替换为用户提供的真实值。本 skill 不含任何具体主机名、账号或 token。

## 0. 硬性边界（先读）

- 只做客户端动作：提交任务、读 UART、查任务、等待、取消本客户端任务、下载输出、查看 health/devices。
- 不部署、不升级、不卸载服务端；不启动/停止/重启 daemon（`daemon start`、`daemon stop`、`serve`、`_supervise` 一律不执行）。
- 不修改服务器上的任何文件：配置文件、初始化 Tcl、env 脚本、硬件工程、bitstream；日志只读。
- 不操作 FPGA/UART 设备，不改 bind/防火墙/端口，不要让用户重启主机来“试试”。
- 不重新生成 token：`init-token` 会生成与 daemon 不匹配的新 token；客户端只使用已有 service token 的副本。
- 状态异常（RECOVERING、QUARANTINED、DEGRADED、端口不可用等）时收集证据并报告用户或部署者，不自行修复或绕过。

## 快速流程

1. 找到可用的 `fpga-scheduler` CLI，或把客户端 wheel 装到本地 venv。
2. 向用户要三样东西：endpoint URL、token 文件路径、（按需）SSH 登录方式。
3. 设 `FPGA_SCHEDULER_URL` / `FPGA_SCHEDULER_TOKEN_FILE`，跑 `fpga-scheduler health` 验证。
4. `fpga-scheduler devices` 确认有 READY 端口。
5. `fpga-scheduler run '<image>' --name '<name>' --timeout <秒>` 执行，读 UART 并汇报。

## 1. 准备客户端命令

优先使用现有环境，不要重装：

```bash
command -v fpga-scheduler
# 若没有全局命令，检查仓库或客户端目录里的 venv
.venv/bin/fpga-scheduler --help
```

需要新装时，只在**客户端本机**建 venv 并安装用户提供的 wheel（wheel 路径向用户要；无外网机器不要尝试联网安装）：

```bash
python3.11 -m venv .venv-fpga-client
.venv-fpga-client/bin/python -m pip install --no-deps '<wheel-path>'
.venv-fpga-client/bin/fpga-scheduler --help
```

- 客户端只需 wheel 和 token，不需要 FPGA 工具链、服务器 config 或部署目录。
- 服务端只监听回环时，客户端仍用同一套 HTTP 协议，经 SSH 隧道访问即可（见第 3 节）。

## 2. 向用户收集并配置连接信息

一次问清，不要猜 host、端口或路径：

| 需要的信息 | 怎么问 | 注意 |
|---|---|---|
| endpoint URL | daemon 的完整地址是什么，形如 `http://<host>:<port>`？还是只能从 FPGA 主机本机访问？ | 只接受 http(s) base URL，不带路径和查询串；不要用默认端口假设实际端口 |
| SSH 访问 | 若 daemon 只在回环监听，是否允许从当前机器 SSH 到 FPGA 主机做端口转发？SSH 目标（`<user>@<host>`，非默认端口/密钥也一并问） | 需要用户明确授权；不要索取 SSH 密码或私钥内容 |
| token 文件 | daemon 的 service token 副本放在哪个路径？ | 必须是同一 token 的副本；只用文件，不要在聊天里传 token 内容 |
| （按需）直连主机 | 是否允许登录 FPGA 主机只读查看 daemon 状态？部署环境脚本或状态目录路径是什么？ | 仅用于只读 status；用户不给就不要自行寻找 |

配置与校验：

```bash
export FPGA_SCHEDULER_URL='<endpoint-url>'
export FPGA_SCHEDULER_TOKEN_FILE='<token-file>'

test -s "$FPGA_SCHEDULER_TOKEN_FILE" || echo 'token 文件不存在或为空'
stat -c 'token 文件权限: %a' "$FPGA_SCHEDULER_TOKEN_FILE"   # 应为 600
chmod 600 "$FPGA_SCHEDULER_TOKEN_FILE" 2>/dev/null || true

fpga-scheduler health
```

- 也支持 `--endpoint` / `--token-file` 参数，但优先用环境变量，避免 token 路径进 shell 历史。
- 不要 `cat` / `echo` token 内容，不要写进命令参数、脚本、日志或 git。
- `health` 失败先修连接（见下一节），不要反复提交任务试探。
- URL 或 token 由用户提供；用户说不清时继续问，不要从其他会话、配置或历史记录里翻找。

## 3. SSH 隧道（daemon 只监听回环时）

```bash
ss -ltn 'sport = :<local-port>'   # 有输出说明本地端口被占用，换一个
ssh -fNT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:<local-port>:127.0.0.1:<daemon-port> '<ssh-target>'

export FPGA_SCHEDULER_URL='http://127.0.0.1:<local-port>'
fpga-scheduler health
```

- 隧道是本地进程，要在任务结束前保持存活。若中途断开，客户端心跳中断，租约到期后未完成任务会被取消；重连后用 `fpga-scheduler job <id>` 查最终状态，不要用同一 ID 重提不同内容。
- 不要为了省隧道去改服务端 bind 或防火墙；不可达就请用户或部署者提供可达地址/隧道。
- 本地端口、远端端口和 SSH 目标都来自用户，不要硬编码。

## 4. 查看服务与端口状态（不需要 SSH）

```bash
fpga-scheduler health     # API/调度线程是否存活；不代表硬件可用
fpga-scheduler devices    # 实际端口状态；health 正常也要看这里再提交
fpga-scheduler jobs       # 活动任务（queued/running/cancelling）
```

- 判断能否提交看 `devices` 是否有 `READY` 端口，而不是只看 health。
- 端口状态：`READY` 可接任务；`BUSY` 正在执行，等待即可；`RECOVERING` 初始化/恢复中，等；`QUARANTINED` 被隔离，不要重试，报告部署者。
- 调度器自动选择端口，不要指定或争抢 FPGA 编号。
- 历史任务：`fpga-scheduler jobs --state completed,failed,cancelled --limit 20`。

## 5. 上传并执行镜像

### 5.1 先确认镜像

- 镜像路径来自用户或构建产物；提交前确认文件存在且非空，并记录大小与本地 sha256 作为证据：

```bash
ls -l '<image>'
sha256sum '<image>'
```

- 程序必须是该部署支持的 NH-only、UART v1 完成协议 bin（自行输出开始与终态记录）。不确定适配性时问用户或构建者，不要自行改镜像、初始化脚本或硬件。
- 不需要先把 bin scp 到服务器：客户端通过 HTTP 直接把镜像内容传给 daemon，分片上传、断点续传并端到端校验 SHA256。
- 大镜像受 daemon 的镜像上限约束；报镜像过大时如实报告，不要改服务端配置。

### 5.2 前台执行（推荐）

```bash
fpga-scheduler run '<image>' --name '<任务名>' --timeout 600 --queue-timeout 3600
```

- 任务 ID 打到 stderr；UART 实时打印到 stdout。`--json` 时 stdout 只输出一个最终 JSON，UART 在结果字段里。
- 成功（`state=completed`、`exit_code=0`）退出码 0；失败或取消为 1；Ctrl+C 为 130 并取消本客户端未结束的任务。
- `--timeout` 是应用执行期限（秒，1..86400），从释放 CPU 复位到收到终态，不含 DDR 装载；`--queue-timeout` 是排队长限；`--idle-seconds` 是旧接口兼容字段，一般不用改。
- `run` / `submit` 都是前台命令，进程要一直活着直到任务结束：客户端退出（包括被 kill）会请求取消自己的未完成任务。
- 长任务用受控后台方式运行对应进程并持续读输出；不要“提交后杀掉进程再另起 wait”，也不要 SIGKILL 之后继续当任务在跑。
- 同一任务 ID 只用于同一份镜像和参数；重试请重新提交得到新 ID，不要复用已冻结的 ID。

### 5.3 Python SDK（需要程序化控制时）

流式执行：

```python
import os
from fpga_scheduler import Client

with Client(os.environ['FPGA_SCHEDULER_URL'],
            token_file=os.environ['FPGA_SCHEDULER_TOKEN_FILE'],
            request_timeout=360) as client:
    with client.run_test('<image>', name='<name>', timeout=600) as stream:
        print('task', stream.id)
        for chunk in stream:
            print(chunk, end='', flush=True)
        result = stream.result
print(result.execution_status, result.app_exit_code, result.ok)
```

提交后等待：

```python
with Client(os.environ['FPGA_SCHEDULER_URL'],
            token_file=os.environ['FPGA_SCHEDULER_TOKEN_FILE']) as client:
    task = client.submit('<image>', name='<name>', timeout=600)
    finished = client.wait(task.id)
    print(task.id, finished.state, finished.result)
```

- `submit()` 只提交不等待，但脚本必须继续运行到任务结束；退出作用域或关闭 Client 会取消该 Client 未结束的任务。
- 读 UART 时按流读取（chunk 可能不按行切）；不要用猜测代替流内容。

### 5.4 结果怎么读

| 字段 | 含义 |
|---|---|
| `state` | `queued` / `running` / `cancelling` / `completed` / `failed` / `cancelled` |
| `execution_status` | `exited`、`trap`、`timeout`、`cancelled`、`protocol_error`、`platform_error`、`interrupted` |
| `app_exit_code` | 应用 main 的原始返回值，仅 `exited` 时有；0 表示应用自己认为成功 |
| `exit_code` | 执行器状态：0 成功；124 超时；130 取消；其他非零失败 |
| `result.details` | UART 终态、DDR 读回、CPU 复位、加载耗时、artifacts 等 |

- 只有 `completed` 且 `exit_code=0` 才是整体成功；`app_exit_code=7` 这类非零是应用返回值，状态通常为 `failed`，不是平台故障。
- 应用必须在 UART 上输出完整的开始/结束完成记录；普通日志里出现 PASS 不代表成功，以 `execution_status` 和完成记录为准。
- `trap` / `timeout` / `platform_error` / `protocol_error` 需要看 UART 和 details 定位；不要据此去改硬件。
- 应用成功但收尾失败时也可能 `ok=False`；以平台状态为准，并把 details 一并汇报。

## 6. 查询、等待与取消

```bash
fpga-scheduler job '<id>'        # 单任务；已结束的任务会显示 UART
fpga-scheduler wait '<id>'       # 等待终态，不取消
fpga-scheduler cancel '<id>'     # 取消；running 会先进入 cancelling
fpga-scheduler jobs --state running
```

- 取消后端口要等清理完成才释放；`cancelling` 不是终态，不要立刻重复提交占端口。
- 查询和读输出不会延长任务租约；只有提交任务的客户端心跳能保活。
- 其他客户端的任务可以查询，但不要干扰；共享 token 下所有操作权限相同。

## 7. 下载 DDR 输出（应用声明输出时）

```bash
fpga-scheduler artifacts '<id>'
fpga-scheduler download '<id>' '<name>' '<destination>'
```

- 下载会校验长度和 SHA256，失败不覆盖原目标文件；可对同一任务重试，不会重跑硬件程序。
- Python：`client.artifacts(id)`、`client.download_artifact(id, name, destination)`。
- 只下载副本到客户端；不要修改服务器上的保存文件。

## 8. 直连 FPGA 主机时查看 daemon 状态（只读）

前提：用户明确允许 SSH，并给出登录目标与部署信息。**只执行只读查询**；不启动、不停止、不修改任何东西。

方式 A：用户提供部署环境脚本时，在 FPGA 主机上只读查看：

```bash
. '<deploy-env.sh>'
fpga-scheduler daemon status
```

输出包含 daemon 状态（RUNNING / STOPPING / DEGRADED / STOPPED）、supervisor PID、配置路径、API health、队列任务数、各端口状态。`Ports: unavailable` 可能只是查询超时：先用 `health` 确认 API，稍后重试 status，不要因此重启服务。

方式 B：在主机上用 loopback 地址调用客户端：

```bash
FPGA_SCHEDULER_URL='http://127.0.0.1:<daemon-port>' FPGA_SCHEDULER_TOKEN_FILE='<token-file>' fpga-scheduler health
```

同样可换成 `devices` / `jobs`。

方式 C：主机上没有 CLI 时，用只读 HTTP 查询同一接口（只读，不安装、不改动服务器环境）。以下片段不会把 token 放进命令行参数：

```bash
python3 - <<'PY'
import json
import urllib.request

token = open('<token-file>').read().strip()
request = urllib.request.Request(
    'http://127.0.0.1:<daemon-port>/devices',
    headers={'Authorization': 'Bearer ' + token})
print(json.dumps(json.load(urllib.request.urlopen(request, timeout=5)),
                 ensure_ascii=False, indent=2))
PY
```

把路径换成 `/health` 或 `/jobs?state=queued,running` 可查其他接口；不要输出 token。

方式 D：只读日志（用户给出 state 目录时）：

```bash
tail -n 100 '<state-dir>/service.log'
```

日志只读，不要删除、清空或改权限；里面可能有任务 ID 和错误，不要回传 token。

状态异常时：保留 `health`、`devices`、`daemon status` 输出和任务 ID，报告用户或部署者。不要执行 `daemon start` / `daemon stop`，不要编辑配置或初始化文件，不要手动 kill 服务端进程。

## 9. 客户端常见错误

| 现象 | 含义 | 客户端处理 |
|---|---|---|
| Connection refused / timeout | endpoint 或隧道错误，服务未监听该地址 | 核对 URL、隧道端口与 `ss` 结果；不可达就请用户或部署者处理 |
| HTTP 401 | token 不匹配 | 换用正确的 token 副本；不要运行 `init-token` |
| 409 session expired / 租约失效 | 客户端断网或退出太久，任务被取消 | 新建 Client 后 `job <id>` 查最终状态；再决定是否重提 |
| 提示 daemon 不支持租约 | 服务端版本较旧 | 报告部署者升级；客户端不要绕过 |
| 400 镜像过大 / 上传冲突 | 超过服务端上限或复用了已冻结 ID | 与用户确认镜像与 ID；不要改服务端配置 |
| 503 存储/容量不足 | 服务端容量等待或配额 | 看 `health` 的存储信息，等容量恢复或报告 |
| queued 很久 | 排队或容量等待 | 看 `job <id>` 的 position / waiting_reason；不要堆积提交占队列 |
| timeout / trap / platform_error | 应用或平台执行失败 | 汇报 UART、execution_status、details；不要动硬件 |
| QUARANTINED / RECOVERING 端口 | 端口隔离或恢复中 | 停止重试并报告；调度器自动分配端口，不要指定 |

## 10. 完成后汇报

至少包含：endpoint（不含 token）、任务 ID 与名称、state、节点/端口、`execution_status`、`app_exit_code`、`exit_code`、耗时（加载/执行）、UART 关键片段或全文、artifacts 清单。失败时附 error / error_details，并明确说明没有修改 daemon 或 FPGA。

## 11. 绝对不要做

- 运行 `fpga-scheduler daemon start` / `daemon stop`、`serve`、`_supervise`，或通过 kill/重启让“服务恢复”。
- 修改或替换服务器上的 config、初始化 Tcl、env 脚本、硬件工程、bitstream、日志。
- 运行 `init-token`，或分发/打印 token 内容；把 token 写进命令参数、脚本或 git。
- 改服务端 bind/防火墙/端口，或要求用户重启主机来规避连接问题。
- 直接操作 UART/FPGA 设备、手动下载 bitstream、重跑硬件初始化脚本。
- 把 `QUARANTINED` / `RECOVERING` 端口当普通故障反复重试，或抢占别人的端口。
