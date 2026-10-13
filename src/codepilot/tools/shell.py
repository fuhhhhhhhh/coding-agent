"""实现固定工作目录、有界输出且可取消的基础 Shell 工具。"""

import asyncio
import os
import signal
import subprocess
from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure, ToolOutput
from codepilot.tools.workspace import Workspace


class ShellArguments(Contract):
    """定义 Shell 命令和 stdout、stderr 各自的最大保留字节数。"""

    command: Annotated[str, Field(min_length=1)]
    max_output_bytes: Annotated[int, Field(ge=1, le=262_144)] = 65_536


async def _read_limited(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    """持续排空管道但只保留前 limit 字节，防止子进程因管道写满阻塞。"""
    if stream is None:
        return b"", False
    collected = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        remaining = limit - len(collected)
        if remaining > 0:
            collected.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(collected), truncated


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    """终止当前进程及其同组后代，并等待直接进程完成回收。"""
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # 进程可能已退出，或平台拒绝组信号；后续仍等待直接子进程。
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except TimeoutError:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await process.wait()


class ShellTool(Tool[ShellArguments]):
    """在工作区启动一个 Shell 进程并返回退出码及有界输出。"""

    name = "shell"
    description = "在工作区运行有超时和输出限制的 Shell 命令"
    effect = "process"
    resources = ("workspace:process",)
    default_timeout = 10.0
    arguments_model = ShellArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存固定工作目录，并暴露最近进程供生命周期测试检查。"""
        self.workspace = workspace
        self.active_process: asyncio.subprocess.Process | None = None

    async def run(self, arguments: ShellArguments) -> ToolOutput:
        """运行命令；取消时终止当前进程并等待其回收。"""
        if not arguments.command.strip():
            raise ToolFailure("invalid_command: command cannot be blank")
        if os.name == "nt":
            # Windows 用新的进程组标志，便于取消时向命令组发送终止信号。
            process = await asyncio.create_subprocess_shell(
                arguments.command,
                cwd=self.workspace.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            # POSIX 用新会话建立独立进程组，killpg 才不会波及 Agent 自身。
            process = await asyncio.create_subprocess_shell(
                arguments.command,
                cwd=self.workspace.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        self.active_process = process
        stdout_task = asyncio.create_task(_read_limited(process.stdout, arguments.max_output_bytes))
        stderr_task = asyncio.create_task(_read_limited(process.stderr, arguments.max_output_bytes))
        try:
            return_code = await process.wait()
            stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            # 独立进程组让取消可以覆盖命令主动启动的后代进程。
            await _stop_process_group(process)
            for task in (stdout_task, stderr_task):
                task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        return ToolOutput(
            is_error=return_code != 0,
            content=(
                f"exit_code={return_code}\n"
                f"stdout_truncated={str(stdout_truncated).lower()}\n"
                f"stderr_truncated={str(stderr_truncated).lower()}\n"
                f"stdout:\n{stdout.decode('utf-8', errors='replace')}\n"
                f"stderr:\n{stderr.decode('utf-8', errors='replace')}"
            ),
        )
