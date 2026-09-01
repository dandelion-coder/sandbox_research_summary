# OpenSandbox Tool 调用链路 Benchmark 与优化实验交接文档

## 1. 项目背景

当前任务围绕 Coding Agent / Agentic RL 场景下的 Sandbox Tool 执行性能进行调研和优化。

重点关注：

- 文件系统 Tool 调用
- Shell / Command Tool 调用
- MCP Tool 调用链路
- Sandbox Runtime
- Tool 执行时延
- 高并发 Agent 场景下的 Tool 执行效率
- Tool 返回内容带来的上下文 / Token 开销

当前主要研究对象：

- OpenSandbox
- CubeSandbox
- Claude Code 文件相关 Tool
- MCP Tool 调用机制

目前优先深入 OpenSandbox。

---

# 2. 总体任务目标

当前组内任务的核心目标可以概括为：

> 分析 Coding Agent 调用 Sandbox Tool 时，从 Agent / MCP 到真正 Sandbox 执行之间的完整调用链路，找出主要性能瓶颈，并尝试从系统实现层面降低 Tool 执行时延和上下文开销。

初始量化目标包括：

```text
Tool 执行时间降低约 50%

Token 消耗降低约 20%
```

这两个数字是研究目标，不代表当前已经实现。

当前 Benchmark 的直接目的不是马上达到 50%，而是首先回答：

```text
一次真实 Tool 调用到底慢在哪里？
```

之后才能决定优化对象。

---

# 3. 当前对整体系统调用关系的理解

目前使用的抽象层级：

```text
Agent
  ↓
MCP
  ↓
Tool
  ↓
SDK
  ↓
API
  ↓
Service / Sandbox / Database
```

对于 OpenSandbox 文件操作，可以具体理解为：

```text
Coding Agent / MCP Client
        ↓
MCP tools/call
        ↓
OpenSandbox MCP Server
        ↓
OpenSandbox Python SDK
        ↓
OpenSandbox Server / Sandbox endpoint
        ↓
execd
        ↓
Linux filesystem / shell
```

例如：

```text
file_read
```

实际大致是：

```text
MCP file_read handler
        ↓
sandbox.files.read_file()
        ↓
Python SDK
        ↓
HTTP request
        ↓
execd /files/download
        ↓
os.Open / file read
        ↓
HTTP response
        ↓
SDK
        ↓
MCP result
        ↓
FastMCP serialization / SSE
        ↓
Client
```

---

# 4. 为什么要做这个 Benchmark

最初曾经尝试分别测不同路径，例如：

```text
native file read
direct HTTP
SDK call
MCP call
```

这种测试能粗略判断热点，但存在一个问题：

> 四个数字来自四条不同请求，不能严格代表一次真实 Tool 调用内部四个连续阶段。

例如：

```text
T1 native
T2 HTTP
T3 SDK
T4 MCP
```

虽然可以比较：

```text
T4 > T3 > T2 > T1
```

但不能简单通过：

```text
T4 - T3
T3 - T2
T2 - T1
```

作为一次真实请求中的阶段时间。

因为每条请求：

- TCP 状态可能不同
- cache 状态不同
- scheduler 状态不同
- FastMCP session 状态不同
- sandbox registry 状态不同
- response payload 不同

因此正式实验必须使用：

> 同一次真实 MCP Tool 请求、同一个 trace_id、同一条时间顺序链路。

---

# 5. 前期独立 Benchmark 已得到的结果

此前曾使用独立路径 Benchmark 测试 `file_read`。

测试文件：

```text
/workspace/django/django/utils/text.py
≈15KB
```

以及：

```text
/workspace/django/django/db/models/sql/query.py
≈124KB
```

得到大致结果：

## 15KB 文件

```text
native file read P50     ≈ 0.016 ms
direct HTTP P50          ≈ 2.28 ms
SDK P50                  ≈ 2.23 ms
MCP P50                  ≈ 10.25 ms
```

