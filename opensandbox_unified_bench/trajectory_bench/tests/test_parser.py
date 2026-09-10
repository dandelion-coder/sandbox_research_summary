from __future__ import annotations

import json
import unittest

from trajectory_bench.parser.swegym_parser import parse_record
from trajectory_bench.parser.tool_mapping import UnsupportedToolError, map_tool_call


class ToolMappingTest(unittest.TestCase):
    def test_directory_view_uses_observation(self) -> None:
        mapped = map_tool_call(
            "str_replace_editor",
            {"command": "view", "path": "/workspace/repo"},
            "Here's the files and directories up to 3 levels deep",
        )
        self.assertEqual(
            mapped,
            {"tool": "list_directory", "arguments": {"path": "/workspace/repo", "depth": 3}},
        )

    def test_file_view_range(self) -> None:
        mapped = map_tool_call(
            "str_replace_editor",
            {"command": "view", "path": "/a.py", "view_range": [4, 8]},
            "4: def example():",
        )
        self.assertEqual(
            mapped,
            {"tool": "file_read", "arguments": {"path": "/a.py", "offset": 4, "limit": 5}},
        )

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedToolError):
            map_tool_call("browser", {}, None)

    def test_editor_mutations(self) -> None:
        created = map_tool_call(
            "str_replace_editor",
            {"command": "create", "path": "/a.py", "file_text": "x = 1\n"},
        )
        replaced = map_tool_call(
            "str_replace_editor",
            {
                "command": "str_replace",
                "path": "/a.py",
                "old_str": "x = 1",
                "new_str": "x = 2",
            },
        )
        inserted = map_tool_call(
            "str_replace_editor",
            {"command": "insert", "path": "/a.py", "insert_line": 1, "new_str": "y = 3"},
        )
        self.assertEqual(created["tool"], "file_write")
        self.assertEqual(replaced["tool"], "file_replace_contents")
        self.assertEqual(inserted["tool"], "file_insert")

    def test_execute_bash(self) -> None:
        mapped = map_tool_call("execute_bash", {"command": "pytest -q"})
        self.assertEqual(
            mapped, {"tool": "command_run", "arguments": {"command": "pytest -q"}}
        )


class ParserTest(unittest.TestCase):
    def test_pairs_observation_by_tool_call_id(self) -> None:
        arguments = {"command": "view", "path": "/workspace/repo"}
        record = {
            "instance_id": "owner__repo-1",
            "run_id": "run-1",
            "resolved": True,
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call-1", "function": {"name": "str_replace_editor", "arguments": json.dumps(arguments)}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
            ],
        }
        normalized = parse_record(record)
        self.assertEqual(normalized["task_id"], "owner__repo-1")
        self.assertEqual(normalized["trajectory"][0]["observation"], "file contents")
        self.assertEqual(normalized["trajectory"][0]["action"]["tool"], "file_read")


if __name__ == "__main__":
    unittest.main()
