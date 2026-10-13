"""针对已知一次性 fixture 仓库提供三个固定操作。"""

import asyncio
import shutil
import sys
from pathlib import Path

from codepilot.contracts import Message, ToolCall, ToolResult

FIXED_SOURCE = "def add(a: int, b: int) -> int:\n    return a + b\n"


def prepare_fixture(source: Path, destination: Path) -> Path:
    """复制两个验收文件，绝不修改仓库中已签入的 fixture。"""
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("calculator.py", "test_calculator.py"):
        shutil.copyfile(source / name, destination / name)
    (destination / ".codepilot-m02-fixture").write_text("disposable fixture\n", encoding="utf-8")
    return destination


def repair_script() -> tuple[Message, ...]:
    """返回 M02 使用的确定性读取、修复、测试和结束回复。"""
    return (
        Message(
            role="assistant",
            tool_calls=(
                ToolCall(call_id="read-1", name="read_file", arguments={"path": "calculator.py"}),
            ),
        ),
        Message(
            role="assistant",
            tool_calls=(
                ToolCall(
                    call_id="write-1",
                    name="write_file",
                    arguments={
                        "path": "calculator.py",
                        "content": FIXED_SOURCE,
                    },
                ),
            ),
        ),
        Message(
            role="assistant",
            tool_calls=(ToolCall(call_id="test-1", name="run_tests", arguments={}),),
        ),
        Message(role="assistant", content="Fixture repair completed; inspect test evidence."),
    )


class FixtureExecutor:
    """仅供 M02 验收：文件名和测试命令固定，不提供 Shell 或工具注册。"""

    def __init__(self, workspace: Path) -> None:
        """将执行器绑定到已标记为一次性 M02 fixture 的现有目录。"""
        self.workspace = workspace.resolve(strict=True)
        # 标记可避免把执行器意外绑定到任意用户工作区。
        if not (self.workspace / ".codepilot-m02-fixture").is_file():
            raise ValueError("FixtureExecutor requires a prepared disposable fixture")
        self.active_process: asyncio.subprocess.Process | None = None

    async def execute(self, call: ToolCall) -> ToolResult:
        """校验并执行三个固定 fixture 操作之一。"""

        def error(content: str) -> ToolResult:
            """构造模型可见的失败结果，并保留原始调用 ID。"""
            return ToolResult(call_id=call.call_id, content=content, is_error=True)

        arguments = call.arguments
        # M03/M04 前，文件访问被有意限制在一个已知 fixture 文件内。
        if call.name in ("read_file", "write_file"):
            expected = {"path"} if call.name == "read_file" else {"path", "content"}
            # 精确键集合可阻止被忽略或拼错的参数触发副作用。
            if set(arguments) != expected or arguments.get("path") != "calculator.py":
                return error("invalid_arguments: only calculator.py and the exact keys are allowed")
            path = self.workspace / "calculator.py"
            # 符号链接可能把固定相对路径重定向到一次性 fixture 之外。
            if path.is_symlink():
                return error("invalid_arguments: fixture file must not be a symlink")
            # 读取在此返回；继续向下执行就表示这是已校验的写请求。
            if call.name == "read_file":
                return ToolResult(call_id=call.call_id, content=path.read_text(encoding="utf-8"))
            content = arguments["content"]
            # 限制受控写入大小，并拒绝非文本 JSON 值。
            if not isinstance(content, str) or len(content.encode("utf-8")) > 4096:
                return error("invalid_arguments: content must be a string of at most 4096 bytes")
            path.write_text(content, encoding="utf-8")
            return ToolResult(call_id=call.call_id, content="calculator.py written")
        # 测试只使用固定 argv，绝不求值模型提供的 Shell 字符串。
        if call.name == "run_tests":
            # 此操作在 M02 fixture 中没有可配置参数。
            if arguments:
                return error("invalid_arguments: run_tests accepts no arguments")
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-p",
                "test_calculator.py",
                "-v",
                cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.active_process = process
            try:
                stdout, stderr = await process.communicate()
            finally:
                # 此已知 fixture 不会启动后代进程；通用清理留给 M04。
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        # 进程可能在检查返回码后、发送 kill 前自行退出。
                        pass
                    await process.wait()
            return ToolResult(
                call_id=call.call_id,
                is_error=process.returncode != 0,
                content=(
                    f"exit_code={process.returncode}\nstdout:\n{stdout.decode('utf-8')}"
                    f"stderr:\n{stderr.decode('utf-8')}"
                ),
            )
        # 未知名称作为下一轮模型反馈返回，不抛出 Python 异常。
        return error(f"unknown_tool: {call.name}")
