"""定义调度器内部可观察且不可变的资源计划。"""

from typing import Literal

from codepilot.contracts.models import Contract


class ExecutionPlan(Contract):
    """描述一次调用的访问模式和保守资源集合。"""

    call_id: str
    mode: Literal["read", "write", "exclusive"]
    resources: tuple[str, ...]
