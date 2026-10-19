"""真实 Rootless Docker 中验收独立 Git 副本、提交交付和直写撤销。"""

import asyncio
import os
from pathlib import Path

import pytest
from test_git_workspace import _git, _repository

from codepilot.app import CodePilotApplication
from codepilot.app.operations import changes
from codepilot.config import BackendConfig, GitWorkspaceConfig, ProductConfig
from codepilot.contracts import ToolCall
from codepilot.permissions import ScriptedApproval

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="需要显式启用 Rootless Docker"
)


def test_product_isolated_docker_git_commit_and_binary_delivery(tmp_path: Path) -> None:
    """统一产品 Shell 在真实容器提交，二进制补丁独立重放且源仓库保持原样。"""
    root = _repository(tmp_path / "repo")
    initial = _git(root, "rev-parse", "HEAD")
    config = ProductConfig(
        state_directory=tmp_path / "state",
        permission="controlled-edit",
        backend=BackendConfig(kind="docker", docker_image="codepilot-sandbox:0.1.0"),
        git_workspace=GitWorkspaceConfig(
            enabled=True,
            verification_command=(
                "python3",
                "-c",
                "import subprocess; subprocess.run(['git', 'status', '--short'], check=True); "
                "from pathlib import Path; assert str(Path.cwd()) == '/workspace'; "
                "assert Path('blob.bin').read_bytes()==b'\\x00new\\xff'",
            ),
        ),
    )
    command = (
        "git status --short && git log -1 --oneline && "
        'python3 -c "from pathlib import Path; '
        "Path('blob.bin').write_bytes(b'\\x00new\\xff')\" && git add blob.bin && "
        "git -c user.name=Fixture -c user.email=f@example.invalid commit -qm task && "
        "git status --short"
    )
    result = asyncio.run(
        CodePilotApplication(config, root, approval=ScriptedApproval([True])).run(
            "isolated Git",
            direct_tool=ToolCall(
                call_id="git", name="shell", arguments={"command": command, "timeout_seconds": 30.0}
            ),
        )
    )
    assert result.report.reason == "completed" and result.resources.backend_closed
    assert result.delivery and result.delivery.replay_status == "passed"
    assert result.delivery.verification and result.delivery.verification.passed
    assert result.delivery.changed_files == ("blob.bin",)
    assert (result.resources.workspace / ".git").is_dir()
    assert _git(root, "rev-parse", "HEAD") == initial
    assert _git(root, "status", "--porcelain") == ""


def test_product_direct_docker_file_undo(tmp_path: Path) -> None:
    """容器文件工具的控制器日志与当前目录共享，零模型撤销保留用户笔记。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").write_bytes(b"user\r\n")
    (root / "notes").write_text("keep")
    config = ProductConfig(
        state_directory=tmp_path / "state",
        permission="controlled-edit",
        backend=BackendConfig(kind="docker", docker_image="codepilot-sandbox:0.1.0"),
    )
    result = asyncio.run(
        CodePilotApplication(config, root).run(
            "edit",
            direct_tool=ToolCall(
                call_id="write", name="write_file", arguments={"path": "a", "content": "agent"}
            ),
        )
    )
    assert result.resources.backend_closed
    identity = result.identity
    assert changes(config, root, identity.session_id, identity.run_id, apply=True)["applied"]
    assert (root / "a").read_bytes() == b"user\r\n"
    assert (root / "notes").read_text() == "keep"
    created = asyncio.run(
        CodePilotApplication(config, root).run(
            "create",
            direct_tool=ToolCall(
                call_id="new", name="write_file", arguments={"path": "new.txt", "content": "new"}
            ),
        )
    )
    assert created.resources.backend_closed and (root / "new.txt").read_text() == "new"
    assert changes(config, root, created.identity.session_id, created.identity.run_id, apply=True)[
        "applied"
    ]
    assert not (root / "new.txt").exists()
