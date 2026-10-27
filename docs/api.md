# HTTP、SDK 与 CLI 契约

服务仅接受共享 Bearer 令牌，包含 health 在内的所有接口均需
Authorization: Bearer <token>。没有用户身份字段；令牌持有者可操作全部任务。
客户端使用 UTF-8 JSON，文件接口使用原始二进制；错误响应为 {"error": "说明"}。

## HTTP

| 方法与路径 | 请求 / 响应 |
|---|---|
| PUT /sessions/{id} | JSON `{}`，可含 heartbeat_seconds / lease_seconds；建立独立租约，返回实际期限 |
| POST /sessions/{id}/heartbeat | 续租；已关闭或到期返回 409，不复活 |
| DELETE /sessions/{id} | 关闭会话并请求取消其未结束任务；幂等 |
| PUT /jobs/{id}/image | 原始 bin，1 byte–16 MiB；返回 id、sha256 |
| POST /jobs | JSON id、sha256、timeout、idle_seconds、queue_timeout；须带 X-FPGA-Session；返回任务，202 |
| GET /jobs/{id} | 单任务，200；不存在为 404 |
| GET /jobs/{id}/output?offset=0&limit=65536 | 增量 UART 字节与任务快照，200；同样需要 token |
| GET /jobs | 返回 jobs、total、limit、offset；默认 queued,running,cancelling |
| DELETE /jobs/{id} | queued → cancelled；running → cancelling，worker 收尾后终态；终态幂等返回 |
| GET /devices | devices 列表与 errors 节点错误字典 |
| GET /health | 服务与调度线程存活检查，正常 200，退出中/线程不存活为 503 |

列表查询参数：state=queued,running、limit=100、offset=0；state 可选择
queued/running/cancelling/completed/failed/cancelled 的组合。limit 范围 1–256，按提交序号排序。
不支持的参数、重复参数及错误参数为 400。

输出查询的 `offset` 是非负字节偏移，`limit` 范围 1–65536；返回
`id、offset、next_offset、data、done、task`，`data` 为 Base64 编码的原始 UART 字节。
按 `next_offset` 继续请求；只有任务已结束且本次已读到末尾时 `done=true`，
此时 `task` 包含最终结果（如有）。其他响应省略 `task.result`，减少重复传输。
没有新数据时立即返回空 `data`，不占用长期连接；客户端默认每 0.2 秒轮询。
Resident 读取持续刷新且不轮转的分任务 UART 文件；不会读取 UVHS 控制日志或旧任务输出。
Simulator 和旧 SSH 后端兼容此接口，但只能在结束后返回输出。

上传成功后显式提交；上传不会入队。提交校验输入的 SHA256，冻结后拒绝 PUT，
即使内容相同也返回 409。重复 POST 的 ID 与全部规范化参数相同则返回现有任务；
不同则为 409；提交会话也必须相同，其他客户端不能接管原任务。客户端可使用此规则恢复丢失的提交响应。幂等记录保留至任务清理。

会话和任务 ID 均为随机 32 位小写十六进制。团队 token 是认证凭据，会话 ID 是任务生命周期标识，
不增加个人权限。SDK 自动建会话并后台续租；默认每 2 秒心跳、15 秒失联到期。
服务配置 client_heartbeat_seconds / client_lease_seconds 改默认值，SDK 构造参数
heartbeat_seconds / lease_seconds 可覆盖；要求 0.01 ≤ 心跳 < 租约 ≤ 3600 秒。
租约在 SQLite 持久化；到期检查独立于硬件探测，断网也可能导致取消。GET 查询和输出读取不续租，
结束 HTTP 连接本身不取消。过期或关闭的身份不可重新打开；需创建新 Client 提交新任务。
旧 SDK / 无会话的 POST 返回 409；旧 daemon 不支持租约时新 SDK 明确拒绝提交。
BOSC SSH 后端尚不能安全运行中取消，因此会话建立返回 409；resident / simulator 支持新提交语义。

示例提交体：

~~~json
{
  "id": "00000000000000000000000000000001",
  "sha256": "<64 lowercase hex characters>",
  "timeout": 600,
  "idle_seconds": 60,
  "queue_timeout": 3600
}
~~~

ID 必须为 32 位小写十六进制。timeout 范围 1–600；idle_seconds 1–120；
queue_timeout 1–86400，均为整数秒，不接受布尔值或额外字段。省略时默认
600、60、3600。请求必须带 Content-Length；不支持 chunked 请求。

任务字段：id、state、created、started、finished、node、fpga、error。
时间为 Unix 秒；未开始/结束和未分配字段为 null。排队任务另有 position（从 1
开始）、waiting_reason。存在执行响应时另有 result：output、exit_code、node、seconds、
execution_status、app_exit_code、details、uart_output。

