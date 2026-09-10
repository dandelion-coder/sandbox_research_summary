from __future__ import annotations

import unittest

from trajectory_bench.summarize import result_row


class SummarizeTest(unittest.TestCase):
    def test_result_row_flattens_metrics(self) -> None:
        row = result_row(
            {
                "task_id": "task-1",
                "backend": "direct_sdk",
                "status": "ok",
                "metrics": {
                    "tool_calls": 2,
                    "total_trajectory_latency_ms": 10.0,
                    "tool_calls_per_second": 200.0,
                    "trajectories_per_hour": 360000.0,
                    "latency": {"mean_ms": 5.0, "p50_ms": 4.0, "p95_ms": 7.0},
                },
            }
        )
        self.assertEqual(row["mean_tool_latency_ms"], 5.0)
        self.assertEqual(row["backend"], "direct_sdk")


if __name__ == "__main__":
    unittest.main()
