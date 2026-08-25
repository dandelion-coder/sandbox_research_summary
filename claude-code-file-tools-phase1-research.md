# Claude Code 文件 Tool 第一阶段调研总结

## 一、Claude Code 默认有哪些文件 Tool？

Claude Code 默认与文件系统直接相关的核心 Tool 是：

| Tool | 功能 | 是否修改文件 |
|---|---|---:|
| `Read` | 读取文件内容 | 否 |
| `Glob` | 根据路径模式查找文件 | 否 |
| `Grep` | 在文件内容中搜索 | 否 |
| `Write` | 创建文件或整体覆盖文件 | 是 |
| `Edit` | 精确替换文件中的局部内容 | 是 |
| `NotebookEdit` | 修改 Jupyter Notebook 单元格 | 是 |

此外还有：

| Tool | 功能 | 定位 |
|---|---|---|
| `Bash` | 执行 Shell 命令 | 通用命令执行 Tool |
| `MultiEdit` | 一次执行多处修改 | 旧版或历史兼容工具，并非当前核心 Tool |

因此最主要的研究对象可以分成：

```text
读取：Read
搜索：Glob、Grep
修改：Edit、Write
命令：Bash
```

`Bash` 也能通过 `cat`、`find`、`grep`、`sed`、`rm` 等命令操作文件，但它不是专用文件 Tool，而是通用 Shell Tool。

Claude Code 通常会引导模型优先使用专用 Tool：

```text
Read 代替 cat
Glob 代替 find
Grep 代替 grep 命令
Edit 代替 sed
Write 代替 Shell 重定向
```

这样可以获得更结构化的参数、权限控制和返回结果。

## 二、Read / Write / Edit / Glob / Grep 分别怎么执行？

这些都是 Claude Code 内置 Tool，默认不需要 MCP Server。

基本调用流程是：

```text
LLM 产生 Tool Call
        ↓
Claude Code Agent Runtime
        ↓
参数校验与权限检查
        ↓
Claude Code 内置 Tool 实现
        ↓
宿主机文件系统
        ↓
结构化结果返回 LLM
```

### 2.1 Read

用途：读取指定文件。

概念上的调用：

```json
{
  "tool": "Read",
  "input": {
    "file_path": "/project/src/login.py",
    "offset": 1,
    "limit": 200
  }
}
```

执行链：

```text
Claude
  ↓
Read(file_path, offset, limit)
  ↓
Claude Code 校验路径和读取权限
  ↓
打开宿主机文件
  ↓
读取指定范围
  ↓
添加行号、截断提示等结构
  ↓
内容进入 Agent Context
```

`Read` 支持的不只是代码文本，还可能包括图片、PDF 和 Jupyter Notebook。

对 Token 消耗而言，重点参数是 `offset`、`limit` 和 `pages`，因为它们决定返回多少内容。

### 2.2 Write

用途：创建新文件，或者使用完整内容覆盖文件。

概念上的调用：

```json
{
  "tool": "Write",
  "input": {
    "file_path": "/project/src/config.py",
    "content": "DEBUG = False\n"
  }
}
```

执行链：

```text
Claude
  ↓
Write(file_path, content)
  ↓
路径和权限检查
  ↓
必要的写入安全检查
  ↓
创建或覆盖文件
  ↓
返回写入结果
```

`Write` 是整体写入。如果一个 2000 行文件只需要改一行，更适合使用 `Edit`，否则模型需要生成整个文件内容，输出 Token 和写入量都会明显增加。

### 2.3 Edit

用途：对文件内容做精确的局部替换。

概念上的调用：

```json
{
  "tool": "Edit",
  "input": {
    "file_path": "/project/src/config.py",
    "old_string": "DEBUG = True",
    "new_string": "DEBUG = False",
    "replace_all": false
  }
}
```

执行链：

```text
Claude
  ↓
Edit(file_path, old_string, new_string)
  ↓
检查文件及权限
  ↓
在文件中查找 old_string
  ↓
确认匹配是否唯一或符合 replace_all
  ↓
替换成 new_string
  ↓
保存文件并返回差异
```