exit_code 是执行器状态，0 对应 completed，取消为 130 / cancelled，其他非零对应 failed。app_exit_code
是 bin 中 main 的原始有符号 32 位返回值，只在收到 exited 终态时存在；例如
main 返回 7 时 app_exit_code=7、exit_code=1、state=failed。应用返回 255
也映射为执行器 1，避免与 SSH 断线码混淆。
execution_status 为 exited、trap、timeout、cancelled、protocol_error、platform_error 或
interrupted。trap/timeout 不伪造应用返回值。平台清理失败可覆盖执行状态，
此时已收到的应用返回值仍可保留用于诊断，整个任务仍然失败。
details 包含 UART 终态、DDR 读回校验、错误说明与执行器耗时等信息。
取消使用 exit_code=130、state=cancelled，超时使用 exit_code=124、state=failed；均不伪造 app_exit_code。
UVHS 命令无确认是平台故障，不当作正常应用超时；不在未知命令仍可能执行时发送另一个 reset。
排队取消也保存 cancelled 结果，hardware_started=false。cancelling 仍占用执行槽，输出接口 done=false；
收尾失败则 platform_error / failed，details.interrupted_status 保留 cancelled 或 timeout，
cleanup_error / cancel_reason 说明原因。已读 UART 保留；取消与完成按 worker 的最终判定先后顺序决定。
uart_output 是独立的程序串口文本；output 保留串口与 UVHS 合并的完整会话日志。
控制台与 UART 的输出可能在 output 中交错，校验程序输出时使用 uart_output。
没有终态、仅出现 PASS 文本或 UVHS 正常退出，均不能报成功。
传输断开等无可靠结果时，
result 可不存在，error 解释是否尚未执行或结果未知。任务 completed 不是通用的
硬件数值正确性证明，需由实验程序验证串口/输出。设备中 number 为物理端口号，
node 为配置中的稳定节点 ID；available、reasons 给出当前可调度性，包含本地
预留和失败冷却。探测失败的节点从 devices 中排除并出现在 errors 中。

错误状态：400 校验失败，401 令牌缺失/错误，404 不存在，409 状态或输入冲突，
408 请求体读取超时，503 容量已满或服务暂不可用。查询结果不触发执行重放。

## Python SDK

~~~python
from fpga_scheduler import Client, SchedulerError

client = Client("http://127.0.0.1:8765", token_file="secrets/service.token")
task = client.submit("input.bin", ident=None, timeout=600,
                     idle_seconds=60, queue_timeout=3600)
print(task.id)
page = client.jobs(("queued", "running"), limit=100, offset=0)
state = client.job(task.id)
devices = client.devices()
finished = client.wait(task.id, timeout=1200, poll_interval=2)
# client.cancel(task.id) 支持排队和运行中取消；随后 wait 等待硬件收尾。
client.close()  # 主动取消这个 Client 的全部未结束任务
~~~

submit/job/cancel/wait 返回 Task dataclass；Task.result 为 Result dataclass 或 None。
Result 有 output、node、exit_code、seconds、execution_status、app_exit_code、details、uart_output
和 ok 属性。jobs 返回分页原始字典，
devices/health 返回原始字典。Client 的 request_timeout 是单次 HTTP 超时，默认 30 秒；
wait(timeout=...) 是轮询等待期限，超时抛 SchedulerError，不会取消已提交任务。
SchedulerError.status 为 HTTP 状态或 None；提交/等待错误携带 task_id 便于恢复查询。

submit 上传后提交，仅在 POST 响应丢失时以同 ID 同参数重试一次。不会因不确定
结果生成新 ID。显式使用原 ID 重试 submit 时，冻结上传返回 409 后由 POST 验证
是否同一输入。输入上传或提交失败均通过异常附带 ID；先查 job 再决定是否重试。

run_test(image, timeout=600, idle_seconds=60, queue_timeout=3600, show=True,
         *, stream=True, poll_interval=0.2)
封装提交和输出读取，立即打印 ID。默认返回 OutputStream；调用者迭代读取文本，
读完后从 stream.result 取得最终 Result。需要同步等待并直接返回 Result 时，
显式设置 stream=False；此时 show=True 自动增量打印 UART，show=False 静默等待。
旧 daemon 缺少输出接口时，只有同步模式回退为结束后打印，流模式需要升级 daemon。
若没有执行结果则抛 SchedulerError。执行非零退出码仍返回 Result(ok=False)。
客户端退出或 Client.close() 取消未结束任务；Ctrl+C / 未捕获异常退出尽量主动通知，
SIGKILL 等由租约到期兜底。submit() 返回并不表示脱离进程执行；批量提交后仍须保持进程存活。
推荐 with Client(...)，离开上下文关闭租约。没有 detached 模式，心跳线程为 daemon 线程，不阻止退出。

