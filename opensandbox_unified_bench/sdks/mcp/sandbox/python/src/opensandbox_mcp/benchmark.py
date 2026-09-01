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

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class BenchmarkEvent(BaseModel):
    """One timestamp in a benchmarked MCP tool invocation."""

    trace_id: str = Field(description="Client-generated invocation identifier.")
    tool: str = Field(description="MCP tool name without a registration prefix.")
    event: str = Field(description="Timeline marker: M0, B0, B1, or M4.")
    wall_time_ns: int = Field(description="Wall clock timestamp for event ordering.")
    monotonic_ns: int = Field(description="Server monotonic timestamp for durations.")
    status: str = Field(default="ok", description="Event status: ok or error.")
    error_type: str | None = Field(default=None, description="Exception class, if any.")


@dataclass
class BenchmarkRecorder:
    """In-memory recorder that keeps measurement I/O out of the hot path."""

    events: list[BenchmarkEvent] = field(default_factory=list)

    def record(
        self,
        trace_id: str | None,
        tool: str,
        event: str,
        *,
        status: str = "ok",
        error_type: str | None = None,
    ) -> None:
        if trace_id is None:
            return
        self.events.append(
            BenchmarkEvent(
                trace_id=trace_id,
                tool=tool,
                event=event,
                wall_time_ns=time.time_ns(),
                monotonic_ns=time.monotonic_ns(),
                status=status,
                error_type=error_type,
            )
        )

    def drain(self, trace_ids: list[str] | None = None) -> list[BenchmarkEvent]:
        """Return and remove events, optionally restricted to selected traces."""
        if trace_ids is None:
            drained = self.events
            self.events = []
            return drained

        selected = set(trace_ids)
        drained = [event for event in self.events if event.trace_id in selected]
        self.events = [event for event in self.events if event.trace_id not in selected]
        return drained


class BenchmarkOperation:
    """Records the common MCP-handler and SDK-call boundaries."""

    def __init__(
        self,
        recorder: BenchmarkRecorder,
        trace_id: str | None,
        tool: str,
    ) -> None:
        self._recorder = recorder
        self._trace_id = trace_id
        self._tool = tool

    async def __aenter__(self) -> BenchmarkOperation:
        self._recorder.record(self._trace_id, self._tool, "M0")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._recorder.record(
            self._trace_id,
            self._tool,
            "M4",
            status="error" if exc_type else "ok",
            error_type=exc_type.__name__ if exc_type else None,
        )

    async def sdk_call(self, call: Callable[[], Awaitable[T]]) -> T:
        """Measure exactly one OpenSandbox SDK awaitable call."""
        self._recorder.record(self._trace_id, self._tool, "B0")
        try:
            result = await call()
        except BaseException as exc:
            self._recorder.record(
                self._trace_id,
                self._tool,
                "B1",
                status="error",
                error_type=type(exc).__name__,
            )
            raise
        self._recorder.record(self._trace_id, self._tool, "B1")
        return result
