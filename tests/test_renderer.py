"""验证人类终端输出会解释可操作的失败原因。"""

import io
from pathlib import Path

from codepilot.app.models import ProductRunResult, ResourceOwnership, RunIdentity
from codepilot.contracts import RunState
from codepilot.contracts.run import RunReport
from codepilot.contracts.stream import RuntimeEvent
from codepilot.ui.renderer import EventRenderer


def test_model_wait_is_visible_only_in_human_output() -> None:
    """等待模型时人类输出给出取消提示，JSONL 仍只有稳定事件。"""
    event = RuntimeEvent(run_id="run-wait", turn_id="turn-1", sequence=1, kind="model_requested")
    stream = io.StringIO()
    EventRenderer(stream)(event)
    assert "正在等待模型响应" in stream.getvalue()
    assert "Ctrl-C" in stream.getvalue()
    machine = io.StringIO()
    EventRenderer(machine, jsonl=True)(event)
    assert "正在等待" not in machine.getvalue()


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


def test_provider_error_shows_safe_type_and_retry_direction(tmp_path: Path) -> None:
    """连接失败应展示脱敏异常类型，避免用户把它误认为工具执行错误。"""
    stream = io.StringIO()
    result = ProductRunResult(
        identity=RunIdentity(session_id="session-a", task_id="task-a", run_id="run-a"),
        report=RunReport(
            run_id="run-a",
            state=RunState.FAILED,
            reason="provider_error",
            detail="APIConnectionError",
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
    assert "Provider 请求失败：APIConnectionError" in output
    assert "本次未执行后续工具" in output
    assert "检查网关网络状态后重试" in output
