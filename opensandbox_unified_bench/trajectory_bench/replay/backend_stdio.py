"""stdio MCP replay backend."""

from __future__ import annotations

from typing import Any

from trajectory_bench.replay.backend_mcp import McpBackendBase


class StdioMcpBackend(McpBackendBase):
    name = "stdio_mcp"

    def __init__(
        self,
        sandbox_id: str,
        *,
        server_command: str = "opensandbox-mcp",
        server_args: list[str] | None = None,
        skip_health_check: bool = False,
    ) -> None:
        super().__init__(sandbox_id, skip_health_check=skip_health_check)
        self.server_command = server_command
        self.server_args = list(server_args or [])
        self._transport_context: Any = None
        self._session_context: Any = None

    async def start(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=self.server_command, args=self.server_args, env=None
        )
        self._transport_context = stdio_client(parameters)
        read_stream, write_stream = await self._transport_context.__aenter__()
        self._session_context = ClientSession(read_stream, write_stream)
        self._session = await self._session_context.__aenter__()
        await self._initialize_session()

    async def close(self) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(None, None, None)
        if self._transport_context is not None:
            await self._transport_context.__aexit__(None, None, None)
        self._session = self._session_context = self._transport_context = None

    async def __aenter__(self) -> "StdioMcpBackend":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
