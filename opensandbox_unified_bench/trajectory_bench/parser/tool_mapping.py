"""Map OpenHands/SWE-Gym tool calls to backend-neutral sandbox actions."""

from __future__ import annotations

import re
from typing import Any


class UnsupportedToolError(ValueError):
    """Raised when a source tool cannot be replayed by a sandbox backend."""


_DIRECTORY_OBSERVATION_MARKERS = (
    "files and directories",
    "directories up to",
    "is a directory",
)


def _view_is_directory(observation: str | None) -> bool:
    lowered = (observation or "").lower()
    return any(marker in lowered for marker in _DIRECTORY_OBSERVATION_MARKERS)


def _directory_depth(observation: str | None) -> int:
    match = re.search(r"up to\s+(\d+)\s+levels?\s+deep", observation or "", re.I)
    return int(match.group(1)) if match else 2


def _view_arguments(arguments: dict[str, Any], observation: str | None) -> dict[str, Any]:
    path = _required_string(arguments, "path")
    if _view_is_directory(observation):
        return {"tool": "list_directory", "arguments": {"path": path, "depth": _directory_depth(observation)}}

    normalized: dict[str, Any] = {"path": path}
    view_range = arguments.get("view_range")
    if isinstance(view_range, list) and view_range:
        start = int(view_range[0])
        normalized["offset"] = max(start, 1)
        if len(view_range) > 1 and int(view_range[1]) >= start:
            normalized["limit"] = int(view_range[1]) - start + 1
    return {"tool": "file_read", "arguments": normalized}


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"tool argument {name!r} must be a non-empty string")
    return value


def map_tool_call(
    name: str,
    arguments: dict[str, Any],
    observation: str | None = None,
) -> dict[str, Any] | None:
    """Return a normalized action, or ``None`` for non-sandbox control tools."""

    if name == "finish":
        return None
    if name == "execute_bash":
        normalized = {"command": _required_string(arguments, "command")}
        working_directory = arguments.get("working_directory")
        if isinstance(working_directory, str) and working_directory:
            normalized["working_directory"] = working_directory
        return {"tool": "command_run", "arguments": normalized}
    if name != "str_replace_editor":
        raise UnsupportedToolError(f"unsupported SWE-Gym tool: {name}")

    command = _required_string(arguments, "command")
    path = _required_string(arguments, "path")
    if command == "view":
        return _view_arguments(arguments, observation)
    if command == "create":
        return {
            "tool": "file_write",
            "arguments": {"path": path, "content": str(arguments.get("file_text", ""))},
        }
    if command == "str_replace":
        return {
            "tool": "file_replace_contents",
            "arguments": {
                "entries": [
                    {
                        "path": path,
                        "old_content": _required_string(arguments, "old_str"),
                        "new_content": str(arguments.get("new_str", "")),
                    }
                ]
            },
        }
    if command == "insert":
        return {
            "tool": "file_insert",
            "arguments": {
                "path": path,
                "line": int(arguments.get("insert_line", 0)),
                "content": str(arguments.get("new_str", "")),
            },
        }
    raise UnsupportedToolError(f"unsupported str_replace_editor command: {command}")
