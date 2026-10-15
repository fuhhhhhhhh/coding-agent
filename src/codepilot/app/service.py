"""组装并运行正式产品链，统一拥有 Provider、后端和临时资源生命周期。"""

import asyncio
import os
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI

from codepilot.app.models import ProductRunResult, ResourceOwnership, RunIdentity
from codepilot.config import ProductConfig
from codepilot.context import (
    ContextArtifactStore,
    ContextManager,
    ContextSeed,
    RepositoryRule,
    assemble_initial_context,
)
from codepilot.contracts import Message, ToolCall
from codepilot.contracts.stream import RuntimeEvent
from codepilot.credentials import resolve_provider_api_key
from codepilot.observability import EventRecorder
from codepilot.permissions import (
    ApprovalProvider,
    ConsoleApproval,
    PermissionedExecutor,
    PermissionEngine,
    PermissionPolicy,
    ask_edits_policy,
    controlled_edit_policy,
    read_only_policy,
)
from codepilot.providers.base import Provider
from codepilot.providers.fake import FakeProvider
from codepilot.providers.openai_compatible import OpenAICompatibleProvider, RequestBudget
from codepilot.providers.stream import StreamingProvider
from codepilot.runtime import Runner
from codepilot.scheduler import ScheduledExecutor
from codepilot.sessions.outputs import CURRENT_OUTPUT, OutputStore
from codepilot.sessions.store import SessionStore, process_identity, safe_id
from codepilot.tools import create_basic_registry
from codepilot.tools.base import ToolSpec
from codepilot.workspace import DockerExecutionBackend, ExecutionBackend, LocalExecutionBackend
from codepilot.workspace.git import GitDelivery, GitTaskWorkspace


class EventConsumer(Protocol):
    """约束终端界面只接收 Runtime 事件，不调用 Provider 或工具。"""

    def __call__(self, event: RuntimeEvent) -> None:
        """同步消费一条已经写入规范内存轨迹的事件。"""
        ...


class _ProviderHandle:
    """把 Provider 和可重复调用的异步关闭动作绑定在一起。"""

    def __init__(
        self,
        provider: Provider | StreamingProvider,
        client: AsyncOpenAI | None = None,
    ) -> None:
        """保存核心 Provider；只有联网实现才持有需要关闭的 SDK 客户端。"""
        self.provider = provider
        self.client = client
        self._closed = False

    async def close(self) -> None:
        """关闭 SDK 连接池；fake Provider 的关闭是幂等空操作。"""
        if self._closed:
            return
        self._closed = True
        if self.client is not None:
            await self.client.close()


