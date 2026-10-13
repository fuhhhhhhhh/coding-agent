"""验证核心数据契约的校验、序列化和标识符约束。"""

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from codepilot.contracts import AgentEvent, Message, RunState, ToolCall, ToolResult


def test_message_event_and_result_json_roundtrip() -> None:
    """往返验证各种消息载荷，并保留调用与结果的配对数据。"""
    call = ToolCall(call_id="c1", name="read_file", arguments={"path": "示例.py", "limit": 10})
    messages = (
        Message(role="user", content="Read the file"),
        Message(role="assistant", content="Inspecting", tool_calls=(call,)),
        Message(
            role="tool", tool_result=ToolResult(call_id="c1", content="not found", is_error=True)
        ),
    )
    for message in messages:
        event = AgentEvent(session_id="s1", run_id="r1", turn_id="t1", sequence=1, message=message)
        encoded = event.model_dump_json()
        assert json.loads(encoded)["message"]["role"] == message.role
        assert AgentEvent.model_validate_json(encoded) == event
    result = messages[2].tool_result
    assert result is not None
    assert messages[1].tool_calls[0].call_id == result.call_id


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "user", "tool_calls": ({"call_id": "c", "name": "read", "arguments": {}},)},
        {"role": "tool", "content": "unpaired result"},
        {"role": "assistant", "tool_result": {"call_id": "c", "content": "x"}},
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": "ok", "sdk_response": {}},
        {"role": "unknown", "content": "ok"},
        {"role": "tool", "content": "duplicate", "tool_result": {"call_id": "c", "content": "x"}},
    ],
)
def test_invalid_message_payloads_are_rejected(payload: dict[str, Any]) -> None:
    """拒绝违反内部消息契约的角色与载荷组合。"""
    with pytest.raises(ValidationError):
        Message.model_validate(payload)


@pytest.mark.parametrize(
    "arguments", ['{"path":', ["README.md"], {"x": object()}, {"x": float("inf")}]
)
def test_tool_arguments_must_be_json_object(arguments: Any) -> None:
    """通用工具参数只接受完整且数值有限的 JSON 对象。"""
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"call_id": "c1", "name": "read", "arguments": arguments})


def test_duplicate_calls_are_rejected() -> None:
    """拒绝单条 assistant 回复中的重复调用 ID。"""
    call = ToolCall(call_id="c1", name="read", arguments={})
    with pytest.raises(ValidationError, match="unique"):
        Message(role="assistant", tool_calls=(call, call))


@pytest.mark.parametrize("sequence", [0, -1, "1", True])
def test_invalid_event_sequence_is_rejected(sequence: Any) -> None:
    """事件序号必须是严格类型的正整数。"""
    with pytest.raises(ValidationError):
        AgentEvent.model_validate(
            {
                "session_id": "s",
                "run_id": "r",
                "turn_id": "t",
                "sequence": sequence,
                "message": {"role": "assistant", "content": "ok"},
            }
        )


def test_run_state_serializes_as_portable_value() -> None:
    """通过 JSON 字符串表示往返验证每个运行状态。"""
    adapter = TypeAdapter(RunState)
    for state in RunState:
        assert adapter.validate_json(adapter.dump_json(state)) is state


@pytest.mark.parametrize("name", ["read_file", "_inspect2"])
def test_tool_name_accepts_identifier_style_names(name: str) -> None:
    """接受 ASCII 标识符形式的工具名，同时不收窄调用 ID。"""
    assert ToolCall(call_id="call-1", name=name, arguments={}).call_id == "call-1"


@pytest.mark.parametrize("name", ["read-file", "2read", "read file", "", "读取", "read\n"])
def test_tool_name_rejects_invalid_names(name: str) -> None:
    """拒绝无法作为稳定内部标识符的工具名。"""
    with pytest.raises(ValidationError):
        ToolCall(call_id="call-1", name=name, arguments={})
