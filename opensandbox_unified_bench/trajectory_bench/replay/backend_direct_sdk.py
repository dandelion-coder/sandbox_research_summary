"""Native OpenSandbox SDK replay backend."""

from __future__ import annotations

from typing import Any

from trajectory_bench.replay.backend import (
    insert_command,
    jsonable,
    list_directory_command,
    read_range_command,
)


class DirectSdkBackend:
    name = "direct_sdk"

    def __init__(self, sandbox_id: str, *, skip_health_check: bool = False) -> None:
        self.sandbox_id = sandbox_id
        self.skip_health_check = skip_health_check
        self._sandbox: Any = None

    async def start(self) -> None:
        from opensandbox import Sandbox

        self._sandbox = await Sandbox.connect(
            self.sandbox_id, skip_health_check=self.skip_health_check
        )

    async def execute(self, action: dict[str, Any]) -> Any:
        if self._sandbox is None:
            raise RuntimeError("backend is not started")
        from opensandbox.models.execd import RunCommandOpts
        from opensandbox.models.filesystem import ContentReplaceEntry

        tool = str(action["tool"])
        arguments = dict(action.get("arguments", {}))
        if tool == "command_run":
            result = await self._sandbox.commands.run(
                str(arguments["command"]),
                opts=RunCommandOpts(
                    background=False,
                    working_directory=arguments.get("working_directory"),
                ),
            )
        elif tool == "file_read":
            if "offset" in arguments or "limit" in arguments:
                result = await self._sandbox.commands.run(
                    read_range_command(
                        str(arguments["path"]),
                        int(arguments.get("offset", 1)),
                        arguments.get("limit"),
                    )
                )
            else:
                result = await self._sandbox.files.read_file(str(arguments["path"]))
        elif tool == "list_directory":
            result = await self._sandbox.commands.run(
                list_directory_command(
                    str(arguments["path"]), int(arguments.get("depth", 2))
                )
            )
        elif tool == "file_write":
            result = await self._sandbox.files.write_file(
                str(arguments["path"]), str(arguments.get("content", ""))
            )
        elif tool == "file_replace_contents":
            entries = [ContentReplaceEntry(**entry) for entry in arguments["entries"]]
            result = await self._sandbox.files.replace_contents_detailed(entries)
        elif tool == "file_insert":
            result = await self._sandbox.commands.run(
                insert_command(
                    str(arguments["path"]),
                    int(arguments["line"]),
                    str(arguments.get("content", "")),
                )
            )
        else:
            raise ValueError(f"unsupported normalized tool: {tool}")
        return jsonable(result)

    async def close(self) -> None:
        if self._sandbox is not None:
            await self._sandbox.close()
            self._sandbox = None

    async def __aenter__(self) -> "DirectSdkBackend":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