MCP Response：

```text
≈33.7KB
```

## 124KB 文件

```text
native P50               ≈ 0.030 ms
direct HTTP P50          ≈ 2.54 ms
SDK P50                  ≈ 2.58 ms
MCP P50                  ≈ 13.41 ms
```

MCP Response：

```text
≈260KB
```

这些结果说明：

```text
真正的磁盘 read 本身几乎不是瓶颈。

MCP 完整链路比 SDK / HTTP 明显更慢。
```

但是这些数据仅用于前期热点发现。

正式实验不能继续把它们当作真实连续阶段。

---

# 6. file_read 第一阶段深入 Trace 实验

为了验证单次请求真实耗时，之前对 `file_read` 做过专项埋点。

该专项代码单独保留在：

```text
/mnt/old_home/xgx/claude-opensandbox-test/opensandbox_file_read
```

该版本不要作为统一 Benchmark 主版本。

专项时间点包括：

```text
C0
M0
M1
M2
S0
S1
S2
M3
M4
C1
```

含义：

```text
C0 Client sends MCP request

M0 MCP file_read handler entry

M1 sandbox lookup done

M2 SDK call start

S0 SDK read_bytes entry

S1 SDK before HTTP

S2 SDK HTTP returned

M3 SDK returned to MCP

M4 MCP result built

C1 Client completely received response
```

---

# 7. 第一阶段真实 Trace 得到的重要结果

以：

```text
django/utils/text.py
```

为例，一次真实请求：

```text
E2E ≈ 9.8~10ms
```

其中典型拆分：

```text
Client → MCP handler
≈ 2.9~3.4ms

MCP handler 内参数/lookup 等
≈ 几十微秒

SDK HTTP → execd → file read → 返回
≈ 3.9~4.2ms

MCP result → Client
≈ 2.6~2.7ms
```

例如一次 Trace：

```text
C0 → M0      ≈ 2.89 ms
M0 → M4      ≈ 4.23 ms
M4 → C1      ≈ 2.66 ms
E2E          ≈ 9.78 ms
```

而 MCP Handler 内除真正 SDK 调用以外的逻辑非常轻。

因此已经可以初步判断：

> MCP Handler 自己的 Python 参数处理不是主要瓶颈。

热点主要在：

```text
1. MCP ingress / dispatch

2. SDK → HTTP → execd → return

3. MCP response serialization / SSE / HTTP return
```

---

# 8. execd / Native 深入拆分曾尝试但暂时停止

此前尝试修改：

```text
components/execd/pkg/web/controller/filesystem_download.go
```

增加：

```text
E0
E1
E2
E3
```

用于进一步拆：

```text
execd handler
native file read
ServeContent
```

但 OpenSandbox execd 使用 Go，需要完整 Go Module 依赖。

当前服务器：

```text
opensandbox/code-interpreter:v1.1.0
```

内部有：

```text
Go 1.25.5
```

但是缺少 execd 需要的大量 Go modules。

Docker / Go module 下载又受到服务器网络代理、DNS 和 HTTPS 自签 CA 限制。

因此当前决定：

> 暂时不继续修改 execd Go 层。

对应 Go 源码已恢复干净。

不要重新引入 execd 埋点，除非后续明确需要进一步拆 Stage 3。

---

# 9. 已发现的重要 Response Payload 问题

在专项 `file_read` Trace 中，进一步检查了 MCP 原始 Response。

测试：

```text
原始文件内容 ≈15161 chars
```

MCP Response：

```text
≈33740 chars
```

约为原始文件：

```text
2.2×
```

检查结构发现：

```text
result
├── content
│   └── content[0].text
│       └── 一个 JSON 字符串
│           ├── path
│           └── content = 文件正文
│
└── structuredContent
    ├── path
    └── content = 同一份文件正文
```

真实统计：

