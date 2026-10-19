"""验证统一产品文件归属、保守撤销、独立 Git 副本与执行能力失败关闭。"""

import asyncio
import json
from pathlib import Path

import pytest
from test_git_workspace import _git, _repository

from codepilot.app import CodePilotApplication
from codepilot.app.operations import changes
from codepilot.config import ProductConfig
from codepilot.contracts import ToolCall
from codepilot.permissions import PermissionedExecutor, PermissionEngine, ask_edits_policy
from codepilot.permissions.approval import ApprovalProvider
from codepilot.permissions.models import ApprovalRequest
from codepilot.tools import create_basic_registry
from codepilot.workspace import BackendLimits, DockerExecutionBackend, LocalExecutionBackend
from codepilot.workspace.changes import ChangeJournal, review
from codepilot.workspace.git import GitTaskWorkspace


def test_product_changes_undo_preserves_dirty_user_files(tmp_path: Path) -> None:
    """正式产品直写已有脏文件，维护命令恢复原始字节和执行位，不碰其他笔记。"""
    root = tmp_path / "root"
    root.mkdir()
    target = root / "a.py"
    target.write_bytes(b"user dirty\r\n")
    target.chmod(0o755)
    (root / "notes").write_text("keep")
    config = ProductConfig(permission="controlled-edit", state_directory=tmp_path / "state")
    result = asyncio.run(
        CodePilotApplication(config, root).run(
            "write",
            direct_tool=ToolCall(
                call_id="write", name="write_file", arguments={"path": "a.py", "content": "agent\n"}
            ),
        )
    )
    identity = result.identity
    preview = changes(config, root, identity.session_id, identity.run_id)
    assert preview["files"][0]["path"] == "a.py" and not preview["applied"]
    assert "agent\n" not in json.dumps(preview)
    assert target.read_text() == "agent\n"
    undone = changes(config, root, identity.session_id, identity.run_id, apply=True)
    assert undone["applied"] and target.read_bytes() == b"user dirty\r\n"
    assert target.stat().st_mode & 0o777 == 0o755
    assert (root / "notes").read_text() == "keep"
    assert not changes(config, root, identity.session_id, identity.run_id, apply=True)["applied"]


def journal_fixture(tmp_path: Path) -> tuple[ChangeJournal, Path]:
    """创建两次连续写同一路径的实际文件工具日志。"""
    root = tmp_path / "root"
    root.mkdir()
    journal = ChangeJournal(create_basic_registry(root), root, tmp_path / "state", "run-test")
    for value in ("first", "second"):
        result = asyncio.run(
            journal.execute(
                ToolCall(
                    call_id=value, name="write_file", arguments={"path": "a", "content": value}
                )
            )
        )
        assert not result.is_error
    journal.finish()
    return journal, root


def test_undo_repeated_writes_and_external_edit_conflict(tmp_path: Path) -> None:
    """整批预检失败不修改任何文件；恢复任务末版后可以反向撤销两次写入。"""
    journal, root = journal_fixture(tmp_path)
    (root / "a").write_text("user later")
    assert review(journal.path, root, apply=True)["conflicts"] == ["a"]
    assert (root / "a").read_text() == "user later"
    (root / "a").write_text("second")
    assert review(journal.path, root, apply=True)["applied"]
    assert not (root / "a").exists()
    assert journal.path.stat().st_mode & 0o777 == 0o600


def test_undo_refuses_symlinks_corruption_and_unknown_intent(tmp_path: Path) -> None:
    """链接替换、正文损坏和崩溃未知意图均不能变成恢复授权。"""
    journal, root = journal_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("private")
    (root / "a").unlink()
    (root / "a").symlink_to(outside)
    with pytest.raises(Exception, match="symbolic links"):
        review(journal.path, root, apply=True)
    assert outside.read_text() == "private"
    (root / "a").unlink()
    (root / "a").write_text("second")
    record = json.loads(journal.path.read_bytes())
    record["state"] = "recording"
    journal.path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="unknown"):
        review(journal.path, root, apply=True)
    record["state"] = "sealed"
    record["entries"][0]["after"]["sha256"] = "0" * 64
    journal.path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="corrupt"):
        review(journal.path, root, apply=True)


