from __future__ import annotations

import unittest
from typing import Any

from trajectory_bench.replay.replay_engine import ReplayEngine


class FakeBackend:
    name = "direct_sdk"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.actions: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def execute(self, action: dict[str, Any]) -> Any:
        self.actions.append(action)
        if self.fail:
            raise RuntimeError("expected")
        return {"ok": True}

    async def close(self) -> None:
        return None


class ReplayEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_and_path_rewriting(self) -> None:
        backend = FakeBackend()
        trajectory = {
            "task_id": "task-1",
            "trajectory": [
                {"step": 0, "action": {"tool": "file_read", "arguments": {"path": "/old/a.py"}}},
                {"step": 1, "action": {"tool": "command_run", "arguments": {"command": "cd /old && true"}}},
            ],
        }
        result = await ReplayEngine(backend, path_replacements={"/old": "/new"}).run(trajectory)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["metrics"]["tool_calls"], 2)
        self.assertGreater(result["metrics"]["trajectories_per_hour"], 0)
        self.assertEqual(result["metrics"]["tool_distribution"], {"command_run": 1, "file_read": 1})
        self.assertEqual(backend.actions[0]["arguments"]["path"], "/new/a.py")
        self.assertIn("cd /new", backend.actions[1]["arguments"]["command"])

    async def test_stops_after_error_by_default(self) -> None:
        backend = FakeBackend(fail=True)
        trajectory = {
            "task_id": "task-1",
            "trajectory": [
                {"step": 0, "action": {"tool": "file_read", "arguments": {"path": "/a"}}},
                {"step": 1, "action": {"tool": "file_read", "arguments": {"path": "/b"}}},
            ],
        }
        result = await ReplayEngine(backend).run(trajectory)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["metrics"]["tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