```text
content serialized chars
≈17502

structuredContent serialized chars
≈16136
```

其中：

```text
content[0].text chars
≈16142
```

而：

```text
structuredContent.content
≈15161
```

说明文件正文实际上在 MCP Response 中携带了两份。

这可能导致：

```text
Response bytes 增大

JSON serialization 增加

SSE / HTTP 传输增加

Client parse 成本增加

Agent 上下文输入增加

Token 开销增加
```

这是目前已经记录下来的一个明确潜在优化点。

但是：

> 统一 Baseline Benchmark 阶段不要先优化它。

需要先测 Baseline，再做 A/B Test。

---

# 10. 为什么现在要设计 Unified Benchmark

前面的专项 Trace 只适用于：

```text
file_read
```

但实际 Coding Agent 高频 Tool 包括：

```text
file_read
file_search
file_write
file_replace_contents
command_run
```

如果每个 Tool 都进入 SDK 内部增加：

```text
S0
S1
S2
```

会导致：

- 修改大量 SDK 文件
- 不同 Tool 埋点逻辑不一致
- Benchmark 侵入性太强
- 难以横向比较

因此当前实验设计改成：

> 在 MCP Tool Handler 外层建立统一时间边界。

SDK 和 execd 保持原样。

---

# 11. Unified Benchmark 总体设计

所有 Tool 统一时间轴：

```text
Client
  |
  | C0
  |
  | Stage 1
  v
M0
  |
  | Stage 2
  v
B0
  |
  | Stage 3
  v
B1
  |
  | result build
  v
M4
  |
  | response serialization / transport
  |
  | Stage 4
  v
C1
Client
```

定义：

```text
C0
客户端开始 MCP HTTP request

M0
MCP Tool Handler 真正开始执行

B0
马上进入真正 SDK 调用

B1
SDK 调用完整返回

M4
MCP Python 返回对象已经构造完成

C1
客户端完整收到 MCP Response
```

---

# 12. 四个正式阶段

主实验统一定义：

```text
Stage 1 = M0 - C0
```

表示：

```text
HTTP ingress
Streamable HTTP
MCP request parse
JSON-RPC dispatch
FastMCP Tool routing
```

---

```text
Stage 2 = B0 - M0
```

表示：

```text
MCP Tool Handler 内部准备

sandbox registry lookup

参数转换

RunCommandOpts / SearchEntry 等请求对象构造
```

---

```text
Stage 3 = B1 - B0
```

表示：

```text
OpenSandbox SDK

HTTP

OpenSandbox Server

Sandbox endpoint

execd

真正 filesystem / command execution

response return
```

这是当前最核心的 Sandbox Tool 执行阶段。

---

```text
Stage 4 = C1 - B1
```

表示：

```text
MCP result 构造

FastMCP output conversion

structuredContent / content

JSON serialize

SSE

HTTP response

Client receive
```

---

# 13. Stage 4 的进一步诊断

保留 M4：

```text
result_build = M4 - B1
```

以及：

```text
response_return = C1 - M4
```

这样可以判断：

```text
Stage 4 慢
```

到底是：

```text
MCP Python result 构造慢
```

还是：

```text
FastMCP serialization / SSE / HTTP return 慢
```

前期 `file_read` 数据已经显示：

```text
B1 → M4
通常只有几十微秒

M4 → C1
约 2.6ms
```

因此 Response 返回链路值得重点关注。

---

# 14. 为什么不直接测函数运行时间

实验核心原则：

> 必须保持真实 MCP 请求路径。

不要把：

```text
sandbox.files.read_file()
```

单独调用 100 次后当作 MCP Stage 3 的唯一结论。

也不要：

```text
直接 curl execd
```

替代真实 MCP 调用。

正式 Stage 数据必须来自：

```text
同一次 JSON-RPC tools/call
```

并通过：

```text
trace_id
```

关联。

---

# 15. Trace ID

统一使用：

