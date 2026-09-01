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

import json

import httpx
import pytest

from opensandbox_mcp.benchmark import BenchmarkOperation, BenchmarkRecorder
from opensandbox_mcp.benchmark_client import (
    HttpResponseSizeRecorder,
    _percentile,
    _render,
    _tool_arguments,
)
from opensandbox_mcp.server import create_server


@pytest.mark.asyncio
async def test_benchmark_operation_records_common_success_timeline() -> None:
    recorder = BenchmarkRecorder()
    recorder.start()

    async def sdk_call() -> str:
        return "done"

    async with BenchmarkOperation(recorder, "trace-1", "file_read") as benchmark:
        assert await benchmark.sdk_call(sdk_call) == "done"

    assert [event.event for event in recorder.events] == ["M0", "B0", "B1", "M4"]
    assert all(event.status == "ok" for event in recorder.events)
    assert [event.monotonic_ns for event in recorder.events] == sorted(
        event.monotonic_ns for event in recorder.events
    )


@pytest.mark.asyncio
async def test_benchmark_operation_records_failure_boundaries() -> None:
    recorder = BenchmarkRecorder()
    recorder.start()

    async def sdk_call() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        async with BenchmarkOperation(
            recorder, "trace-2", "file_write"
        ) as benchmark:
            await benchmark.sdk_call(sdk_call)

    assert [event.event for event in recorder.events] == ["M0", "B0", "B1", "M4"]
    assert recorder.events[2].status == "error"
    assert recorder.events[2].error_type == "ValueError"
    assert recorder.events[3].status == "error"


def test_recorder_is_noop_without_trace_id_and_can_drain_selected_traces() -> None:
    recorder = BenchmarkRecorder()
    recorder.record(None, "file_read", "M0")
    assert recorder.events == []

    recorder.start()
    recorder.record(None, "file_read", "M0")
    recorder.record("a", "file_read", "M0")
    recorder.record("b", "file_write", "M0")

    assert [event.trace_id for event in recorder.drain(["a"])] == ["a"]
    assert [event.trace_id for event in recorder.events] == ["b"]


def test_client_helpers_render_nested_case_arguments_and_percentiles() -> None:
    rendered = _render(
        {
            "path": "/tmp/{trace_id}",
            "paths": ["/{iteration}"],
            "content": "mapping = {'literal': True}",
        },
        {"trace_id": "abc", "iteration": 3},
    )
    assert rendered == {
        "path": "/tmp/abc",
        "paths": ["/3"],
        "content": "mapping = {'literal': True}",
    }
    assert _percentile([1.0, 2.0, 3.0], 0.5) == 2.0


def test_tool_arguments_force_registered_sandbox_without_schema_trace() -> None:
    assert _tool_arguments(
        "command_run",
        {"command": "pwd", "connect_if_missing": True},
        "sandbox-1",
    ) == {
        "command": "pwd",
        "sandbox_id": "sandbox-1",
        "connect_if_missing": False,
    }


@pytest.mark.asyncio
async def test_benchmark_context_does_not_change_tool_input_schemas() -> None:
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    instrumented = {
        "command_run",
        "file_read",
        "file_write",
        "file_delete",
        "file_search",
        "file_create_directories",
        "file_delete_directories",
        "file_move",
        "file_replace_contents",
    }

    assert instrumented <= tools.keys()
    for name in instrumented:
        properties = tools[name].inputSchema.get("properties", {})
        assert "ctx" not in properties
        assert "benchmark_trace_id" not in properties


@pytest.mark.asyncio
async def test_http_response_size_uses_complete_response_body_bytes() -> None:
    recorder = HttpResponseSizeRecorder()
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:8000/mcp",
        content=json.dumps({"jsonrpc": "2.0", "id": 201, "method": "tools/call"}),
    )
    response = httpx.Response(200, content=b"complete-response-body", request=request)

    await recorder.on_response(response)

    assert recorder.response_bytes_by_trace == {"201": 22}
