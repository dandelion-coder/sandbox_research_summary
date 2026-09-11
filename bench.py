#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(values: list[float]) -> dict[str, float | None]:
    return {
        "mean_ms": statistics.mean(values) if values else None,
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
    }


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if callable(getattr(value, "model_dump", None)):
        try:
            return jsonable(value.model_dump(mode="json"))
        except Exception:
            pass
    return str(value)


def output_meta(value: Any) -> tuple[int, str]:
    raw = json.dumps(
        jsonable(value), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()


def rewrite_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [rewrite_strings(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: rewrite_strings(v, replacements) for k, v in value.items()}
    return value


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


CONTROL_TOOLS = {
    "Task", "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "TaskUpdate", "SendMessage", "ScheduleWakeup", "CronCreate", "CronDelete",
    "CronList", "Workflow", "Skill", "EnterWorktree", "ExitWorktree",
    "ReportFindings", "ListMcpResourcesTool", "ReadMcpResourceDir",
    "ReadMcpResourceTool", "WebFetch", "WebSearch",
}


def is_filesystem_operation(logical_tool: str, command: str = "") -> bool:
    if logical_tool in {"Read", "Write", "Edit", "Grep", "Glob"}:
        return True
    if logical_tool == "Bash":
        cmd = command.lower()
        fs_commands = (
            "ls", "cat", "head", "tail", "grep", "find", "sed", "awk",
            "cp", "mv", "rm", "mkdir", "touch", "chmod", "wc",
        )
        command_pattern = "|".join(re.escape(item) for item in fs_commands)
        # Match a command at the start of a shell segment, including after pipes,
        # semicolons, &&/||, or newlines. This avoids classifying `echo cat` as FS.
        if re.search(
            rf"(?:^|[;|&\n]\s*)(?:sudo\s+)?(?:{command_pattern})(?:\s|$)",
            cmd,
        ):
            return True
    return False


def normalized_record(
    logical_tool: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    fs_operation: bool,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    record = {
        "logical_tool": logical_tool,
        "tool": tool,
        "fs_operation": fs_operation,
        "arguments": arguments,
    }
    if skip_reason is not None:
        record["skip_reason"] = skip_reason
    return record


def load_atif_files(paths: list[Path]) -> list[dict[str, Any]]:
    sessions = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                item = dict(item)
                item["_source_file"] = str(path)
                sessions.append(item)
    return sessions


def infer_original_cwd(session: dict[str, Any]) -> str | None:
    cwds = session.get("agent", {}).get("extra", {}).get("cwds") or []
    if cwds:
        return str(cwds[0])
    for step in session.get("steps", []):
        cwd = step.get("extra", {}).get("cwd")
        if cwd:
            return str(cwd)
    return None


def normalize_claude_tool_call(
    function_name: str,
    arguments: dict[str, Any],
    historical_cwd: str | None,
    replay_root: str,
    extra_path_maps: dict[str, str],
) -> dict[str, Any]:
    replacements = dict(extra_path_maps)
    if historical_cwd:
        replacements[historical_cwd] = replay_root
    args = rewrite_strings(dict(arguments or {}), replacements)
    name = function_name

    if name == "Bash":
        command = str(args["command"])
        return normalized_record(
            "Bash",
            "command_run",
            {"command": command, "working_directory": replay_root},
            fs_operation=is_filesystem_operation(name, command),
        )

    if name == "Read":
        path = args.get("file_path") or args.get("path")
        if not isinstance(path, str):
            raise ValueError("Read missing file_path/path")
        offset, limit = args.get("offset"), args.get("limit")
        if offset is None and limit is None:
            return normalized_record(
                "Read", "file_read", {"path": path},
                fs_operation=True,
            )
        start = max(int(offset or 1), 1)
        limit_value = None if limit is None else int(limit)
        command = (
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            f"p = Path({path!r})\n"
            f"start = {start}\n"
            f"limit = {limit_value!r}\n"
            "lines = p.read_text(errors='replace').splitlines(True)\n"
            "chunk = lines[start-1:] if limit is None else lines[start-1:start-1+limit]\n"
            "print(''.join(chunk), end='')\n"
            "PY"
        )
        return normalized_record(
            "Read", "command_run",
            {"command": command, "working_directory": replay_root},
            fs_operation=True,
        )

    if name == "Write":
        path = args.get("file_path") or args.get("path")
        if not isinstance(path, str):
            raise ValueError("Write missing file_path/path")
        return normalized_record(
            "Write", "file_write",
            {"path": path, "content": str(args.get("content", ""))},
            fs_operation=True,
        )

    if name == "Edit":
        path = args.get("file_path") or args.get("path")
        if not isinstance(path, str):
            raise ValueError("Edit missing file_path/path")
        old = args.get("old_string")
        new = args.get("new_string")
        if args.get("replace_all", False):
            command = (
                "python3 - <<'PY'\n"
                "from pathlib import Path\n"
                f"p = Path({path!r})\n"
                f"old = {str(old)!r}\n"
                f"new = {str(new)!r}\n"
                "text = p.read_text()\n"
                "if old not in text:\n    raise SystemExit('old_string not found')\n"
                "p.write_text(text.replace(old, new))\n"
                "PY"
            )
            return normalized_record(
                "Edit", "command_run",
                {"command": command, "working_directory": replay_root},
                fs_operation=True,
            )
        return normalized_record(
            "Edit", "file_replace_contents",
            {"entries": [{
                "path": path,
                "old_content": str(old),
                "new_content": str(new),
            }]},
            fs_operation=True,
        )

    if name == "Grep":
        pattern = args.get("pattern")
        path = args.get("path", replay_root)
        glob = args.get("glob")
        include = f" --include={json.dumps(str(glob))}" if glob else ""
        command = (
            f"grep -R -n{include} -- {json.dumps(str(pattern))} "
            f"{json.dumps(str(path))}"
        )
        return normalized_record(
            "Grep", "command_run",
            {"command": command, "working_directory": replay_root},
            fs_operation=True,
        )

    if name == "Glob":
        pattern = args.get("pattern")
        path = args.get("path", replay_root)
        command = (
            "python3 - <<'PY'\nfrom pathlib import Path\n"
            f"root = Path({str(path)!r})\npattern = {str(pattern)!r}\n"
            "for p in root.glob(pattern):\n    print(p)\nPY"
        )
        return normalized_record(
            "Glob", "command_run",
            {"command": command, "working_directory": replay_root},
            fs_operation=True,
        )

    reason = "control/non-filesystem tool" if name in CONTROL_TOOLS else "unsupported tool"
    return normalized_record(
        name, "skip", {}, fs_operation=False,
        skip_reason=reason,
    )


def extract_calls_from_atif(
    session: dict[str, Any], replay_root: str, extra_path_maps: dict[str, str]
) -> list[dict[str, Any]]:
    calls = []
    session_cwd = infer_original_cwd(session)
    for step in session.get("steps", []):
        if step.get("source") != "agent":
            continue
        step_cwd = step.get("extra", {}).get("cwd") or session_cwd
        for call_index, tool_call in enumerate(step.get("tool_calls") or []):
            function_name = tool_call.get("function_name") or tool_call.get("name")
            normalized = normalize_claude_tool_call(
                str(function_name), tool_call.get("arguments") or {},
                str(step_cwd) if step_cwd else None, replay_root, extra_path_maps,
            )
            normalized.update({
                "source_step_id": step.get("step_id"),
                "source_timestamp": step.get("timestamp"),
                "tool_call_id": tool_call.get("tool_call_id"),
                "call_index_in_step": call_index,
                "historical_cwd": step_cwd,
            })
            calls.append(normalized)
    return calls


async def create_fresh_sandbox(
    image: str, domain: str, protocol: str, timeout_seconds: int
) -> str:
    from opensandbox import Sandbox
    from opensandbox.config import ConnectionConfig
    from opensandbox.models.sandboxes import SandboxImageSpec

    config = ConnectionConfig(domain=domain, protocol=protocol).with_transport_if_missing()
    proxy_env = {
        "http_proxy": "http://p_atlas:proxy%40123@141.3.151.103:10002",
        "https_proxy": "http://p_atlas:proxy%40123@141.3.151.103:10002",
        "no_proxy": "127.0.0.1,.huawei.com,localhost,local,.local",
    }
    sandbox = await Sandbox.create(
        SandboxImageSpec(image=image),
        timeout=timedelta(seconds=timeout_seconds),
        connection_config=config,
        env=proxy_env,
    )
    sandbox_id = getattr(sandbox, "id", None) or getattr(sandbox, "sandbox_id", None)
    if not sandbox_id:
        raise RuntimeError("Sandbox.create succeeded but no sandbox id was found")
    try:
        await sandbox.close()
    except Exception:
        pass
    return str(sandbox_id)


async def kill_sandbox(sandbox_id: str, domain: str, protocol: str) -> None:
    from opensandbox import Sandbox
    from opensandbox.config import ConnectionConfig

    config = ConnectionConfig(domain=domain, protocol=protocol).with_transport_if_missing()
    sandbox = await Sandbox.connect(
        sandbox_id, connection_config=config, skip_health_check=True
    )
    try:
        await sandbox.kill()
    finally:
        try:
            await sandbox.close()
        except Exception:
            pass


class DirectSdkBackend:
    name = "direct_sdk"

    def __init__(self, sandbox_id, domain, protocol, skip_health_check):
        self.sandbox_id = sandbox_id
        self.domain = domain
        self.protocol = protocol
        self.skip_health_check = skip_health_check
        self.sandbox = None

    async def start(self):
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig

        config = ConnectionConfig(
            domain=self.domain, protocol=self.protocol
        ).with_transport_if_missing()
        self.sandbox = await Sandbox.connect(
            self.sandbox_id,
            connection_config=config,
            skip_health_check=self.skip_health_check,
        )

    async def execute(self, tool, arguments):
        from opensandbox.models.execd import RunCommandOpts
        from opensandbox.models.filesystem import ContentReplaceEntry

        if tool == "command_run":
            return await self.sandbox.commands.run(
                str(arguments["command"]),
                opts=RunCommandOpts(
                    background=False,
                    working_directory=arguments.get("working_directory"),
                ),
            )
        if tool == "file_read":
            return await self.sandbox.files.read_file(str(arguments["path"]))
        if tool == "file_write":
            return await self.sandbox.files.write_file(
                str(arguments["path"]), str(arguments.get("content", ""))
            )
        if tool == "file_replace_contents":
            entries = [ContentReplaceEntry(**entry) for entry in arguments["entries"]]
            return await self.sandbox.files.replace_contents_detailed(entries)
        raise ValueError(f"Unsupported Direct SDK tool: {tool}")

    async def close(self):
        if self.sandbox is not None:
            await self.sandbox.close()


class McpBackendBase:
    def __init__(self, sandbox_id, skip_health_check):
        self.sandbox_id = sandbox_id
        self.skip_health_check = skip_health_check
        self.session = None

    @staticmethod
    def check_result(tool, result):
        if getattr(result, "isError", False):
            raise RuntimeError(
                f"MCP tool {tool} failed: {getattr(result, 'content', result)}"
            )

    async def initialize_session(self):
        await self.session.initialize()
        await self.session.list_tools()
        result = await self.session.call_tool("sandbox_connect", arguments={
            "sandbox_id": self.sandbox_id,
            "skip_health_check": self.skip_health_check,
        })
        self.check_result("sandbox_connect", result)

    async def execute(self, tool, arguments):
        args = dict(arguments)
        args["sandbox_id"] = self.sandbox_id
        args["connect_if_missing"] = False
        result = await self.session.call_tool(tool, arguments=args)
        self.check_result(tool, result)
        structured = getattr(result, "structuredContent", None)
        return jsonable(
            structured if structured is not None else getattr(result, "content", None)
        )


class StdioMcpBackend(McpBackendBase):
    name = "stdio_mcp"

    def __init__(self, sandbox_id, domain, protocol, server_command, server_args, skip_health_check):
        super().__init__(sandbox_id, skip_health_check)
        self.domain = domain
        self.protocol = protocol
        self.server_command = server_command
        self.server_args = list(server_args)
        self.transport_cm = self.session_cm = None

    async def start(self):
        params = StdioServerParameters(
            command=self.server_command,
            args=self.server_args + ["--domain", self.domain, "--protocol", self.protocol],
            env=None,
        )
        self.transport_cm = stdio_client(params)
        read_stream, write_stream = await self.transport_cm.__aenter__()
        self.session_cm = ClientSession(read_stream, write_stream)
        self.session = await self.session_cm.__aenter__()
        await self.initialize_session()

    async def close(self):
        if self.session_cm is not None:
            await self.session_cm.__aexit__(None, None, None)
        if self.transport_cm is not None:
            await self.transport_cm.__aexit__(None, None, None)


class HttpMcpBackend(McpBackendBase):
    name = "http_mcp"

    def __init__(self, sandbox_id, mcp_url, skip_health_check):
        super().__init__(sandbox_id, skip_health_check)
        self.mcp_url = mcp_url
        self.http_client = self.transport_cm = self.session_cm = None

    async def start(self):
        self.http_client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(30, read=300)
        )
        self.transport_cm = streamable_http_client(
            self.mcp_url, http_client=self.http_client
        )
        read_stream, write_stream, _ = await self.transport_cm.__aenter__()
        self.session_cm = ClientSession(read_stream, write_stream)
        self.session = await self.session_cm.__aenter__()
        await self.initialize_session()

    async def close(self):
        if self.session_cm is not None:
            await self.session_cm.__aexit__(None, None, None)
        if self.transport_cm is not None:
            await self.transport_cm.__aexit__(None, None, None)
        if self.http_client is not None:
            await self.http_client.aclose()


def session_label(session, index):
    return str(session.get("session_id") or f"session-{index}")


def describe_call(call):
    logical_tool = call.get("logical_tool", "")
    args = call.get("arguments", {})
    if logical_tool == "Bash":
        text = str(args.get("command", "")).replace("\n", " ").strip()
        return text[:140] + ("..." if len(text) > 140 else "")
    if logical_tool in {"Read", "Write"}:
        return str(args.get("path") or args.get("file_path") or "")
    if logical_tool == "Edit":
        entries = args.get("entries") or []
        if entries and isinstance(entries, list):
            return str(entries[0].get("path", ""))
        return str(args.get("path") or args.get("file_path") or "")
    if logical_tool in {"Grep", "Glob"}:
        text = str(args.get("command") or args.get("pattern") or "")
        return text[:140] + ("..." if len(text) > 140 else "")
    return str(args)[:140]


async def replay_session(
    backend, session, session_index, replay_root, extra_path_maps, continue_on_error
):
    sid = session_label(session, session_index)
    calls = extract_calls_from_atif(session, replay_root, extra_path_maps)
    agent = session.get("agent", {})
    step_rows, latencies, fs_latencies = [], [], []
    failed = skipped = 0
    logical_distribution, executed_distribution = Counter(), Counter()
    wall0 = time.perf_counter_ns()
    total_calls = len(calls)

    for call_index, call in enumerate(calls):
        logical_tool, tool = call["logical_tool"], call["tool"]
        call_no = call_index + 1
        desc = describe_call(call)
        fs_operation = bool(call.get("fs_operation", False))
        print(
            f"[{backend.name}] call {call_no}/{total_calls} {logical_tool} {desc}",
            flush=True,
        )

        common = {
            "session_id": sid,
            "source_file": session.get("_source_file", ""),
            "backend": backend.name,
            "call_index": call_index,
            "source_step_id": call.get("source_step_id"),
            "tool_call_id": call.get("tool_call_id"),
            "logical_tool": logical_tool,
            "command": desc,
            "fs_operation": fs_operation,
            "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
            "historical_cwd": call.get("historical_cwd"),
            "replay_cwd": replay_root,
        }

        if tool == "skip":
            skipped += 1
            reason = call.get("skip_reason", "")
            print(
                f"[{backend.name}] call {call_no}/{total_calls} skipped reason={reason}",
                flush=True,
            )
            step_rows.append({
                **common,
                "executed_tool": "",
                "status": "skipped",
                "latency_ms": "",
                "output_bytes": "",
                "output_sha256": "",
                "error_type": "",
                "error": reason,
            })
            continue

        logical_distribution[logical_tool] += 1
        executed_distribution[tool] += 1
        t0 = time.perf_counter_ns()
        result = None
        error = None
        try:
            result = await backend.execute(tool, call["arguments"])
        except Exception as exc:
            error = exc
        latency_ms = (time.perf_counter_ns() - t0) / 1_000_000
        latencies.append(latency_ms)
        if fs_operation:
            fs_latencies.append(latency_ms)

        if error is None:
            size, sha = output_meta(result)
            status, etype, etext = "ok", "", ""
        else:
            size, sha = 0, ""
            status, etype, etext = "error", type(error).__name__, str(error)
            failed += 1

        print(
            f"[{backend.name}] call {call_no}/{total_calls} done "
            f"{latency_ms:.3f} ms status={status}",
            flush=True,
        )
        step_rows.append({
            **common,
            "executed_tool": tool,
            "status": status,
            "latency_ms": latency_ms,
            "output_bytes": size,
            "output_sha256": sha,
            "error_type": etype,
            "error": etext,
        })
        if error is not None and not continue_on_error:
            break

    wall_ms = (time.perf_counter_ns() - wall0) / 1_000_000
    total_ms = sum(latencies)
    trajectory_row = {
        "session_id": sid,
        "source_file": session.get("_source_file", ""),
        "backend": backend.name,
        "agent_name": agent.get("name"),
        "agent_version": agent.get("version"),
        "model_name": agent.get("model_name"),
        "status": "error" if failed else "ok",
        "normalized_calls": len(calls),
        "executed_calls": len(latencies),
        "skipped_calls": skipped,
        "failed_calls": failed,
        "total_tool_latency_ms": total_ms,
        "fs_calls": len(fs_latencies),
        "total_fs_latency_ms": sum(fs_latencies),
        "replay_wall_time_ms": wall_ms,
        "mean_call_ms": statistics.mean(latencies) if latencies else None,
        "p50_call_ms": percentile(latencies, 0.50),
        "p95_call_ms": percentile(latencies, 0.95),
        "logical_tool_distribution": json.dumps(
            dict(sorted(logical_distribution.items())), ensure_ascii=False
        ),
        "executed_tool_distribution": json.dumps(
            dict(sorted(executed_distribution.items())), ensure_ascii=False
        ),
    }
    return trajectory_row, step_rows


async def run_backend(backend, sessions, args, path_maps):
    trs, steps = [], []
    await backend.start()
    try:
        for i, session in enumerate(sessions):
            calls = extract_calls_from_atif(session, args.replay_root, path_maps)
            print(
                f"[{backend.name}] [{i + 1}/{len(sessions)}] "
                f"session={session_label(session, i)} calls={len(calls)}"
            )
            tr, sr = await replay_session(
                backend, session, i, args.replay_root, path_maps, args.continue_on_error
            )
            trs.append(tr)
            steps.extend(sr)
            print(
                f"  status={tr['status']} executed={tr['executed_calls']} "
                f"skipped={tr['skipped_calls']} failed={tr['failed_calls']} "
                f"total={tr['total_tool_latency_ms']:.3f} ms"
            )
            if tr["status"] == "error" and not args.continue_on_error:
                break
    finally:
        await backend.close()
    return trs, steps


def build_backend_summary(trs, steps):
    result = []
    for backend in sorted({r["backend"] for r in trs}):
        bt = [r for r in trs if r["backend"] == backend]
        ok_t = [r for r in bt if r["status"] == "ok"]
        tl = [float(r["total_tool_latency_ms"]) for r in ok_t]
        bs = [
            r for r in steps
            if r["backend"] == backend
            and r["status"] == "ok"
            and r["latency_ms"] != ""
        ]
        sl = [float(r["latency_ms"]) for r in bs]
        result.append({
            "backend": backend,
            "sessions": len(bt),
            "successful_sessions": len(ok_t),
            "failed_sessions": len(bt) - len(ok_t),
            "successful_calls": len(bs),
            "trajectory_mean_ms": statistics.mean(tl) if tl else None,
            "trajectory_p50_ms": percentile(tl, 0.50),
            "trajectory_p95_ms": percentile(tl, 0.95),
            "call_mean_ms": statistics.mean(sl) if sl else None,
            "call_p50_ms": percentile(sl, 0.50),
            "call_p95_ms": percentile(sl, 0.95),
            "aggregate_tool_time_ms": sum(sl),
        })
    return result


def build_tool_summary(steps):
    grouped = defaultdict(list)
    for row in steps:
        if row["status"] == "ok" and row["latency_ms"] != "":
            grouped[(row["backend"], row["logical_tool"])].append(
                float(row["latency_ms"])
            )
    result = []
    for (backend, logical_tool), values in sorted(grouped.items()):
        result.append({
            "backend": backend,
            "logical_tool": logical_tool,
            "samples": len(values),
            **summarize(values),
        })
    return result


def build_fs_summary(steps):
    grouped = defaultdict(list)
    for row in steps:
        if (
            row["status"] == "ok"
            and row["latency_ms"] != ""
            and row.get("fs_operation") is True
        ):
            grouped[row["backend"]].append(float(row["latency_ms"]))
    result = []
    for backend, values in sorted(grouped.items()):
        result.append({
            "backend": backend,
            "samples": len(values),
            **summarize(values),
        })
    return result


def parse_path_maps(values):
    result = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"--path-map must be OLD=NEW: {item}")
        old, new = item.split("=", 1)
        result[old] = new
    return result


def make_backend(name, sandbox_id, args):
    if name == "direct-sdk":
        return DirectSdkBackend(
            sandbox_id, args.opensandbox_domain, args.opensandbox_protocol,
            args.skip_health_check,
        )
    if name == "stdio":
        return StdioMcpBackend(
            sandbox_id, args.opensandbox_domain, args.opensandbox_protocol,
            args.server_command, args.server_arg, args.skip_health_check,
        )
    return HttpMcpBackend(sandbox_id, args.mcp_url, args.skip_health_check)


async def resolve_runs(args):
    requested = ["direct-sdk", "stdio", "http"] if args.backend == "all" else [args.backend]
    created = []
    if args.auto_create:
        if not args.image:
            raise ValueError("--auto-create requires --image")
        runs = []
        for name in requested:
            print(f"[create] backend={name} image={args.image}")
            sid = await create_fresh_sandbox(
                args.image, args.opensandbox_domain, args.opensandbox_protocol,
                args.sandbox_timeout,
            )
            print(f"[create] backend={name} sandbox_id={sid}")
            runs.append((name, sid))
            created.append(sid)
        return runs, created
    if args.backend != "all":
        if not args.sandbox_id:
            raise ValueError("single backend requires --sandbox-id unless --auto-create is used")
        return [(args.backend, args.sandbox_id)], created
    runs = [
        ("direct-sdk", args.direct_sandbox_id),
        ("stdio", args.stdio_sandbox_id),
        ("http", args.http_sandbox_id),
    ]
    if any(not sid for _, sid in runs):
        raise ValueError("--backend all needs three sandbox IDs unless --auto-create is used")
    if len({sid for _, sid in runs}) != 3:
        raise ValueError("stateful three-path replay requires 3 different sandbox IDs")
    return [(name, str(sid)) for name, sid in runs], created


async def async_main(args):
    sessions = load_atif_files(args.atif)
    if args.limit is not None:
        sessions = sessions[:args.limit]
    if not sessions:
        raise ValueError("No ATIF sessions selected")
    extra_path_maps = parse_path_maps(args.path_map)
    args.output.mkdir(parents=True, exist_ok=True)
    runs, created_ids = await resolve_runs(args)
    all_trs, all_steps = [], []
    try:
        for backend_name, sandbox_id in runs:
            print("\n" + "=" * 96)
            print(
                f"BACKEND      : {backend_name}\n"
                f"SANDBOX ID   : {sandbox_id}\n"
                f"REPLAY ROOT  : {args.replay_root}\n"
                f"ATIF SESSIONS: {len(sessions)}"
            )
            print("=" * 96)
            backend = make_backend(backend_name, sandbox_id, args)
            trs, steps = await run_backend(
                backend, sessions, args, extra_path_maps
            )
            all_trs.extend(trs)
            all_steps.extend(steps)

        write_csv(args.output / "trajectory_results.csv", all_trs)
        write_csv(args.output / "step_results.csv", all_steps)
        write_csv(
            args.output / "backend_summary.csv",
            build_backend_summary(all_trs, all_steps),
        )
        write_csv(args.output / "tool_summary.csv", build_tool_summary(all_steps))
        write_csv(args.output / "fs_summary.csv", build_fs_summary(all_steps))

        metadata = {
            "atif_files": [str(p) for p in args.atif],
            "session_count": len(sessions),
            "backend": args.backend,
            "auto_create": args.auto_create,
            "image": args.image,
            "sandbox_ids": {name: sid for name, sid in runs},
            "replay_root": args.replay_root,
            "opensandbox_domain": args.opensandbox_domain,
            "opensandbox_protocol": args.opensandbox_protocol,
            "mcp_url": args.mcp_url,
            "path_map": args.path_map,
            "created_sandboxes": created_ids,
            "cleanup_created": args.cleanup_created,
            "timing_boundary": (
                "Per-call timer wraps only Direct SDK or MCP tool invocation; "
                "sandbox creation/connect and MCP initialization are excluded."
            ),
            "timestamp_ns": time.time_ns(),
        }
        (args.output / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nSaved results to: {args.output}")
    finally:
        if args.cleanup_created:
            for sid in created_ids:
                try:
                    print(f"[cleanup] killing sandbox {sid}")
                    await kill_sandbox(
                        sid, args.opensandbox_domain, args.opensandbox_protocol
                    )
                except Exception as exc:
                    print(f"[cleanup] failed sandbox={sid}: {exc}")


def main():
    p = argparse.ArgumentParser(
        description=(
            "Replay DeepSWE/Claude Code ATIF-v1.7 over Direct SDK, stdio MCP, "
            "HTTP MCP"
        )
    )
    p.add_argument("--atif", type=Path, action="append", required=True)
    p.add_argument("--backend", choices=("direct-sdk", "stdio", "http", "all"), default="all")
    p.add_argument("--sandbox-id")
    p.add_argument("--direct-sandbox-id")
    p.add_argument("--stdio-sandbox-id")
    p.add_argument("--http-sandbox-id")
    p.add_argument("--auto-create", action="store_true")
    p.add_argument("--image")
    p.add_argument("--sandbox-timeout", type=int, default=86400)
    p.add_argument("--cleanup-created", action="store_true")
    p.add_argument("--replay-root", default="/app")
    p.add_argument("--path-map", action="append", default=[])
    p.add_argument("--opensandbox-domain", default="127.0.0.1:8081")
    p.add_argument("--opensandbox-protocol", choices=("http", "https"), default="http")
    p.add_argument("--mcp-url", default="http://127.0.0.1:8999/mcp")
    p.add_argument("--server-command", default="opensandbox-mcp")
    p.add_argument("--server-arg", action="append", default=[])
    p.add_argument("--limit", type=int)
    p.add_argument("--continue-on-error", action="store_true")
    p.add_argument("--skip-health-check", action="store_true")
    p.add_argument(
        "--output", type=Path,
        default=Path("trajectory_bench/results/atif_replay"),
    )
    asyncio.run(async_main(p.parse_args()))


if __name__ == "__main__":
    main()
