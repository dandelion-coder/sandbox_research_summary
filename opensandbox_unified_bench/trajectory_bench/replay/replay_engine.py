"""Execute a normalized trajectory and calculate comparable latency metrics."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from typing import Any

from trajectory_bench.replay.backend import Backend


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(values: list[float]) -> dict[str, float | None]:
    return {
        "mean_ms": sum(values) / len(values) if values else None,
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
    }


def _rewrite(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    if isinstance(value, list):
        return [_rewrite(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, replacements) for key, item in value.items()}
    return value


class ReplayEngine:
    def __init__(
        self,
        backend: Backend,
        *,
        continue_on_error: bool = False,
        path_replacements: dict[str, str] | None = None,
    ) -> None:
        self.backend = backend
        self.continue_on_error = continue_on_error
        self.path_replacements = dict(path_replacements or {})

    async def run(self, normalized: dict[str, Any]) -> dict[str, Any]:
        steps = normalized.get("trajectory")
        if not isinstance(steps, list):
            raise ValueError("normalized trajectory must contain a trajectory list")

        records: list[dict[str, Any]] = []
        latencies: list[float] = []
        tool_latencies: dict[str, list[float]] = defaultdict(list)
        distribution: Counter[str] = Counter()
        total_started = time.perf_counter_ns()

        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("action"), dict):
                raise ValueError("each trajectory step must contain an action object")
            action = _rewrite(step["action"], self.path_replacements)
            tool = str(action["tool"])
            distribution[tool] += 1
            started = time.perf_counter_ns()
            error: Exception | None = None
            output: Any = None
            try:
                output = await self.backend.execute(action)
            except Exception as exc:
                error = exc
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            latencies.append(elapsed_ms)
            tool_latencies[tool].append(elapsed_ms)
            encoded = json.dumps(output, ensure_ascii=False, sort_keys=True, default=str).encode()
            records.append(
                {
                    "step": step.get("step", len(records)),
                    "tool": tool,
                    "latency_ms": elapsed_ms,
                    "status": "error" if error else "ok",
                    "error_type": type(error).__name__ if error else None,
                    "error": str(error) if error else None,
                    "output_bytes": len(encoded) if error is None else 0,
                    "output_sha256": hashlib.sha256(encoded).hexdigest() if error is None else None,
                }
            )
            if error is not None and not self.continue_on_error:
                break

        replay_wall_ms = (time.perf_counter_ns() - total_started) / 1_000_000
        total_ms = sum(latencies)
        failures = sum(record["status"] == "error" for record in records)
        return {
            "schema_version": "1.0",
            "task_id": normalized.get("task_id"),
            "backend": self.backend.name,
            "status": "error" if failures else "ok",
            "metrics": {
                "total_trajectory_latency_ms": total_ms,
                "replay_wall_time_ms": replay_wall_ms,
                "tool_calls": len(records),
                "successful_tool_calls": len(records) - failures,
                "failed_tool_calls": failures,
                "tool_calls_per_second": len(records) / (total_ms / 1000) if total_ms else None,
                "trajectories_per_hour": 3_600_000 / total_ms if total_ms else None,
                "tool_distribution": dict(sorted(distribution.items())),
                "latency": _summary(latencies),
                "latency_by_tool": {
                    tool: {"count": len(values), **_summary(values)}
                    for tool, values in sorted(tool_latencies.items())
                },
            },
            "steps": records,
        }
