"""运行离线 M02 验收流程，并将观察记录保存为 JSONL。"""

import asyncio
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from codepilot.contracts import Message, RunState, ToolCall, ToolResult
from codepilot.contracts.run import RunLimits, RunObservation, RunReport
from codepilot.fixtures.m02 import FIXED_SOURCE, FixtureExecutor, prepare_fixture, repair_script
from codepilot.providers.fake import FakeProvider
from codepilot.runtime import Runner


class BrokenExecutor:
    """始终失败的执行器，用于演示普通工具异常如何反馈给模型。"""

    async def execute(self, call: ToolCall) -> ToolResult:
        """抛出受控工具异常，用于验证 Runtime 的错误转换。"""
        raise RuntimeError("controlled tool failure")


class BrokenProvider:
    """始终失败的 Provider，用于演示 provider_error 终态。"""

    async def complete(self, messages: Sequence[Message]) -> Message:
        """抛出受控 Provider 异常，用于验证请求失败终态。"""
        raise ConnectionError("controlled provider failure")


class WaitingProvider:
    """持续挂起的 Provider，用于确定性触发超时与外部取消。"""

    def __init__(self) -> None:
        """创建同步信号，用于观察 Provider 何时开始执行。"""
        self.entered = asyncio.Event()

    async def complete(self, messages: Sequence[Message]) -> Message:
        """无限等待，使超时和外部取消场景保持确定性。"""
        self.entered.set()
        await asyncio.Event().wait()
        return Message(role="assistant", content="unreachable")


async def run_scenarios(fixture_source: Path) -> dict[str, RunReport]:
    """执行并断言全部 M02 正常场景和受控失败场景。"""
    reports: dict[str, RunReport] = {}
    # 所有文件副作用都留在此临时目录，并在代码块退出时消失。
    with tempfile.TemporaryDirectory(prefix="codepilot-m02-") as temporary:
        root = prepare_fixture(fixture_source, Path(temporary) / "repo")
        executor = FixtureExecutor(root)
        initial = (Message(role="user", content="Fix add and verify the fixture tests."),)
        baseline_call = Message(
            role="assistant",
            tool_calls=(ToolCall(call_id="baseline", name="run_tests", arguments={}),),
        )
        reports["baseline"] = await Runner(
            FakeProvider((baseline_call, Message(role="assistant", content="Baseline checked."))),
            executor,
            run_id="m02-baseline",
        ).run(initial)
        baseline_results = [
            event.result for event in reports["baseline"].observations if event.result
        ]
        # 失败基线用于证明后续测试转绿确实由脚本修复造成。
        assert baseline_results[0].is_error, "fixture must fail before the repair"
        reports["repair"] = await Runner(
            FakeProvider(repair_script()), executor, run_id="m02-repair"
        ).run(initial)
        # Runtime 完成不足以证明任务成功，因此还要核对文件内容和测试结果。
        assert reports["repair"].state == RunState.COMPLETED
        assert (root / "calculator.py").read_text(encoding="utf-8") == FIXED_SOURCE
        results = [event.result for event in reports["repair"].observations if event.result]
        assert len(results) == 3 and not results[-1].is_error
        reports["no_calls"] = await Runner(
            FakeProvider((Message(role="assistant", content="No tools needed."),)),
            executor,
            run_id="m02-no-calls",
        ).run(initial)
        failures = tuple(
            Message(
                role="assistant",
                tool_calls=(ToolCall(call_id=f"bad-{i}", name="missing_tool", arguments={}),),
            )
            for i in range(4)
        )
        reports["repeated_failure"] = await Runner(
            FakeProvider(failures), executor, run_id="m02-repeated-failure"
        ).run(initial)
        good_reads = tuple(
            Message(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        call_id=f"read-{i}", name="read_file", arguments={"path": "calculator.py"}
                    ),
                ),
            )
            for i in range(4)
        )
        reports["step_limit"] = await Runner(
            FakeProvider(good_reads), executor, RunLimits(max_steps=2), run_id="m02-step-limit"
        ).run(initial)
        reports["tool_exception"] = await Runner(
            FakeProvider(
                (good_reads[0], Message(role="assistant", content="Tool failure received."))
            ),
            BrokenExecutor(),
            run_id="m02-tool-exception",
        ).run(initial)
        reports["provider_error"] = await Runner(
            BrokenProvider(), executor, run_id="m02-provider-error"
        ).run(initial)
        invalid = Message(
            role="assistant",
            tool_calls=(
                ToolCall(
                    call_id="invalid-1", name="write_file", arguments={"path": "calculator.py"}
                ),
            ),
        )
        reports["argument_error"] = await Runner(
            FakeProvider(
                (invalid, Message(role="assistant", content="Invalid arguments received."))
            ),
            executor,
            run_id="m02-argument-error",
        ).run(initial)
        reports["tool_budget"] = await Runner(
            FakeProvider(good_reads),
            executor,
            RunLimits(max_tool_calls=0),
            run_id="m02-tool-budget",
        ).run(initial)
        reports["time_limit"] = await Runner(
            WaitingProvider(),
            executor,
            RunLimits(timeout_seconds=0.03),
            run_id="m02-time-limit",
        ).run(initial)
        waiting = WaitingProvider()
        runner = Runner(waiting, executor, run_id="m02-cancelled")
        task = asyncio.create_task(runner.run(initial))
        await asyncio.wait_for(waiting.entered.wait(), timeout=2)
        task.cancel()
        # 外部取消必须继续传播，同时留下可检查的终态报告。
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert runner.report is not None
        reports["cancelled"] = runner.report
    return reports


def main() -> None:
    """运行 M02 场景、校验预期原因，并为每个场景写入一份 JSONL 轨迹。"""
    source = Path("examples/fixture_repo")
    reports = asyncio.run(run_scenarios(source))
    evidence = Path("docs/evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    expected = {
        "baseline": "completed",
        "repair": "completed",
        "no_calls": "completed",
        "repeated_failure": "repeated_failure",
        "step_limit": "step_limit",
        "tool_exception": "completed",
        "provider_error": "provider_error",
        "argument_error": "completed",
        "tool_budget": "tool_budget",
        "time_limit": "time_limit",
        "cancelled": "cancelled",
    }
    for scenario, report in reports.items():
        # 终止原因不符时必须先失败，避免写入错误证据。
        assert report.reason == expected[scenario]
        with (evidence / f"M02-{scenario}.jsonl").open("w", encoding="utf-8") as file:
            for observation in report.observations:
                line = observation.model_dump_json()
                # 对每条记录做往返校验，确保保存的证据符合契约。
                assert RunObservation.model_validate_json(line) == observation
                file.write(line + "\n")
        print(
            json.dumps(
                {
                    "scenario": scenario,
                    "state": report.state.value,
                    "reason": report.reason,
                    "steps": report.steps,
                    "tool_calls": report.tool_calls,
                }
            )
        )


if __name__ == "__main__":
    main()