```text
MCP JSON-RPC request id
```

作为 trace id。

Client：

```json
{
  "jsonrpc": "2.0",
  "id": 201,
  "method": "tools/call"
}
```

MCP：

```python
trace_id = ctx.request_id
```

因此：

```text
Client trace_id
==
MCP trace_id
```

---

# 16. 时间戳

跨 Client / MCP Process：

```python
time.time_ns()
```

用于：

```text
C0
M0
B0
B1
M4
C1
```

原因：

> wall clock 可以跨进程比较。

客户端 E2E 同时使用：

```python
time.perf_counter_ns()
```

用于高精度本进程 duration：

```text
E2E = c1_perf - c0_perf
```

禁止：

```text
跨进程相减 perf_counter_ns
```

---

# 17. 当前实验目录结构

当前实验根目录：

```text
/mnt/old_home/xgx/claude-opensandbox-test
```

内部：

```text
8.31_naive_test

benchmark

benchmark_results

opensandbox_file_read

opensandbox_unified_bench
```

用途：

```text
opensandbox_file_read
```

保存以前 file_read 专项详细 Trace。

不要改。

---

```text
opensandbox_unified_bench
```

当前正式统一 Benchmark 实验代码。

后续代码修改只在这里。

---

# 18. Unified Benchmark 仓库

路径：

```text
/mnt/old_home/xgx/claude-opensandbox-test/opensandbox_unified_bench
```

该代码来自：

```text
/mnt/old_home/xgx/sandbox-research/OpenSandbox
```

的干净副本。

此前已经恢复掉旧 `file_read` 专项埋点。

Unified Benchmark 不修改：

```text
sdks/sandbox/python/src/opensandbox/adapters/filesystem_adapter.py
```

不修改：

```text
components/execd
```

当前主要修改文件：

```text
sdks/mcp/sandbox/python/src/opensandbox_mcp/server.py
```

---

# 19. OpenSandbox Server

实验 OpenSandbox Server 使用：

```text
127.0.0.1:8081
```

配置文件：

```text
opensandbox_unified_bench/server/sandbox.toml
```

启动类似：

```bash
opensandbox-server \
  --config /mnt/old_home/xgx/claude-opensandbox-test/opensandbox_unified_bench/server/sandbox.toml
```

健康检查：

```bash
curl http://127.0.0.1:8081/health
```

---

# 20. MCP Server

当前 unified MCP 启动：

```bash
opensandbox-mcp \
  --transport streamable-http \
  --domain 127.0.0.1:8081 \
  --protocol http
```

当前监听：

```text
http://127.0.0.1:8000/mcp
```

之前专项实验使用过：

```text
8999
```

不要混用。

---

# 21. MCP 虚拟环境

Unified MCP：

```text
/mnt/old_home/xgx/claude-opensandbox-test/
opensandbox_unified_bench/
sdks/mcp/sandbox/python/.venv
```

确认：

```bash
which python
```

应为：

```text
...opensandbox_unified_bench/sdks/mcp/sandbox/python/.venv/bin/python
```

不要使用：

```text
sandbox-research/OpenSandbox/.../.venv
```

因为之前 `cp -a` 复制 venv 后出现过绝对路径残留问题。

---

# 22. 当前 Sandbox

当前实验 sandbox：

```text
3d2f0cd3-2ec2-4ff9-a301-62b3dbd17f6e
```

Django repo：

```text
/workspace/django
```

Git commit：

```text
73cc09f14f13fedddc14d6ba5b287cb33c24e4a4
```

Commit message：

```text
Fixed #36945 -- Made .values() and .order_by()
resolve FilteredRelation aliases correctly.
```

Django repo 约：

```text
7108 files
≈360MB
```

---

# 23. 固定测试文件

小/中型：

```text
/workspace/django/django/utils/text.py
```

约：

```text
15KB
```

大源码：

```text
/workspace/django/django/db/models/sql/query.py
```

