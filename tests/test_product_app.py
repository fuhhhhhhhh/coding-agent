"""验证统一产品主链的正常运行、初始化失败、取消和资源清理。"""

import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

import codepilot.app.service as service
from codepilot.app import CodePilotApplication
from codepilot.config import BackendConfig, GitWorkspaceConfig, ProductConfig, ProviderConfig
from codepilot.contracts import Message, ToolCall
from codepilot.providers.base import Provider
from codepilot.providers.fake import FakeProvider
from codepilot.workspace import LocalExecutionBackend


def _config(state: Path) -> ProductConfig:
    """为产品测试创建只读、离线且状态目录外置的确定性配置。"""
    return ProductConfig(
        provider=ProviderConfig(),
        backend=BackendConfig(),
        permission="read-only",
        state_directory=state,
        git_workspace=GitWorkspaceConfig(enabled=False),
    )


def test_product_chain_runs_fake_and_closes_owned_resources(tmp_path: Path) -> None:
    """fake 任务必须经过后端、调度、权限、Context、Runner 和 Trace 全链。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("fixture\n")
    state = tmp_path / "state"

    result = asyncio.run(
        CodePilotApplication(_config(state), workspace).run(
            "检查工作区",
            session_id="session-test",
            task_id="task-test",
            run_id="run-test",
        )
    )

    assert result.report.reason == "completed"
    assert result.report.tool_calls == 1
    assert [audit.outcome for audit in result.permission_audits] == ["allowed"]
    assert [event.kind for event in result.backend_events] == [
        "backend_started",
        "tool_executed",
        "backend_closed",
    ]
    assert result.resources.backend_closed
    assert result.resources.context_artifacts_cleaned
    assert result.resources.trace_path is not None
    assert result.resources.trace_path.stat().st_mode & 0o777 == 0o600


def test_product_chain_writes_only_task_worktree_and_exports_verified_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式产品链应隔离写入、导出补丁并在干净起点执行固定复验。"""
    workspace = tmp_path / "repository"
    workspace.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=workspace, check=True)
    subprocess.run(("git", "config", "user.name", "Test"), cwd=workspace, check=True)
    subprocess.run(
        ("git", "config", "user.email", "test@example.invalid"), cwd=workspace, check=True
    )
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=workspace, check=True)
    subprocess.run(("git", "commit", "-qm", "base"), cwd=workspace, check=True)
    verification_script = (
        "from pathlib import Path; assert Path('answer.py').read_text() == 'answer = 42\\n'"
    )
    config = ProductConfig(
        provider=ProviderConfig(),
        backend=BackendConfig(),
        permission="controlled-edit",
        state_directory=tmp_path / "state",
        git_workspace=GitWorkspaceConfig(
            verification_command=(
                sys.executable,
                "-c",
                verification_script,
            )
        ),
    )

    def provider_factory(*arguments: object, **keywords: object) -> service._ProviderHandle:
        """让产品链确定性写入一个文件后结束。"""
        del arguments, keywords
        return service._ProviderHandle(
            FakeProvider(
                (
                    Message(
                        role="assistant",
                        tool_calls=(
                            ToolCall(
                                call_id="write-answer",
                                name="write_file",
                                arguments={"path": "answer.py", "content": "answer = 42\n"},
                            ),
                        ),
                    ),
                    Message(role="assistant", content="完成"),
                )
            )
        )

    monkeypatch.setattr(service, "_create_provider", provider_factory)
    result = asyncio.run(CodePilotApplication(config, workspace).run("创建答案文件"))

    assert not (workspace / "answer.py").exists()
    assert result.delivery is not None
    assert result.delivery.state == "changes_retained"
    assert result.delivery.changed_files == ("answer.py",)
    assert result.delivery.verification is not None and result.delivery.verification.passed
    assert (result.delivery.workspace / "answer.py").read_text(encoding="utf-8") == "answer = 42\n"


