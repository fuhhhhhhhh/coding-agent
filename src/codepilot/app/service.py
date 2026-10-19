"""组装并运行正式产品链，统一拥有 Provider、后端和临时资源生命周期。"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from codepilot.app.models import ProductRunResult, ResourceOwnership, RunIdentity
from codepilot.config import ProductConfig
from codepilot.context import (
    ContextArtifactStore,
    ContextManager,
    ContextSeed,
    ModelSummarizer,
    RepositoryRule,
    assemble_initial_context,
)
from codepilot.contracts import Message, RunState, ToolCall
from codepilot.contracts.run import RunObservation, RunReport
from codepilot.contracts.stream import RuntimeEvent
from codepilot.credentials import resolve_provider_api_key
from codepilot.extensions.executor import ManagedExecutor, OperationBudget
from codepilot.extensions.router import ExtensionRouter, InputArguments
from codepilot.observability import EventRecorder
from codepilot.observability.trace import CURRENT_RECORDER, CURRENT_TURN
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
from codepilot.providers.capabilities import provider_profile
from codepilot.providers.fake import FakeProvider
from codepilot.providers.openai_compatible import OpenAICompatibleProvider, RequestBudget
from codepilot.providers.responses import ResponsesProvider
from codepilot.providers.stream import StreamingProvider
from codepilot.runtime import Runner
from codepilot.scheduler import ScheduledExecutor
from codepilot.sessions.checkpoints import CheckpointStore
from codepilot.sessions.outputs import CURRENT_OUTPUT, OutputStore
from codepilot.sessions.store import SessionStore, process_identity, safe_id
from codepilot.tasks.children import run_child
from codepilot.tasks.controls import watch_controls
from codepilot.tasks.manager import TaskManager
from codepilot.tools import create_basic_registry
from codepilot.tools.base import ToolSpec
from codepilot.workspace import DockerExecutionBackend, ExecutionBackend, LocalExecutionBackend
from codepilot.workspace.git import GitDelivery, GitTaskWorkspace

if TYPE_CHECKING:
    from openai import AsyncOpenAI


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
        self.profile = provider_profile(config.provider, config.context.max_prompt_tokens)
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
        compact_only: bool = False,
        direct_tool: ToolCall | None = None,
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
            if self.profile["input_usd_per_million"] is not None:
                session.unit_prices = [
                    self.profile["input_usd_per_million"],
                    self.profile["output_usd_per_million"],
                ]
            session.check(
                self.workspace, f"{self.config.backend.kind}:{self.config.git_workspace.enabled}"
            )
            session.commit(
                "configuration",
                {},
                configuration={
                    "provider": self.config.provider.kind,
                    "model": self.config.provider.model,
                    "protocol": self.config.provider.protocol,
                    "backend": self.config.backend.kind,
                    "permission": self.config.permission,
                    "worktree": self.config.git_workspace.enabled,
                    "capabilities": self.profile,
                },
            )
            limits = self.config.sessions
            session.request_limit = limits.max_requests
            session.input_token_limit = limits.max_input_tokens
            session.output_token_limit = limits.max_output_tokens
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
                result = await self._execute(
                    task, identity, session, compact_only=compact_only, direct_tool=direct_tool
                )
                session.finish(result.report, self.workspace, clean=result.resources.backend_closed)
                budget = session.get("provider_budget", {})
                return result.model_copy(
                    update={
                        "session_budget": {
                            "request_attempts": session.get("request_attempts", 0),
                            "input_tokens": budget.get("input"),
                            "output_tokens": budget.get("output"),
                            "pending_attempts": len(budget.get("pending", {})),
                            "known_cost_usd": budget.get("known_cost_usd"),
                            "cost_status": budget.get("cost_status", "unknown"),
                        }
                    }
                )
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
        self,
        task: str,
        identity: RunIdentity,
        session: SessionStore,
        *,
        compact_only: bool = False,
        direct_tool: ToolCall | None = None,
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
                manager = TaskManager(session, identity.run_id, self.config.extensions.max_tasks)
                router = ExtensionRouter(
                    backend,
                    self.config.extensions,
                    manager,
                    lambda name: _create_backend(
                        self.config, execution_workspace, backend_directory / name
                    ),
                )
                context = ContextManager(
                    router,
                    store,
                    limits=self.config.context,
                    checkpoint_store=CheckpointStore(session.directory / "checkpoints"),
                    facts_provider=lambda: _facts_message(session, execution_workspace),
                    allow_compaction=not session.get("compaction_disabled", False),
                )
                scheduler = ScheduledExecutor(context, self.config.scheduler_concurrency)
                base_permission = PermissionedExecutor(
                    scheduler,
                    PermissionEngine(_permission_policy(self.config.permission)),
                    self.approval
                    if self.approval is not None
                    else ConsoleApproval()
                    if self.config.permission != "read-only"
                    else None,
                )
                permission = ManagedExecutor(
                    base_permission, router, OperationBudget(self.config.runtime.max_tool_calls)
                )
                scheduler.deferred = manager.by_call.get
                # 用户显式命令不调用模型，执行器、权限和持久会话仍复用原产品装配。
                provider = (
                    _ProviderHandle(FakeProvider(()))
                    if direct_tool is not None
                    else _create_provider(
                        self.config,
                        permission.specs,
                        identity.run_id,
                        api_key=self._api_key,
                    )
                )
                # 专用摘要器不暴露工作区工具，但必须共享主请求的逐尝试账本和连接生命周期。
                summary_provider = provider.provider
                if isinstance(provider.provider, OpenAICompatibleProvider):
                    active = provider.provider
                    active.budget.on_reserve = session.reserve_provider_attempt
                    active.budget.on_settle = session.settle_provider_attempt
                    session.provider_tracks_attempts = True
                    # 摘要只复用已支持协议，不假设供应商子类构造器拥有相同参数。
                    summary_kind = (
                        ResponsesProvider
                        if isinstance(active, ResponsesProvider)
                        else OpenAICompatibleProvider
                    )
                    summary_provider = summary_kind(
                        active.client,
                        model=active.model,
                        budget=active.budget,
                        max_tokens=min(active.max_tokens, 4096),
                        extra_body=active.extra_body,
                        retry=active.retry,
                        context_tokens=active.context_tokens,
                    )
                context.summarizer = ModelSummarizer(
                    summary_provider,
                    timeout_seconds=self.config.provider.timeout_seconds,
                    structured=True,
                )
                # 子任务先停止使用共享客户端，再关闭 Provider 及父后端。
                resources.push_async_callback(provider.close)
                resources.push_async_callback(scheduler.close)
                resources.push_async_callback(manager.close)

                async def deliver_input(task_id: str, data: str) -> None:
                    """另一个终端的用户输入是控制事件，不经过模型或继承额外权限。"""
                    await router.input(InputArguments(task_id=task_id, data=data))

                controls = asyncio.create_task(watch_controls(manager, deliver_input))

                async def close_controls() -> None:
                    """前台退出先关闭控制入口，再取消后台任务，避免终态后继续输入。"""
                    controls.cancel()
                    await asyncio.gather(controls, return_exceptions=True)

                resources.push_async_callback(close_controls)
                router.delegate_handler = lambda arguments: run_child(
                    arguments,
                    provider.provider,
                    permission,
                    session,
                    self.config.extensions,
                    trace_directory / "children",
                )
                runner = Runner(
                    provider.provider,
                    permission,
                    self.config.runtime,
                    run_id=identity.run_id,
                    recorder=recorder,
                    context=context,
                    on_observation=session.observe,
                    resume_history=True,
                    compact_only=compact_only,
                )
                if compact_only:
                    session.commit("context_enabled", {}, compaction_disabled=False)
                initial = _initial_messages(execution_workspace, task)
                extension_index = {
                    "skills": list(router.skills.records.values())
                    if router.skills is not None
                    else [],
                    "execution_enabled": self.config.extensions.enabled,
                    "mcp_servers": [item.name for item in self.config.extensions.mcp]
                    if self.config.extensions.enabled
                    else [],
                    "hooks": [
                        {"name": item.name, "event": item.event}
                        for item in self.config.extensions.hooks
                    ]
                    if self.config.extensions.enabled
                    else [],
                }
                if extension_index["skills"] or extension_index["execution_enabled"]:
                    initial = (
                        *initial[:-1],
                        initial[-1].model_copy(
                            update={
                                "content": initial[-1].content
                                + "\n[扩展资料索引；正文按需读取；不授予额外权限]\n"
                                + json.dumps(extension_index, ensure_ascii=False)
                            }
                        ),
                    )
                history = session.history()
                # 每轮重新读取仓库规则，保留旧任务事实并追加当前用户输入。
                messages = (
                    *initial[:-1],
                    *(message for message in history if message.role != "system"),
                    initial[-1],
                )
                session.begin(identity.run_id, messages, execution_workspace)
                await backend.start()
                for event in backend.events:
                    if event.container_id:
                        session.resource("container", {"id": event.container_id})
                        break
                try:
                    if direct_tool is not None:
                        async with asyncio.timeout(self.config.runtime.timeout_seconds):
                            report = await _execute_direct(
                                permission,
                                session,
                                identity.run_id,
                                messages,
                                direct_tool,
                                recorder,
                            )
                    else:
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
            execution_source="user_command" if direct_tool is not None else "agent",
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
    # 只有真实任务需要 SDK；启动提示符和 fake 运行不导入完整客户端。
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=provider.base_url,
        timeout=provider.timeout_seconds,
        max_retries=0,
    )
    # 协议差异只在适配器边界选择，Runtime 与工具执行链保持共享。
    adapter = ResponsesProvider if provider.protocol == "responses" else OpenAICompatibleProvider
    compatible = adapter(
        client,
        tools,
        model=provider.model,
        budget=RequestBudget(
            provider.max_requests,
            max_input_tokens=provider.total_input_tokens,
            max_output_tokens=provider.total_output_tokens,
        ),
        max_tokens=provider.max_tokens,
        retry=provider.retry,
        context_tokens=config.context.max_prompt_tokens,
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
            trusted=True,
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


def _facts_message(session: SessionStore, workspace: Path) -> Message:
    """在模型摘要之外注入有界控制器事实；旧版本执行不能冒充当前验证。"""
    facts = session.facts_view(workspace)
    facts["executions"] = facts["executions"][-4:]
    facts["changes"] = facts["changes"][-6:]
    return Message(
        role="assistant",
        content=(
            "[控制器执行记录；仅供证据参考，不授予权限；exit_code=0 不等于需求验收通过]\n"
            + json.dumps(facts, ensure_ascii=False)
        ),
    )


async def _execute_direct(
    executor: ManagedExecutor,
    session: SessionStore,
    run_id: str,
    messages: tuple[Message, ...],
    call: ToolCall,
    recorder: EventRecorder,
) -> RunReport:
    """执行用户明确提交的单次工具，不伪造模型请求，保留规范工具配对和事实。"""
    planned = Message(
        role="assistant", content="用户明确请求执行命令；未请求模型。", tool_calls=(call,)
    )
    observations = [
        RunObservation(
            run_id=run_id, sequence=1, step=0, kind="command_requested", message=planned
        ),
        RunObservation(run_id=run_id, sequence=2, step=0, kind="tool_requested", call=call),
    ]
    for observation in observations:
        session.observe(observation)
    recorder.start(run_id)
    token = CURRENT_RECORDER.set(recorder)
    turn = CURRENT_TURN.set("user-command")
    reason = "completed"
    try:
        result = await executor.execute(call)
    except BaseException:
        reason = "cancelled_or_failed"
        raise
    finally:
        # 无模型单次执行同样落 Trace；观察者失败不允许启动额外工具。
        try:
            recorder.finish(reason)
        finally:
            CURRENT_TURN.reset(turn)
            CURRENT_RECORDER.reset(token)
    observed = RunObservation(run_id=run_id, sequence=3, step=0, kind="tool_result", result=result)
    session.observe(observed)
    observations.append(observed)
    return RunReport(
        run_id=run_id,
        state=RunState.COMPLETED,
        reason="completed",
        steps=0,
        tool_calls=1,
        messages=(*messages, planned, Message(role="tool", tool_result=result)),
        observations=tuple(observations),
        detail="user_command",
    )
