"""覆盖 M02 Runtime 的正常闭环、资源限制和关键故障分支。"""

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.contracts import Message, RunState, ToolCall, ToolResult
from codepilot.contracts.run import RunLimits, RunObservation, RunReport
from codepilot.fixtures.m02 import FIXED_SOURCE, FixtureExecutor, prepare_fixture, repair_script
from codepilot.providers.fake import FakeProvider
from codepilot.runtime import Runner

INITIAL = (Message(role="user", content="Perform the fixture task."),)
FINAL = Message(role="assistant", content="Done.")
FIXTURE_SOURCE = Path(__file__).resolve().parents[1] / "examples" / "fixture_repo"


def request(call_id: str = "c1", name: str = "read_file", **arguments: str) -> Message:
    """构造只含一个调用的 assistant 消息，简化 Runtime 场景。"""
    return Message(
        role="assistant",
        tool_calls=(ToolCall(call_id=call_id, name=name, arguments=dict(arguments)),),
    )


class RecordingExecutor:
    """记录收到的调用，并按测试参数模拟成功、失败或契约错误。"""

    def __init__(self, *, fail: bool = False, raises: bool = False, mismatch: bool = False) -> None:
        """配置会记录每次调用尝试的确定性执行器。"""
        self.calls: list[ToolCall] = []
        self.fail = fail
        self.raises = raises
        self.mismatch = mismatch

    async def execute(self, call: ToolCall) -> ToolResult:
        """记录调用，然后按配置返回结果或抛出受控异常。"""
        self.calls.append(call)
        # 抛出异常用于验证 Runner 将异常转换为结果的逻辑。
        if self.raises:
            raise OSError("controlled exception text is not exposed")
        return ToolResult(
            call_id="wrong" if self.mismatch else call.call_id,
            content="controlled failure" if self.fail else "ok",
            is_error=self.fail,
        )


class RecordingProvider(FakeProvider):
    """在 FakeProvider 之外保留每次请求历史的深拷贝。"""

    def __init__(self, responses: Sequence[Message]) -> None:
        """初始化 fake 回复脚本和独立的请求历史日志。"""
        super().__init__(responses)
        self.requests: list[tuple[Message, ...]] = []

    async def complete(self, messages: Sequence[Message]) -> Message:
        """在委托给有限 fake 脚本前，对每次请求进行快照。"""
        self.requests.append(tuple(message.model_copy(deep=True) for message in messages))
        return await super().complete(messages)


def assert_terminal(report: RunReport) -> None:
    """断言序号连续、只有一个最终观察，并可完成 JSON 往返。"""
    assert [event.sequence for event in report.observations] == list(
        range(1, len(report.observations) + 1)
    )
    assert sum(event.kind == "run_stopped" for event in report.observations) == 1
    assert report.observations[-1].state == report.state
    assert report.observations[-1].reason == report.reason
    assert RunReport.model_validate_json(report.model_dump_json()) == report
    for event in report.observations:
        assert RunObservation.model_validate_json(event.model_dump_json()) == event


def test_fixture_repair_reads_writes_and_runs_real_tests(tmp_path: Path) -> None:
    """修复一次性仓库，并验证文件、测试与调用配对证据。"""

    async def scenario() -> None:
        """在 pytest 临时目录中运行异步 fixture 流程。"""
        root = prepare_fixture(FIXTURE_SOURCE, tmp_path / "repo")
        executor = FixtureExecutor(root)
        baseline = await executor.execute(
            ToolCall(call_id="baseline", name="run_tests", arguments={})
        )
        assert baseline.is_error and "FAIL" in baseline.content
        provider = RecordingProvider(repair_script())
        report = await Runner(provider, executor).run(INITIAL)
        assert report.state == RunState.COMPLETED
        assert (report.steps, report.tool_calls) == (4, 3)
        assert (root / "calculator.py").read_text(encoding="utf-8") == FIXED_SOURCE
        assert "return a - b" in (FIXTURE_SOURCE / "calculator.py").read_text(encoding="utf-8")
        final_history = provider.requests[-1]
        calls = [call.call_id for message in final_history for call in message.tool_calls]
        results = [message.tool_result for message in final_history if message.tool_result]
        assert calls == [result.call_id for result in results] == ["read-1", "write-1", "test-1"]
        assert all(not result.is_error for result in results)
        assert "exit_code=0" in results[-1].content and "OK" in results[-1].content
        assert_terminal(report)

    asyncio.run(scenario())


def test_no_calls_completes_without_executor_and_runner_cannot_be_reused() -> None:
    """纯文本回复时无工具完成，并限制每个 Runner 实例只运行一次。"""

    async def scenario() -> None:
        """执行无调用回复，然后尝试被禁止的 Runner 重用。"""
        executor = RecordingExecutor()
        runner = Runner(FakeProvider((FINAL,)), executor)
        report = await runner.run(INITIAL)
        assert report.reason == "completed" and not executor.calls
        with pytest.raises(RuntimeError, match="single-use"):
            await runner.run(INITIAL)
        assert_terminal(report)

    asyncio.run(scenario())


def test_multiple_calls_remain_sequential_and_paired() -> None:
    """保留单条回复中多个调用的模型顺序和 ID 配对。"""

    async def scenario() -> None:
        """执行两个调用，并检查 Provider 随后收到的请求历史。"""
        calls = (
            ToolCall(call_id="a", name="read", arguments={}),
            ToolCall(call_id="b", name="read", arguments={}),
        )
        provider = RecordingProvider((Message(role="assistant", tool_calls=calls), FINAL))
        executor = RecordingExecutor()
        report = await Runner(provider, executor).run(INITIAL)
        assert [call.call_id for call in executor.calls] == ["a", "b"]
        assert [
            message.tool_result.call_id for message in provider.requests[-1] if message.tool_result
        ] == ["a", "b"]
        assert_terminal(report)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,arguments,error",
    [
        ("missing_tool", {}, "unknown_tool"),
        ("write_file", {"path": "calculator.py"}, "invalid_arguments"),
        ("write_file", {"path": "../calculator.py", "content": "bad"}, "invalid_arguments"),
    ],
)
def test_tool_errors_are_returned_to_model_without_writes(
    tmp_path: Path, name: str, arguments: dict[str, str], error: str
) -> None:
    """将未知或非法请求作为错误返回，同时保持 fixture 不变。"""

    async def scenario() -> None:
        """在非法调用前后捕获文件字节，并检查模型反馈。"""
        root = prepare_fixture(FIXTURE_SOURCE, tmp_path / "repo")
        before = (root / "calculator.py").read_bytes()
        provider = RecordingProvider((request(name=name, **arguments), FINAL))
        report = await Runner(provider, FixtureExecutor(root)).run(INITIAL)
        result = provider.requests[-1][-1].tool_result
        assert result is not None and result.is_error and error in result.content
        assert result.call_id == "c1"
        assert (root / "calculator.py").read_bytes() == before
        assert report.reason == "completed"
        assert_terminal(report)

    asyncio.run(scenario())


def test_executor_exception_becomes_error_feedback() -> None:
    """将普通执行器异常转换为已配对、模型可见的结果。"""
    provider = RecordingProvider((request(), FINAL))
    report = asyncio.run(Runner(provider, RecordingExecutor(raises=True)).run(INITIAL))
    result = provider.requests[-1][-1].tool_result
    assert result is not None and result.is_error and result.content == "tool_exception: OSError"
    assert report.state == RunState.COMPLETED
    assert_terminal(report)


def test_successful_repetition_hits_step_limit() -> None:
    """用硬性步数上限终止持续成功但永不结束的调用模式。"""
    executor = RecordingExecutor()
    provider = RecordingProvider(tuple(request(call_id=f"c{i}") for i in range(5)))
    report = asyncio.run(Runner(provider, executor, RunLimits(max_steps=2)).run(INITIAL))
    assert report.reason == "step_limit" and report.steps == 2
    assert len(provider.requests) == len(executor.calls) == 2
    assert report.state == RunState.FAILED
    assert_terminal(report)


def test_failed_repetition_ignores_ids_and_argument_key_order() -> None:
    """即使 ID 和映射键顺序变化，也能识别语义相同的重复失败。"""
    responses = (
        request("c1", path="x", mode="r"),
        request("c2", mode="r", path="x"),
        request("c3", path="x", mode="r"),
        request("c4", path="x", mode="r"),
    )
    executor = RecordingExecutor(fail=True)
    report = asyncio.run(Runner(FakeProvider(responses), executor).run(INITIAL))
    assert report.reason == "repeated_failure" and len(executor.calls) == 3
    assert_terminal(report)


def test_different_failure_signature_resets_streak() -> None:
    """避免把不连续的失败签名当作同一次连续失败。"""
    responses = (request("c1", path="x"), request("c2", path="y"), request("c3", path="x"), FINAL)
    report = asyncio.run(
        Runner(
            FakeProvider(responses),
            RecordingExecutor(fail=True),
            RunLimits(max_consecutive_failures=2),
        ).run(INITIAL)
    )
    assert report.reason == "completed"
    assert_terminal(report)


