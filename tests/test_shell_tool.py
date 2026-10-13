"""验证基础 Shell 的工作目录、输出、超时和取消清理。"""

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from codepilot.contracts import ToolCall
from codepilot.tools import ShellTool, ToolRegistry, Workspace


def test_shell_reports_exit_code_both_streams_and_fixed_cwd(tmp_path: Path) -> None:
    """命令只能从工作区启动，并保留 stdout、stderr 与非零退出码。"""
    command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "import os,sys; print(os.getcwd()); print('bad', file=sys.stderr); sys.exit(3)"
    )
    result = asyncio.run(
        ToolRegistry((ShellTool(Workspace(tmp_path)),)).execute(
            ToolCall(call_id="shell", name="shell", arguments={"command": command})
        )
    )
    assert result.is_error
    assert "exit_code=3" in result.content
    assert str(tmp_path.resolve()) in result.content and "bad" in result.content


def test_shell_limits_each_output_stream(tmp_path: Path) -> None:
    """持续排空大输出但只把配置上限内的内容返回给模型。"""
    command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "import sys; print('a' * 100); print('b' * 100, file=sys.stderr)"
    )
    result = asyncio.run(
        ToolRegistry((ShellTool(Workspace(tmp_path)),)).execute(
            ToolCall(
                call_id="shell",
                name="shell",
                arguments={"command": command, "max_output_bytes": 20},
            )
        )
    )
    assert "stdout_truncated=true" in result.content
    assert "stderr_truncated=true" in result.content
    assert len(result.content) < 200


def test_registry_timeout_reaps_current_shell_process(tmp_path: Path) -> None:
    """工具级超时取消 Shell 协程，并确认当前进程已经退出。"""

    class FastTimeoutShell(ShellTool):
        """把默认超时缩短到适合单元测试的值。"""

        default_timeout = 0.05

    async def scenario() -> None:
        """运行长等待命令并检查超时结果与进程返回码。"""
        tool = FastTimeoutShell(Workspace(tmp_path))
        registry = ToolRegistry((tool,))
        # exec 让 Shell 被测试进程替换，避免此 M03 测试主动制造后代进程清理问题。
        command = f"exec {shlex.quote(sys.executable)} -c " + shlex.quote(
            "import time; time.sleep(60)"
        )
        result = await registry.execute(
            ToolCall(call_id="shell", name="shell", arguments={"command": command})
        )
        assert result.is_error and result.content.startswith("tool_timeout:")
        assert tool.active_process is not None and tool.active_process.returncode is not None

    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="本测试检查 POSIX 进程组")
def test_timeout_terminates_child_process_group(tmp_path: Path) -> None:
    """命令启动子进程后超时，子进程组也必须被终止。"""

    async def scenario() -> None:
        """记录子进程 PID，超时后确认 PID 已不再存在。"""
        tool = ShellTool(Workspace(tmp_path))
        tool.default_timeout = 0.1
        pid_file = tmp_path / "child.pid"
        child_code = (
            f"import os,time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
        )
        command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
            "import subprocess,time; "
            f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}]); "
            "time.sleep(60)"
        )
        result = await ToolRegistry((tool,)).execute(
            ToolCall(call_id="shell", name="shell", arguments={"command": command})
        )
        assert result.is_error and "tool_timeout" in result.content
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        # 被终止的后代可能短暂处于僵尸态，但不得继续运行或产生输出。
        for _ in range(20):
            proc_stat = Path(f"/proc/{child_pid}/stat")
            if not proc_stat.exists():
                break
            state = proc_stat.read_text(encoding="utf-8").split()[2]
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("child process is still running after process-group cleanup")

    asyncio.run(scenario())
