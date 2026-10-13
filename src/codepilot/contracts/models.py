"""经过校验的 M01 非流式数据；不包含执行或持久化逻辑。"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Identifier = Annotated[str, Field(min_length=1, pattern=r"\S")]


class Contract(BaseModel):
    """为所有内部契约提供严格、不可变且不接受未知字段的共同基类。"""

    # 内部边界不允许静默类型转换或丢弃未知字段。
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class ToolCall(Contract):
    """描述模型请求执行的一次工具调用及其 JSON 参数。"""

    call_id: Identifier
    name: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    # 这里只接受完整 JSON 对象；工具专用 Schema 校验属于 M03。
    arguments: dict[str, JsonValue]


class ToolResult(Contract):
    """描述工具调用的可配对结果；业务失败由 is_error 表示。"""

    call_id: Identifier
    content: str
    is_error: bool = False


class Message(Contract):
    """统一表示 system、user、assistant 和 tool 四种内部消息。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None

    @model_validator(mode="after")
    def check_role_payload(self) -> Self:
        """根据消息角色校验允许出现的载荷字段。"""
        # 工具请求只能由 assistant 产生；其他角色携带请求会混淆状态所有权。
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool calls")
        # tool 消息只携带一个结构化结果，不能再附加第二份自由文本。
        if self.role == "tool":
            if self.tool_result is None or self.content:
                raise ValueError("tool messages require tool_result and no separate content")
        elif self.tool_result is not None:
            raise ValueError("only tool messages may contain tool_result")
        # 空消息不提供任何信息，并会让终止语义变得含糊。
        if not self.content.strip() and not self.tool_calls and self.tool_result is None:
            raise ValueError("message must contain text, tool calls, or a tool result")
        ids = [call.call_id for call in self.tool_calls]
        # 同一回复中的重复 ID 会导致后续结果无法唯一配对。
        if len(ids) != len(set(ids)):
            raise ValueError("tool call IDs must be unique within a message")
        return self


class RunState(StrEnum):
    """运行状态词汇；状态转换和停止逻辑由 Runner 管理。"""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentEvent(Contract):
    """整条消息级观察记录；它不是流式增量，也不是工具执行记录。"""

    session_id: Identifier
    run_id: Identifier
    turn_id: Identifier
    sequence: Annotated[int, Field(ge=1)]
    kind: Literal["message_received"] = "message_received"
    message: Message