def test_journal_storage_failure_prevents_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保存意图发生磁盘失败时，不得先产生工作区副作用。"""
    import codepilot.workspace.changes as module

    root = tmp_path / "root"
    root.mkdir()
    journal = ChangeJournal(create_basic_registry(root), root, tmp_path / "state", "run-test")

    def fail(*args: object) -> None:
        """注入意图保存失败，模拟配额或磁盘故障。"""
        raise OSError("disk full")

    monkeypatch.setattr(module, "save", fail)
    result = asyncio.run(
        journal.execute(
            ToolCall(call_id="write", name="write_file", arguments={"path": "a", "content": "bad"})
        )
    )
    assert result.is_error and not (root / "a").exists()


def test_independent_clone_delivers_committed_binary_without_source_metadata(
    tmp_path: Path,
) -> None:
    """任务副本提交后 status 干净仍导出二进制补丁，且源分支不变。"""
    source = _repository(tmp_path / "repo")
    initial = _git(source, "rev-parse", "HEAD")
    manager = GitTaskWorkspace(
        source,
        tmp_path / "state" / "run",
        session_id="session-test",
        task_id="task-test",
        run_id="run-test",
        independent_repository=True,
    )
    root = manager.prepare()
    assert (root / ".git").is_dir()
    assert not (root / ".git/objects/info/alternates").exists()
    assert _git(root, "remote") == ""
    (root / "blob.bin").write_bytes(b"\0new\xff")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=f@example.invalid",
        "commit",
        "-qm",
        "task",
    )
    assert _git(root, "status", "--porcelain") == ""
    delivery = asyncio.run(manager.finish())
    assert delivery.changed_files == ("blob.bin",) and delivery.replay_status == "passed"
    assert delivery.patch_path and b"GIT binary patch" in delivery.patch_path.read_bytes()
    assert _git(source, "rev-parse", "HEAD") == initial
    assert (source / "blob.bin").read_bytes() == b"\x00base\xff"
    with pytest.raises(ValueError, match="still has changes"):
        manager.cleanup()


def test_hard_quota_and_network_requests_fail_closed(tmp_path: Path) -> None:
    """当前后端未实现容量受限盘或受控网络时，明确拒绝而非静默降级。"""
    limits = BackendLimits(workspace_bytes=1024)
    with pytest.raises(ValueError, match="hard workspace"):
        LocalExecutionBackend(tmp_path, limits=limits)
    with pytest.raises(ValueError, match="hard workspace"):
        DockerExecutionBackend(tmp_path, "fixture", limits=limits)
    with pytest.raises(ValueError, match="network access"):
        DockerExecutionBackend(tmp_path, "fixture", network="bridge")


def test_local_commands_do_not_inherit_provider_or_ssh_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 Shell 不继承合成 Provider key/代理/SSH agent，仍保留 PATH。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-only-never-real")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/synthetic-agent")
    monkeypatch.setenv("HTTPS_PROXY", "https://synthetic.invalid")
    registry = create_basic_registry(tmp_path)
    result = asyncio.run(
        registry.execute(ToolCall(call_id="env", name="shell", arguments={"command": "env"}))
    )
    assert not result.is_error
    assert "synthetic" not in result.content and "PATH=" in result.content


def test_policy_change_and_expired_approval_do_not_start_tools(tmp_path: Path) -> None:
    """人工批准期间策略变化或等待过期，都不能启动已失效的写入。"""
    engine = PermissionEngine(ask_edits_policy())

    class Approval(ApprovalProvider):
        """批准时注入策略版本替换。"""

        async def approve(self, request: ApprovalRequest) -> bool:
            """模拟审批期间管理员把策略更新成新版本。"""
            engine.policy = engine.policy.model_copy(update={"approval_ttl_seconds": 1.0})
            return True

    executor = PermissionedExecutor(create_basic_registry(tmp_path), engine, Approval())
    call = ToolCall(call_id="write", name="write_file", arguments={"path": "a", "content": "bad"})
    result = asyncio.run(executor.execute(call))
    assert result.is_error and "policy changed" in result.content and not (tmp_path / "a").exists()


def test_expired_approval_and_queued_policy_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批准失效与排队后的策略漂移都由执行器拦截，不依赖模型正确判断。"""
    from types import SimpleNamespace

    import codepilot.permissions.executor as module
    from codepilot.permissions import ScriptedApproval
    from codepilot.scheduler import ScheduledExecutor

    class ExpiredApproval(ApprovalProvider):
        """把批准返回时间推进到有效期之外，不使用真实长时间等待。"""

        async def approve(self, request: ApprovalRequest) -> bool:
            """仅替换执行器的时钟视图，保持 asyncio 事件循环时钟不变。"""
            monkeypatch.setattr(
                module, "time", SimpleNamespace(monotonic=lambda: request.issued_at + 301)
            )
            return True

    call = ToolCall(call_id="write", name="write_file", arguments={"path": "a", "content": "bad"})
    executor = PermissionedExecutor(
        create_basic_registry(tmp_path), PermissionEngine(ask_edits_policy()), ExpiredApproval()
    )
    assert asyncio.run(executor.execute(call)).is_error
    monkeypatch.undo()

    async def scenario() -> None:
        """预先占据写路径资源，使批准后的工具真正排队，再改变权限策略。"""
        scheduler = ScheduledExecutor(create_basic_registry(tmp_path))
        engine = PermissionEngine(ask_edits_policy())
        permission = PermissionedExecutor(scheduler, engine, ScriptedApproval([True]))
        blocker = await scheduler._enqueue(scheduler.plan(call))
        await scheduler._acquire(blocker, 0)
        pending = asyncio.create_task(permission.execute(call))
        while not scheduler._waiting:
            await asyncio.sleep(0)
        engine.policy = engine.policy.model_copy(update={"safe_process_reads": False})
        await scheduler._release(blocker.plan)
        result = await pending
        assert result.is_error and not (tmp_path / "a").exists()
        await scheduler.close()

    asyncio.run(scenario())


