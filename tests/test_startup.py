"""验证终端启动不提前加载 SDK、发请求或创建任务。"""

import os
import subprocess
import sys
from pathlib import Path


def test_prompt_startup_does_not_load_sdk_or_create_state(tmp_path: Path) -> None:
    """真实子进程启动并退出提示符后，SDK 未加载且没有会话副作用。"""
    code = (
        "import sys; from codepilot.ui.cli import main; "
        "assert 'openai' not in sys.modules; "
        "assert main(['--workspace', sys.argv[1], '--state-directory', sys.argv[2]]) == 0; "
        "assert 'openai' not in sys.modules"
    )
    state = tmp_path / "state"
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(state)],
        input="/exit\n",
        text=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    assert process.returncode == 0, process.stderr
    assert "CodePilot" in process.stdout
    assert not state.exists()


def test_checkpoint_module_imports_in_fresh_process() -> None:
    """检查点可独立导入，不能依赖 app 先导入才能避开循环依赖。"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import codepilot.sessions.checkpoints; import sys; assert 'openai' not in sys.modules",
        ],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
