# Copyright 2026 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def _render(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        rendered = value
        for name, replacement in variables.items():
            rendered = rendered.replace(f"{{{name}}}", str(replacement))
        return rendered
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, variables) for key, item in value.items()}
    return value


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _server_events(result: Any) -> list[dict[str, Any]]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        candidate = structured.get("result", structured.get("events", structured))
        if isinstance(candidate, list):
            return [event for event in candidate if isinstance(event, dict)]

    for content in getattr(result, "content", []):
        text = getattr(content, "text", None)
        if not isinstance(text, str):
            continue
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, list):
            return [event for event in candidate if isinstance(event, dict)]
        if isinstance(candidate, dict) and isinstance(candidate.get("result"), list):
            return candidate["result"]
    raise RuntimeError("benchmark_drain_events returned no structured event list")


class BenchmarkClientSession(ClientSession):
    """ClientSession that timestamps the SDK-assigned JSON-RPC request id."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client_events: list[dict[str, Any]] = []
        self.invocation_meta: dict[str, dict[str, Any]] = {}
        self._armed_measurement: dict[str, Any] | None = None

    def arm_measurement(self, meta: dict[str, Any]) -> str:
        if self._armed_measurement is not None:
            raise RuntimeError("A benchmark measurement is already armed")
        trace_id = str(self._request_id)
        self._armed_measurement = {**meta, "trace_id": trace_id}
        self.invocation_meta[trace_id] = meta
        return trace_id

    async def send_request(
        self,
        request: Any,
        result_type: Any,
        request_read_timeout_seconds: Any = None,
        metadata: Any = None,
        progress_callback: Any = None,
    ) -> Any:
        measurement = self._armed_measurement
        if measurement is None:
            return await super().send_request(
                request,
                result_type,
                request_read_timeout_seconds,
                metadata,
                progress_callback,
            )

        self._armed_measurement = None
        trace_id = measurement["trace_id"]
        tool = measurement["tool"]
        self.client_events.append(
            {
                "trace_id": trace_id,
                "tool": tool,
                "event": "C0",
                "wall_time_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "process": "client",
                "status": "ok",
                "error_type": None,
            }
        )
        error: BaseException | None = None
        try:
            return await super().send_request(
                request,
                result_type,
                request_read_timeout_seconds,
                metadata,
                progress_callback,
            )
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.client_events.append(
                {
                    "trace_id": trace_id,
                    "tool": tool,
                    "event": "C1",
                    "wall_time_ns": time.time_ns(),
                    "monotonic_ns": time.monotonic_ns(),
                    "process": "client",
                    "status": "error" if error else "ok",
                    "error_type": type(error).__name__ if error else None,
                }
            )


async def _wait_for_server(url: str, process: asyncio.subprocess.Process) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"MCP server exited with code {process.returncode}")
        try:
            _, writer = await asyncio.open_connection(host, port)
        except OSError:
            await asyncio.sleep(0.1)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError(f"MCP server did not listen at {url} within 30 seconds")


def _server_args(args: argparse.Namespace) -> list[str]:
    server_args = list(args.server_arg)
    if args.opensandbox_domain:
        server_args.extend(("--domain", args.opensandbox_domain))
    server_args.extend(("--protocol", args.opensandbox_protocol))
    if args.transport == "streamable-http":
        server_args.extend(("--transport", "streamable-http"))
    return server_args


@asynccontextmanager
async def _open_session(args: argparse.Namespace):
    server_args = _server_args(args)
    if args.transport == "stdio":
        server = StdioServerParameters(
            command=args.server_command,
            args=server_args,
            env=None,
        )
        async with stdio_client(server) as (read_stream, write_stream):
            async with BenchmarkClientSession(read_stream, write_stream) as session:
                yield session
        return

    process: asyncio.subprocess.Process | None = None
    try:
        if not args.no_start_server:
            process = await asyncio.create_subprocess_exec(
                args.server_command,
                *server_args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await _wait_for_server(args.mcp_url, process)
        async with streamable_http_client(args.mcp_url) as transport:
            read_stream, write_stream, _ = transport
            async with BenchmarkClientSession(read_stream, write_stream) as session:
                yield session
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


async def _call(session: ClientSession, tool: str, arguments: dict[str, Any]) -> Any:
    result = await session.call_tool(tool, arguments=arguments)
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool {tool} failed: {result.content}")
    return result


async def _run_action(
    session: ClientSession,
    action: Mapping[str, Any],
    variables: Mapping[str, Any],
) -> None:
    tool = str(action["tool"])
    await _call(
        session,
        tool,
        _tool_arguments(
            tool,
            _render(dict(action.get("arguments", {})), variables),
            str(variables["sandbox_id"]),
        ),
    )


def _tool_arguments(
    tool: str,
    arguments: dict[str, Any],
    sandbox_id: str,
) -> dict[str, Any]:
    if tool.startswith("file_") or tool.startswith("command_"):
        arguments["sandbox_id"] = sandbox_id
        arguments["connect_if_missing"] = False
    return arguments


async def run_benchmark(args: argparse.Namespace) -> None:
    config = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = config["cases"] if isinstance(config, dict) else config
    if args.case:
        selected = set(args.case)
        cases = [case for case in cases if str(case.get("name")) in selected]
    if not cases:
        raise ValueError("Benchmark configuration contains no selected cases")
    if args.iterations < 1 or args.warmup < 0:
        raise ValueError("iterations must be positive and warmup must be non-negative")
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    measured_trace_ids: list[str] = []

    async with _open_session(args) as session:
        await session.initialize()
        await session.list_tools()
        await _call(
            session,
            "sandbox_connect",
            {
                "sandbox_id": args.sandbox_id,
                "skip_health_check": args.skip_health_check,
            },
        )
        await _call(session, "benchmark_start", {})

        for case in cases:
            case_name = str(case["name"])
            warmup = int(case.get("warmup", args.warmup))
            iterations = int(case.get("iterations", args.iterations))
            if iterations < 1 or warmup < 0:
                raise ValueError(
                    f"Case {case_name!r} has invalid warmup or iterations"
                )
            for sequence in range(warmup + iterations):
                measured = sequence >= warmup
                iteration = sequence - warmup if measured else sequence
                variables = {
                    "sandbox_id": args.sandbox_id,
                    "case": case_name,
                    "iteration": iteration,
                    "invocation": f"{case_name}-{sequence}",
                }

                for action in case.get("setup", []):
                    await _run_action(session, action, variables)

                call = case["call"]
                tool = str(call["tool"])
                if measured:
                    trace_id = session.arm_measurement(
                        {
                            "case": case_name,
                            "iteration": iteration,
                            "tool": tool,
                        }
                    )
                    measured_trace_ids.append(trace_id)
                    variables["trace_id"] = trace_id
                else:
                    variables["trace_id"] = f"warmup-{case_name}-{sequence}"
                call_args = _tool_arguments(
                    tool,
                    _render(dict(call.get("arguments", {})), variables),
                    args.sandbox_id,
                )

                error: BaseException | None = None
                try:
                    await _call(session, tool, call_args)
                except BaseException as exc:
                    error = exc

                for action in case.get("teardown", []):
                    await _run_action(session, action, variables)
                if error is not None:
                    raise error

        drain_result = await _call(
            session,
            "benchmark_drain_events",
            {"trace_ids": measured_trace_ids},
        )
        server_events = _server_events(drain_result)
        client_events = session.client_events
        invocation_meta = session.invocation_meta

    for event in server_events:
        event["process"] = "mcp_server"
    all_events = client_events + server_events
    all_events.sort(key=lambda event: (event["wall_time_ns"], event["event"]))

    raw_path = output_dir / "events.jsonl"
    with raw_path.open("w", encoding="utf-8", newline="\n") as output:
        for event in all_events:
            meta = invocation_meta.get(event["trace_id"], {})
            output.write(json.dumps({**meta, **event}, ensure_ascii=False) + "\n")

    by_trace: dict[str, dict[str, dict[str, Any]]] = {}
    for event in all_events:
        by_trace.setdefault(event["trace_id"], {})[event["event"]] = event

    measurements: list[dict[str, Any]] = []
    for trace_id in measured_trace_ids:
        timeline = by_trace.get(trace_id, {})
        missing = [
            marker
            for marker in ("C0", "M0", "B0", "B1", "M4", "C1")
            if marker not in timeline
        ]
        if missing:
            raise RuntimeError(f"Trace {trace_id} is missing events: {', '.join(missing)}")
        wall = {marker: event["wall_time_ns"] for marker, event in timeline.items()}
        stage1 = wall["M0"] - wall["C0"]
        stage2 = wall["B0"] - wall["M0"]
        stage3 = wall["B1"] - wall["B0"]
        stage4 = wall["C1"] - wall["B1"]
        e2e = wall["C1"] - wall["C0"]
        result_build = wall["M4"] - wall["B1"]
        response_return = wall["C1"] - wall["M4"]
        client_e2e_mono = (
            timeline["C1"]["monotonic_ns"] - timeline["C0"]["monotonic_ns"]
        )
        server_stage2_mono = (
            timeline["B0"]["monotonic_ns"] - timeline["M0"]["monotonic_ns"]
        )
        server_stage3_mono = (
            timeline["B1"]["monotonic_ns"] - timeline["B0"]["monotonic_ns"]
        )
        server_result_build_mono = (
            timeline["M4"]["monotonic_ns"] - timeline["B1"]["monotonic_ns"]
        )
        meta = invocation_meta[trace_id]
        measurements.append(
            {
                **meta,
                "trace_id": trace_id,
                "stage1_ms": stage1 / 1_000_000,
                "stage2_ms": stage2 / 1_000_000,
                "stage3_ms": stage3 / 1_000_000,
                "stage4_ms": stage4 / 1_000_000,
                "e2e_ms": e2e / 1_000_000,
                "result_build_ms": result_build / 1_000_000,
                "response_return_ms": response_return / 1_000_000,
                "client_e2e_monotonic_ms": client_e2e_mono / 1_000_000,
                "server_stage2_monotonic_ms": server_stage2_mono / 1_000_000,
                "server_stage3_monotonic_ms": server_stage3_mono / 1_000_000,
                "server_result_build_monotonic_ms": (
                    server_result_build_mono / 1_000_000
                ),
                "stage_sum_error_ns": stage1 + stage2 + stage3 + stage4 - e2e,
                "timeline_order_valid": (
                    wall["C0"]
                    <= wall["M0"]
                    <= wall["B0"]
                    <= wall["B1"]
                    <= wall["M4"]
                    <= wall["C1"]
                ),
            }
        )

    measurement_path = output_dir / "measurements.csv"
    with measurement_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(measurements[0]))
        writer.writeheader()
        writer.writerows(measurements)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in measurements:
        grouped.setdefault((row["case"], row["tool"]), []).append(row)
    metric_names = (
        "stage1_ms",
        "stage2_ms",
        "stage3_ms",
        "stage4_ms",
        "e2e_ms",
        "result_build_ms",
        "response_return_ms",
    )
    summaries: list[dict[str, Any]] = []
    for (case_name, tool), rows in grouped.items():
        summary: dict[str, Any] = {
            "case": case_name,
            "tool": tool,
            "samples": len(rows),
        }
        for metric in metric_names:
            values = [float(row[metric]) for row in rows]
            summary[f"{metric}_p50"] = statistics.median(values)
            summary[f"{metric}_p95"] = _percentile(values, 0.95)
        summaries.append(summary)

    summary_path = output_dir / "summary.csv"
    with summary_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)

    print(f"Wrote {len(measurements)} measurements to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark OpenSandbox MCP file tools by common timeline stages."
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--no-start-server", action="store_true")
    parser.add_argument("--server-command", default="opensandbox-mcp")
    parser.add_argument("--server-arg", action="append", default=[])
    parser.add_argument("--opensandbox-domain", default=None)
    parser.add_argument(
        "--opensandbox-protocol",
        choices=("http", "https"),
        default="http",
    )
    parser.add_argument("--skip-health-check", action="store_true")
    asyncio.run(run_benchmark(parser.parse_args()))


if __name__ == "__main__":
    main()
