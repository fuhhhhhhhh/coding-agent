"""演示 M04 的版本冲突、可审查 diff 和 Shell 进程组清理。"""

import asyncio
import json
import shlex
import sys
import tempfile
from pathlib import Path

from codepilot.contracts import ToolCall, ToolResult
from codepilot.tools import ToolRegistry, Workspace
from codepilot.tools.edit_file import EditFileTool
from codepilot.tools.read_file import ReadFileTool
from codepilot.tools.shell import ShellTool


def call(registry: ToolRegistry, name: str, **arguments: object) -> ToolResult:
    """执行单个工具调用并返回结果，供演示场景复用。"""
    return asyncio.run(
        registry.execute(
            ToolCall.model_validate({"call_id": name, "name": name, "arguments": arguments})
        )
    )


def main() -> None:
    """运行 M04 三个场景并输出可审查的 JSON 摘要。"""
    with tempfile.TemporaryDirectory(prefix="codepilot-m04-") as temporary:
        root = Path(temporary)
        path = root / "value.txt"
        path.write_text("old\n", encoding="utf-8")
        workspace = Workspace(root)
        edit = EditFileTool(workspace)
        registry = ToolRegistry((ReadFileTool(workspace), edit))
        read = call(registry, "read_file", path="value.txt", include_sha256=True)
        expected = read.content.splitlines()[0].removeprefix("sha256=")
        success = call(
            registry,
            "edit_file",
            path="value.txt",
            old_text="old",
            new_text="new",
            expected_sha256=expected,
        )
        print(
            json.dumps(
                {"scenario": "atomic_edit", "error": success.is_error, "content": success.content}
            )
        )

        path.write_text("external\n", encoding="utf-8")
        conflict = call(
            registry,
            "edit_file",
            path="value.txt",
            old_text="external",
            new_text="agent",
            expected_sha256=expected,
        )
        print(
            json.dumps(
                {
                    "scenario": "hash_conflict",
                    "error": conflict.is_error,
                    "content": conflict.content,
                }
            )
        )

        shell = ShellTool(workspace)
        shell.default_timeout = 0.05
        shell_registry = ToolRegistry((shell,))
        command = f"exec {shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; time.sleep(60)"
        )
        timeout = asyncio.run(
            shell_registry.execute(
                ToolCall(call_id="shell", name="shell", arguments={"command": command})
            )
        )
        print(
            json.dumps(
                {"scenario": "shell_timeout", "error": timeout.is_error, "content": timeout.content}
            )
        )


if __name__ == "__main__":
    main()
