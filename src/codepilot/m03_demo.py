"""用 Fake Provider 驱动六个基础工具并保存 M03 正常与失败轨迹。"""

import asyncio
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from codepilot.contracts import Message, RunState, ToolCall, ToolResult
from codepilot.contracts.run import RunObservation, RunReport
from codepilot.providers.fake import FakeProvider
from codepilot.runtime import Runner
from codepilot.tools import ToolRegistry, create_basic_registry


def request(call_id: str, name: str, **arguments: object) -> Message:
    """从 JSON 兼容参数构造一条单工具 assistant 回复。"""
    call = ToolCall.model_validate({"call_id": call_id, "name": name, "arguments": arguments})
    return Message(role="assistant", tool_calls=(call,))


async def run_case(run_id: str, registry: ToolRegistry, script: tuple[Message, ...]) -> RunReport:
    """用给定脚本执行一次隔离 Runner，并要求模型最终正常结束。"""
    report = await Runner(FakeProvider(script), registry, run_id=run_id).run(
        (Message(role="user", content="Inspect and update the temporary fixture."),)
    )
    assert report.state == RunState.COMPLETED
    return report


async def run_scenarios(fixture_source: Path) -> dict[str, RunReport]:
    """在临时工作区运行六工具闭环和四个关键错误场景。"""
    reports: dict[str, RunReport] = {}
    with tempfile.TemporaryDirectory(prefix="codepilot-m03-") as temporary:
        root = Path(temporary) / "repo"
        shutil.copytree(fixture_source, root)
        registry = create_basic_registry(root)
        test_command = (
            f"{shlex.quote(sys.executable)} -I -B -m unittest discover "
            "-s . -p test_calculator.py -v"
        )
        happy_script = (
            request("read", "read_file", path="calculator.py"),
            request(
                "edit",
                "edit_file",
                path="calculator.py",
                old_text="return a - b",
                new_text="return a + b",
            ),
            request("write", "write_file", path="result.txt", content="repair prepared\n"),
            request("glob", "glob", pattern="*.py"),
            request("search", "search", query="return a + b", pattern="*.py"),
            request("shell", "shell", command=test_command),
            Message(role="assistant", content="All six tools completed and tests passed."),
        )
        reports["happy"] = await run_case("m03-happy", registry, happy_script)
        results = [event.result for event in reports["happy"].observations if event.result]
        assert len(results) == 6 and all(not result.is_error for result in results)
        assert "OK" in results[-1].content
        assert (root / "calculator.py").read_text(encoding="utf-8").endswith("return a + b\n")

        invalid_script = (
            request("invalid", "write_file", path="result.txt", content="bad", extra=True),
            Message(role="assistant", content="Invalid arguments observed."),
        )
        reports["invalid_arguments"] = await run_case(
            "m03-invalid-arguments", registry, invalid_script
        )
        assert "invalid_arguments" in _only_result(reports["invalid_arguments"]).content

        escape_script = (
            request("escape", "read_file", path="../outside.txt"),
            Message(role="assistant", content="Path escape rejected."),
        )
        reports["path_escape"] = await run_case("m03-path-escape", registry, escape_script)
        assert "invalid_path" in _only_result(reports["path_escape"]).content

        (root / "repeat.txt").write_text("same\nsame\n", encoding="utf-8")
        ambiguous_script = (
            request(
                "ambiguous",
                "edit_file",
                path="repeat.txt",
                old_text="same",
                new_text="changed",
            ),
            Message(role="assistant", content="Ambiguous edit rejected."),
        )
        reports["ambiguous_edit"] = await run_case("m03-ambiguous-edit", registry, ambiguous_script)
        assert "found 2" in _only_result(reports["ambiguous_edit"]).content
        assert (root / "repeat.txt").read_text(encoding="utf-8") == "same\nsame\n"

        (root / "binary.dat").write_bytes(b"text\x00binary")
        binary_script = (
            request("binary", "read_file", path="binary.dat"),
            Message(role="assistant", content="Binary input rejected."),
        )
        reports["binary"] = await run_case("m03-binary", registry, binary_script)
        assert "binary_file" in _only_result(reports["binary"]).content
    return reports


def _only_result(report: RunReport) -> ToolResult:
    """返回单错误场景中的唯一工具结果，并验证它确实失败。"""
    results = [event.result for event in report.observations if event.result]
    assert len(results) == 1 and results[0].is_error
    return results[0]


def main() -> None:
    """执行全部场景，将逐事件 JSONL 写入证据目录并打印摘要。"""
    reports = asyncio.run(run_scenarios(Path("examples/fixture_repo")))
    evidence = Path("docs/evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    for scenario, report in reports.items():
        with (evidence / f"M03-{scenario}.jsonl").open("w", encoding="utf-8") as file:
            for observation in report.observations:
                line = observation.model_dump_json()
                # 写入前往返校验，避免把不符合契约的演示数据当作证据。
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