class CodePilotApplication:
    """为一次新任务建立正式组件链，并在任意退出路径统一回收资源。"""

    def __init__(
        self,
        config: ProductConfig,
        workspace: Path,
        *,
        approval: ApprovalProvider | None = None,
        on_event: EventConsumer | None = None,
    ) -> None:
        """校验工作区和状态目录关系；此阶段不创建文件、容器或网络连接。"""
        self.config = config
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        self.state_root = _state_root(config).absolute()
        if self.state_root.is_relative_to(self.workspace):
            raise ValueError("state directory must be outside workspace")
        # 密钥在创建目录、容器或 Trace 前解析，并且只保存在当前应用对象内存中。
        self._api_key = resolve_provider_api_key(config.provider)
        self.approval = approval
        self.on_event = on_event

    async def run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> ProductRunResult:
        """执行一个新任务，并在返回结果前关闭后端、客户端及临时 Artifact。"""
        if not task.strip():
            raise ValueError("task must not be empty")
        identity = RunIdentity(
            session_id=session_id or f"session-{uuid.uuid4().hex}",
            task_id=task_id or f"task-{uuid.uuid4().hex}",
            run_id=run_id or f"run-{uuid.uuid4().hex}",
        )
        for value in (identity.session_id, identity.task_id, identity.run_id):
            safe_id(value)
        with SessionStore(self.state_root, identity.session_id) as session:
            session.secrets = (self._api_key,) if self._api_key else ()
            session.check(
                self.workspace, f"{self.config.backend.kind}:{self.config.git_workspace.enabled}"
            )
            session.commit(
                "configuration",
                {},
                configuration={
                    "provider": self.config.provider.kind,
                    "model": self.config.provider.model,
                    "backend": self.config.backend.kind,
                    "permission": self.config.permission,
                    "worktree": self.config.git_workspace.enabled,
                },
            )
            limits = self.config.sessions
            session.request_limit = limits.max_requests
            session.history_limit = limits.history_bytes
            page_size = session.db.execute("PRAGMA page_size").fetchone()[0]
            session.db.execute(
                f"PRAGMA max_page_count={max(1, limits.database_bytes // page_size)}"
            )
            session.commit(
                "retention_policy",
                {},
                retention_days=limits.retention_days,
                expires_at=time.time() + limits.retention_days * 86400,
            )
            output = OutputStore(
                session.directory / "outputs",
                command_bytes=limits.command_output_bytes,
                task_bytes=limits.task_output_bytes,
                session_bytes=limits.session_output_bytes,
            )
            output.on_process = lambda pid: session.resource(
                "process", {"pid": pid, "start": process_identity(pid)}
            )
            token = CURRENT_OUTPUT.set(output)
            try:
                result = await self._execute(task, identity, session)
                session.finish(result.report, self.workspace, clean=result.resources.backend_closed)
                return result
            except BaseException:
                # 执行异常与进程崩溃都不推断副作用未发生；持久事实保留供显式协调。
                try:
                    session.finish(None, self.workspace, clean=False)
                except Exception:
                    pass
                raise
            finally:
                CURRENT_OUTPUT.reset(token)

    async def _execute(
        self, task: str, identity: RunIdentity, session: SessionStore
    ) -> ProductRunResult:
        """在持久会话锁内执行单次 run，复用原有装配与清理链。"""
        run_directory = self.state_root / identity.session_id / identity.task_id / identity.run_id
        trace_directory = run_directory / "traces"
        backend_directory = run_directory / "backend"
        context_directory = session.directory / "artifacts"

        task_workspace: GitTaskWorkspace | None = None
        execution_workspace = self.workspace
        if self.config.git_workspace.enabled:
            git_config = self.config.git_workspace
            task_workspace = GitTaskWorkspace(
                self.workspace,
                run_directory,
                session_id=identity.session_id,
                task_id=identity.task_id,
                run_id=identity.run_id,
                base_commit=git_config.base_commit,
                require_clean_source=git_config.require_clean_source,
                max_patch_bytes=git_config.max_patch_bytes,
            )
            # 固定 commit 和独立 worktree 必须在后端或 Provider 创建前准备完成。
            execution_workspace = task_workspace.prepare()

        backend: ExecutionBackend | None = None
        report = None
        permission = None
        recorder: EventRecorder | None = None
        delivery: GitDelivery | None = None
        try:
            # 从后端构造开始纳入失败交付，避免初始化异常留下无人管理的 task worktree。
            backend = _create_backend(self.config, execution_workspace, backend_directory)
            store = ContextArtifactStore(
                context_directory,
                persistent=True,
                max_bytes=self.config.sessions.context_artifact_bytes,
            )
            recorder = EventRecorder(trace_directory, self.on_event)
            async with AsyncExitStack() as resources:
                # 先登记已构造资源，后续装配或启动失败也会进入相同清理出口。
                # 会话持有 Artifact，run 退出只清理执行资源，不删除可恢复正文。
                resources.push_async_callback(backend.close)
                context = ContextManager(backend, store, limits=self.config.context)
                scheduler = ScheduledExecutor(context, self.config.scheduler_concurrency)
                permission = PermissionedExecutor(
                    scheduler,
                    PermissionEngine(_permission_policy(self.config.permission)),
                    self.approval
                    if self.approval is not None
                    else ConsoleApproval()
                    if self.config.permission != "read-only"
                    else None,
                )
                provider = _create_provider(
                    self.config,
                    permission.specs,
                    identity.run_id,
                    api_key=self._api_key,
                )
                # Provider 最后登记，因此退出时先关闭连接，再关闭后端和 ArtifactStore。
                resources.push_async_callback(provider.close)
                runner = Runner(
                    provider.provider,
                    permission,
                    self.config.runtime,
                    run_id=identity.run_id,
                    recorder=recorder,
                    context=context,
                    on_observation=session.observe,
                    resume_history=True,
                )
                initial = _initial_messages(execution_workspace, task)
                history = session.history()
                # 每轮重新读取仓库规则，保留旧任务事实并追加当前用户输入。
                messages = (
                    *initial[:-1],
                    *(message for message in history if message.role != "system"),
                    initial[-1],
                )
                session.begin(identity.run_id, messages)
                await backend.start()
                for event in backend.events:
                    if event.container_id:
                        session.resource("container", {"id": event.container_id})
                        break
                try:
                    report = await runner.run(messages)
                except asyncio.CancelledError:
                    # Runner 已经记录 cancelled；后端仍需封闭入口并停止活动进程或容器。
                    await asyncio.shield(backend.cancel())
                    raise
        except BaseException:
            if task_workspace is not None:
                # 故障任务也导出已经产生的改动；清理不得覆盖原始异常或丢弃文件。
                try:
                    await asyncio.shield(task_workspace.finish(failed=True))
                except Exception:
                    pass
            raise

        if task_workspace is not None:
            git_config = self.config.git_workspace
            delivery = await task_workspace.finish(
                verification_command=git_config.verification_command,
                verification_timeout_seconds=git_config.verification_timeout_seconds,
            )

        if report is None or permission is None or backend is None or recorder is None:
            # 正常控制流不会到达这里，显式防御能避免返回未初始化的产品结果。
            raise RuntimeError("run completed without a report")
        return ProductRunResult(
            identity=identity,
            report=report,
            resources=ResourceOwnership(
                workspace=execution_workspace,
                source_workspace=self.workspace,
                state_directory=run_directory,
                trace_path=recorder.path,
                backend_id=backend.backend_id,
                backend_closed=any(event.kind == "backend_closed" for event in backend.events),
                context_artifacts_cleaned=not any(context_directory.glob("*.txt")),
            ),
            permission_audits=tuple(permission.audits),
            backend_events=backend.events,
            delivery=delivery,
        )


