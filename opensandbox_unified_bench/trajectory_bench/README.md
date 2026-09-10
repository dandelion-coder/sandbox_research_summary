# OpenSandbox trajectory replay benchmark

This experiment replays sandbox actions extracted from SWE-Gym OpenHands trajectories through one of three equivalent client paths: Streamable HTTP MCP, stdio MCP, or the native OpenSandbox Python SDK. It is independent from existing microbenchmarks and does not modify their data or results.

## What is measured

The replay engine measures only sequential sandbox tool execution. It does not reproduce model inference time or feed new observations back into an agent. `total_trajectory_latency_ms` is the sum of the individually timed backend calls; `replay_wall_time_ms` additionally exposes the local loop/bookkeeping time. Each result also contains calls per second, theoretical trajectories per hour, call counts, tool distribution, per-call latency, and aggregate mean/P50/P95 latency.

Use a clean sandbox containing the task repository at its original revision for every backend. Reusing a mutated sandbox makes edit and test actions non-comparable. A replay executes bash commands from the dataset inside the selected sandbox; inspect trajectories before running them.

## Parse a SWE-Gym row

The parser accepts a single row, a JSON array/JSONL file, or the response returned by the Hugging Face Dataset Server `/rows` endpoint.

```bash
python -m trajectory_bench.parser.swegym_parser \
  trajectory_bench/data/raw/getmoto__moto-5321.dataset-server.json \
  trajectory_bench/data/trajectories/getmoto__moto-5321.json
```

`str_replace_editor` actions are normalized as follows:

| Source action | Normalized action |
| --- | --- |
| `view` of a file | `file_read` |
| `view` of a directory | `list_directory` |
| `create` | `file_write` |
| `str_replace` | `file_replace_contents` |
| `insert` | `file_insert` |
| `execute_bash` | `command_run` |

Directory views are distinguished using the recorded tool observation. `finish` is metadata/control flow and is not replayed.

The current MCP server does not expose line-range reads, directory listing, or line insertion as dedicated tools. For those normalized actions, every backend deliberately executes the same generated `sed`, `find`, or Python command through `command_run`. This keeps the remote operation comparable instead of measuring different implementations.

## Replay

Native SDK:

```bash
python -m trajectory_bench.run trajectory_bench/data/trajectories/getmoto__moto-5321.json \
  --backend direct-sdk --sandbox-id "$SANDBOX_ID" \
  --output trajectory_bench/results/getmoto.direct-sdk.json
```

stdio MCP (the server is started as a child process):

```bash
python -m trajectory_bench.run trajectory_bench/data/trajectories/getmoto__moto-5321.json \
  --backend stdio --sandbox-id "$SANDBOX_ID" \
  --server-command opensandbox-mcp \
  --output trajectory_bench/results/getmoto.stdio.json
```

Streamable HTTP MCP (start the MCP server separately first):

```bash
python -m trajectory_bench.run trajectory_bench/data/trajectories/getmoto__moto-5321.json \
  --backend http --sandbox-id "$SANDBOX_ID" --mcp-url http://127.0.0.1:8000/mcp \
  --output trajectory_bench/results/getmoto.http.json
```

If the checkout path differs from the trajectory, add a repeatable mapping such as `--path-map /workspace/original=/workspace/current`. Replacements apply to explicit paths and paths embedded in commands.

Combine results from clean, equivalent sandboxes into a single CSV comparison:

```bash
python -m trajectory_bench.summarize \
  trajectory_bench/results/getmoto.http.json \
  trajectory_bench/results/getmoto.stdio.json \
  trajectory_bench/results/getmoto.direct-sdk.json \
  --output trajectory_bench/results/summary.csv
```

## Environment and validation

Run from the repository root with the MCP package environment, which already includes both `mcp` and the local `opensandbox` SDK:

```bash
uv run --project sdks/mcp/sandbox/python \
  python -m unittest discover -s trajectory_bench/tests -v
```
