"""验证工具注册、Schema、参数校验和统一失败结果。"""

import asyncio

import pytest

from codepilot.contracts import ToolCall
from codepilot.contracts.models import Contract
from codepilot.tools import Tool, ToolFailure, ToolRegistry


class EchoArguments(Contract):
    """定义测试工具唯一允许的严格文本参数。"""

    text: str


class EchoTool(Tool[EchoArguments]):
    """记录实际执行次数并回显经过校验的文本。"""

    name = "echo"
    description = "回显文本"
    effect = "read"
    resources = ("memory",)
    arguments_model = EchoArguments

    def __init__(self) -> None:
        """初始化执行计数，供测试确认校验失败没有副作用。"""
        self.runs = 0

    async def run(self, arguments: EchoArguments) -> str:
        """增加执行计数并返回文本。"""
        self.runs += 1
        return arguments.text


class FailingTool(EchoTool):
    """产生预期业务失败，验证错误能进入统一结果。"""

    name = "fail"

    async def run(self, arguments: EchoArguments) -> str:
        """用稳定错误文本拒绝本次调用。"""
        raise ToolFailure("controlled failure")


class WaitingTool(EchoTool):
    """持续等待，用于验证工具级超时会取消协程。"""

    name = "wait"
    default_timeout = 0.02

    def __init__(self) -> None:
        """创建清理信号以证明取消已到达工具。"""
        super().__init__()
        self.cleaned = asyncio.Event()

    async def run(self, arguments: EchoArguments) -> str:
        """等待取消，并在 finally 中记录清理完成。"""
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaned.set()
        return "unreachable"


def test_spec_comes_from_the_execution_model() -> None:
    """确认公开 Schema 和实际校验共享同一个参数模型。"""
    tool = EchoTool()
    spec = tool.spec
    assert spec.name == "echo" and spec.effect == "read"
    assert spec.input_schema["additionalProperties"] is False
    assert spec.input_schema["required"] == ["text"]


def test_registry_rejects_duplicates_and_preserves_order() -> None:
    """拒绝同名覆盖，并按注册顺序公开声明。"""
    first = EchoTool()
    registry = ToolRegistry((first, FailingTool()))
    assert [spec.name for spec in registry.specs] == ["echo", "fail"]
    with pytest.raises(ValueError, match="duplicate tool name"):
        registry.register(EchoTool())


def test_invalid_and_unknown_calls_do_not_execute() -> None:
    """未知工具、缺字段和未知字段都产生错误结果且没有副作用。"""
    tool = EchoTool()
    registry = ToolRegistry((tool,))
    calls = (
        ToolCall(call_id="unknown", name="missing", arguments={}),
        ToolCall(call_id="missing", name="echo", arguments={}),
        ToolCall(call_id="extra", name="echo", arguments={"text": "x", "extra": 1}),
    )
    results = [asyncio.run(registry.execute(call)) for call in calls]
    assert all(result.is_error for result in results)
    assert results[0].content.startswith("unknown_tool:")
    assert all(result.call_id == call.call_id for result, call in zip(results, calls, strict=True))
    assert tool.runs == 0


def test_success_business_failure_and_timeout_are_distinct() -> None:
    """区分成功、预期工具失败和工具级超时三类结果。"""

    async def scenario() -> None:
        """执行三类工具并检查超时协程已经完成清理。"""
        waiting = WaitingTool()
        registry = ToolRegistry((EchoTool(), FailingTool(), waiting))
        success = await registry.execute(
            ToolCall(call_id="ok", name="echo", arguments={"text": "你好"})
        )
        failure = await registry.execute(
            ToolCall(call_id="bad", name="fail", arguments={"text": "x"})
        )
        timeout = await registry.execute(
            ToolCall(call_id="slow", name="wait", arguments={"text": "x"})
        )
        assert success.content == "你好" and not success.is_error
        assert failure.is_error and failure.content == "tool_error: controlled failure"
        assert timeout.is_error and timeout.content.startswith("tool_timeout:")
        assert waiting.cleaned.is_set()

    asyncio.run(scenario())
