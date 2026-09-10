"""Command-line runner for one normalized trajectory and one backend."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from trajectory_bench.replay.backend_direct_sdk import DirectSdkBackend
from trajectory_bench.replay.backend_http_mcp import HttpMcpBackend
from trajectory_bench.replay.backend_stdio import StdioMcpBackend
from trajectory_bench.replay.replay_engine import ReplayEngine


def _path_maps(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"path map must be SOURCE=TARGET: {value!r}")
        source, target = value.split("=", 1)
        result[source] = target
    return result


def _backend(args: argparse.Namespace) -> Any:
    common = {"sandbox_id": args.sandbox_id, "skip_health_check": args.skip_health_check}
    if args.backend == "direct-sdk":
        return DirectSdkBackend(**common)
    if args.backend == "stdio":
        return StdioMcpBackend(
            **common, server_command=args.server_command, server_args=args.server_arg
        )
    return HttpMcpBackend(**common, url=args.mcp_url)


async def _run(args: argparse.Namespace) -> None:
    normalized = json.loads(args.trajectory.read_text(encoding="utf-8-sig"))
    backend = _backend(args)
    try:
        await backend.start()
        result = await ReplayEngine(
            backend,
            continue_on_error=args.continue_on_error,
            path_replacements=_path_maps(args.path_map),
        ).run(normalized)
    finally:
        await backend.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trajectory", type=Path)
    parser.add_argument("--backend", choices=("direct-sdk", "stdio", "http"), required=True)
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--server-command", default="opensandbox-mcp")
    parser.add_argument("--server-arg", action="append", default=[])
    parser.add_argument("--path-map", action="append", default=[], metavar="SOURCE=TARGET")
    parser.add_argument("--skip-health-check", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
