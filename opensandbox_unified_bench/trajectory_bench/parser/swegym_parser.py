"""Parse SWE-Gym OpenHands trajectories into the normalized replay schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trajectory_bench.parser.tool_mapping import UnsupportedToolError, map_tool_call


def _decode_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        raise ValueError("tool arguments must be a JSON object or JSON string")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("decoded tool arguments must be an object")
    return decoded


def _observations(messages: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        if isinstance(call_id, str):
            result[call_id] = str(message.get("content") or "")
    return result


def parse_record(
    record: dict[str, Any],
    *,
    skip_unsupported: bool = False,
    source_dataset: str = "SWE-Gym/OpenHands-Sampled-Trajectories",
) -> dict[str, Any]:
    """Normalize one Hugging Face dataset row or equivalent record."""

    messages = record.get("messages")
    if not isinstance(messages, list):
        raise ValueError("record.messages must be a list")
    typed_messages = [message for message in messages if isinstance(message, dict)]
    observations = _observations(typed_messages)
    trajectory: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for message_index, message in enumerate(typed_messages):
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            call_id = str(call.get("id") or "")
            source_arguments = _decode_arguments(function.get("arguments", {}))
            observation = observations.get(call_id, "")
            try:
                action = map_tool_call(name, source_arguments, observation)
            except UnsupportedToolError as exc:
                if not skip_unsupported:
                    raise
                skipped.append({"tool_call_id": call_id, "source_tool": name, "reason": str(exc)})
                continue
            if action is None:
                skipped.append({"tool_call_id": call_id, "source_tool": name, "reason": "control tool"})
                continue
            trajectory.append(
                {
                    "step": len(trajectory),
                    "action": action,
                    "observation": observation,
                    "source": {
                        "message_index": message_index,
                        "tool_call_id": call_id,
                        "tool": name,
                        "arguments": source_arguments,
                    },
                }
            )

    task_id = record.get("instance_id", record.get("task_id"))
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("record must contain instance_id or task_id")
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "metadata": {
            "source_dataset": source_dataset,
            "run_id": record.get("run_id"),
            "resolved": record.get("resolved"),
            "source_message_count": len(typed_messages),
            "skipped_actions": skipped,
        },
        "trajectory": trajectory,
    }


def load_record(path: Path, row_index: int = 0) -> dict[str, Any]:
    """Load a row from a plain record, Dataset Server response, array, or JSONL."""

    text = path.read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        payload = rows

    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        payload = payload["rows"]
    if isinstance(payload, list):
        payload = payload[row_index]
    if isinstance(payload, dict) and isinstance(payload.get("row"), dict):
        payload = payload["row"]
    if not isinstance(payload, dict):
        raise ValueError("input did not resolve to a trajectory record")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--row-index", type=int, default=0)
    parser.add_argument("--skip-unsupported", action="store_true")
    args = parser.parse_args()
    normalized = parse_record(
        load_record(args.input, args.row_index),
        skip_unsupported=args.skip_unsupported,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
