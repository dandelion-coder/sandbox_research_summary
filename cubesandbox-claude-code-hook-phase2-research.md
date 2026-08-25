# CubeSandbox Claude Code Hook 第二阶段调研总结

## 一、调研范围

本阶段只追踪以下调用链：

```text
Claude Code Bash
  → PreToolUse
  → cubesandbox_rewrite.py
  → Claude Code 内置 Bash
  → cubesandbox_exec.py
  → Cube Python SDK
  → Sandbox.commands.run()
  → envd Process API
  → MicroVM
```

重点回答 Hook 输入、命令改写、session 映射、Sandbox 复用及命令进入 MicroVM 的过程。

## 二、核心文件

| 文件 | 职责 |
|---|---|
| `docs/guide/integrations/claude-code.md` | 集成架构与限制 |
| `examples/claude-code-integration/hooks/install.sh` | 注册 `PreToolUse(Bash)` |
| `hooks/cubesandbox_rewrite.py` | 校验 Hook JSON并改写 Bash 参数 |
| `hooks/cubesandbox_exec.py` | Sandbox缓存、复用、状态保存和命令执行 |
| `sdk/python/cubesandbox/_commands.py` | 将命令转换为 envd Process RPC |
| `sdk/python/cubesandbox/sandbox.py` | `Sandbox.create/connect` 与数据面客户端 |

## 三、Hook 注册方式

`install.sh` 将以下配置合并到 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "~/.claude/hooks/cubesandbox_rewrite.py || exit 2"
      }]
    }]
  }
}
```

只有内置 `Bash` 被重定向；`Read/Write/Edit` 继续操作宿主机。Hook 失败时 `exit 2` 阻止命令，属于 fail-closed，避免原命令落到宿主机。

## 四、问题1：Hook 输入是什么？

`cubesandbox_rewrite.py` 从 stdin 读取 Claude Code 提供的 JSON：

```json
{
  "session_id": "session-123",
  "cwd": "/home/user/project",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {
    "command": "pytest",
    "timeout": 120000
  }
}
```

主要字段：

| 字段 | 用途 |
|---|---|
| `tool_name` | 只处理 `Bash` |
| `tool_input.command` | 原始命令 |
| `tool_input.timeout` | 毫秒转秒后传给 executor |
| `session_id` | 作为 Sandbox缓存键 |
| `cwd` | 首次创建时尝试只读挂载项目目录 |

## 五、问题2：如何改写 command？

Hook 将：

```text
Bash("pytest")
```

改为类似：

```bash
python3 cubesandbox_exec.py \
  --session=session-123 \
  --mount=/home/user/project \
  --timeout=120.000 \
  -- 'pytest'
```

实现步骤：

1. 构造参数数组；
2. 用 `shlex.join(argv)` 安全引用；
3. 复制原 `tool_input`；
4. 替换其中的 `command`；
5. 通过 `hookSpecificOutput.updatedInput` 返回；
6. 返回 `permissionDecision: allow`。

原命令作为单独参数传给 executor，分号、重定向和换行不会在宿主机逃逸，只在 MicroVM Shell 中解释。

## 六、问题3：如何找到 session_id？

`session_id` 直接来自 Claude Code Hook payload；缺失时回退为 `default`。executor 对它计算 SHA-256，生成：

```text
~/.cache/cubesandbox-hook/
├── <session-digest>.json
└── <session-digest>.lock
```

状态 JSON 保存：

```json
{
  "sandbox_id": "sandbox-xyz",
  "mount": "/home/user/project",
  "state_token": "随机安全令牌"
}
```

关系为：

```text
Claude session_id → SHA-256状态文件 → sandbox_id → MicroVM
```

## 七、问题4：Sandbox 创建还是复用？

结论：

> 一个 Claude Code `session_id` 复用一个 Sandbox，不是每个 Tool Call 创建一个 Sandbox。

```text
获取 session lock
  → 读取状态文件
  → 找到 sandbox_id？
      ├─ 是：Sandbox.connect()，成功则复用
      └─ 否：Sandbox.create()，保存新 sandbox_id
```

首次调用包含 `Sandbox.create()`；后续调用通常只需 `Sandbox.connect()` 加数据面执行。状态损坏、Sandbox TTL 到期、连接失败或执行 `--reset` 时才重建。默认 TTL 为1800秒。

复用的是 MicroVM，不是永久 Shell：

```text
一个 session → 一个持续复用的 MicroVM
每个 Bash Tool Call → MicroVM 内新建 /bin/bash 进程
```

## 八、cwd 和环境变量如何保持？

每次 Bash 都是新进程，因此 executor 通过 `_state_shell()` 在 MicroVM 内维护：

```text
/tmp/.cubesandbox-state-<token>/
├── cwd
└── env
```

执行前 `source env` 并恢复 cwd；执行后用 `pwd`、`export -p` 保存新状态，同时保留原命令退出码。为避免自动执行恶意代码，不持久化：

```text
BASH_ENV、ENV、LD_PRELOAD、PROMPT_COMMAND
```

同 session 使用 `fcntl.flock` 串行化命令，保证 cwd/env 一致；不同 session 可以并行。

## 九、宿主机 Workspace 挂载

首次创建 Sandbox 时，executor 尝试将 Claude Code 的 cwd 以相同绝对路径只读挂载：

```text
Host /home/user/project
      ↓ readOnly mount
