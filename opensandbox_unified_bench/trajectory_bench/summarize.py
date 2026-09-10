"""Combine trajectory replay result files into one comparison table."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


FIELDS = (
    "task_id",
    "backend",
    "status",
    "tool_calls",
    "total_trajectory_latency_ms",
    "mean_tool_latency_ms",
    "p50_tool_latency_ms",
    "p95_tool_latency_ms",
    "tool_calls_per_second",
    "trajectories_per_hour",
)


def result_row(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result["metrics"]
    latency = metrics["latency"]
    return {
        "task_id": result.get("task_id"),
        "backend": result["backend"],
        "status": result["status"],
        "tool_calls": metrics["tool_calls"],
        "total_trajectory_latency_ms": metrics["total_trajectory_latency_ms"],
        "mean_tool_latency_ms": latency["mean_ms"],
        "p50_tool_latency_ms": latency["p50_ms"],
        "p95_tool_latency_ms": latency["p95_ms"],
        "tool_calls_per_second": metrics["tool_calls_per_second"],
        "trajectories_per_hour": metrics["trajectories_per_hour"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = [
        result_row(json.loads(path.read_text(encoding="utf-8-sig")))
        for path in args.results
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
