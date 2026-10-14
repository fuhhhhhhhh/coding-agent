"""定义供应商无关的流式分片、用量与运行事件。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts.models import Contract, Identifier, ToolCall


class Usage(Contract):
    """区分供应商计量和本地估算；缺失值使用未知而非零。"""

    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    source: Literal["provider", "estimate", "unknown"] = "unknown"


class StreamChunk(Contract):
    """描述增量文本、按索引分开的调用参数、用量或显式结束标记。"""

    kind: Literal["text_delta", "tool_call_delta", "usage_updated", "turn_finished"]
    text: str = ""
    index: Annotated[int, Field(ge=0)] = 0
    call_id: str = ""
    name: str = ""
    arguments_delta: str = ""
    usage: Usage | None = None
    finish_reason: Literal["stop", "tool_calls"] | None = None


class RuntimeEvent(Contract):
    """关联运行、回合和调用；正文仅供内存观察，默认不落盘。"""

    run_id: Identifier
    turn_id: str
    sequence: Annotated[int, Field(ge=1)]
    kind: Literal[
        "model_requested",
        "text_delta",
        "tool_call_delta",
        "tool_call_ready",
        "tool_started",
        "tool_finished",
        "approval_requested",
        "approval_resolved",
        "usage_updated",
        "turn_finished",
        "run_finished",
    ]
    call_id: str = ""
    call_index: int | None = None
    text: str = ""
    call: ToolCall | None = None
    usage: Usage | None = None
    duration_seconds: float | None = None
    outcome: str = ""
    error_type: str = ""
