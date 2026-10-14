"""公开统一执行后端、隔离策略和生命周期结果。"""

from codepilot.workspace.base import ExecutionBackend, find_backend
from codepilot.workspace.docker import DockerExecutionBackend
from codepilot.workspace.local import LocalExecutionBackend
from codepilot.workspace.models import BackendEvent, BackendLimits, CommandResult

__all__ = [
    "BackendEvent",
    "BackendLimits",
    "CommandResult",
    "DockerExecutionBackend",
    "ExecutionBackend",
    "LocalExecutionBackend",
    "find_backend",
]