约：

```text
124KB
```

---

# 24. Sandbox Registry 注意事项

MCP Server 重启后：

```text
sandbox registry
```

会丢失。

正式 Benchmark 前需要：

```text
initialize MCP
```

然后第一次 Tool：

```json
"connect_if_missing": true
```

把 sandbox 注册回来。

该调用只作为：

```text
warmup / reconnect
```

不能计入正式数据。

之后：

```json
"connect_if_missing": false
```

---

# 25. 当前统一 Server 埋点设计

需要实现通用 Helper，例如：

```python
async def _bench_sdk_call(coro):
    b0 = time.time_ns()
    result = await coro
    b1 = time.time_ns()
    return result, b0, b1
```

以及：

```python
def _bench_emit(
    trace_id,
    tool,
    m0,
    b0,
    b1,
    m4,
):
    ...
```

不要在每个 Handler 复制全部计时代码。

---

# 26. Server Benchmark 输出

每次 Tool 调用只 print 一次：

```text
[BENCH]
trace_id=201
tool=file_read
M0=...
B0=...
B1=...
M4=...
```

不要在：

```text
M0
B0
B1
M4
```

每个点分别 print。

因为打印本身会干扰微秒级测量。

---

# 27. JSONL Server Trace

同时写：

```text
/tmp/opensandbox_unified_bench.jsonl
```

格式：

```json
{
  "trace_id": "201",
  "tool": "file_read",
  "M0": 1788180149454417000,
  "B0": 1788180149488420530,
  "B1": 1788180149492437980,
  "M4": 1788180149492450840
}
```

Client 根据 trace id 自动读取。

---

# 28. 当前 Tool Handler

文件：

```text
sdks/mcp/sandbox/python/src/opensandbox_mcp/server.py
```

主要位置：

```text
command_run
~424

file_read
~482

file_write
~513

file_search
~578

file_replace_contents
~679
```

---

# 29. command_run 时间边界

原逻辑：

```python
sandbox = await _get_or_connect_sandbox(...)

opts = RunCommandOpts(...)

execution = await sandbox.commands.run(
    command,
    opts=opts,
)

return execution
```

因此：

```text
M0
```

放 Handler body 开头。

```text
B0
```

放：

```python
sandbox.commands.run(...)
```

前。

```text
B1
```

放 SDK 返回后。

```text
M4
```

放 return 前。

---

# 30. file_read 时间边界

```python
sandbox = await _get_or_connect_sandbox(...)

content = await sandbox.files.read_file(...)

result = FileReadResponse(
    path=path,
    content=content,
)

return result
```

Stage 3：

```text
B0
↓
sandbox.files.read_file()
↓
B1
```

---

# 31. file_write

Stage 3：

```text
B0
↓
sandbox.files.write_file()
↓
B1
```

之后：

```python
StatusResponse(status="written")
```

属于：

```text
B1 → M4
```

---

# 32. file_search

原逻辑：

```python
SearchEntry(
    path=path,
    pattern=pattern,
)
```

建议把 `SearchEntry` 构造放 B0 之前。

因此：

```text
SearchEntry 构造
```

属于 Stage 2。

Stage 3 只包括：

```text
sandbox.files.search(...)
```

---

# 33. file_replace_contents

将：

```python
return await sandbox.files.replace_contents_detailed(...)
```

拆成：

```python
results = await ...
return results
```

`replace_entries` 构造：

```python
ContentReplaceEntry(...)
```

属于 Stage 2。

真正 replace SDK：

```text
B0 → B1
```

---

# 34. Context

项目当前已经导入：

```python
from mcp.server.fastmcp import Context, FastMCP
```

项目已有写法：

```python
ctx: Context[ServerSession, None] | None = None
```

Benchmark Handler 保持同样类型风格。

trace：

```python
trace_id = (
    ctx.request_id
    if ctx is not None
    else "unknown"
)
```

