"""验证统一 CLI 的 JSONL 输出、退出码和副作用前配置失败。"""

import json
from pathlib import Path

import pytest

from codepilot.ui.cli import main


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
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "CODEPILOT_TEST_MISSING_KEY" in captured.err
    assert not state.exists()
