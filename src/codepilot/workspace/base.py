"""声明 Local 与 Docker 必须共同满足的执行后端边界。"""

from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from codepilot.contracts import ToolCall, ToolResult
from codepilot.tools.base import ToolSpec
from codepilot.workspace.models import BackendEvent, CommandResult


@runtime_checkable
class ExecutionBackend(Protocol):
    """统一任务工作区中的工具、命令、取消、产物和生命周期。"""

    @property
    def root(self) -> Path:
        """返回控制器固定的实际工作区，供 Action 与扩展共同核对身份。"""
        ...

    @property
    def backend_id(self) -> str:
        """返回控制器生成的后端实例 ID，用于关联资源归属和审计。"""
        ...

    @property
    def is_container(self) -> bool:
        """说明该环境是否提供容器执行边界。"""
        ...

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """返回后端实际支持的工具声明。"""
        ...

    @property
    def events(self) -> tuple[BackendEvent, ...]:
        """返回按发生顺序冻结的生命周期事件。"""
        ...

    async def start(self) -> None:
        """准备一次任务环境；重复调用不得创建第二个环境。"""
        ...

    async def execute(self, call: ToolCall) -> ToolResult:
        """在任务环境中执行经过内部契约表示的工具调用。"""
        ...

    async def run_argv(
        self, command: tuple[str, ...], *, timeout_seconds: float | None = None
    ) -> CommandResult:
        """在相同任务环境内执行无需 Shell 解释的固定验证命令。"""
        ...

    async def cancel(self) -> None:
        """停止后端当前执行并阻止后续调用。"""
        ...

    async def export_artifact(self, relative_path: str, destination: Path) -> Path:
        """把受限工作区文件复制到控制器声明的目标位置。"""
        ...

    async def close(self) -> None:
        """清理后端拥有的进程或容器；重复调用必须安全。"""
        ...

    async def __aenter__(self) -> Self:
        """启动后端并返回自身。"""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """离开上下文时无条件清理任务环境。"""
        ...


def find_backend(executor: object) -> ExecutionBackend | None:
    """沿执行包装器的 registry 链查找唯一底层 ExecutionBackend。"""
    current = executor
    visited: set[int] = set()
    while id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ExecutionBackend):
            return current
        candidate = getattr(current, "registry", None)
        if candidate is None:
            return None
        current = candidate
    # 包装链出现环时保守地视为没有可管理后端。
    return None
