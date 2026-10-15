"""验证大输出落盘、按需读取、原子回合压缩和预算故障。"""

import asyncio
import re
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from codepilot.context import (
    ContextArtifactStore,
    ContextLimits,
    ContextManager,
    ContextPreparationError,
    ContextSeed,
    ContextView,
    ModelSummarizer,
    RepositoryRule,
    SummaryResult,
    assemble_initial_context,
)
from codepilot.contracts import Message, ToolCall, ToolResult
from codepilot.contracts.stream import Usage
from codepilot.observability.trace import EventRecorder
from codepilot.providers.fake import FakeProvider
from codepilot.runtime import Runner
from codepilot.tools.base import ToolSpec


class ScriptedExecutor:
    """返回预设正文并公开一个最小只读工具声明。"""

    def __init__(self, content: str) -> None:
        """保存每次普通工具调用都要返回的正文。"""
        self.content = content
        self.specs = (
            ToolSpec(
                name="large_output",
                description="返回受控的大输出",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effect="read",
                resources=("fixture:read",),
                default_timeout=1.0,
                supports_cancel=True,
            ),
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        """用相同 call ID 返回构造时保存的正文。"""
        return ToolResult(call_id=call.call_id, content=self.content)


class RecordingSummarizer:
    """记录收到的完整旧回合，并返回固定结构化摘要。"""

    def __init__(self, *, fail: bool = False, content: str | None = None) -> None:
        """配置正常摘要、超长摘要或受控异常。"""
        self.fail = fail
        self.content = content or (
            "用户目标与约束：修复解析器且不能改接口。\n"
            "待办：检查边界输入。\n"
            "已修改内容：parser.py。\n"
            "测试与故障证据：test_parser 已通过。\n"
            "未解决风险：Windows 未验证。"
        )
        self.requests: list[tuple[Message, ...]] = []

    async def summarize(self, messages: Sequence[Message], target_tokens: int) -> SummaryResult:
        """保存输入快照；失败模式在产生任何摘要前抛错。"""
        self.requests.append(tuple(message.model_copy(deep=True) for message in messages))
        if self.fail:
            raise RuntimeError("受控摘要失败")
        return SummaryResult(
            content=self.content,
            usage=Usage(input_tokens=80, output_tokens=30, source="provider"),
        )


class RecordingProvider:
    """保存每次实际 prompt 视图，并按顺序返回脚本消息。"""

    def __init__(self, responses: Sequence[Message]) -> None:
        """保存回复脚本和空请求记录。"""
        self.responses = list(responses)
        self.requests: list[tuple[Message, ...]] = []

    async def complete(self, messages: Sequence[Message]) -> Message:
        """记录深拷贝 prompt 后消费一条回复。"""
        self.requests.append(tuple(message.model_copy(deep=True) for message in messages))
        return self.responses.pop(0)


class HangingProvider:
    """模拟已经开始但始终不能在摘要总时限内完成的 Provider。"""

    async def complete(self, messages: Sequence[Message]) -> Message:
        """等待足够长时间，使 ModelSummarizer 的墙钟时限先触发。"""
        await asyncio.sleep(60)
        return Message(role="assistant", content="不应完成")


def limits(**updates: int) -> ContextLimits:
    """构造适合小型测试触发阈值的合法上下文限制。"""
    values = {
        "max_prompt_tokens": 2_000,
        "response_reserve_tokens": 200,
        "compact_trigger_tokens": 1_200,
        "compact_target_tokens": 900,
        "recent_rounds": 1,
        "large_output_bytes": 1_024,
        "artifact_preview_chars": 400,
        "artifact_read_chars": 256,
    }
    values.update(updates)
    return ContextLimits.model_validate(values)


def tool_round(index: int, size: int = 260) -> tuple[Message, Message]:
    """构造调用与结果严格配对的一个完整历史回合。"""
    call_id = f"call-{index}"
    return (
        Message(
            role="assistant",
            content=f"准备检查第 {index} 项",
            tool_calls=(ToolCall(call_id=call_id, name="large_output", arguments={}),),
        ),
        Message(
            role="tool",
            tool_result=ToolResult(call_id=call_id, content=f"result-{index}:" + "x" * size),
        ),
    )


def test_initial_context_keeps_repository_rules_and_task_separate() -> None:
    """仓库规则使用 system 角色，任务使用 user 角色，不预加载无关代码文件。"""
    messages = assemble_initial_context(
        ContextSeed(
            repository_rules=(
                RepositoryRule(source="AGENTS.md", content="所有注释使用中文"),
                RepositoryRule(source="src/AGENTS.md", content="工具结果必须有界"),
            ),
            task_information="修复解析器并运行测试",
        )
    )
    assert [message.role for message in messages] == ["system", "system", "user"]
    assert "AGENTS.md" in messages[0].content
    assert "修复解析器" in messages[-1].content
    assert all(not message.tool_calls and message.tool_result is None for message in messages)


def test_large_output_is_persisted_previewed_and_read_by_range(tmp_path: Path) -> None:
    """100KB 输出不进入 prompt，且中段错误线索和完整读取入口都保留。"""

    async def scenario() -> None:
        """执行大输出工具，再通过 opaque artifact ID 读取错误附近范围。"""
        middle = "UNIQUE_MIDDLE_ERROR: parser exploded"
        raw = "H" * 55_000 + "\n" + middle + "\n" + "T" * 55_000
        store = ContextArtifactStore(tmp_path / "artifacts")
        manager = ContextManager(ScriptedExecutor(raw), store, limits=limits())
        compact = await manager.execute(
            ToolCall(call_id="huge-1", name="large_output", arguments={})
        )
        assert len(compact.content.encode()) < 4_000
        assert raw not in compact.content
        assert middle in compact.content
        assert "summary=共 3 行，检测到 1 条有界错误线索" in compact.content
        artifact_id = re.search(r"artifact_id=(artifact-[0-9a-f]{32})", compact.content)
        assert artifact_id is not None
        path = next((tmp_path / "artifacts").glob("*.txt"))
        assert path.read_text(encoding="utf-8") == raw
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        start = raw.index(middle)
        fragment = await manager.execute(
            ToolCall(
                call_id="read-1",
                name="read_context_artifact",
                arguments={
                    "artifact_id": artifact_id.group(1),
                    "start": start,
                    "max_chars": 128,
                },
            )
        )
        assert not fragment.is_error and middle in fragment.content
        assert "range=" in fragment.content and "has_more=true" in fragment.content
        missing = await manager.execute(
            ToolCall(
                call_id="read-2",
                name="read_context_artifact",
                arguments={"artifact_id": "artifact-" + "0" * 32},
            )
        )
        assert missing.is_error and "artifact_not_found" in missing.content
        store.cleanup()
        assert not path.exists()

    asyncio.run(scenario())


def test_artifact_write_failure_keeps_only_copy_of_raw_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘写入失败时返回原始 ToolResult，不能产生悬空引用。"""

    async def scenario() -> None:
        """注入 OSError 并检查明确降级记录和完整正文。"""
        raw = "only-copy:" + "z" * 2_000
        store = ContextArtifactStore(tmp_path / "artifacts")
        manager = ContextManager(ScriptedExecutor(raw), store, limits=limits())

        def fail_write(call_id: str, content: str) -> object:
            """模拟磁盘只读，不创建任何部分文件。"""
            raise OSError("read-only fixture")

        monkeypatch.setattr(store, "write", fail_write)
        result = await manager.execute(
            ToolCall(call_id="huge-2", name="large_output", arguments={})
        )
        assert result.content == raw
        assert manager.records[-1].kind == "artifact_write_failed"
        assert not list((tmp_path / "artifacts").iterdir())

    asyncio.run(scenario())


def test_runner_sends_reference_instead_of_full_tool_output(tmp_path: Path) -> None:
    """Runner 报告与下一次 Provider 请求只携带短引用，原文只在 artifact。"""

    async def scenario() -> None:
        """执行两轮 fake Provider 并检查第二轮实际请求。"""
        secret_tail = "RAW_PAYLOAD_SHOULD_NOT_ENTER_PROMPT"
        raw = "a" * 50_000 + secret_tail + "b" * 60_000
        store = ContextArtifactStore(tmp_path / "artifacts")
        manager = ContextManager(ScriptedExecutor(raw), store, limits=limits())
        provider = RecordingProvider(
            (
                Message(
                    role="assistant",
                    tool_calls=(ToolCall(call_id="huge-3", name="large_output", arguments={}),),
                ),
                Message(role="assistant", content="已根据引用完成分析"),
            )
        )
        recorder = EventRecorder()
        report = await Runner(provider, manager, context=manager, recorder=recorder).run(
            (Message(role="user", content="检查大输出"),)
        )
        second_prompt = "\n".join(message.model_dump_json() for message in provider.requests[1])
        assert report.reason == "completed"
        assert "工具输出已落盘" in second_prompt
        assert secret_tail not in second_prompt
        assert next((tmp_path / "artifacts").glob("*.txt")).read_text().endswith("b" * 60_000)
        assert any(event.kind == "context_artifact_written" for event in recorder.events)

    asyncio.run(scenario())


def test_compaction_preserves_prefix_recent_pair_and_records_cost(tmp_path: Path) -> None:
    """压缩只替换旧完整回合，并保留初始约束、近期调用结果与摘要用量。"""

    async def scenario() -> None:
        """手动压缩四个回合并核对摘要输入和最终消息配对。"""
        summarizer = RecordingSummarizer()
        manager = ContextManager(
            ScriptedExecutor("small"),
            ContextArtifactStore(tmp_path / "artifacts"),
            limits=limits(compact_trigger_tokens=1_500, compact_target_tokens=1_000),
            summarizer=summarizer,
        )
        rounds = [tool_round(index) for index in range(4)]
        history = (
            Message(role="system", content="必须保留接口兼容"),
            Message(role="user", content="修复解析器并运行测试"),
            *(message for group in rounds for message in group),
        )
        view = await manager.compact(history)
        assert isinstance(view, ContextView) and view.compacted
        assert view.messages[0:2] == history[0:2]
        assert view.messages[-2:] == rounds[-1]
        assert view.messages[2].role == "system"
        assert "用户目标与约束" in view.messages[2].content
        # 摘要器得到前三个完整 assistant+tool 组，没有拆开任何调用与结果。
        assert len(summarizer.requests[0]) == 6
        assert [message.role for message in summarizer.requests[0]] == [
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
            "tool",
        ]
        record = manager.records[-1]
        assert record.kind == "compacted"
        assert record.tokens_before is not None and record.tokens_after is not None
        assert record.tokens_after < record.tokens_before
        assert record.usage is not None and record.usage.input_tokens == 80

    asyncio.run(scenario())


def test_runner_automatically_compacts_only_provider_view(tmp_path: Path) -> None:
    """多轮历史越过阈值后自动压缩，但 RunReport 仍保存规范的逐条历史。"""

    async def scenario() -> None:
        """运行三个工具回合，并检查后续请求出现摘要而报告没有摘要消息。"""
        summarizer = RecordingSummarizer()
        manager = ContextManager(
            ScriptedExecutor("runtime-result:" + "r" * 300),
            ContextArtifactStore(tmp_path / "artifacts"),
            limits=limits(
                max_prompt_tokens=700,
                response_reserve_tokens=100,
                compact_trigger_tokens=350,
                compact_target_tokens=310,
            ),
            summarizer=summarizer,
        )
        provider = RecordingProvider(
            (
                *(
                    Message(
                        role="assistant",
                        content=f"检查回合 {index}",
                        tool_calls=(
                            ToolCall(
                                call_id=f"auto-{index}",
                                name="large_output",
                                arguments={},
                            ),
                        ),
                    )
                    for index in range(3)
                ),
                Message(role="assistant", content="自动压缩后完成"),
            )
        )
        recorder = EventRecorder()
        report = await Runner(provider, manager, context=manager, recorder=recorder).run(
            (Message(role="user", content="保持接口并完成三轮检查"),)
        )
        assert report.reason == "completed"
        assert summarizer.requests
        assert any(
            any(message.content.startswith("[历史压缩摘要]") for message in request)
            for request in provider.requests
        )
        assert not any(message.content.startswith("[历史压缩摘要]") for message in report.messages)
        assert any(record.kind == "compacted" for record in manager.records)
        assert any(event.kind == "context_compacted" for event in recorder.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["summary_error", "summary_too_large"])
def test_compaction_failure_stops_clearly(tmp_path: Path, failure: str) -> None:
    """摘要异常或摘要后仍超目标时返回明确错误，不发送超预算 prompt。"""

    async def scenario() -> None:
        """选择故障摘要器并检查脱敏失败记录。"""
        summarizer = RecordingSummarizer(
            fail=failure == "summary_error",
            content="oversized-summary" + "q" * 2_000,
        )
        manager = ContextManager(
            ScriptedExecutor("small"),
            ContextArtifactStore(tmp_path / "artifacts"),
            limits=limits(compact_trigger_tokens=1_200, compact_target_tokens=700),
            summarizer=summarizer,
        )
        history = (
            Message(role="user", content="保留这个唯一目标"),
            *(message for index in range(4) for message in tool_round(index)),
        )
        with pytest.raises(ContextPreparationError) as captured:
            await manager.compact(history)
        expected = "summary_failed" if failure == "summary_error" else "summary_above_target"
        assert expected in str(captured.value)
        assert manager.records[-1].kind == "compaction_failed"
        assert "受控摘要失败" not in manager.records[-1].model_dump_json()

    asyncio.run(scenario())


def test_single_oversized_message_stops_runner_before_provider(tmp_path: Path) -> None:
    """唯一用户消息超过输入硬预算时，Runner 以 context_limit 且零请求停止。"""

    async def scenario() -> None:
        """用不可拆分初始约束触发硬停止，并验证 fake 回复没有被消费。"""
        manager = ContextManager(
            ScriptedExecutor("small"),
            ContextArtifactStore(tmp_path / "artifacts"),
            limits=limits(
                max_prompt_tokens=800,
                response_reserve_tokens=100,
                compact_trigger_tokens=600,
                compact_target_tokens=400,
            ),
        )
        provider = FakeProvider((Message(role="assistant", content="不应被请求"),))
        recorder = EventRecorder()
        report = await Runner(provider, manager, context=manager, recorder=recorder).run(
            (Message(role="user", content="唯一约束：" + "长" * 1_000),)
        )
        assert report.reason == "context_limit"
        assert report.steps == 0 and report.tool_calls == 0
        assert report.observations[-1].detail == "single_message_too_large"
        assert any(event.kind == "context_degraded" for event in recorder.events)
        assert not any(event.kind == "model_requested" for event in recorder.events)

    asyncio.run(scenario())


def test_context_limit_order_is_validated() -> None:
    """拒绝没有输入空间或目标值不低于触发值的预算配置。"""
    with pytest.raises(ValueError):
        ContextLimits(max_prompt_tokens=128, response_reserve_tokens=100)
    with pytest.raises(ValueError):
        ContextLimits(compact_target_tokens=1_000, compact_trigger_tokens=1_000)


def test_model_summarizer_has_whole_request_timeout() -> None:
    """独立摘要调用即使不经过 Runner，也必须受整次墙钟时限约束。"""

    async def scenario() -> None:
        """运行挂起 Provider，并确认超时取消发生在短测试期限内。"""
        summarizer = ModelSummarizer(HangingProvider(), timeout_seconds=0.01)
        with pytest.raises(TimeoutError):
            await summarizer.summarize((Message(role="user", content="旧历史"),), 100)

    asyncio.run(scenario())


def test_invalid_tool_pairing_is_observable_and_never_summarized(tmp_path: Path) -> None:
    """工具结果 ID 与调用不一致时明确停止，并且不把损坏历史交给摘要器。"""

    async def scenario() -> None:
        """构造错误配对，检查失败记录以及摘要器零调用。"""
        summarizer = RecordingSummarizer()
        manager = ContextManager(
            ScriptedExecutor("small"),
            ContextArtifactStore(tmp_path / "artifacts"),
            limits=limits(compact_trigger_tokens=100, compact_target_tokens=80),
            summarizer=summarizer,
        )
        history = (
            Message(role="user", content="保留任务目标"),
            Message(
                role="assistant",
                tool_calls=(ToolCall(call_id="expected", name="large_output", arguments={}),),
            ),
            Message(
                role="tool",
                tool_result=ToolResult(call_id="unexpected", content="错误配对"),
            ),
        )
        with pytest.raises(ContextPreparationError, match="history_pairing_invalid"):
            await manager.compact(history)
        assert not summarizer.requests
        assert manager.records[-1].kind == "compaction_failed"
        assert manager.records[-1].outcome == "history_pairing_invalid"

    asyncio.run(scenario())
