"""演示 M06 的首次失败、反馈纠错、空补丁和验证器超时轨迹。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from codepilot.contracts import Message, ToolCall
from codepilot.evals import IndependentVerifier, LocalHarness, TaskSpec
from codepilot.providers.fake import FakeProvider
from codepilot.tools.factory import create_basic_registry


def _provider(task: TaskSpec, attempt: int, feedback: str) -> FakeProvider:
    """按尝试次数返回错误或正确的固定修复候选。"""
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
            Message(role="assistant", content="候选已提交"),
        )
    )


def main() -> None:
    """创建一次临时任务并输出可观察的 M06 纠错摘要。"""
    with tempfile.TemporaryDirectory(prefix="codepilot-m06-") as directory:
        root = Path(directory) / "source"
        root.mkdir()
        (root / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8"
        )
        verifier_code = (
            "from pathlib import Path; import sys; "
            "sys.exit(0 if 'return a + b' in Path('calculator.py').read_text() else 1)"
        )
        task = TaskSpec(
            task_id="add-fix",
            repository=str(root),
            description="修复 add 函数。",
            max_attempts=2,
        )
        result = asyncio.run(
            LocalHarness(
                IndependentVerifier((sys.executable, "-I", "-c", verifier_code)),
                create_basic_registry,
                trace_directory=Path(".codepilot-traces/evals"),
            ).run(task, _provider)
        )
        print(
            json.dumps(
                {
                    "task_id": result.task_id,
                    "status": result.status,
                    "attempts": len(result.attempts),
                    "verification": [record.verification.status for record in result.attempts],
                    "changed_files": result.changed_files,
                    "detail": result.detail,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