如果 `old_string` 不存在、匹配位置不唯一，或者文件在读取后发生变化，`Edit` 可能拒绝修改并要求模型重新读取。

相较完整 `Write`，`Edit` 通常只需发送需要替换的局部内容，能够减少模型输出 Token。

### 2.4 Glob

用途：按照文件路径模式查找文件。

概念上的调用：

```json
{
  "tool": "Glob",
  "input": {
    "pattern": "src/**/*.py",
    "path": "/project"
  }
}
```

执行链：

```text
Claude
  ↓
Glob(pattern, path)
  ↓
校验搜索目录和读取权限
  ↓
遍历或索引目录
  ↓
匹配文件路径
  ↓
返回文件路径列表
```

`Glob` 只根据文件名和路径查找，不搜索文件正文。

不同 Claude Code 安装方式可能使用不同底层搜索实现。某些原生 macOS/Linux 构建使用嵌入式 `bfs`，Windows 和 npm 安装版本可能继续使用独立的专用搜索路径。因此性能测试必须记录平台和安装方式。

### 2.5 Grep

用途：搜索文件正文。

概念上的调用：

```json
{
  "tool": "Grep",
  "input": {
    "pattern": "LoginService",
    "path": "/project/src",
    "glob": "*.py",
    "output_mode": "content"
  }
}
```

执行链：

```text
Claude
  ↓
Grep(pattern, path, glob)
  ↓
校验目录和读取权限
  ↓
调用内嵌搜索能力或 ripgrep/ugrep
  ↓
扫描文件内容
  ↓
整理匹配文件、行号和内容
  ↓
结果返回 Claude
```

`Glob` 和 `Grep` 的区别：

```text
Glob：按文件路径查找，例如找到所有 *.py
Grep：按文件正文查找，例如找到包含 LoginService 的代码
```

通过限制 `path`、`glob`、`head_limit` 和 `output_mode`，可以减少搜索时延和返回 Token。

### 2.6 Bash

用途：在 Shell 环境中执行命令。

概念上的调用：

```json
{
  "tool": "Bash",
  "input": {
    "command": "pytest tests/test_login.py",
    "timeout": 120000
  }
}
```

执行链：

```text
Claude
  ↓
Bash(command)
  ↓
命令权限与安全检查
  ↓
启动 Shell 或子进程
  ↓
操作系统执行命令
  ↓
收集 stdout、stderr、exit code
  ↓
结果返回 Claude
```

`Bash` 可以做文件操作，但返回结果通常没有专用 Tool 那么结构化。

## 三、Hook 在这些 Tool 前后怎么插入？

Hook 是可选的工具生命周期拦截机制，不是 Tool 的执行方式。

没有 Hook：

```text
Claude
  ↓
内置 Tool
  ↓
文件系统
  ↓
结果返回 Claude
```

有 Hook：

```text
Claude 生成 Tool Call
        ↓
PreToolUse
        ↓
权限检查
        ↓
内置 Tool 或 MCP Tool 执行
        ↓
成功或失败
       /       \
PostToolUse   PostToolUseFailure
       \       /
        ↓
结果返回 Claude
```

### 3.1 PreToolUse

触发时间：模型已经生成 Tool 名称和参数，但 Tool 还没有执行。

它可以：

- 检查参数；
- 允许或拒绝执行；
- 要求用户确认；
- 修改 Tool 参数；
- 记录开始时间。

例如阻止读取 `.env`：

```text
Claude 调用 Read(".env")
        ↓
PreToolUse
        ↓
检查 file_path
        ↓
发现属于敏感文件
        ↓
拒绝 Read
```

例如将 Bash 转发到 Sandbox：

```text
Claude 原调用：Bash("pytest")
        ↓
PreToolUse 修改参数
        ↓
Bash("cube-executor pytest")
        ↓
命令进入 CubeSandbox
```

