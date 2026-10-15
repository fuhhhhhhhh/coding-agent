"""验证人类终端输出会解释可操作的失败原因。"""

import io
from pathlib import Path

from codepilot.app.models import ProductRunResult, ResourceOwnership, RunIdentity
from codepilot.contracts import RunState
from codepilot.contracts.run import RunReport
from codepilot.ui.renderer import EventRenderer


def test_output_limit_explains_that_partial_tool_was_not_executed(tmp_path: Path) -> None:
    """输出截断应给出调额方向，并明确半截工具调用没有执行。"""
    stream = io.StringIO()
    result = ProductRunResult(
        identity=RunIdentity(session_id="session-a", task_id="task-a", run_id="run-a"),
        report=RunReport(
            run_id="run-a",
            state=RunState.FAILED,
            reason="output_limit",
            steps=1,
            tool_calls=0,
            messages=(),
            observations=(),
        ),
        resources=ResourceOwnership(
            workspace=tmp_path,
            state_directory=tmp_path / "state",
            backend_id="backend-a",
            backend_closed=True,
            context_artifacts_cleaned=True,
        ),
        permission_audits=(),
        backend_events=(),
    )

    EventRenderer(stream).finished(result)

    output = stream.getvalue()
    assert "output_limit" in output
    assert "max_tokens" in output
    assert "不完整工具调用未执行" in output
