"""非流式运行观察记录与限制；不依赖供应商或 UI。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts.models import Contract, Identifier, Message, RunState, ToolCall, ToolResult

StopReason = Literal[
    "completed",
    "step_limit",
    "tool_budget",
    "time_limit",
    "repeated_failure",
    "provider_error",
    "contract_error",
    "cancelled",
]


class RunLimits(Contract):
    """集中定义一次运行可消耗的资源上限，防止 Agent 无限循环。"""

    # 每次 Provider 请求计为一步，至少允许发起一次请求。
    max_steps: Annotated[int, Field(ge=1)] = 8
    # 工具调用按单个 ToolCall 计数；设为零可以完全禁止工具执行。
    max_tool_calls: Annotated[int, Field(ge=0)] = 16
    # 整次运行共享同一个单调时钟期限，包含 Provider 与工具耗时。
    timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    # 同名同参数的连续失败达到该次数时停止，避免无效重试。
    max_consecutive_failures: Annotated[int, Field(ge=1)] = 3


class RunObservation(Contract):
    """记录运行中的一个有序事实，用于调试、测试和后续界面展示。"""

    # run_id 将事件绑定到唯一运行；sequence 给出运行内的严格顺序。
    run_id: Identifier
    sequence: Annotated[int, Field(ge=1)]
    # step 表示事件发生时已经尝试的 Provider 请求次数。
    step: Annotated[int, Field(ge=0)]
    # kind 决定本条记录描述模型、工具还是最终停止事件。
    kind: Literal[
        "model_requested", "model_replied", "tool_requested", "tool_result", "run_stopped"
    ]
    # 以下载荷按 kind 选用；未使用的字段保持为空，避免重复建模事件类。
    message: Message | None = None
    call: ToolCall | None = None
    result: ToolResult | None = None
    # state、reason 和 detail 主要用于唯一的 run_stopped 记录。
    state: RunState | None = None
    reason: StopReason | None = None
    detail: str = ""


class RunReport(Contract):
    """冻结一次运行的终态、计数、完整历史和可观察轨迹。"""

    # 身份与终止字段回答“哪次运行以什么结果结束”。
    run_id: Identifier
    state: RunState
    reason: StopReason
    # 计数用于核对资源限制是否按真实尝试次数生效。
    steps: int
    tool_calls: int
    # messages 面向下一轮推理语义，observations 面向运行过程诊断。
    messages: tuple[Message, ...]
    observations: tuple[RunObservation, ...]