### 3.2 PostToolUse

触发时间：Tool 已经成功执行。

它适合：

- 记录结束时间和调用耗时；
- 审计 Tool 结果；
- 文件修改后运行 formatter；
- 检查是否写入调试代码；
- 处理或压缩 Tool 返回；
- 向 Claude 补充上下文。

但是 `PostToolUse` 不能撤销已经发生的操作：`Write` 已经写入、`Bash` 已经执行、MCP 请求已经发出。

### 3.3 PostToolUseFailure

Tool 执行失败时触发，适合记录失败、统计错误类型和向 Claude 提供恢复建议。

### 3.4 是否所有 Tool 都会执行 Hook？

内置 Tool 和 MCP Tool 都支持进入 Hook 生命周期，但只有配置了对应事件并且 `matcher` 匹配时，Hook handler 才真正运行。

例如：

```json
"matcher": "Edit|Write"
```

结果：

```text
Edit     → 执行 Hook
Write    → 执行 Hook
Read     → 不执行 Hook
Grep     → 不执行 Hook
MCP Tool → 不执行 Hook
```

## 四、MCP Tool 和内置 Tool 有什么区别？

### 4.1 内置 Tool

内置 Tool 是 Claude Code 自己实现并随产品提供的，例如：

```text
Read、Write、Edit、Glob、Grep、Bash、Agent、WebFetch、WebSearch
```

执行链：

```text
Claude Code
    ↓
内置 Tool 实现
    ↓
文件系统 / Shell / 网络
```

特点：

- 通常无需额外配置；
- 调用链较短；
- 与 Claude Code 权限系统结合紧密；
- Tool 名称和能力由 Claude Code 提供。

### 4.2 MCP Tool

MCP Tool 是外部 MCP Server 提供的扩展能力。

例如：

```text
OpenSandbox MCP Server
├── file_read
├── file_write
├── command_run
└── sandbox_create
```

执行链：

```text
Claude Code
    ↓
MCP Client
    ↓
MCP 协议
    ↓
MCP Server
    ↓
Tool 实现
    ↓
SDK / API / 后台服务
```

特点：

- 需要配置 MCP Server；
- 可以扩展 Claude Code 原本没有的能力；
- 可以连接 Sandbox、数据库、GitHub 等外部服务；
- 通常比内置 Tool 多一层协议和进程或网络通信。

| 对比项 | 内置 Tool | MCP Tool |
|---|---|---|
| 提供者 | Claude Code | 外部 MCP Server |
| 是否需要配置 | 通常不需要 | 需要 |
| 执行位置 | Claude Code 内部或本机 | MCP Server 及其后端 |
| 调用链 | 较短 | 相对更长 |
| 可扩展性 | 相对固定 | 很强 |
| 能否使用 Hook | 可以 | 可以 |
| 是否依赖 MCP | 不依赖 | 依赖 |

## 五、内置 Read / Write / Edit / Glob / Grep / Bash 的关系

```text
Claude Code Agent Runtime
           │
           ├── Read
           │     └── 读取文件
           ├── Glob
           │     └── 搜索文件路径
           ├── Grep
           │     └── 搜索文件内容
           ├── Edit
           │     └── 局部替换文件
           ├── Write
           │     └── 创建或覆盖文件
           └── Bash
                 └── 执行 Shell 命令
```

从时延和 Token 角度看：

| Tool | 主要时延来源 | 主要 Token 来源 |
|---|---|---|
| `Read` | 文件打开、读取、返回 | 文件正文 |
| `Glob` | 目录遍历 | 文件路径列表 |
| `Grep` | 内容扫描、正则匹配 | 匹配行和上下文 |
| `Edit` | 查找旧内容、替换、保存 | `old_string` / `new_string` |
| `Write` | 写入完整文件 | 模型生成的完整文件 |
| `Bash` | 进程启动、命令执行 | `stdout` / `stderr` |

## 六、PreToolUse / PostToolUse 的配置方式

Hook 通常可以配置在：

