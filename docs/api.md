# HTTP、SDK 与 CLI 契约

服务仅接受共享 Bearer 令牌，包含 health 在内的所有接口均需
Authorization: Bearer <token>。没有用户身份字段；令牌持有者可操作全部任务。
客户端使用 UTF-8 JSON，文件接口使用原始二进制；错误响应为 {"error": "说明"}。

## HTTP

| 方法与路径 | 请求 / 响应 |
|---|---|
| PUT /jobs/{id}/image | 原始 bin，1 byte–16 MiB；返回 id、sha256 |
| POST /jobs | JSON id、sha256、timeout、idle_seconds、queue_timeout；返回任务，202 |
| GET /jobs/{id} | 单任务，200；不存在为 404 |
| GET /jobs | 返回 jobs、total、limit、offset；默认 queued,running |
| DELETE /jobs/{id} | queued → cancelled；终态幂等返回，running 返回 409 |
| GET /devices | devices 列表与 errors 节点错误字典 |
| GET /health | 服务与调度线程存活检查，正常 200，退出中/线程不存活为 503 |

列表查询参数：state=queued,running、limit=100、offset=0；state 可选择
queued/running/completed/failed/cancelled 的组合。limit 范围 1–256，按提交序号排序。
不支持的参数、重复参数及错误参数为 400。

上传成功后显式提交；上传不会入队。提交校验输入的 SHA256，冻结后拒绝 PUT，
即使内容相同也返回 409。重复 POST 的 ID 与全部规范化参数相同则返回现有任务；
不同则为 409。客户端可使用此规则恢复丢失的提交响应。幂等记录保留至任务清理。

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

exit_code 是远端执行器状态，0 对应 completed，其余对应 failed。app_exit_code
是 bin 中 main 的原始有符号 32 位返回值，只在收到 exited 终态时存在；例如
main 返回 7 时 app_exit_code=7、exit_code=1、state=failed。应用返回 255
也映射为执行器 1，避免与 SSH 断线码混淆。
execution_status 为 exited、trap、timeout、protocol_error、platform_error 或
interrupted。trap/timeout 不伪造应用返回值。平台清理失败可覆盖执行状态，
此时已收到的应用返回值仍可保留用于诊断，整个任务仍然失败。
details 包含 UART 终态、DDR 读回校验、错误说明与执行器耗时等信息。
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
# client.cancel(task.id) 只取消尚未分配的任务。
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

run_test(image, timeout=600, idle_seconds=60, queue_timeout=3600, show=True)
封装提交和等待，立即打印 ID；返回 Result。若没有执行结果则抛 SchedulerError。
执行非零退出码仍返回 Result(ok=False)。Ctrl-C 尝试取消排队任务；已运行则保留任务。

## CLI

~~~text
fpga-scheduler [--endpoint URL] [--token-file PATH] [--json] <command>
~~~

| 命令 | 含义 |
|---|---|
| init-token PATH | 以 600 权限创建随机令牌，拒绝覆盖 |
| serve --config PATH | 启动单实例服务 |
| submit IMAGE [--id ID] [--timeout N] [--idle-seconds N] [--queue-timeout N] | 上传并提交，立即输出任务 |
| job ID | 查询 |
| jobs [--state queued,running] [--limit N] [--offset N] | 分页列表 |
| cancel ID | 取消排队任务 |
| wait ID [--timeout N] [--poll-interval N] | 等待终态；不会取消 |
| devices / health | 设备与服务检查 |

全局参数在子命令前；支持环境变量 FPGA_SCHEDULER_URL、FPGA_SCHEDULER_TOKEN_FILE。
不从命令行接收令牌内容，避免进入进程参数或 shell 历史。--json 输出单个 JSON；
错误写 stderr。成功为退出码 0，wait 得到 failed/cancelled 或普通错误为 1，
Ctrl-C 为 130。submit 成功仅代表任务已接收。
