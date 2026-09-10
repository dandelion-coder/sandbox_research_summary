"""Shared MCP action dispatch for stdio and Streamable HTTP transports."""

from __future__ import annotations

import json
from typing import Any

from trajectory_bench.replay.backend import (
    insert_command,
    jsonable,
    list_directory_command,
    read_range_command,
)


class McpBackendBase:
    name = "mcp"

    def __init__(self, sandbox_id: str, *, skip_health_check: bool = False) -> None:
        self.sandbox_id = sandbox_id
        self.skip_health_check = skip_health_check
        self._session: Any = None

    async def _initialize_session(self) -> None:
        if self._session is None:
            raise RuntimeError("MCP session was not created")
        await self._session.initialize()
        result = await self._session.call_tool(
            "sandbox_connect",
            arguments={
                "sandbox_id": self.sandbox_id,
                "skip_health_check": self.skip_health_check,
            },
        )
        self._raise_for_tool_error("sandbox_connect", result)

    @staticmethod
    def _raise_for_tool_error(tool: str, result: Any) -> None:
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {tool} failed: {getattr(result, 'content', result)}")

    def _mcp_call(self, action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        tool = str(action["tool"])
        arguments = dict(action.get("arguments", {}))
        if tool == "file_read" and ("offset" in arguments or "limit" in arguments):
            tool, arguments = "command_run", {
                "command": read_range_command(
                    str(arguments["path"]),
                    int(arguments.get("offset", 1)),
                    arguments.get("limit"),
                )
            }
        elif tool == "list_directory":
            tool, arguments = "command_run", {
                "command": list_directory_command(
                    str(arguments["path"]), int(arguments.get("depth", 2))
                )
            }
        elif tool == "file_insert":
            tool, arguments = "command_run", {
                "command": insert_command(
                    str(arguments["path"]),
                    int(arguments["line"]),
                    str(arguments.get("content", "")),
                )
            }
        elif tool == "file_write":
            arguments = {
                "path": arguments["path"],
                "content": arguments.get("content", ""),
            }
        elif tool not in {"command_run", "file_read", "file_replace_contents"}:
            raise ValueError(f"unsupported normalized tool: {tool}")
        arguments.update(
            {"sandbox_id": self.sandbox_id, "connect_if_missing": False}
        )
        return tool, arguments

    async def execute(self, action: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("backend is not started")
        tool, arguments = self._mcp_call(action)
        result = await self._session.call_tool(tool, arguments=arguments)
        self._raise_for_tool_error(tool, result)
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return jsonable(structured)
        content = getattr(result, "content", None)
        if isinstance(content, list) and len(content) == 1:
            text = getattr(content[0], "text", None)
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return jsonable(content)