```text
~/.claude/settings.json
项目 .claude/settings.json
.claude/settings.local.json
插件 hooks/hooks.json
组织管理配置
```

示例：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python check_file_tool.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python record_file_change.py"
          }
        ]
      }
    ]
  }
}
```

执行结果：

```text
Read       → 只执行 PreToolUse
Edit/Write → PreToolUse → Tool → PostToolUse
Glob/Grep/Bash → matcher 不匹配，不运行这两个 Hook
```

Hook 配置的三个组成部分：

```text
Event：什么时候执行，例如 PreToolUse
Matcher：匹配哪些 Tool，例如 Edit|Write
Handler：匹配后执行什么，例如 Python 脚本
```

## 七、MCP Tool 的命名和执行方式

MCP Tool 在 Claude Code 内一般采用：

```text
mcp__<server_name>__<tool_name>
```

例如：

```text
mcp__filesystem__read_file
mcp__filesystem__write_file
mcp__opensandbox__command_run
mcp__github__search_repositories
```

拆开看：

```text
mcp__      表示这是 MCP Tool
filesystem 表示 MCP Server 名称
read_file  表示 Server 提供的 Tool 名称
```

执行流程：

```text
LLM 生成 MCP Tool Call
        ↓
Claude Code Agent Runtime
        ↓
PreToolUse（如果匹配）
        ↓
Claude Code MCP Client
        ↓
MCP transport
  ├── stdio
  └── streamable HTTP
        ↓
MCP Server
        ↓
Tool handler
        ↓
SDK / API / 本地程序
        ↓
Service / Sandbox / Database
        ↓
结果返回 MCP Server
        ↓
Claude Code MCP Client
        ↓
PostToolUse / PostToolUseFailure
        ↓
结果进入 Claude Context
```

以 OpenSandbox 为例：

```text
Claude
  ↓
mcp__opensandbox__file_read
  ↓
PreToolUse（可选）
  ↓
OpenSandbox MCP Server
  ↓
OpenSandbox SDK
  ↓
OpenSandbox API
  ↓
Sandbox 内 execd
  ↓
读取 Sandbox 文件
  ↓
PostToolUse（可选）
  ↓
结果返回 Claude
```

## 八、完整调用关系

```text
                      Claude
                         │
                  Agent Runtime
                         │
             PreToolUse（可选）
                         │
          ┌──────────────┴──────────────┐
          │                             │
      内置 Tool                      MCP Tool
          │                             │
 Claude Code 内部实现              MCP Client
          │                             │
 文件系统 / Shell                   MCP Server
          │                             │
          │                     SDK / API / Service
          │                             │
          └──────────────┬──────────────┘
                         │
                  执行成功或失败
                    /          \
             PostToolUse   PostToolUseFailure
                    \          /
                         │
                    返回 Claude
```

## 九、阶段结论

Claude Code 默认通过内置 `Read`、`Glob`、`Grep`、`Edit`、`Write` 和 `Bash` 完成本地文件与命令操作；MCP Tool 则通过 MCP Client 和外部 MCP Server 扩展能力。

Hook 不是必须的 Tool 调用方式，而是可选地插在内置 Tool 和 MCP Tool 前后：

- `PreToolUse` 负责执行前检查、阻止或修改参数；
- `PostToolUse` 负责成功后的检查、记录和结果处理；
- `PostToolUseFailure` 负责处理执行失败。

对后续“工具调用时延降低”和“Token 消耗降低”研究而言，建议重点测量：

1. 内置 Tool 与 MCP Tool 的端到端调用时延；
2. 配置 Hook 前后的额外时延；
3. `Read` 全文件读取与分段读取的 Token 差异；
4. `Glob/Grep` 筛选后再 `Read` 的 Token 节省；
5. `Edit` 与完整 `Write` 的输出 Token 和执行时延差异；
6. MCP 的 stdio 与 streamable HTTP transport 时延；
7. `Bash` 原始输出与结构化、压缩输出的 Token 差异。