---

# 35. Client Benchmark 设计

需要实现一个统一 Client，例如：

```text
bench_unified.py
```

职责：

```text
initialize MCP

获取 session id

warmup/register sandbox

遍历 test cases

生成唯一 request id

记录 C0

tools/call

完整收到 Response

记录 C1

通过 trace_id 查询 server JSONL

读取 M0/B0/B1/M4

计算四个 Stage

记录 raw result

汇总统计
```

---

# 36. 每个 Sample 保存的数据

至少：

```text
case
tool
iteration
trace_id

C0
M0
B0
B1
M4
C1

stage1_ms
stage2_ms
stage3_ms
stage4_ms

result_build_ms
response_return_ms

wall_e2e_ms
perf_e2e_ms

response_bytes
status_code

valid
invalid_reason
```

---

# 37. 正式实验 Case

最终固定如下。

| # | 类型 | 操作 | 固定目标 | 为什么测 |
|---:|---|---|---|---|
| 1 | file_read | 小/中型源码读取 | `django/utils/text.py` | Coding Agent 高频读源码 |
| 2 | file_read | 大源码读取 | `django/db/models/sql/query.py` ≈124KB | 看 payload 大小对链路影响 |
| 3 | file_search | 精确文件定位 | `**/query.py` | 文件定位 |
| 4 | file_search | 广泛 glob 搜索 | `**/*query*.py` | 搜索范围/结果数量影响 |
| 5 | file_write | 写文件 | benchmark 临时文件 | Write 类 Tool |
| 6 | file_replace_contents | 修改文件 | benchmark 临时文件 | Edit 类 Tool |
| 7 | command_run | `pwd` | Django repo | 极轻 shell baseline |
| 8 | command_run | `ls` | Django repo | 目录遍历 |
| 9 | command_run | `grep -R` | 搜 `FilteredRelation` | 内容定位 |
| 10 | command_run | `git status --short` | Django repo | Coding Agent 高频命令 |

---

# 38. 为什么选择这些 Case

目标不是覆盖所有 OpenSandbox API，而是覆盖典型 Coding Agent 工作负载。

Agent 常见操作：

```text
找文件
↓
读文件
↓
搜索 symbol / string
↓
编辑文件
↓
运行 shell
↓
检查 Git 状态
```

所以这 10 个 Case 基本覆盖：

```text
Read
Search
Write
Edit
Shell
Git
```

---

# 39. 为什么测两个 file_read

小文件：

```text
≈15KB
```

主要看：

```text
固定协议开销
```

大文件：

```text
≈124KB
```

主要看：

```text
payload size
JSON serialization
SSE transport
response duplication
```

对 Stage 4 的影响。

---

# 40. file_search 的定义

OpenSandbox：

```text
file_search
```

主要是：

```text
glob 文件路径搜索
```

不是全文内容搜索。

因此：

```text
FilteredRelation
```

不能作为 file_search 内容 Case。

全文搜索由：

```text
command_run + grep
```

承担。

---

# 41. command_run 工作负载层次

```text
pwd
```

几乎没有业务计算，主要测试：

```text
固定 Tool 调用成本
```

---

```text
ls
```

增加：

```text
目录读取
结果返回
```

---

```text
grep -R
```

增加：

```text
大量文件扫描
匹配
输出
```

---

```text
git status --short
```

是 Coding Agent 极常见 Git 操作。

---

# 42. file_write / replace 可重复性

必须避免上一轮改变下一轮实验条件。

建议：

```text
/workspace/django/.opensandbox_bench_tmp.txt
```

setup 内容：

```text
OPEN_SANDBOX_BENCH_ORIGINAL
```

replace：

```text
ORIGINAL
→
REPLACED
```

每轮 replace 前：

```text
重新写 ORIGINAL
```

setup 不计时。

---

# 43. Git 状态固定

`git status --short` 对 worktree 状态敏感。

因此：