def test_invalid_product_input_has_no_state_side_effect(tmp_path: Path) -> None:
    """空任务和位于工作区内的状态目录必须在启动后端前失败。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="outside workspace"):
        CodePilotApplication(_config(workspace / ".state"), workspace)
    assert not (workspace / ".state").exists()

    state = tmp_path / "state"
    application = CodePilotApplication(_config(state), workspace)
    with pytest.raises(ValueError, match="task must not be empty"):
        asyncio.run(application.run("   "))
    assert not state.exists()


def test_provider_initialization_failure_still_closes_backend_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider 装配异常时也必须走统一关闭出口，不能留下活动后端或文件。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: list[LocalExecutionBackend] = []

    def backend_factory(
        config: ProductConfig, root: Path, audit_directory: Path
    ) -> LocalExecutionBackend:
        """记录尚未启动的真实 Local 后端，供失败后核对关闭状态。"""
        backend = LocalExecutionBackend(root, audit_directory=audit_directory)
        captured.append(backend)
        return backend

    def broken_provider(*arguments: object, **keywords: object) -> service._ProviderHandle:
        """在任何模型请求前模拟 SDK 客户端初始化失败。"""
        raise RuntimeError("provider initialization failed")

    monkeypatch.setattr(service, "_create_backend", backend_factory)
    monkeypatch.setattr(service, "_create_provider", broken_provider)
    with pytest.raises(RuntimeError, match="provider initialization failed"):
        asyncio.run(CodePilotApplication(_config(tmp_path / "state"), workspace).run("检查"))

    assert captured
    assert [event.kind for event in captured[0].events] == ["backend_closed"]
    assert not list((tmp_path / "state").rglob("*.txt"))


def test_backend_start_failure_uses_the_same_cleanup_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后端部分启动后抛错时，产品层仍需调用幂等 close 回收其所有资源。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured: list[LocalExecutionBackend] = []

    class FailingBackend(LocalExecutionBackend):
        """在记录启动后模拟后端握手失败。"""

        async def start(self) -> None:
            """先进入已启动状态，再抛出错误以模拟部分初始化。"""
            await super().start()
            raise RuntimeError("backend handshake failed")

    def backend_factory(
        config: ProductConfig, root: Path, audit_directory: Path
    ) -> LocalExecutionBackend:
        """创建并保留故障后端，供退出后检查关闭事件。"""
        backend = FailingBackend(root, audit_directory=audit_directory)
        captured.append(backend)
        return backend

    monkeypatch.setattr(service, "_create_backend", backend_factory)
    with pytest.raises(RuntimeError, match="backend handshake failed"):
        asyncio.run(CodePilotApplication(_config(tmp_path / "state"), workspace).run("检查"))

    assert [event.kind for event in captured[0].events] == [
        "backend_started",
        "backend_closed",
    ]


class _WaitingProvider(Provider):
    """保持模型请求挂起，直到测试取消产品运行。"""

    def __init__(self) -> None:
        """创建可观察请求起点和永不主动结束的等待事件。"""
        self.started = asyncio.Event()
        self.wait_forever = asyncio.Event()

    async def complete(self, messages: Sequence[Message]) -> Message:
        """确认输入存在后标记已启动，并等待外部取消。"""
        assert messages
        self.started.set()
        await self.wait_forever.wait()
        return Message(role="assistant", content="不可到达")


def test_cancellation_reaches_backend_and_records_one_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消必须停止 Provider、封闭后端并只写入一条 cancelled 运行终态。"""

    async def scenario() -> None:
        """启动挂起模型请求，取消产品任务后检查内存事件和持久 Trace。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state = tmp_path / "state"
        provider = _WaitingProvider()
        backends: list[LocalExecutionBackend] = []

        def backend_factory(
            config: ProductConfig, root: Path, audit_directory: Path
        ) -> LocalExecutionBackend:
            """保留产品创建的 Local 后端以核对取消和关闭事件。"""
            backend = LocalExecutionBackend(root, audit_directory=audit_directory)
            backends.append(backend)
            return backend

        def provider_factory(*arguments: object, **keywords: object) -> service._ProviderHandle:
            """把联网或脚本 Provider 替换为可取消的测试 Provider。"""
            return service._ProviderHandle(provider)

        monkeypatch.setattr(service, "_create_backend", backend_factory)
        monkeypatch.setattr(service, "_create_provider", provider_factory)
        task = asyncio.create_task(
            CodePilotApplication(_config(state), workspace).run(
                "等待取消",
                session_id="session-cancel",
                task_id="task-cancel",
                run_id="run-cancel",
            )
        )
        await provider.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert [event.kind for event in backends[0].events] == [
            "backend_started",
            "backend_cancelled",
            "backend_closed",
        ]
        trace = next(state.rglob("trace-*.jsonl"))
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        terminal = [record for record in records if record["kind"] == "run_finished"]
        assert len(terminal) == 1
        assert terminal[0]["outcome"] == "cancelled"

    asyncio.run(scenario())
