"""验证任务 worktree 隔离、补丁重放、复验和保守清理。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codepilot.contracts import ToolCall
from codepilot.tools import create_basic_registry
from codepilot.workspace.git import GitTaskWorkspace


def _git(root: Path, *arguments: str) -> str:
    """在测试仓库运行固定 Git 命令并返回文本。"""
    process = subprocess.run(
        ("git", *arguments),
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr)
    return process.stdout


def _repository(root: Path) -> Path:
    """创建包含文本、可执行文件和二进制文件的干净测试仓库。"""
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "CodePilot Test")
    _git(root, "config", "user.email", "codepilot@example.invalid")
    (root / "value.txt").write_text("base\n", encoding="utf-8")
    (root / "remove.txt").write_text("remove\n", encoding="utf-8")
    (root / "script.sh").write_text("#!/bin/sh\necho base\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00base\xff")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _manager(source: Path, state: Path, suffix: str) -> GitTaskWorkspace:
    """为测试建立带唯一身份的任务工作树管理器。"""
    return GitTaskWorkspace(
        source,
        state / suffix,
        session_id="session-git",
        task_id=f"task-{suffix}",
        run_id=f"run-{suffix}",
    )


def test_two_task_worktrees_do_not_pollute_each_other_or_source(tmp_path: Path) -> None:
    """同一 commit 的两个任务应能写出不同结果，且源工作树始终保持干净。"""

    async def scenario() -> None:
        """并行准备任务目录，再分别导出可独立重放的补丁。"""
        source = _repository(tmp_path / "repo")
        first = _manager(source, tmp_path / "state", "first")
        second = _manager(source, tmp_path / "state", "second")
        first_root, second_root = await asyncio.gather(
            asyncio.to_thread(first.prepare), asyncio.to_thread(second.prepare)
        )
        (first_root / "value.txt").write_text("first\n", encoding="utf-8")
        (second_root / "value.txt").write_text("second\n", encoding="utf-8")

        first_delivery, second_delivery = await asyncio.gather(first.finish(), second.finish())

        assert (source / "value.txt").read_text(encoding="utf-8") == "base\n"
        assert (first_root / "value.txt").read_text(encoding="utf-8") == "first\n"
        assert (second_root / "value.txt").read_text(encoding="utf-8") == "second\n"
        assert first_delivery.patch_sha256 != second_delivery.patch_sha256
        assert first_delivery.replay_status == second_delivery.replay_status == "passed"
        assert _git(source, "status", "--porcelain") == ""

    asyncio.run(scenario())


def test_patch_preserves_add_delete_mode_and_binary_then_runs_verifier(tmp_path: Path) -> None:
    """交付补丁应覆盖新增、删除、模式和二进制内容，并在干净起点复验。"""

    async def scenario() -> None:
        """修改四类 Git 内容并要求 replay 工作树执行固定断言。"""
        source = _repository(tmp_path / "repo")
        manager = _manager(source, tmp_path / "state", "delivery")
        workspace = manager.prepare()
        (workspace / "new.txt").write_text("new\n", encoding="utf-8")
        (workspace / "remove.txt").unlink()
        (workspace / "blob.bin").write_bytes(b"\x00changed\xfe")
        os.chmod(workspace / "script.sh", 0o755)
        command = (
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "assert Path('new.txt').read_text() == 'new\\n'; "
            "assert not Path('remove.txt').exists(); "
            "assert Path('blob.bin').read_bytes() == b'\\x00changed\\xfe'; "
            "assert Path('script.sh').stat().st_mode & 0o111",
        )

        delivery = await manager.finish(verification_command=command)

        assert delivery.replay_status == "passed"
        assert delivery.verification is not None and delivery.verification.passed
        assert delivery.patch_path is not None
        assert delivery.patch_path.stat().st_mode & 0o777 == 0o600
        patch = delivery.patch_path.read_bytes()
        assert b"GIT binary patch" in patch
        assert b"deleted file mode" in patch
        assert b"new file mode 100644" in patch
        assert b"old mode 100644" in patch and b"new mode 100755" in patch
        with pytest.raises(ValueError, match="still has changes"):
            manager.cleanup()

    asyncio.run(scenario())


def test_invalid_base_and_dirty_source_fail_before_worktree_creation(tmp_path: Path) -> None:
    """不存在的 commit 或脏源目录应在创建任务 worktree 前明确失败。"""
    source = _repository(tmp_path / "repo")
    invalid = GitTaskWorkspace(
        source,
        tmp_path / "state" / "invalid",
        session_id="session-git",
        task_id="task-invalid",
        run_id="run-invalid",
        base_commit="missing-commit",
    )
    with pytest.raises(ValueError, match="git command failed"):
        invalid.prepare()
    assert not invalid.worktree_root.exists()

    (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    dirty = _manager(source, tmp_path / "state", "dirty")
    dirty.require_clean_source = True
    with pytest.raises(ValueError, match="must be clean"):
        dirty.prepare()
    assert not dirty.worktree_root.exists()


def test_non_git_workspace_explains_worktree_requirement(tmp_path: Path) -> None:
    """显式隔离模式遇到非 Git 目录时应说明它需要已有 commit。"""
    source = tmp_path / "plain"
    source.mkdir()
    manager = _manager(source, tmp_path / "state", "plain")

    with pytest.raises(ValueError, match="worktree mode requires"):
        manager.prepare()


def test_shell_supports_common_git_workflow_in_dirty_workspace(tmp_path: Path) -> None:
    """Shell 应直接支持初始化、分支、提交及本地远端的 fetch/pull/push。"""

    async def scenario() -> None:
        """经同一个 Shell 工具执行本地 Git 流程，并保留预先存在的未跟踪文件。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "user-note.txt").write_text("用户尚未提交\n", encoding="utf-8")
        registry = create_basic_registry(workspace)

        async def shell(command: str, suffix: str) -> None:
            """执行一条 Git Shell 命令，并要求工具层报告成功。"""
            result = await registry.execute(
                ToolCall(call_id=f"git-{suffix}", name="shell", arguments={"command": command})
            )
            assert not result.is_error, result.content
            assert "exit_code=0" in result.content

        await shell("git init -q -b main", "init")
        await shell(
            "git config user.name 'CodePilot Test' && "
            "git config user.email codepilot@example.invalid",
            "identity",
        )
        (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
        await shell("git add tracked.txt && git commit -qm base", "commit")
        await shell("git switch -qc feature", "branch")

        remote = tmp_path / "remote.git"
        _git(tmp_path, "init", "--bare", "-q", str(remote))
        await shell(f"git remote add origin {remote} && git push -qu origin feature", "push")

        collaborator = tmp_path / "collaborator"
        _git(tmp_path, "clone", "-q", "--branch", "feature", str(remote), str(collaborator))
        _git(collaborator, "config", "user.name", "Collaborator")
        _git(collaborator, "config", "user.email", "collaborator@example.invalid")
        (collaborator / "remote.txt").write_text("remote\n", encoding="utf-8")
        _git(collaborator, "add", "remote.txt")
        _git(collaborator, "commit", "-qm", "remote change")
        _git(collaborator, "push", "-q", "origin", "feature")

        await shell("git fetch origin && git pull --ff-only origin feature", "fetch-pull")

        assert (workspace / "remote.txt").read_text(encoding="utf-8") == "remote\n"
        assert (workspace / "user-note.txt").read_text(encoding="utf-8") == "用户尚未提交\n"

    asyncio.run(scenario())


def test_replay_failure_retains_patch_and_original_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补丁无法重放时应返回失败交付状态，同时保留原修改和补丁。"""

    async def scenario() -> None:
        """只在 replay apply 阶段注入冲突，核对保守保留行为。"""
        source = _repository(tmp_path / "repo")
        manager = _manager(source, tmp_path / "state", "replay-failure")
        workspace = manager.prepare()
        (workspace / "value.txt").write_text("changed\n", encoding="utf-8")
        original = manager._git_at

        def fail_apply(cwd: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
            """让验证副本的 apply 明确失败，其余 Git 操作保持真实。"""
            if cwd == manager.replay_root and arguments and arguments[0] == "apply":
                raise ValueError("synthetic replay conflict")
            return original(cwd, *arguments, env=env)

        monkeypatch.setattr(manager, "_git_at", fail_apply)
        delivery = await manager.finish()

        assert delivery.state == "failed_retained"
        assert delivery.replay_status == "failed"
        assert delivery.patch_path is not None and delivery.patch_path.is_file()
        assert (workspace / "value.txt").read_text(encoding="utf-8") == "changed\n"
        assert not manager.replay_root.exists()

    asyncio.run(scenario())
