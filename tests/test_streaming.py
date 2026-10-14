"""验证分片原子执行边界、故障取消、权限事件和默认脱敏回放。"""

import asyncio
import json
import stat
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from codepilot.contracts import Message
from codepilot.contracts.run import RunLimits
from codepilot.contracts.stream import StreamChunk, Usage
from codepilot.observability.trace import CURRENT_RECORDER, EventRecorder
from codepilot.permissions import (
    PermissionedExecutor,
    PermissionEngine,
    ScriptedApproval,
    ask_edits_policy,
)
from codepilot.providers.fake_stream import FakeStreamProvider
from codepilot.runtime import Runner
from codepilot.tools import create_basic_registry

INITIAL = (Message(role="user", content="secret user prompt"),)
DONE = (
    StreamChunk(kind="text_delta", text="secret answer"),
    StreamChunk(kind="turn_finished", finish_reason="stop"),
)


def call_chunk(index: int, call_id: str, arguments: str) -> StreamChunk:
    """构造有不同索引的写入片段。"""
    return StreamChunk(
        kind="tool_call_delta",
        index=index,
        call_id=call_id,
        name="write_file",
        arguments_delta=arguments,
    )


def test_interleaved_calls_and_redacted_trace(tmp_path: Path) -> None:
    """交错参数只在整个回复校验后执行；磁盘不保存正文或参数。"""

    async def scenario() -> None:
        """运行带两次审批和供应商用量的完整工具回合。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        recorder = EventRecorder(tmp_path / "traces")
        approval = ScriptedApproval((True, True))
        executor = PermissionedExecutor(
            create_basic_registry(workspace), PermissionEngine(ask_edits_policy()), approval
        )
        provider = FakeStreamProvider(
            (
                (
                    call_chunk(0, "a", '{"path":"a.txt",'),
                    call_chunk(1, "b", '{"path":"b.txt",'),
                    StreamChunk(
                        kind="tool_call_delta", index=1, arguments_delta='"content":"secret B"}'
                    ),
                    StreamChunk(
                        kind="tool_call_delta", index=0, arguments_delta='"content":"secret A"}'
                    ),
                    StreamChunk(
                        kind="usage_updated",
                        usage=Usage(input_tokens=12, output_tokens=9, source="provider"),
                    ),
                    StreamChunk(kind="turn_finished", finish_reason="tool_calls"),
                ),
                DONE,
            )
        )
        report = await Runner(provider, executor, recorder=recorder).run(INITIAL)
        assert report.reason == "completed" and report.tool_calls == 2
        assert (workspace / "a.txt").read_text() == "secret A"
        assert (workspace / "b.txt").read_text() == "secret B"
        kinds = [event.kind for event in recorder.events]
        assert kinds.count("tool_call_ready") == 2
        assert kinds.count("approval_requested") == 2
        assert kinds.count("tool_started") == 2
        assert kinds.count("turn_finished") == 2
        assert kinds.count("run_finished") == 1
        assert kinds.index("tool_call_ready") > max(
            i for i, kind in enumerate(kinds) if kind == "tool_call_delta"
        )
        assert kinds.index("approval_resolved") < kinds.index("tool_started")
        assert [event.sequence for event in recorder.events] == list(range(1, len(kinds) + 1))
        traces = list((tmp_path / "traces").glob("*.jsonl"))
        assert len(traces) == 1
        assert stat.S_IMODE(traces[0].stat().st_mode) == 0o600
        disk = traces[0].read_text()
        assert "secret" not in disk and "a.txt" not in disk
        persisted = [json.loads(line) for line in disk.splitlines()]
        assert len(persisted) == len(kinds)
        summary = json.loads(traces[0].with_suffix(".summary.json").read_text())
        assert stat.S_IMODE(traces[0].with_suffix(".summary.json").stat().st_mode) == 0o600
        assert summary["cost"] is None
        usages = summary["usage_by_turn"]
        assert usages[0]["usage"]["source"] == "provider"
        assert usages[1]["usage"]["input_tokens"] is None
        assert CURRENT_RECORDER.get() is None

    asyncio.run(scenario())


def test_stream_chunk_rejects_payload_for_another_kind() -> None:
    """拒绝会被组装器静默忽略的跨类型载荷组合。"""
    invalid = (
        {"kind": "text_delta", "text": "ok", "arguments_delta": "{}"},
        {"kind": "tool_call_delta", "call_id": "a", "finish_reason": "tool_calls"},
        {"kind": "usage_updated", "usage": Usage(), "text": "hidden"},
        {"kind": "turn_finished", "finish_reason": "stop", "usage": Usage()},
    )
    for payload in invalid:
        # 每类分片只接受自己的载荷，避免正文、参数或用量无声丢失。
        with pytest.raises(ValueError):
            StreamChunk.model_validate(payload)


@pytest.mark.parametrize(
    "arguments",
    ['{"path":', "[]", '{"path":"a","content":"x","content":"y"}', '{"path":"a","content":NaN}'],
)
def test_bad_json_prevents_entire_batch(tmp_path: Path, arguments: str) -> None:
    """同批第二个调用非法时，第一个合法写入也不能提前执行。"""

    async def scenario() -> None:
        """组装一批合法和非法参数，验证零写入。"""
        provider = FakeStreamProvider(
            (
                (
                    call_chunk(0, "a", '{"path":"a","content":"x"}'),
                    call_chunk(1, "b", arguments),
                    StreamChunk(kind="turn_finished", finish_reason="tool_calls"),
                ),
            )
        )
        report = await Runner(provider, create_basic_registry(tmp_path)).run(INITIAL)
        assert report.reason == "contract_error"
        assert report.tool_calls == 0 and not list(tmp_path.iterdir())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tail,reason",
    [
        ((), "contract_error"),
        ((OSError("secret network credential"),), "provider_error"),
        ((StreamChunk(kind="turn_finished", finish_reason="stop"),), "contract_error"),
        (
            (
                StreamChunk(kind="turn_finished", finish_reason="tool_calls"),
                StreamChunk(kind="text_delta", text="late"),
            ),
            "contract_error",
        ),
    ],
)
def test_interrupted_stream_has_terminal_without_tools(
    tmp_path: Path,
    tail: tuple[StreamChunk | Exception, ...],
    reason: str,
) -> None:
    """结束标记缺失、错误或结束后追加分片都不执行工具。"""

    async def scenario() -> None:
        """验证故障终态与错误正文不泄露。"""
        provider = FakeStreamProvider(((call_chunk(0, "a", '{"path":"a","content":"x"}'), *tail),))
        runner = Runner(provider, create_basic_registry(tmp_path))
        report = await runner.run(INITIAL)
        assert report.reason == reason and report.tool_calls == 0
        assert [e.kind for e in runner.recorder.events].count("run_finished") == 1
        assert runner.recorder.events[-2].kind == "turn_finished"
        assert "secret network" not in report.model_dump_json()
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


class HangingStream:
    """完整调用后挂起，检查结束前不会启动工具且取消会关闭流。"""

    def __init__(self) -> None:
        """保存已产出片段与资源释放信号。"""
        self.started = asyncio.Event()
        self.closed = False

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamChunk]:
        """产出调用但不结束，finally 记录关闭。"""
        try:
            yield call_chunk(0, "a", '{"path":"a","content":"x"}')
            self.started.set()
            await asyncio.Event().wait()
        finally:
            self.closed = True


def test_cancel_closes_stream_and_stops_tools(tmp_path: Path) -> None:
    """取消向调用方传播，并留下唯一终态和零副作用。"""

    async def scenario() -> None:
        """在收到完整参数但没有结束标记时注入取消。"""
        provider = HangingStream()
        runner = Runner(provider, create_basic_registry(tmp_path))
        running = asyncio.create_task(runner.run(INITIAL))
        await provider.started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert provider.closed
        assert runner.report is not None and runner.report.reason == "cancelled"
        assert runner.report.tool_calls == 0 and not list(tmp_path.iterdir())
        assert sum(e.kind == "run_finished" for e in runner.recorder.events) == 1
        assert CURRENT_RECORDER.get() is None

    asyncio.run(scenario())


def test_stream_timeout(tmp_path: Path) -> None:
    """流等待纳入原有总预算，超时回收并产生终态。"""

    async def scenario() -> None:
        """设置短预算验证挂起流会被关闭。"""
        provider = HangingStream()
        runner = Runner(provider, create_basic_registry(tmp_path), RunLimits(timeout_seconds=0.02))
        report = await runner.run(INITIAL)
        assert report.reason == "time_limit" and provider.closed
        assert report.tool_calls == 0

    asyncio.run(scenario())


def test_replay_normalizes_ids_and_times(tmp_path: Path) -> None:
    """同一脚本在不同运行 ID 下得到相同元数据轨迹。"""

    async def scenario() -> None:
        """回放两次并规范化非确定信息；这不是模型成功率。"""
        records = []
        for run_id in ("first", "second"):
            runner = Runner(
                FakeStreamProvider((DONE,)), create_basic_registry(tmp_path), run_id=run_id
            )
            await runner.run(INITIAL)
            records.append(runner.recorder.normalized())
        assert records[0] == records[1]

    asyncio.run(scenario())


def test_cancel_during_approval_never_starts_tool(tmp_path: Path) -> None:
    """等待审批时取消，当前调用和同批后续调用都不能启动。"""
    from codepilot.permissions import ApprovalProvider
    from codepilot.permissions.models import ApprovalRequest

    class WaitingApproval(ApprovalProvider):
        """用事件表示已经进入审批等待。"""

        def __init__(self) -> None:
            """保存等待信号供测试准确注入取消。"""
            self.waiting = asyncio.Event()

        async def approve(self, request: ApprovalRequest) -> bool:
            """等待外部取消，不产生同意答案。"""
            self.waiting.set()
            await asyncio.Event().wait()
            return True

    async def scenario() -> None:
        """审批挂起时取消整个 Runner，并检查事件与文件。"""
        approval = WaitingApproval()
        executor = PermissionedExecutor(
            create_basic_registry(tmp_path), PermissionEngine(ask_edits_policy()), approval
        )
        provider = FakeStreamProvider(
            (
                (
                    call_chunk(0, "a", '{"path":"a","content":"x"}'),
                    call_chunk(1, "b", '{"path":"b","content":"x"}'),
                    StreamChunk(kind="turn_finished", finish_reason="tool_calls"),
                ),
            )
        )
        runner = Runner(provider, executor)
        running = asyncio.create_task(runner.run(INITIAL))
        await approval.waiting.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not list(tmp_path.iterdir())
        kinds = [event.kind for event in runner.recorder.events]
        assert "approval_requested" in kinds and "tool_started" not in kinds
        finished = [event for event in runner.recorder.events if event.kind == "tool_finished"]
        assert len(finished) == 1 and finished[0].call_id == "a"
        assert finished[0].outcome == "cancelled"

    asyncio.run(scenario())