- Benchmark 临时文件应在 Git status Case 前删除
- 或使用 Django 已忽略路径
- 或明确 setup 到固定相同状态

每轮必须保证输入状态一致。

---

# 44. Warmup

每个 Case：

```text
Warmup = 5
```

用于：

- HTTP keep-alive
- Python code path warmup
- registry stable
- filesystem cache warmup

Warmup 不进入最终统计。

---

# 45. Iterations

建议正式：

```text
Iterations = 100
```

每个 Case 得到 100 个 raw samples。

---

# 46. 汇总指标

对：

```text
Stage1
Stage2
Stage3
Stage4
E2E
result_build
response_return
```

统计：

```text
P50
P95
Mean
Min
Max
Std 可选
```

---

# 47. Response Size

每个 Sample 记录：

```python
len(response.content)
```

这样后续可以分析：

```text
response_bytes
vs
Stage4
```

尤其：

```text
file_read small
vs
file_read large
```

---

# 48. Sanity Check

每个 sample 必须验证：

```text
C0 <= M0 <= B0 <= B1 <= M4 <= C1
```

如果出现：

```text
negative stage
```

标记：

```text
valid=false
```

不能静默进入统计。

---

同时：

```text
stage_sum =
stage1 +
stage2 +
stage3 +
stage4
```

应接近：

```text
wall_e2e = C1 - C0
```

并与：

```text
perf_e2e
```

接近。

---

# 49. 结果文件

建议输出：

```text
benchmark_raw.jsonl
benchmark_raw.csv
benchmark_summary.csv
```

最好再输出：

```text
benchmark_metadata.json
```

记录：

```text
date
sandbox id
git commit
MCP URL
iterations
warmup
case definitions
```

便于实验复现。

---

# 50. Summary Table

最终希望直接得到：

| Case | Stage1 P50 | Stage2 P50 | Stage3 P50 | Stage4 P50 | E2E P50 | E2E P95 | Resp Bytes |
|---|---:|---:|---:|---:|---:|---:|---:|
| file_read small | | | | | | | |
| file_read large | | | | | | | |
| file_search exact | | | | | | | |
| file_search glob | | | | | | | |
| file_write | | | | | | | |
| file_replace | | | | | | | |
| command pwd | | | | | | | |
| command ls | | | | | | | |
| command grep | | | | | | | |
| git status | | | | | | | |

---

# 51. 最终希望回答的问题

实验不是为了单纯得到：

```text
file_read = 10ms
```

而是回答：

### Q1

一次 Coding Agent Tool 调用主要时间花在哪？

```text
MCP ingress?
MCP handler?
Sandbox execution?
Response serialization?
```

### Q2

文件 Tool 和 shell Tool 是否有相同瓶颈？

### Q3

结果大小增加时：

```text
Stage3
```

还是：

```text
Stage4
```

增长更明显？

### Q4

如果优化 Response payload：

```text
Stage4
```

能下降多少？

### Q5

如果未来优化 SDK / execd：

```text
Stage3
```

能下降多少？

---

# 52. 后续优化实验思路

Baseline 跑完之后，根据最大 Stage 决定优化。

例如：

## 如果 Stage 4 最大

优先研究：

```text
content + structuredContent duplication

JSON serialization

SSE

FastMCP structured output

large payload
```

---

## 如果 Stage 3 最大

研究：

```text
SDK HTTP overhead

connection reuse

OpenSandbox Server proxy

execd request path

filesystem implementation

command startup
```

---

## 如果 Stage 1 最大

研究：

```text
Streamable HTTP

MCP dispatch

FastMCP routing

session handling

HTTP stack
```

---

# 53. A/B Test 原则

任何优化必须使用：

> 同一套 Unified Benchmark

比较：

```text
Baseline
vs
Optimized
```

例如：

```text
file_read small

Baseline:
Stage4 P50 = ...

Optimized:
Stage4 P50 = ...

Improvement = ...
```