def test_post_write_external_change_is_not_owned(tmp_path: Path) -> None:
    """工具完成到控制器确认之间的外部写入必须标未知，不能给出撤销授权。"""
    from codepilot.contracts import ToolResult

    class ChangedRegistry:
        """模拟文件写出后另一个参与者立即改了该文件。"""

        specs = create_basic_registry(tmp_path).specs

        async def execute(self, call: ToolCall) -> ToolResult:
            """制造与工具目标不同的最终版本。"""
            (tmp_path / "a").write_text("user concurrent")
            return ToolResult(call_id=call.call_id, content="written")

    journal = ChangeJournal(ChangedRegistry(), tmp_path, tmp_path.parent / "journal", "run-test")
    asyncio.run(
        journal.execute(
            ToolCall(
                call_id="write", name="write_file", arguments={"path": "a", "content": "agent"}
            )
        )
    )
    journal.finish()
    with pytest.raises(ValueError, match="unknown"):
        review(journal.path, tmp_path, apply=True)
    assert (tmp_path / "a").read_text() == "user concurrent"


def test_safe_git_query_does_not_execute_repository_fsmonitor(tmp_path: Path) -> None:
    """原生 Git 查询不得因仓库配置静默执行 fsmonitor，也不继承全局身份配置。"""
    root = _repository(tmp_path / "repo")
    marker = root / "unexpected-hook"
    _git(root, "config", "core.fsmonitor", f"touch {marker}")
    result = asyncio.run(
        create_basic_registry(root).execute(
            ToolCall(call_id="git", name="shell", arguments={"command": "git status --short"})
        )
    )
    assert not result.is_error and not marker.exists()


def test_explicit_deny_overrides_safe_query(tmp_path: Path) -> None:
    """安全查询只减少审批摩擦，不能盖过管理员明确的拒绝规则。"""
    from codepilot.permissions.models import PermissionRule

    policy = ask_edits_policy().model_copy(
        update={"rules": (PermissionRule(decision="deny", tool_names=("shell",)),)}
    )
    executor = PermissionedExecutor(create_basic_registry(tmp_path), PermissionEngine(policy))
    result = asyncio.run(
        executor.execute(ToolCall(call_id="ls", name="shell", arguments={"command": "ls"}))
    )
    assert result.is_error and executor.audits[-1].reason == "matched_deny_rule"


def test_new_file_race_never_overwrites_user_content(tmp_path: Path) -> None:
    """意图落盘后用户新建同名文件，真实写工具必须以原子不存在条件拒绝。"""
    from codepilot.contracts import ToolResult

    class RacingRegistry:
        """在实际注册表启动前制造用户新文件。"""

        specs = create_basic_registry(tmp_path).specs

        async def execute(self, call: ToolCall) -> ToolResult:
            """写入用户内容后委托真实工具，模拟容器传输等待窗口。"""
            (tmp_path / "a").write_text("user created")
            return await create_basic_registry(tmp_path).execute(call)

    journal = ChangeJournal(
        RacingRegistry(), tmp_path, tmp_path.parent / "race-journal", "run-race"
    )
    result = asyncio.run(
        journal.execute(
            ToolCall(
                call_id="write", name="write_file", arguments={"path": "a", "content": "agent"}
            )
        )
    )
    assert result.is_error and (tmp_path / "a").read_text() == "user created"
    journal.finish()
    assert review(journal.path, tmp_path, apply=True)["conflicts"] == ["a"]


