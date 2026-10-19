"""传递一次调用的执行前授权复核，不缓存或复用其他调用的批准。"""

from collections.abc import Callable
from contextvars import ContextVar

from codepilot.contracts.models import ToolResult

EXECUTION_GUARD: ContextVar[Callable[[], ToolResult | None] | None] = ContextVar(
    "execution_guard", default=None
)
