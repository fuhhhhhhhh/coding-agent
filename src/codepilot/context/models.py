"""定义大输出引用、上下文预算、压缩结果和可观察记录。"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from codepilot.contracts import Message
from codepilot.contracts.models import Contract, Identifier
from codepilot.contracts.stream import Usage


class ContextLimits(Contract):
    """集中保存 prompt 预算、大输出阈值和近期回合保留策略。"""

    max_prompt_tokens: Annotated[int, Field(ge=128)] = 32_000
    response_reserve_tokens: Annotated[int, Field(ge=1)] = 4_096
    compact_trigger_tokens: Annotated[int, Field(ge=64)] = 24_000
    compact_target_tokens: Annotated[int, Field(ge=32)] = 16_000
    recent_rounds: Annotated[int, Field(ge=1)] = 4
    large_output_bytes: Annotated[int, Field(ge=1_024)] = 65_536
    artifact_preview_chars: Annotated[int, Field(ge=128, le=16_384)] = 2_000
    artifact_read_chars: Annotated[int, Field(ge=128, le=65_536)] = 8_000

    @model_validator(mode="after")
    def check_budget_order(self) -> Self:
        """保证预留、触发值和目标值形成可执行的递减预算。"""
        usable = self.max_prompt_tokens - self.response_reserve_tokens
        # 回答预留必须给输入留下空间，否则任何历史都会在请求前失败。
        if usable < 64:
            raise ValueError("response reserve leaves no usable prompt budget")
        # 自动压缩必须在真正的硬上限前发生，压缩目标还要低于触发阈值。
        if self.compact_trigger_tokens > usable:
            raise ValueError("compact trigger exceeds usable prompt budget")
        if self.compact_target_tokens >= self.compact_trigger_tokens:
            raise ValueError("compact target must be below compact trigger")
        return self

    @property
    def usable_prompt_tokens(self) -> int:
        """返回扣除回答预留后允许发送给 Provider 的最大输入估算。"""
        return self.max_prompt_tokens - self.response_reserve_tokens


class ArtifactReference(Contract):
    """用不暴露宿主路径的 ID 描述一份原始工具输出。"""

    artifact_id: Identifier
    call_id: Identifier
    characters: Annotated[int, Field(ge=0)]
    bytes: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ArtifactSlice(Contract):
    """返回原始输出的一个字符范围，并说明是否还有后续内容。"""

    artifact_id: Identifier
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=0)]
    total_characters: Annotated[int, Field(ge=0)]
    content: str
    has_more: bool


class SummaryResult(Contract):
    """保存摘要正文和摘要调用本身产生的 Token 用量。"""

    content: Annotated[str, Field(min_length=1)]
    usage: Usage = Usage()


class ContextView(Contract):
    """保存一次 Provider 请求实际看到的消息视图和预算估算。"""

    messages: tuple[Message, ...]
    estimated_tokens: Annotated[int, Field(ge=0)]
    compacted: bool = False


class ContextRecord(Contract):
    """记录落盘、读取、压缩和降级事实，不保存原始正文。"""

    sequence: Annotated[int, Field(ge=1)]
    kind: Literal[
        "artifact_written",
        "artifact_read",
        "artifact_write_failed",
        "compacted",
        "compaction_failed",
    ]
    artifact_id: str = ""
    call_id: str = ""
    outcome: str = ""
    original_bytes: int | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    usage: Usage | None = None
