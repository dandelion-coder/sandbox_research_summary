"""Streamable HTTP MCP replay backend for an already-running server."""

from __future__ import annotations

from typing import Any

from trajectory_bench.replay.backend_mcp import McpBackendBase


class HttpMcpBackend(McpBackendBase):
    name = "http_mcp"

    def __init__(
        self,
        sandbox_id: str,
        *,
        url: str = "http://127.0.0.1:8000/mcp",
        skip_health_check: bool = False,
    ) -> None:
        super().__init__(sandbox_id, skip_health_check=skip_health_check)
        self.url = url
        self._transport_context: Any = None
        self._session_context: Any = None

    async def start(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self._transport_context = streamable_http_client(self.url)
        read_stream, write_stream, _ = await self._transport_context.__aenter__()
        self._session_context = ClientSession(read_stream, write_stream)
        self._session = await self._session_context.__aenter__()
        await self._initialize_session()

    async def close(self) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(None, None, None)
        if self._transport_context is not None:
            await self._transport_context.__aexit__(None, None, None)
        self._session = self._session_context = self._transport_context = None

    async def __aenter__(self) -> "HttpMcpBackend":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()