def test_batch_budget_rejects_before_first_execution() -> None:
    """在首次执行前整体拒绝超出额度的调用批次。"""
    calls = (
        ToolCall(call_id="a", name="read", arguments={}),
        ToolCall(call_id="b", name="read", arguments={}),
    )
    executor = RecordingExecutor()
    report = asyncio.run(
        Runner(
            FakeProvider((Message(role="assistant", tool_calls=calls),)),
            executor,
            RunLimits(max_tool_calls=1),
        ).run(INITIAL)
    )
    assert report.reason == "tool_budget" and report.tool_calls == 0 and not executor.calls
    assert_terminal(report)


def test_provider_error_is_terminal_and_not_retried() -> None:
    """Provider 首次失败后停止，不执行工具，也不重试。"""
    executor = RecordingExecutor()
    report = asyncio.run(Runner(FakeProvider(()), executor).run(INITIAL))
    assert report.reason == "provider_error" and report.steps == 1 and not executor.calls
    assert report.detail == "FakeProviderExhausted"
    assert report.observations[-1].detail == "FakeProviderExhausted"
    assert_terminal(report)


def test_reused_call_id_is_rejected_before_reexecution() -> None:
    """在第二次副作用开始前拒绝运行内重复调用 ID。"""
    executor = RecordingExecutor()
    report = asyncio.run(Runner(FakeProvider((request(), request())), executor).run(INITIAL))
    assert report.reason == "contract_error" and len(executor.calls) == 1
    assert_terminal(report)


def test_mismatched_result_id_stops_without_model_feedback() -> None:
    """执行结果 ID 不匹配时停止，且不追加无法配对的工具消息。"""
    provider = RecordingProvider((request(), FINAL))
    report = asyncio.run(Runner(provider, RecordingExecutor(mismatch=True)).run(INITIAL))
    assert report.reason == "contract_error" and len(provider.requests) == 1
    assert all(message.role != "tool" for message in report.messages)
    assert_terminal(report)


@pytest.mark.parametrize("operation", ["provider", "tool"])
def test_deadline_cancels_current_awaitable_and_stops(operation: str) -> None:
    """在截止时间取消挂起的 Provider 或工具，并只产生一条终态记录。"""

    async def scenario() -> None:
        """选择一个挂起组件，并确认其 finally 分支收到取消。"""
        cleaned = asyncio.Event()
        executor = RecordingExecutor()

        class WaitingProvider:
            """挂起 Provider 请求，供测试确认超时会取消当前 await。"""

            async def complete(self, messages: Sequence[Message]) -> Message:
                """阻塞直到取消，然后在 ``finally`` 中发出清理信号。"""
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return FINAL

        class WaitingExecutor:
            """挂起工具执行，供测试确认超时会取消当前 await。"""

            async def execute(self, call: ToolCall) -> ToolResult:
                """阻塞一个工具直到取消，然后发出清理信号。"""
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return ToolResult(call_id=call.call_id, content="unreachable")

        runner = (
            Runner(WaitingProvider(), executor, RunLimits(timeout_seconds=0.03))
            if operation == "provider"
            else Runner(
                FakeProvider((request(), request("c2"))),
                WaitingExecutor(),
                RunLimits(timeout_seconds=0.03),
            )
        )
        report = await asyncio.wait_for(runner.run(INITIAL), timeout=2)
        assert report.reason == "time_limit" and cleaned.is_set()
        assert report.steps == 1
        assert report.tool_calls == (0 if operation == "provider" else 1)
        assert_terminal(report)

    asyncio.run(scenario())


def test_external_cancel_propagates_and_preserves_one_terminal() -> None:
    """记录 cancelled 终态报告后，继续传播调用方取消。"""

    async def scenario() -> None:
        """在工具开始后取消，使清理和调用尝试计数可观察。"""
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        class WaitingExecutor:
            """暴露进入和清理信号，供测试确认外部取消完整传播。"""

            async def execute(self, call: ToolCall) -> ToolResult:
                """发出进入信号、等待取消，并在 ``finally`` 中标记清理。"""
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return ToolResult(call_id=call.call_id, content="unreachable")

        runner = Runner(FakeProvider((request(), request("c2"))), WaitingExecutor())
        task = asyncio.create_task(runner.run(INITIAL))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and runner.report is not None
        assert runner.report.reason == "cancelled" and runner.report.tool_calls == 1
        assert_terminal(runner.report)

    asyncio.run(scenario())


