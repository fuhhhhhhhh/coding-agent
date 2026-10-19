"""定义执行后端的生命周期、命令结果和隔离策略契约。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts.models import Contract


class BackendLimits(Contract):
    """集中保存任务容器的资源与输出硬限制。"""

    workspace_bytes: Annotated[int, Field(gt=0)] | None = None
    cpus: Annotated[float, Field(gt=0)] = 1.0
    memory_bytes: Annotated[int, Field(ge=16 * 1024 * 1024)] = 256 * 1024 * 1024
    pids: Annotated[int, Field(ge=16)] = 128
    command_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    output_bytes: Annotated[int, Field(ge=1024, le=4 * 1024 * 1024)] = 262_144
    artifact_bytes: Annotated[int, Field(ge=1)] = 16 * 1024 * 1024


class CommandResult(Contract):
    """保存后端内 argv 命令的有界输出及可识别资源故障。"""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    oom_killed: bool = False
    cancelled: bool = False
    output_reference: str = ""


class BackendEvent(Contract):
    """记录不含命令正文和环境值的后端生命周期事实。"""

    sequence: Annotated[int, Field(ge=1)]
    kind: Literal[
        "backend_started",
        "tool_executed",
        "command_executed",
        "backend_cancelled",
        "resource_sampled",
        "artifact_exported",
        "backend_closed",
        "backend_close_failed",
        "stale_container_removed",
    ]
    backend_id: str
    container_id: str = ""
    call_id: str = ""
    outcome: str = ""
    duration_seconds: float | None = None
    cpu_percent: float | None = None
    memory_bytes: int | None = None
