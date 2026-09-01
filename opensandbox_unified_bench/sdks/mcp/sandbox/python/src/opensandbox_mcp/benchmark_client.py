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
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
    await _call(
        session,
        str(action["tool"]),
        _render(dict(action.get("arguments", {})), variables),
    )


async def run_benchmark(args: argparse.Namespace) -> None:
    config = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = config["cases"] if isinstance(config, dict) else config
    if not cases:
        raise ValueError("Benchmark configuration must contain at least one case")
    if args.iterations < 1 or args.warmup < 0:
        raise ValueError("iterations must be positive and warmup must be non-negative")
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    server = StdioServerParameters(
        command=args.server_command,
        args=args.server_arg,
        env=None,
    )
    client_events: list[dict[str, Any]] = []
    invocation_meta: dict[str, dict[str, Any]] = {}
    measured_trace_ids: list[str] = []

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

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
                    trace_id = uuid.uuid4().hex
                    variables = {
                        "sandbox_id": args.sandbox_id,
                        "case": case_name,
                        "iteration": iteration,
                        "trace_id": trace_id,
                    }

                    for action in case.get("setup", []):
                        await _run_action(session, action, variables)

                    call = case["call"]
                    call_args = _render(dict(call.get("arguments", {})), variables)
                    if measured:
                        call_args["benchmark_trace_id"] = trace_id
                        invocation_meta[trace_id] = {
                            "case": case_name,
                            "iteration": iteration,
                            "tool": str(call["tool"]),
                        }
                        measured_trace_ids.append(trace_id)
                        client_events.append(
                            {
                                "trace_id": trace_id,
                                "tool": str(call["tool"]),
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
                        await _call(session, str(call["tool"]), call_args)
                    except BaseException as exc:
                        error = exc
                    finally:
                        if measured:
                            client_events.append(
                                {
                                    "trace_id": trace_id,
                                    "tool": str(call["tool"]),
                                    "event": "C1",
                                    "wall_time_ns": time.time_ns(),
                                    "monotonic_ns": time.monotonic_ns(),
                                    "process": "client",
                                    "status": "error" if error else "ok",
                                    "error_type": type(error).__name__ if error else None,
                                }
                            )

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
        total = timeline["C1"]["monotonic_ns"] - timeline["C0"]["monotonic_ns"]
        server_total = timeline["M4"]["monotonic_ns"] - timeline["M0"]["monotonic_ns"]
        pre = timeline["B0"]["monotonic_ns"] - timeline["M0"]["monotonic_ns"]
        sdk = timeline["B1"]["monotonic_ns"] - timeline["B0"]["monotonic_ns"]
        post = timeline["M4"]["monotonic_ns"] - timeline["B1"]["monotonic_ns"]
        meta = invocation_meta[trace_id]
        measurements.append(
            {
                **meta,
                "trace_id": trace_id,
                "total_ms": total / 1_000_000,
                "server_ms": server_total / 1_000_000,
                "pre_sdk_ms": pre / 1_000_000,
                "sdk_ms": sdk / 1_000_000,
                "post_sdk_ms": post / 1_000_000,
                "mcp_transport_ms": (total - server_total) / 1_000_000,
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
        "total_ms",
        "server_ms",
        "pre_sdk_ms",
        "sdk_ms",
        "post_sdk_ms",
        "mcp_transport_ms",
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
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--server-command", default="opensandbox-mcp")
    parser.add_argument("--server-arg", action="append", default=[])
    asyncio.run(run_benchmark(parser.parse_args()))


if __name__ == "__main__":
    main()
