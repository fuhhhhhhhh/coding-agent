"""覆盖 M08 受限并发、资源冲突、结果配对和取消传播。"""

import asyncio
import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from codepilot.contracts import Message, ToolCall, ToolResult
from codepilot.contracts.models import Contract
from codepilot.observability.trace import EventRecorder
from codepilot.permissions import (
    PermissionedExecutor,
    PermissionEngine,
    ScriptedApproval,
    ask_edits_policy,
)
from codepilot.providers.fake import FakeProvider
from codepilot.runtime import Runner
from codepilot.scheduler import ScheduledExecutor
from codepilot.tools import ReadFileTool, ShellTool, Tool, ToolRegistry, Workspace


class DelayArguments(Contract):
    """定义测试工具的资源路径、标签和可选延迟。"""

    path: str
    label: str
    delay: float = 0.0


@dataclass
class Activity:
    """记录工具开始、结束、活动峰值和人工释放信号。"""

    started: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    active: int = 0
    maximum: int = 0
    two_started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None


class DelayReadTool(Tool[DelayArguments]):
    """用异步等待模拟可以安全并发的独立只读调用。"""

    name = "delay_read"
    description = "模拟只读延迟"
    effect: Literal["read", "write", "process"] = "read"
    resources = ("workspace:read",)
    arguments_model = DelayArguments

    def __init__(self, activity: Activity) -> None:
        """保存共享活动记录以观察真实重叠。"""
        self.activity = activity

    async def run(self, arguments: DelayArguments) -> str:
        """记录活动区间，并按测试配置等待时间或释放信号。"""
        activity = self.activity
        activity.started.append(arguments.label)
        activity.active += 1
        activity.maximum = max(activity.maximum, activity.active)
        if activity.active >= 2:
            activity.two_started.set()
        try:
            if activity.release is not None:
                await activity.release.wait()
            elif arguments.delay:
                await asyncio.sleep(arguments.delay)
            return arguments.label
        finally:
            activity.active -= 1
            activity.finished.append(arguments.label)


class DelayWriteTool(DelayReadTool):
    """复用活动记录模拟按路径互斥的写工具。"""

    name = "delay_write"
    description = "模拟写入延迟"
    effect = "write"
    resources = ("workspace:write",)


class AppendTool(Tool[DelayArguments]):
    """模拟包含让出点的文件读改写，用于暴露丢失更新。"""

    name = "append"
    description = "读取后追加标签"
    effect = "write"
    resources = ("workspace:write",)
    arguments_model = DelayArguments

    def __init__(self, root: Path) -> None:
        """保存测试临时目录，不接触用户工作文件。"""
        self.root = root

    async def run(self, arguments: DelayArguments) -> str:
        """读取旧内容、主动让出执行权后写回追加结果。"""
        path = self.root / arguments.path
        content = path.read_text(encoding="utf-8")
        await asyncio.sleep(0.01)
        path.write_text(content + arguments.label, encoding="utf-8")
        return arguments.label


def _call(call_id: str, name: str, path: str, *, delay: float = 0.0) -> ToolCall:
    """构造带稳定标签的调度测试调用。"""
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments={"path": path, "label": call_id, "delay": delay},
    )


def test_independent_reads_overlap_with_limit() -> None:
    """两个独立读取在容量为二时真实重叠，并受全局上限约束。"""

    async def scenario() -> None:
        """等待两个工具同时开始后统一释放。"""
        activity = Activity(release=asyncio.Event())
        scheduler = ScheduledExecutor(ToolRegistry((DelayReadTool(activity),)), max_concurrency=2)
        tasks = [
            asyncio.create_task(scheduler.execute(_call("a", "delay_read", "a.txt"))),
            asyncio.create_task(scheduler.execute(_call("b", "delay_read", "b.txt"))),
        ]
        await asyncio.wait_for(activity.two_started.wait(), timeout=0.5)
        assert activity.maximum == 2
        assert activity.release is not None
        activity.release.set()
        results = await asyncio.gather(*tasks)
        assert [result.call_id for result in results] == ["a", "b"]

    asyncio.run(scenario())