def _state_root(config: ProductConfig) -> Path:
    """选择显式状态目录或符合 XDG 约定的用户级默认目录。"""
    if config.state_directory is not None:
        return config.state_directory.expanduser()
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return base / "codepilot"


def _create_backend(
    config: ProductConfig, workspace: Path, audit_directory: Path
) -> ExecutionBackend:
    """根据严格配置构造唯一工作区后端，构造本身不启动容器。"""
    backend = config.backend
    if backend.kind == "local":
        return LocalExecutionBackend(
            workspace,
            limits=backend.limits,
            # Git 与测试命令都统一经 Shell 执行，避免维护第二套命令参数语义。
            registry=create_basic_registry(workspace),
            audit_directory=audit_directory,
        )
    if backend.docker_image is None:
        # ProductConfig 已阻止该状态；保留防御以缩小未来配置变更的故障范围。
        raise ValueError("docker image is required")
    return DockerExecutionBackend(
        workspace,
        backend.docker_image,
        limits=backend.limits,
        audit_directory=audit_directory,
    )


def _create_provider(
    config: ProductConfig,
    tools: tuple[ToolSpec, ...],
    run_id: str,
    *,
    api_key: str | None,
) -> _ProviderHandle:
    """构造 fake 或 OpenAI-compatible Provider，并让调用方统一关闭客户端。"""
    provider = config.provider
    if provider.kind == "fake":
        script = (
            Message(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        call_id=f"{run_id}-glob",
                        name="glob",
                        arguments={"pattern": "*"},
                    ),
                ),
            ),
            Message(role="assistant", content="Fake provider 已完成一次只读工作区检查。"),
        )
        return _ProviderHandle(FakeProvider(script))
    if api_key is None:
        # fake 已在前一分支返回；真实 Provider 到这里必须已经解析出凭据。
        raise ValueError("real provider API key was not resolved")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=provider.base_url,
        timeout=provider.timeout_seconds,
        max_retries=0,
    )
    # 这里仅承诺 OpenAI Chat Completions 兼容；能力探测和复杂重试属于 P6。
    compatible = OpenAICompatibleProvider(
        client,
        tools,
        model=provider.model,
        budget=RequestBudget(provider.max_requests),
        max_tokens=provider.max_tokens,
    )
    return _ProviderHandle(compatible, client)


def _permission_policy(name: str) -> PermissionPolicy:
    """把配置中的稳定预设名映射到现有权限策略对象。"""
    if name == "read-only":
        return read_only_policy()
    if name == "controlled-edit":
        return controlled_edit_policy()
    return ask_edits_policy()


def _initial_messages(workspace: Path, task: str) -> tuple[Message, ...]:
    """组装内置行为边界、根目录 AGENTS.md 和用户任务三类初始信息。"""
    rules = [
        RepositoryRule(
            source="CodePilot",
            content=(
                "你是在一个固定工作区运行的 Coding Agent。先检查证据，再使用已提供的工具完成任务；"
                "工具错误必须根据结果处理，不能声称未验证的修改或测试已经成功。"
                "需要 Git 操作时使用 shell 中的原生 git 命令；不要清除或覆盖用户已有的未提交修改。"
            ),
        )
    ]
    agents = workspace / "AGENTS.md"
    if agents.is_file() and not agents.is_symlink():
        # 根规则设置读取上限，防止启动阶段把异常大文件直接注入模型上下文。
        if agents.stat().st_size > 256 * 1024:
            raise ValueError("root AGENTS.md exceeds 256 KiB")
        rules.append(RepositoryRule(source="AGENTS.md", content=agents.read_text(encoding="utf-8")))
    return assemble_initial_context(
        ContextSeed(repository_rules=tuple(rules), task_information=task.strip())
    )
