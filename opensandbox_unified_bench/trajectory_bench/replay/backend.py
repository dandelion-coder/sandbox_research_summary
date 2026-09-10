"""Common replay backend contract and helpers."""

from __future__ import annotations

import base64
import json
import shlex
from typing import Any, Protocol


class Backend(Protocol):
    name: str

    async def start(self) -> None: ...

    async def execute(self, action: dict[str, Any]) -> Any: ...

    async def close(self) -> None: ...


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return jsonable(model_dump(mode="json"))
    return str(value)


def insert_command(path: str, line: int, content: str) -> str:
    """Build a shell-safe, single-invocation implementation of editor insert."""

    path64 = base64.b64encode(path.encode()).decode()
    content64 = base64.b64encode(content.encode()).decode()
    program = (
        "import base64,pathlib;"
        f"p=pathlib.Path(base64.b64decode('{path64}').decode());"
        f"s=base64.b64decode('{content64}').decode();"
        "a=p.read_text().splitlines(keepends=True);"
        f"a[{max(line, 0)}:{max(line, 0)}]=[s if s.endswith('\\n') else s+'\\n'];"
        "p.write_text(''.join(a))"
    )
    return f"python3 -c {json.dumps(program)}"


def list_directory_command(path: str, depth: int) -> str:
    return (
        f"find {shlex.quote(path)} -mindepth 1 "
        f"-maxdepth {max(depth, 0)} -print"
    )


def read_range_command(path: str, offset: int, limit: int | None) -> str:
    start = max(offset, 1)
    end: int | str = start + limit - 1 if limit is not None else "$"
    expression = shlex.quote(f"{start},{end}p")
    return f"sed -n {expression} {shlex.quote(path)}"