VM   /home/user/project
```

挂载失败时仍创建无挂载 Sandbox，不退回宿主机执行。当前模型是：

```text
Read/Write/Edit → 宿主机可写项目
Bash            → MicroVM内只读项目
```

因此需要写入项目目录的构建、测试或依赖安装可能失败。

## 十、问题5：Command 如何进入 MicroVM？

executor 调用：

```python
sandbox.commands.run(
    _state_shell(command, state_token, mount),
    timeout=timeout,
    user=sandbox_user,
)
```

SDK 构造：

```python
ProcessConfig(
    cmd="/bin/bash",
    args=["-l", "-c", wrapped_command],
    envs=envs,
    cwd=cwd or "",
)
```

并通过 Connect RPC 请求：

```http
POST :49983/process.Process/Start
```

完整数据面路径：

```text
cubesandbox_exec.py
 → Sandbox.commands.run()
 → Cube SDK
 → CubeProxy
 → envd :49983
 → /process.Process/Start
 → /bin/bash -l -c <wrapped command>
 → MicroVM Linux进程
```

envd 返回 `StartEvent → DataEvent → EndEvent`。SDK收集 stdout、stderr 和 exit code，executor 再交给 Claude Code Bash。

## 十一、控制面与数据面

```text
控制面：
Sandbox.create/connect/kill
 → CubeAPI → CubeMaster/Cubelet → MicroVM生命周期

数据面：
Sandbox.commands.run
 → CubeProxy → envd:49983 → MicroVM进程
```

首次 Tool Call包含控制面创建和数据面执行；后续 Tool Call只连接/验证现有 Sandbox并执行命令。

## 十二、时延分析

首次调用：

```text
T_first =
T_hook + T_executor_start + T_sandbox_create
+ T_envd_rpc + T_bash_start + T_command + T_result
```

复用调用：

```text
T_reuse =
T_hook + T_executor_start + T_sandbox_connect
+ T_envd_rpc + T_bash_start + T_command + T_result
```

session复用消除了最大的 `T_sandbox_create`，但仍存在：

1. 每次启动 rewrite Python；
2. 每次启动 executor Python；
3. 每次导入 SDK、读取配置和状态；
4. 每次执行 `Sandbox.connect()`；
5. SDK调用中创建/关闭连接池；
6. 每次在 VM 内启动新 Bash；
7. 同 session 的文件锁排队；
8. envd底层虽流式，executor 当前缓冲后返回。

## 十三、5个问题汇总

| 问题 | 结论 |
|---|---|
| Hook 输入 | stdin JSON；核心字段为 `tool_name`、`tool_input.command/timeout`、`session_id`、`cwd` |
| command改写 | 改成 `python3 cubesandbox_exec.py --session ... -- <原命令>`，用 `shlex` 引用并通过 `updatedInput` 返回 |
| session_id来源 | Claude Code payload；SHA-256后用于状态/锁文件，再映射到 `sandbox_id` |
| Sandbox生命周期 | 一个 session 复用一个 Sandbox；失效、损坏、过期或 reset 才重建 |
| 命令进入 VM | SDK向 envd `:49983/process.Process/Start` 发 Connect RPC，在 VM 内启动 `/bin/bash -l -c` |

## 十四、阶段结论

CubeSandbox 通过 `PreToolUse` 将 Claude Code 内置 Bash透明改写为 executor 调用，并用 `session_id` 复用同一个 MicroVM。cwd 和导出环境通过 VM 内状态文件恢复，形成“Sandbox持久、Shell状态近似持久”的执行方式。

对工具时延而言，复用消除了重复创建 MicroVM 的主要成本；但宿主机 Python启动、控制面 connect、连接池、VM内 Bash启动、同 session 串行锁和缓冲输出仍是下一阶段应测量和优化的部分。

## 十五、后续测试建议

1. 首次 Bash与复用 Bash的 P50/P95/P99；
2. 宿主机 `echo` 与 Sandbox `echo` 的额外开销；
3. rewrite Hook 单独时延；
4. executor启动、SDK import和配置加载时延；
5. `Sandbox.connect()` 时延；
6. envd Process RPC往返时延；
7. VM内 `/bin/bash -l -c` 启动时延；
8. 缓冲与流式返回的首字节时延；
9. 同 session并发命令的锁等待；
10. 多 session、多 Sandbox并发吞吐。