默认的 `stream=True` 返回 `OutputStream`，不等待执行结束；此时由调用者打印，`show` 不自动打印。
该对象迭代产生 UTF-8 文本块，支持拆分字符及无换行输出；耗尽后 `stream.result` 是
最终 Result，`stream.task` 是最终 Task。任务失败但有结果时仍能读完，检查 `result.ok`；
取消有 Result(execution_status='cancelled', ok=False)；结果未知时抛带 `task_id` 的 SchedulerError。迭代前 result 为 None。

~~~python
with client.run_test("hello.bin", timeout=120) as stream:
    print("task:", stream.id)
    for chunk in stream:
        print(chunk, end="", flush=True)
    result = stream.result
print(result.execution_status, result.app_exit_code, result.ok)
~~~

已有任务可用 `client.stream_output(id, offset=0, timeout=None, poll_interval=0.2)`，
无需再次提交。多个读者独立读取；关闭流、提前退出迭代、等待超时本身不取消任务，
但提交进程退出、close 或失联超过租约会取消。短暂请求失败不立刻取消，心跳独立于读取。
可保存 `stream.id` 和 `stream.offset`，重连后传给 stream_output 继续读取；
offset 排除尚未解码的半个 UTF-8 字符，避免重连丢字。流不自动重连；服务停止后需等
重新启动再查询原 ID。输出和续读受原有任务保留期限限制。

## CLI

~~~text
fpga-scheduler [--endpoint URL] [--token-file PATH] [--json] <command>
~~~

| 命令 | 含义 |
|---|---|
| init-token PATH | 以 600 权限创建随机令牌，拒绝覆盖 |
| serve --config PATH | 启动单实例服务 |
| daemon start/status/stop [--config PATH] | 启动、查看状态、正常停止；配置可由 FPGA_SCHEDULER_CONFIG 指定 |
| run IMAGE [--id ID] [--timeout N] [--idle-seconds N] [--queue-timeout N] | 前台提交、保持心跳、打印 UART、等待收尾；submit 为同义命令 |
| job ID | 查询 |
| jobs [--state queued,running] [--limit N] [--offset N] | 分页列表 |
| cancel ID | 请求取消排队或运行任务；cancelling 尚未完成收尾 |
| wait ID [--timeout N] [--poll-interval N] | 等待终态；不会取消 |
| devices / health | 设备与服务检查 |

默认输出可读文本：daemon status 显示进程、API、活动任务数和端口表；jobs / devices 显示表格，
job / cancel / wait 显示任务详情，run / submit 实时打印 UART 后显示最终摘要，不重复打印 UART。
终端默认按状态着色，表头为青色；管道和重定向默认无颜色，NO_COLOR 环境变量或 TERM=dumb 关闭自动着色。
--color auto/always/never 可放在命令前或后，显式 always 可强制开启；JSON 始终不加颜色，UART 内容不额外着色。
--json 可放在子命令前或后，例如 fpga-scheduler jobs --json，保留原始字段供脚本读取；
JSON 模式 stdout 只包含一个最终 JSON，run / submit 的 UART 位于 result，任务 ID 提示写 stderr。
其余全局参数在子命令前；支持环境变量 FPGA_SCHEDULER_URL、FPGA_SCHEDULER_TOKEN_FILE。
不从命令行接收令牌内容，避免进入进程参数或 shell 历史。错误写 stderr。
成功为退出码 0，run / submit / wait 得到 failed/cancelled 或普通错误为 1，
Ctrl-C 为 130 并取消该客户端的任务。
不能再用命令替换捕获 submit 然后另起 wait 作为后台提交。

## Resident 模式

保留原有 HTTP 路由和 SDK 方法名称；提交必须升级至租约协议，新增上述会话和增量输出接口。devices 增加 state（READY/BUSY/RECOVERING/QUARANTINED）、
session 和 downloads；首次初始化可提高 Client(request_timeout=360)。
Result.details 包含 session、uvhs_pid、fpga、downloads、ddr_readback_matches、reset_confirmed。
应用 exit 0 而收尾失败时 ok=False。取消/超时确认 CPU 复位和 UART 静默后保留 session，不冷却；
若收尾或控制通道失败，隔离端口，不自动重建。仅适用于现有 NH-only profile，不承诺 DMA/E6 安全。
CLI 新增 daemon start/status/stop --config <file>。