def test_same_path_writes_are_fifo_serialized() -> None:
    """同一路径写入按到达顺序串行，后来者不会提前进入工具。"""

    async def scenario() -> None:
        """阻塞首个写入并确认第二个仍在资源队列。"""
        activity = Activity(release=asyncio.Event())
        scheduler = ScheduledExecutor(ToolRegistry((DelayWriteTool(activity),)))
        first = asyncio.create_task(scheduler.execute(_call("first", "delay_write", "same.txt")))
        while activity.started != ["first"]:
            await asyncio.sleep(0)
        second = asyncio.create_task(scheduler.execute(_call("second", "delay_write", "same.txt")))
        await asyncio.sleep(0.02)
        assert activity.started == ["first"] and activity.maximum == 1
        assert activity.release is not None
        activity.release.set()
        await asyncio.gather(first, second)
        assert activity.started == ["first", "second"]

    asyncio.run(scenario())


def test_cancelled_waiter_never_enters_tool() -> None:
    """取消仍在容量队列中的调用，释放前序任务后也不能补启动。"""

    async def scenario() -> None:
        """用并发上限一制造稳定排队点并注入取消。"""
        activity = Activity(release=asyncio.Event())
        scheduler = ScheduledExecutor(ToolRegistry((DelayReadTool(activity),)), max_concurrency=1)
        first = asyncio.create_task(scheduler.execute(_call("first", "delay_read", "a.txt")))
        while activity.started != ["first"]:
            await asyncio.sleep(0)
        queued = asyncio.create_task(scheduler.execute(_call("queued", "delay_read", "b.txt")))
        await asyncio.sleep(0)
        queued.cancel()
        try:
            await queued
        except asyncio.CancelledError:
            pass
        assert activity.release is not None
        activity.release.set()
        await first
        assert activity.started == ["first"]

    asyncio.run(scenario())


def test_same_file_read_modify_write_keeps_both_changes(tmp_path: Path) -> None:
    """同文件读改写即使同时提交也按顺序保留两次变更。"""

    async def scenario() -> None:
        """并发提交两个易丢失更新的追加调用并检查最终内容。"""
        path = tmp_path / "value.txt"
        path.write_text("start", encoding="utf-8")
        scheduler = ScheduledExecutor(ToolRegistry((AppendTool(tmp_path),)))
        await asyncio.gather(
            scheduler.execute(_call("A", "append", "value.txt")),
            scheduler.execute(_call("B", "append", "value.txt")),
        )
        assert path.read_text(encoding="utf-8") == "startAB"

    asyncio.run(scenario())


def test_runner_returns_results_by_call_id_not_completion_order() -> None:
    """并发完成顺序相反，下一轮历史仍保持模型调用 ID 顺序。"""

    async def scenario() -> None:
        """让第二个调用先结束并比较事件顺序与消息顺序。"""
        activity = Activity()
        scheduler = ScheduledExecutor(ToolRegistry((DelayReadTool(activity),)))
        calls = (
            _call("slow", "delay_read", "slow.txt", delay=0.05),
            _call("fast", "delay_read", "fast.txt", delay=0.001),
        )
        provider = FakeProvider(
            (
                Message(role="assistant", tool_calls=calls),
                Message(role="assistant", content="完成"),
            )
        )
        runner = Runner(provider, scheduler)
        report = await runner.run((Message(role="user", content="并发读取"),))
        results = [message.tool_result for message in report.messages if message.tool_result]
        assert [result.call_id for result in results] == ["slow", "fast"]
        finished = [
            event.call_id for event in runner.recorder.events if event.kind == "tool_finished"
        ]
        assert finished == ["fast", "slow"]

    asyncio.run(scenario())


def test_disk_trace_redacts_dynamic_resource_path(tmp_path: Path) -> None:
    """调度事件在内存保留资源键，落盘时不泄漏实际文件路径。"""

    async def scenario() -> None:
        """运行一次调度调用并比较内存事件和磁盘元数据。"""
        activity = Activity()
        scheduler = ScheduledExecutor(ToolRegistry((DelayReadTool(activity),)))
        provider = FakeProvider(
            (
                Message(
                    role="assistant",
                    tool_calls=(_call("read", "delay_read", "secret-name.txt", delay=0.001),),
                ),
                Message(role="assistant", content="完成"),
            )
        )
        recorder = EventRecorder(tmp_path / "traces")
        runner = Runner(provider, scheduler, recorder=recorder)
        await runner.run((Message(role="user", content="读取"),))
        queued = next(event for event in recorder.events if event.kind == "tool_queued")
        assert queued.resources == ("path:secret-name.txt",)
        disk = "\n".join(path.read_text() for path in (tmp_path / "traces").iterdir())
        assert "secret-name.txt" not in disk
        records = [
            json.loads(line)
            for line in next((tmp_path / "traces").glob("*.jsonl")).read_text().splitlines()
        ]
        queued_record = next(record for record in records if record["kind"] == "tool_queued")
        assert queued_record["resources"] == ["path:<redacted>"]

    asyncio.run(scenario())


