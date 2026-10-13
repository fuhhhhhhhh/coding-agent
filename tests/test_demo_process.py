"""从临时工作目录检查演示入口的可观察行为。"""

import subprocess
import sys
from pathlib import Path

import pytest

from codepilot.contracts import AgentEvent


def test_demo_process_outputs_two_events_without_reading_files(tmp_path: Path) -> None:
    """证明 M01 子进程只输出请求，不会访问请求中提到的文件。"""
    config = tmp_path / "config.toml"
    config.write_text('provider = "fake"\nmodel = "fake-m01"\n', encoding="utf-8")
    before = sorted(path.name for path in tmp_path.iterdir())
    result = subprocess.run(
        [sys.executable, "-m", "codepilot.demo", str(config)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    events = [AgentEvent.model_validate_json(line) for line in result.stdout.splitlines()]
    assert len(events) == 2
    assert events[0].message.content
    assert events[1].message.tool_calls[0].arguments == {"path": "README.md"}
    # 此处不存在 README.md：fake 回复必须仍是请求，不能变成文件结果。
    assert not (tmp_path / "README.md").exists()
    assert all(event.message.tool_result is None for event in events)
    assert sorted(path.name for path in tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ('provider = "real-provider"', "ValidationError"),
        (None, "FileNotFoundError"),
        ('provider = "unterminated', "TOMLDecodeError"),
    ],
)
def test_invalid_config_exits_before_emitting_events(
    tmp_path: Path, content: str | None, error: str
) -> None:
    """确保配置失败产生非零退出码，且不输出残缺事件流。"""
    config = tmp_path / "config.toml"
    if content is not None:
        config.write_text(content, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "codepilot.demo", str(config)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr
