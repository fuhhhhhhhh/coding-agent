"""定义供应商无关的流式分片、用量与运行事件。"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

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

    @model_validator(mode="after")
    def check_kind_payload(self) -> Self:
        """按分片类型拒绝无关字段，防止用量或参数被静默丢弃。"""
        if self.kind == "text_delta":
            if not self.text or self._has_call_payload() or self.usage or self.finish_reason:
                raise ValueError("text_delta requires only nonempty text")
        elif self.kind == "tool_call_delta":
            if self.text or self.usage or self.finish_reason:
                raise ValueError("tool_call_delta contains unrelated payload")
            # 空工具片段既不能推进组装，也可能掩盖供应商映射错误。
            if not (self.call_id or self.name or self.arguments_delta):
                raise ValueError("tool_call_delta requires call data")
        elif self.kind == "usage_updated":
            if self.usage is None or self.text or self._has_call_payload() or self.finish_reason:
                raise ValueError("usage_updated requires only usage")
        elif self.finish_reason is None or self.text or self._has_call_payload() or self.usage:
            raise ValueError("turn_finished requires only finish_reason")
        return self

    def _has_call_payload(self) -> bool:
        """判断当前分片是否携带任何工具调用专用字段。"""
        return bool(self.index or self.call_id or self.name or self.arguments_delta)


class RuntimeEvent(Contract):
    """关联运行、回合和调用；正文仅供内存观察，默认不落盘。"""

    run_id: Identifier
    turn_id: str
    sequence: Annotated[int, Field(ge=1)]
    kind: Literal[
        "model_requested",
        "provider_attempt",
        "provider_retry",
        "network_attempt",
        "text_delta",
        "tool_call_delta",
        "tool_call_ready",
        "tool_started",
        "tool_finished",
        "approval_requested",
        "approval_resolved",
        "tool_queued",
        "tool_scheduled",
        "tool_queue_cancelled",
        "usage_updated",
        "context_artifact_written",
        "context_compacted",
        "context_degraded",
        "turn_finished",
        "run_finished",
    ]
    call_id: str = ""
    call_index: int | None = None
    text: str = ""
    call: ToolCall | None = None
    resources: tuple[str, ...] = ()
    usage: Usage | None = None
    duration_seconds: float | None = None
    artifact_id: str = ""
    original_bytes: int | None = None
    prompt_tokens_before: int | None = None
    prompt_tokens_after: int | None = None
    outcome: str = ""
    error_type: str = ""
    attempt_id: str = ""
