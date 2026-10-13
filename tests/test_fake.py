"""验证有限 FakeProvider 的顺序消费、隔离和失败语义。"""

import asyncio

import pytest

from codepilot.contracts import AgentEvent, Message, ToolCall
from codepilot.demo import collect_demo_events
from codepilot.providers.fake import FakeProvider, FakeProviderExhausted


def test_demo_returns_text_and_tool_request_and_roundtrips() -> None:
    """验证默认脚本返回一条文本和一个可序列化工具请求。"""
    events = asyncio.run(collect_demo_events(FakeProvider()))
    assert [event.sequence for event in events] == [1, 2]
    assert events[0].message.content == "M01 fake: ready to inspect a file."
    assert events[1].message.tool_calls[0].name == "read_file"
    assert events[1].message.tool_calls[0].arguments == {"path": "README.md"}
    for event in events:
        assert AgentEvent.model_validate_json(event.model_dump_json()) == event


def test_exhaustion_is_explicit_and_does_not_repeat() -> None:
    """确保 fake 耗尽后持续报错，不会重放最后一条回复。"""

    async def scenario() -> None:
        """消费完整脚本，并检查之后两次调用以同样方式失败。"""
        provider = FakeProvider()
        await collect_demo_events(provider)
        for _ in range(2):
            with pytest.raises(FakeProviderExhausted, match="exhausted"):
                await provider.complete((Message(role="user", content="again"),))

    asyncio.run(scenario())


def test_empty_request_does_not_consume_response() -> None:
    """拒绝空历史，同时保留下一条脚本回复。"""

    async def scenario() -> None:
        """向同一 fake 先提交非法请求，再提交合法请求。"""
        provider = FakeProvider()
        with pytest.raises(ValueError, match="at least one"):
            await provider.complete(())
        assert (await provider.complete((Message(role="user", content="hi"),))).content

    asyncio.run(scenario())


def test_empty_script_is_not_replaced_with_default_script() -> None:
    """区分显式空脚本与省略脚本参数。"""
    with pytest.raises(FakeProviderExhausted):
        asyncio.run(FakeProvider(()).complete((Message(role="user", content="hi"),)))


def test_script_is_snapshotted_and_instances_are_independent() -> None:
    """防止调用方修改和 Provider 实例状态影响未来回复。"""
    call = ToolCall(call_id="c1", name="read", arguments={"path": "original"})
    provider = FakeProvider((Message(role="assistant", tool_calls=(call,)),))
    call.arguments["path"] = "changed"
    reply = asyncio.run(provider.complete((Message(role="user", content="hi"),)))
    assert reply.tool_calls[0].arguments["path"] == "original"
    assert asyncio.run(collect_demo_events(FakeProvider())) == asyncio.run(
        collect_demo_events(FakeProvider())
    )


def test_fake_rejects_non_assistant_response() -> None:
    """拒绝包含 Provider 无权返回角色的脚本。"""
    with pytest.raises(ValueError, match="assistant"):
        FakeProvider((Message(role="user", content="wrong"),))