不要更换测试方法后再比较。

---

# 54. 当前优先级

当前不是优化阶段。

当前第一优先级：

> 把 Unified Benchmark Framework 写正确、跑稳定、自动输出统计表。

顺序：

```text
1. server instrumentation

2. client benchmark

3. file_read_small 单次验证

4. file_read_small 100 次验证

5. 扩展 10 Case

6. Baseline 完整实验

7. 分析 hotspot

8. 开始优化
```

---

# 55. Codex 当前具体任务

Codex 接手后，首先检查：

```text
git diff
```

不要假设代码完全和本文一致。

先阅读：

```text
sdks/mcp/sandbox/python/src/opensandbox_mcp/server.py
```

确认当前已经有哪些 Benchmark 修改。

---

## Task A

完成通用 Server instrumentation：

```text
M0
B0
B1
M4
```

要求：

- 通用 helper
- 最小改动
- 不改变 Tool 行为
- 不改变 return type
- 不修改 SDK
- 不修改 execd

---

## Task B

写统一 Client Benchmark。

要求：

```text
自动 initialize
自动 session
自动 warmup reconnect
自动 trace id
自动 C0/C1
自动关联 Server JSONL
自动四阶段计算
自动异常检测
自动保存结果
```

---

## Task C

先只跑：

```text
file_read_small
```

确认：

```text
Stage sum ≈ E2E
```

并确认：

```text
response_bytes ≈ 33.7KB
```

与前期实验大致一致。

---

## Task D

扩展 10 Case。

---

## Task E

生成 raw + summary。

---

# 56. 明确禁止的改动

当前 Codex 不要：

- 修改 Docker
- 修改 Docker daemon
- 修改系统网络
- 修改 execd Go
- 修改 OpenSandbox SDK
- 优化 Response duplication
- 重构 MCP Framework
- 修改协议行为
- 删除 structuredContent
- 更换 Django commit
- 大范围修改测试仓库

当前仅：

```text
Benchmark instrumentation
+
Benchmark harness
```

---

# 57. 代码实现风格要求

希望代码：

```text
简单
清晰
通用
低侵入
容易删除
容易关闭
容易扩展新的 Tool case
```

避免：

```text
每个 Tool 写几十行重复 Benchmark 代码
```

优先：

```text
helper
case config
统一 recorder
统一 aggregator
```

---

# 58. 对 Benchmark 自身扰动的要求

Benchmark 埋点自身必须尽量轻。

尤其不要：

```text
M0 print
B0 print
B1 print
M4 print
```

四次分别打印。

应该：

```text
记录时间
↓
M4 后一次写日志
```

因为：

```text
Stage2
```

可能只有几十微秒。

频繁日志 I/O 会严重污染数据。

---

# 59. 第一阶段成功标准

最先只验证：

```text
file_read_small
```

输出：

```text
C0
M0
B0
B1
M4
C1
```

且：

```text
C0 < M0 < B0 < B1 < M4 < C1
```

输出类似：

```text
Stage1  2.x ms
Stage2  0.0x ms
Stage3  4.x ms
Stage4  2.x ms

E2E    9~10ms
```

不要求数字完全一致，但数量级应该接近此前专项 Trace。

---

# 60. 项目最终研究路线

整个研究路线可以概括为：

```text
理解 OpenSandbox Tool 调用架构
        ↓
定位真实 Tool 调用链路
        ↓
建立同一次请求 Trace
        ↓
统一划分四个阶段
        ↓
覆盖 Coding Agent 高频 Tool
        ↓
建立 Baseline
        ↓
分析热点
        ↓
提出优化
        ↓
A/B Test
        ↓
评估 latency / payload / token 改善
```

最终目标不是只优化一个：

```text
file_read
```

而是希望建立：

> 一套针对 Coding Agent Sandbox Tool 调用的系统级性能分析和优化方法。