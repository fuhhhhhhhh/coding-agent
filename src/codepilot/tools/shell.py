"""实现固定工作目录、有界输出且可取消的基础 Shell 工具。"""

import asyncio
from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.sessions.outputs import CURRENT_CALL, CURRENT_OUTPUT
from codepilot.tools.base import Tool, ToolFailure, ToolOutput
from codepilot.tools.process import collect, spawn, workspace_environment
from codepilot.tools.workspace import Workspace


class ShellArguments(Contract):
    """定义 Shell 命令和 stdout、stderr 各自的最大保留字节数。"""

    command: Annotated[str, Field(min_length=1)]
    max_output_bytes: Annotated[int, Field(ge=1, le=262_144)] = 65_536
    timeout_seconds: Annotated[float, Field(gt=0, le=900)] = 10.0


class ShellTool(Tool[ShellArguments]):
    """在工作区启动一个 Shell 进程并返回退出码及有界输出。"""

    name = "shell"
    description = (
        "在工作区运行有超时和输出限制的 Shell 命令；可使用 git、测试器和项目命令。"
        "Git commit、branch、fetch、pull、push 等操作通过本工具执行"
    )
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
        factory = CURRENT_OUTPUT.get()
        call_id = CURRENT_CALL.get() or "shell"
        capture = factory.begin(call_id) if factory is not None else None
        # 先成功登记捕获，再允许命令启动；磁盘故障不能先造成用户文件副作用。
        process = await spawn(
            (arguments.command,),
            cwd=str(self.workspace.root),
            shell=True,
            capture=capture,
            env=workspace_environment(),
        )
        self.active_process = process
        stdout, stdout_truncated, stderr, stderr_truncated, reference = await collect(
            process, arguments.max_output_bytes, call_id, capture
        )
        return ToolOutput(
            is_error=process.returncode != 0,
            content=(
                f"exit_code={process.returncode}\n"
                f"stdout_truncated={str(stdout_truncated).lower()}\n"
                f"stderr_truncated={str(stderr_truncated).lower()}\n"
                f"stdout:\n{stdout.decode('utf-8', errors='replace')}\n"
                f"stderr:\n{stderr.decode('utf-8', errors='replace')}\n{reference}"
            ),
        )

    def timeout(self, arguments: ShellArguments) -> float:
        """长测试可显式增加时限，数值已经过 Schema 上界校验并绑定审批。"""
        # 未显式传入时仍尊重后端或测试设定的默认时限，不能被模型默认值意外放宽。
        return (
            arguments.timeout_seconds
            if "timeout_seconds" in arguments.model_fields_set
            else self.default_timeout
        )
