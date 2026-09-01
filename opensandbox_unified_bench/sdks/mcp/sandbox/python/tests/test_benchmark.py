from __future__ import annotations

import pytest

from opensandbox_mcp.benchmark import BenchmarkOperation, BenchmarkRecorder
from opensandbox_mcp.benchmark_client import _percentile, _render


@pytest.mark.asyncio
async def test_benchmark_operation_records_common_success_timeline() -> None:
    recorder = BenchmarkRecorder()

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