def test_project_configuration_cannot_raise_backend_limits(tmp_path: Path) -> None:
    """仓库可以降低数值配额，不能提高用户上限或取消硬限制请求。"""
    from codepilot.config import load_product_config

    user = tmp_path / "user.yaml"
    user.write_text("backend:\n  limits:\n    cpus: 2.0\n    workspace_bytes: 4096\n")
    root = tmp_path / "root"
    root.mkdir()
    project = root / ".codepilot.yaml"
    project.write_text("backend:\n  limits:\n    cpus: 4.0\n")
    with pytest.raises(ValueError, match="不能扩大执行配额"):
        load_product_config(workspace=root, user_path=user)
    project.write_text("backend:\n  limits:\n    workspace_bytes: null\n")
    with pytest.raises(ValueError, match="不能扩大执行配额"):
        load_product_config(workspace=root, user_path=user)
    project.write_text("backend:\n  limits:\n    cpus: 1.0\n")
    assert load_product_config(workspace=root, user_path=user).backend.limits.cpus == 1.0


def test_candidate_git_filters_are_not_executed_by_delivery_controller(tmp_path: Path) -> None:
    """候选新增 Git filter 不能把容器执行逃逸成宿主交付进程执行。"""
    source = _repository(tmp_path / "source")
    manager = GitTaskWorkspace(
        source,
        tmp_path / "state/run",
        session_id="session-test",
        task_id="task-test",
        run_id="run-test",
        independent_repository=True,
    )
    root = manager.prepare()
    marker = tmp_path / "host-filter-must-not-run"
    _git(root, "config", "filter.evil.clean", f"touch {marker}; cat")
    (root / ".gitattributes").write_text("*.txt filter=evil\n")
    (root / "value.txt").write_text("candidate\n")
    delivery = asyncio.run(manager.finish())
    assert delivery.replay_status == "passed" and not marker.exists()
    assert (
        manager.run_directory / "task-git-config.before-delivery"
    ).stat().st_mode & 0o777 == 0o600
    assert _git(source, "status", "--porcelain") == ""


def test_verifier_factory_failure_never_falls_back_to_host(tmp_path: Path) -> None:
    """显式后端启动失败只能记环境错误，不能在宿主重试候选代码。"""
    from codepilot.evals import IndependentVerifier
    from codepilot.workspace import ExecutionBackend

    marker = tmp_path / "host-marker"

    def fail(root: Path) -> ExecutionBackend:
        """模拟选定的隔离后端不可用。"""
        raise RuntimeError("backend unavailable")

    verifier = IndependentVerifier(("touch", str(marker)), backend_factory=fail)
    result = asyncio.run(verifier.verify(tmp_path))
    assert result.status == "error" and not marker.exists()


def test_verifier_cleanup_failure_retains_replay_workspace(tmp_path: Path) -> None:
    """不能确认验证后端关闭时保留其挂载代码，失败也不销毁副作用证据。"""
    source = _repository(tmp_path / "repo")
    manager = GitTaskWorkspace(
        source,
        tmp_path / "state/run",
        session_id="session-test",
        task_id="task-test",
        run_id="run-test",
    )
    root = manager.prepare()
    (root / "value.txt").write_text("changed\n")

    class UnclosedBackend(LocalExecutionBackend):
        """模拟测试命令完成后资源清理失败。"""

        async def close(self) -> None:
            """保留未关闭事实；真实命令已退出，不留下实际活进程。"""
            raise RuntimeError("cleanup failed")

    result = asyncio.run(
        manager.finish(verification_command=("true",), verification_backend_factory=UnclosedBackend)
    )
    assert result.state == "failed_retained"
    assert result.verification and result.verification.status == "error"
    assert manager.replay_root.is_dir()


def test_slash_change_commands_do_not_start_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """交互撤销命令只路由到会话维护，错误参数立即提示而不发送模型。"""
    import argparse

    import codepilot.app.operations as operations
    from codepilot.ui.cli import _handle_slash_command

    seen: list[bool] = []

    def review_command(
        config: ProductConfig, root: Path, session: str, run: str, *, apply: bool = False
    ) -> dict[str, object]:
        """记录外壳是否明确请求执行，只返回有限维护结果。"""
        assert session == "session-test" and run == "run-test"
        seen.append(apply)
        return {"applied": apply}

    monkeypatch.setattr(operations, "changes", review_command)
    args = argparse.Namespace(workspace=tmp_path)
    config = ProductConfig()
    for task in ("/changes run-test", "/undo run-test", "/undo run-test --apply", "/undo"):
        assert _handle_slash_command(task, args, config, "session-test")
    assert seen == [False, False, True]
    assert "用法" in capsys.readouterr().err
