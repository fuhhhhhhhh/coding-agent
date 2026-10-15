"""验证统一 CLI 的交互、JSONL 输出、退出码和副作用前配置失败。"""

import argparse
import json
import re
from pathlib import Path

import pytest

from codepilot.ui.cli import _overrides, build_parser, main


def test_cli_worktree_flag_explicitly_enables_isolation() -> None:
    """新的 worktree 参数应显式开启隔离，并拒绝与旧直写参数同时出现。"""
    parser = build_parser()
    arguments = parser.parse_args(["--worktree"])

    assert _overrides(arguments)["git_workspace"] == {"enabled": True}

    conflicting = parser.parse_args(["--worktree", "--direct-workspace"])
    with pytest.raises(ValueError, match="cannot be used together"):
        _overrides(conflicting)


def test_cli_fake_run_emits_stable_identity_and_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式 run 命令必须沿产品主链完成，并输出相同的会话、任务和运行 ID。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    code = main(
        [
            "run",
            "检查目录",
            "--workspace",
            str(workspace),
            "--permission",
            "read-only",
            "--state-directory",
            str(state),
            "--session-id",
            "session-cli",
            "--task-id",
            "task-cli",
            "--run-id",
            "run-cli",
            "--jsonl",
            "--direct-workspace",
        ]
    )

    assert code == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["kind"] == "product_started"
    assert records[-1]["kind"] == "product_result"
    assert records[-1]["session_id"] == "session-cli"
    assert records[-1]["task_id"] == "task-cli"
    assert records[-1]["run_id"] == "run-cli"
    assert records[-1]["backend_closed"] is True


def test_cli_missing_key_fails_before_state_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Provider 缺少密钥时返回配置错误，且不创建 Trace 或后端目录。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("CODEPILOT_TEST_MISSING_KEY", raising=False)

    code = main(
        [
            "run",
            "检查目录",
            "--workspace",
            str(workspace),
            "--provider",
            "openai-compatible",
            "--model",
            "qwen-test",
            "--base-url",
            "https://mock.invalid/v1",
            "--api-key-env",
            "CODEPILOT_TEST_MISSING_KEY",
            "--state-directory",
            str(state),
            "--direct-workspace",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "CODEPILOT_TEST_MISSING_KEY" in captured.err
    assert not state.exists()


def test_bare_cli_runs_multiple_tasks_in_one_interactive_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接运行 codepilot 后应连续接收任务，共用会话 ID 并为每条任务单独执行。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    answers = iter(("检查一次", "检查第二次", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    code = main(
        [
            "--workspace",
            str(workspace),
            "--permission",
            "read-only",
            "--state-directory",
            str(state),
            "--direct-workspace",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    sessions = re.findall(r"会话 (session-[0-9a-f]+)｜任务", captured.out)
    assert len(sessions) == 2
    assert sessions[0] == sessions[1]
    assert captured.out.count("[运行结束] completed") == 2


def test_interactive_task_start_failure_does_not_close_prompt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条任务意外启动失败后，交互外壳仍应读取并执行下一条输入。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    answers = iter(("第一条", "第二条", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    calls: list[str] = []

    async def fake_run(arguments: argparse.Namespace, _config: object) -> int:
        """第一次注入启动异常，第二次返回成功以验证交互循环仍存活。"""
        task = arguments.task
        calls.append(task)
        if len(calls) == 1:
            raise RuntimeError("synthetic failure")
        return 0

    monkeypatch.setattr("codepilot.ui.cli._run", fake_run)

    code = main(["--workspace", str(workspace), "--direct-workspace"])

    assert code == 0
    assert calls == ["第一条", "第二条"]
    assert "RuntimeError；可以继续输入" in capsys.readouterr().err


def test_interactive_eof_exits_without_starting_a_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终端 EOF 应正常退出，不能生成一个空任务或错误终态。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def end_of_input(_prompt: str) -> str:
        """模拟 Ctrl-D 或管道输入关闭。"""
        raise EOFError

    monkeypatch.setattr("builtins.input", end_of_input)

    code = main(["--workspace", str(workspace), "--direct-workspace"])

    assert code == 0
    assert "[运行结束]" not in capsys.readouterr().out


def test_run_accepts_shared_options_before_subcommand(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享选项放在 run 前也不能被子解析器的默认值覆盖。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    code = main(
        [
            "--workspace",
            str(workspace),
            "--permission",
            "read-only",
            "--state-directory",
            str(state),
            "run",
            "检查目录",
            "--jsonl",
            "--direct-workspace",
        ]
    )

    assert code == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]["kind"] == "product_result"
