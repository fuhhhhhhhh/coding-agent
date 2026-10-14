"""覆盖 M06 本地评测、反馈纠错和不可评测故障分类。"""

import asyncio
import sys
from pathlib import Path

from codepilot.contracts import Message, ToolCall
from codepilot.evals import IndependentVerifier, LocalHarness, TaskSpec
from codepilot.providers.fake import FakeProvider
from codepilot.tools.factory import create_basic_registry
from codepilot.workspace import LocalExecutionBackend


def _task(root: Path, attempts: int = 2) -> TaskSpec:
    """创建一个只依赖源文件内容的固定任务，验证器不在工作区中。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8"
    )
    return TaskSpec(
        task_id="add-fix",
        repository=str(root),
        description="修复 calculator.py 的 add，使其返回两数之和。",
        max_attempts=attempts,
    )


def _verifier() -> IndependentVerifier:
    """返回从工作区外启动、只读取目标文件的独立验证命令。"""
    code = (
        "from pathlib import Path; import sys; "
        "text=Path('calculator.py').read_text(); "
        "sys.exit(0 if 'return a + b' in text else 1)"
    )
    return IndependentVerifier((sys.executable, "-I", "-c", code))


def _provider_factory(task: TaskSpec, attempt: int, feedback: str) -> FakeProvider:
    """第一次产生错误候选，收到独立失败反馈后产生正确候选。"""
    content = (
        "def add(a: int, b: int) -> int:\n    return a + b\n"
        if attempt > 1
        else ("def add(a: int, b: int) -> int:\n    return a * b\n")
    )
    return FakeProvider(
        (
            Message(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        call_id=f"write-{attempt}",
                        name="write_file",
                        arguments={"path": "calculator.py", "content": content},
                    ),
                ),
            ),
            Message(role="assistant", content=f"candidate {attempt} submitted"),
        )
    )


def test_harness_retries_after_independent_failure(tmp_path: Path) -> None:
    """验证首次失败反馈进入第二次 Provider，并最终成功。"""

    async def scenario() -> None:
        """运行两次候选并检查补丁和状态。"""
        task = _task(tmp_path / "source")
        trace_directory = tmp_path / "traces"
        harness = LocalHarness(_verifier(), create_basic_registry, trace_directory=trace_directory)
        result = await harness.run(task, _provider_factory)
        assert result.status == "success"
        assert len(result.attempts) == 2
        assert result.changed_files == ("calculator.py",)
        assert "return a + b" in result.patch
        assert result.attempts[0].verification.status == "failed"
        assert result.attempts[1].verification.passed
        assert len(list(trace_directory.glob("*.jsonl"))) == 2
        assert len(list(trace_directory.glob("*.summary.json"))) == 2

    asyncio.run(scenario())


def test_harness_distinguishes_empty_unresolved_patch(tmp_path: Path) -> None:
    """模型提前结束且空补丁时只能记为未解决，不能当作完成。"""

    async def scenario() -> None:
        """运行没有工具调用的有限 Provider。"""
        task = _task(tmp_path / "source", attempts=1)

        def provider_factory(_: TaskSpec, __: int, ___: str) -> FakeProvider:
            """返回没有修改工作区的提前结束回复。"""
            return FakeProvider((Message(role="assistant", content="done"),))

        result = await LocalHarness(
            _verifier(), create_basic_registry, trace_directory=tmp_path / "traces"
        ).run(task, provider_factory)
        assert result.status == "unresolved"
        assert result.changed_files == ()
        assert result.patch == ""
        assert result.detail == "attempt budget exhausted"

    asyncio.run(scenario())


def test_harness_marks_verifier_failure_as_unevaluable(tmp_path: Path) -> None:
    """验证器进程不存在时分类为不可评测，而不是代码失败。"""

    async def scenario() -> None:
        """执行一个能修改代码但无法启动验证器的任务。"""
        task = _task(tmp_path / "source", attempts=1)
        verifier = IndependentVerifier((str(tmp_path / "missing-verifier"),))
        result = await LocalHarness(
            verifier, create_basic_registry, trace_directory=tmp_path / "traces"
        ).run(task, _provider_factory)
        assert result.status == "unevaluable"
        assert result.attempts[0].verification.status == "error"

    asyncio.run(scenario())


def test_harness_marks_verifier_timeout_separately(tmp_path: Path) -> None:
    """验证器挂起时停止纠错并标记 timeout。"""

    async def scenario() -> None:
        """用短睡眠命令触发验证时限。"""
        task = _task(tmp_path / "source", attempts=3)
        verifier = IndependentVerifier(
            (sys.executable, "-c", "import time; time.sleep(1)"), timeout_seconds=0.01
        )
        result = await LocalHarness(
            verifier, create_basic_registry, trace_directory=tmp_path / "traces"
        ).run(task, _provider_factory)
        assert result.status == "timeout"
        assert len(result.attempts) == 1
        assert result.attempts[0].verification.status == "timeout"

    asyncio.run(scenario())


def test_harness_manages_backend_lifecycle_across_attempts(tmp_path: Path) -> None:
    """Harness 应为整个 trial 只启动一个后端，并在返回前完成清理。"""

    async def scenario() -> None:
        """通过工厂捕获后端，运行纠错后检查唯一启停事件。"""
        task = _task(tmp_path / "source")
        task = task.model_copy(
            update={
                "development_command": (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; print(Path('calculator.py').exists())",
                )
            }
        )
        backends: list[LocalExecutionBackend] = []

        def factory(root: Path) -> LocalExecutionBackend:
            """保存 Harness 创建的后端，供生命周期断言。"""
            backend = LocalExecutionBackend(root)
            backends.append(backend)
            return backend

        result = await LocalHarness(_verifier(), factory, trace_directory=tmp_path / "traces").run(
            task, _provider_factory
        )
        assert result.status == "success"
        assert len(backends) == 1
        kinds = [event.kind for event in backends[0].events]
        assert kinds.count("backend_started") == 1
        assert kinds.count("command_executed") == 2
        assert kinds[-1] == "backend_closed"

    asyncio.run(scenario())