def test_fixture_test_process_is_reaped_on_timeout(tmp_path: Path) -> None:
    """运行超时时终止并回收固定 unittest 子进程。"""

    async def scenario() -> None:
        """用休眠替换 fixture 测试，并检查子进程返回码。"""
        root = prepare_fixture(FIXTURE_SOURCE, tmp_path / "repo")
        (root / "test_calculator.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        executor = FixtureExecutor(root)
        runner = Runner(
            FakeProvider((request(name="run_tests"), FINAL)),
            executor,
            RunLimits(timeout_seconds=0.2),
        )
        report = await asyncio.wait_for(runner.run(INITIAL), timeout=3)
        assert report.reason == "time_limit"
        assert (
            executor.active_process is not None and executor.active_process.returncode is not None
        )
        assert_terminal(report)

    asyncio.run(scenario())


def test_invalid_limits_and_initial_history_are_rejected() -> None:
    """尽早拒绝不可能的限制值和尚未支持的 assistant 恢复历史。"""
    with pytest.raises(ValidationError):
        RunLimits(max_steps=0)
    with pytest.raises(ValidationError):
        RunLimits(timeout_seconds=float("inf"))
    runner = Runner(FakeProvider(), RecordingExecutor())
    with pytest.raises(ValueError):
        asyncio.run(runner.run((FINAL,)))
    assert runner.state == RunState.CREATED and runner.report is None


def test_provider_role_violation_stops_before_tools() -> None:
    """将 Provider 返回的非 assistant 消息视为终态契约错误。"""

    class WrongRoleProvider:
        """返回 user 消息，以模拟违反 Provider 输出角色约束。"""

        async def complete(self, messages: Sequence[Message]) -> Message:
            """返回结构合法但角色违反 Provider 边界的消息。"""
            return Message(role="user", content="wrong role")

    executor = RecordingExecutor()
    report = asyncio.run(Runner(WrongRoleProvider(), executor).run(INITIAL))
    assert report.reason == "contract_error" and not executor.calls
    assert_terminal(report)


def test_success_resets_failure_streak() -> None:
    """确保一次成功调用会打断其他方面完全相同的连续失败。"""

    class AlternatingExecutor:
        """按失败、成功、失败的顺序返回结果以验证计数重置。"""

        def __init__(self) -> None:
            """初始化计数器，使第二次执行成为唯一成功调用。"""
            self.count = 0

        async def execute(self, call: ToolCall) -> ToolResult:
            """依次返回失败、成功、失败，同时保留调用 ID。"""
            self.count += 1
            return ToolResult(call_id=call.call_id, content="result", is_error=self.count != 2)

    responses = (request("c1"), request("c2"), request("c3"), FINAL)
    report = asyncio.run(
        Runner(
            FakeProvider(responses),
            AlternatingExecutor(),
            RunLimits(max_consecutive_failures=2),
        ).run(INITIAL)
    )
    assert report.reason == "completed" and report.tool_calls == 3
    assert_terminal(report)


def test_repeated_failure_in_batch_prevents_remaining_calls() -> None:
    """多调用批次达到重复失败阈值时立即在批次内停止。"""
    calls = tuple(ToolCall(call_id=f"c{i}", name="read", arguments={}) for i in range(4))
    executor = RecordingExecutor(fail=True)
    report = asyncio.run(
        Runner(
            FakeProvider((Message(role="assistant", tool_calls=calls),)),
            executor,
            RunLimits(max_consecutive_failures=2),
        ).run(INITIAL)
    )
    assert report.reason == "repeated_failure" and len(executor.calls) == 2
    assert_terminal(report)


def test_provider_cannot_mutate_runtime_history_through_request() -> None:
    """Provider 修改请求副本时，保持 Runner 内部历史不变。"""

    class MutatingProvider(FakeProvider):
        """尝试篡改请求历史，用于验证 Runtime 传入的是隔离副本。"""

        async def complete(self, messages: Sequence[Message]) -> Message:
            """回复前修改收到的副本中的嵌套参数。"""
            for message in messages:
                for call in message.tool_calls:
                    call.arguments["path"] = "changed by provider"
            return await super().complete(messages)

    report = asyncio.run(
        Runner(
            MutatingProvider((request(path="original"), FINAL)),
            RecordingExecutor(),
        ).run(INITIAL)
    )
    call = next(call for message in report.messages for call in message.tool_calls)
    assert call.arguments["path"] == "original"
    assert_terminal(report)