def test_approval_finishes_before_call_enters_scheduler() -> None:
    """写调用先完成逐次审批，再发布资源排队事件。"""

    async def scenario() -> None:
        """组合权限和调度包装器并检查事件先后关系。"""
        activity = Activity()
        scheduler = ScheduledExecutor(ToolRegistry((DelayWriteTool(activity),)))
        executor = PermissionedExecutor(
            scheduler, PermissionEngine(ask_edits_policy()), ScriptedApproval((True,))
        )
        provider = FakeProvider(
            (
                Message(
                    role="assistant",
                    tool_calls=(_call("write", "delay_write", "value.txt", delay=0.001),),
                ),
                Message(role="assistant", content="完成"),
            )
        )
        runner = Runner(provider, executor)
        report = await runner.run((Message(role="user", content="写入"),))
        assert report.reason == "completed"
        kinds = [event.kind for event in runner.recorder.events]
        assert kinds.index("approval_resolved") < kinds.index("tool_queued")

    asyncio.run(scenario())


def test_process_and_unknown_calls_use_exclusive_plan() -> None:
    """进程工具和未知工具采用全工作区独占计划。"""
    activity = Activity()
    scheduler = ScheduledExecutor(ToolRegistry((DelayReadTool(activity),)))
    process = ToolCall(call_id="shell", name="shell", arguments={"command": "true"})
    unknown = ToolCall(call_id="unknown", name="missing", arguments={})
    assert scheduler.plan(process).mode == "exclusive"
    assert scheduler.plan(unknown).mode == "exclusive"


def test_runner_cancel_stops_shell_and_never_starts_queued_read(tmp_path: Path) -> None:
    """取消并发批次时清理活动进程，并阻止独占任务后的排队读取。"""

    async def scenario() -> None:
        """等待 Shell 启动后取消 Runner，并核对进程和事件终态。"""
        (tmp_path / "value.txt").write_text("x", encoding="utf-8")
        workspace = Workspace(tmp_path)
        shell = ShellTool(workspace)
        registry = ToolRegistry((shell, ReadFileTool(workspace)))
        command = f"exec {shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; time.sleep(60)"
        )
        provider = FakeProvider(
            (
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(call_id="shell", name="shell", arguments={"command": command}),
                        ToolCall(call_id="read", name="read_file", arguments={"path": "value.txt"}),
                    ),
                ),
            )
        )
        runner = Runner(provider, ScheduledExecutor(registry))
        running = asyncio.create_task(runner.run((Message(role="user", content="运行"),)))
        while shell.active_process is None:
            await asyncio.sleep(0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert shell.active_process.returncode is not None
        started = [
            event.call_id for event in runner.recorder.events if event.kind == "tool_started"
        ]
        assert started == ["shell"]
        cancelled = [
            event.call_id
            for event in runner.recorder.events
            if event.kind == "tool_queue_cancelled"
        ]
        assert cancelled == ["read"]

    asyncio.run(scenario())


def test_runner_does_not_interrupt_cleanup_with_a_second_cancel(tmp_path: Path) -> None:
    """父级 gather 已传播取消时，不得再次取消正在回收进程的子任务。"""

    async def scenario() -> None:
        """通过清理屏障固定二次取消时序，避免依赖操作系统的偶然调度。"""
        entered = asyncio.Event()
        release = asyncio.Event()
        interrupted = asyncio.Event()

        class CleaningExecutor:
            """用挂起清理模拟等待进程组退出的真实执行器。"""

            supports_parallel = True

            async def execute(self, call: ToolCall) -> ToolResult:
                """接收第一次取消后保持清理，检测第二次取消是否破坏收尾。"""
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # 排队任务立即退出，使 gather 在活动任务仍清理时向父级抛出取消。
                    if call.call_id == "b":
                        raise
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        interrupted.set()
                        raise
                    raise
                raise AssertionError("unreachable")

        provider = FakeProvider(
            (
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(call_id="a", name="shell", arguments={}),
                        ToolCall(call_id="b", name="shell", arguments={}),
                    ),
                ),
            )
        )
        running = asyncio.create_task(
            Runner(provider, CleaningExecutor()).run((Message(role="user", content="取消"),))
        )
        await asyncio.sleep(0.01)
        running.cancel()
        await entered.wait()
        await asyncio.sleep(0.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not interrupted.is_set()

    asyncio.run(scenario())
