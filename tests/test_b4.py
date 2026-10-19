"""验证 B4 的权限、预算、资源租约、扩展来源与真实进程清理。"""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pytest

from codepilot.config import ProductConfig, load_product_config
from codepilot.context import ContextArtifactStore, ContextManager
from codepilot.contracts import Message, ToolCall
from codepilot.contracts.run import RunLimits
from codepilot.extensions.executor import ManagedExecutor, OperationBudget, ReadOnlyExecutor
from codepilot.extensions.models import ExtensionConfig, HookConfig, McpConfig
from codepilot.extensions.router import ExtensionRouter
from codepilot.extensions.skills import SkillCatalog
from codepilot.permissions import (
    PermissionedExecutor,
    PermissionEngine,
    ScriptedApproval,
    controlled_edit_policy,
)
from codepilot.permissions.approval import ApprovalProvider
from codepilot.scheduler import ScheduledExecutor
from codepilot.sessions.outputs import CURRENT_OUTPUT, OutputStore
from codepilot.sessions.store import SessionStore, process_identity
from codepilot.tasks.manager import TaskManager
from codepilot.tools.base import ToolFailure
from codepilot.workspace import LocalExecutionBackend


@asynccontextmanager
async def environment(
    root: Path, config: ExtensionConfig, *, approvals: tuple[bool, ...] = (True,) * 30
) -> AsyncIterator[tuple[ManagedExecutor, ExtensionRouter, TaskManager]]:
    """以正式权限/调度/Context 包装真实 Local 后端，测试不绕过执行边界。"""
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    with SessionStore(root / "state", "b4") as stored:
        stored.check(workspace, "local:False")
        stored.begin("parent", (Message(role="user", content="测试"),), workspace)
        output = OutputStore(stored.directory / "outputs")
        output.on_process = lambda pid: stored.resource(
            "process", {"pid": pid, "start": process_identity(pid)}
        )
        token = CURRENT_OUTPUT.set(output)
        backend = LocalExecutionBackend(workspace)
        manager = TaskManager(stored, "parent", config.max_tasks)
        router = ExtensionRouter(
            backend, config, manager, lambda name: LocalExecutionBackend(workspace)
        )
        context = ContextManager(router, ContextArtifactStore(stored.directory / "artifacts"))
        scheduler = ScheduledExecutor(context, 4)
        permission = PermissionedExecutor(
            scheduler, PermissionEngine(controlled_edit_policy()), ScriptedApproval(approvals)
        )
        executor = ManagedExecutor(permission, router, OperationBudget(50))
        scheduler.deferred = manager.by_call.get
        await backend.start()
        try:
            yield executor, router, manager
        finally:
            await manager.close()
            await scheduler.close()
            await backend.close()
            CURRENT_OUTPUT.reset(token)


def call(name: str, arguments: dict[str, Any], identity: str | None = None) -> ToolCall:
    """为断言生成稳定、不同的调用身份。"""
    return ToolCall(call_id=identity or name, name=name, arguments=arguments)


def test_skill_metadata_loading_and_version_or_link_refusal(tmp_path: Path) -> None:
    """技能发现不返回正文，加载时拒绝版本漂移和符号链接。"""
    skill = tmp_path / ".codepilot/skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: review code\n---\nPRIVATE_SKILL_BODY")
    catalog = SkillCatalog(tmp_path)
    assert "PRIVATE_SKILL_BODY" not in json.dumps(catalog.records)
    assert "PRIVATE_SKILL_BODY" in catalog.load("review")
    skill.write_text(skill.read_text() + "changed")
    with pytest.raises(ToolFailure, match="changed"):
        catalog.load("review")
    skill.unlink()
    target = tmp_path / "outside"
    target.write_text("data")
    skill.symlink_to(target)
    with pytest.raises(ToolFailure, match="symlink"):
        SkillCatalog(tmp_path)


def test_project_cannot_enable_executable_extensions(tmp_path: Path) -> None:
    """项目只能收紧配置，不能通过 Hook 或 MCP 自动获得进程能力。"""
    config = tmp_path / ".codepilot/config.yaml"
    config.parent.mkdir()
    config.write_text("extensions:\n  enabled: true\n")
    with pytest.raises(ValueError, match="extensions"):
        load_product_config(workspace=tmp_path, user_path=tmp_path / "missing")


def test_background_lease_allows_reads_but_holds_writes_and_cancel_cleans(tmp_path: Path) -> None:
    """返回 ID 后实际进程仍占租约，前台读可继续，写入等取消完成才执行。"""

    async def scenario() -> None:
        """以事件/文件屏障核对进程启动与调度，避免随机睡眠判定。"""
        async with environment(tmp_path, ExtensionConfig(enabled=True)) as (
            executor,
            router,
            manager,
        ):
            result = await executor.execute(
                call("task_start", {"command": "printf ready > marker; sleep 60"})
            )
            identity = json.loads(result.content)["task_id"]
            async with asyncio.timeout(10):
                while not (router.root / "marker").exists():
                    await asyncio.sleep(0.01)
            reading = await executor.execute(call("read_file", {"path": "marker"}))
            assert not reading.is_error and "ready" in reading.content
            writing = asyncio.create_task(
                executor.execute(call("write_file", {"path": "later", "content": "done"}))
            )
            await asyncio.sleep(0)
            assert not writing.done()
            cancelled = await executor.execute(call("task_cancel", {"task_id": identity}))
            assert json.loads(cancelled.content)["state"] == "cancelled"
            assert not (await asyncio.wait_for(writing, 5)).is_error
            for resource in manager.session.get("resources", []):
                if resource["kind"] == "process":
                    assert not process_identity(resource["pid"])

    asyncio.run(scenario())


def test_denied_task_starts_nothing_and_child_cannot_write(tmp_path: Path) -> None:
    """任务启动拒绝无进程，子任务入口再次拒绝未暴露的写入和递归能力。"""

    async def scenario() -> None:
        """独立检查文件和句柄，不以工具返回成功冒充实际执行。"""
        async with environment(tmp_path, ExtensionConfig(enabled=True), approvals=(False,)) as (
            executor,
            router,
            manager,
        ):
            denied = await executor.execute(call("task_start", {"command": "touch forbidden"}))
            assert (
                denied.is_error and not manager.handles and not (router.root / "forbidden").exists()
            )
            child = ReadOnlyExecutor(executor)
            for name in ("shell", "write_file", "delegate", "mcp_call", "task_start"):
                assert (await child.execute(call(name, {}))).is_error

    asyncio.run(scenario())


def test_hook_blocking_invalid_output_and_original_result(tmp_path: Path) -> None:
    """Hook 返回不合法参数改写时停止；明确 block 不允许原工具副作用。"""

    async def scenario() -> None:
        """分别提供合法阻断和伪造参数返回，通过真实子进程执行。"""
        for index, output in enumerate(
            ({"decision": "block"}, {"decision": "continue", "arguments": {}})
        ):
            root = tmp_path / str(index)
            root.mkdir()
            config = ExtensionConfig(
                enabled=True,
                hooks=[
                    HookConfig(
                        name="guard",
                        event="before_tool",
                        argv=[sys.executable, "-c", f"print({json.dumps(json.dumps(output))})"],
                    )
                ],
            )
            async with environment(root, config) as (executor, router, manager):
                result = await executor.execute(
                    call("write_file", {"path": "forbidden", "content": "x"})
                )
                assert result.is_error and not (router.root / "forbidden").exists()
                assert any(
                    item["kind"] == "process" for item in manager.session.get("resources", [])
                )
                assert executor.budget.used == 2

    asyncio.run(scenario())


def test_interactive_task_input_no_echo_and_owner_cleanup(tmp_path: Path) -> None:
    """交互进程等待输入时可读状态并发送文本，输入本身不自动回显。"""

    async def scenario() -> None:
        """真实 PTY 读写，核对输入处理结果和任务终态。"""
        async with environment(tmp_path, ExtensionConfig(enabled=True)) as (
            executor,
            router,
            manager,
        ):
            command = (
                f"{sys.executable} -c "
                '\'print("READY",flush=True); value=input(); '
                'print("length="+str(len(value)),flush=True)\''
            )
            started = await executor.execute(
                call("task_start", {"command": command, "interactive": True})
            )
            identity = json.loads(started.content)["task_id"]
            async with asyncio.timeout(10):
                while "READY" not in manager.status(identity)["output"]:
                    await asyncio.sleep(0.01)
            sent = await executor.execute(
                call("task_input", {"task_id": identity, "data": "synthetic-secret\n"})
            )
            assert not sent.is_error
            await asyncio.wait_for(manager.handles[identity], 10)
            record = manager.status(identity)
            assert record["state"] == "succeeded" and "synthetic-secret" not in record["output"]
            assert "length=16" in record["output"]
            raw = b"".join(
                path.read_bytes() for path in (manager.session.directory / "outputs").glob("*.bin")
            )
            assert b"READY" in raw and b"synthetic-secret" not in raw

    asyncio.run(scenario())


MCP_SERVER = r"""
import sys,json
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    method=request.get('method')
    if method=='initialize':
        result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':'echo','description':'echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}}]}
    elif method=='tools/call':
        result={'content':[{'type':'text','text':request['params']['arguments']['text']}],'isError':False}
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
"""


def test_official_mcp_client_in_same_permission_chain_and_schema_validation(tmp_path: Path) -> None:
    """真实 stdio JSON-RPC 经官方 SDK 与权限链，非法参数不进入远端调用。"""

    async def scenario() -> None:
        """目录与调用分别审批；服务端每次调用后被关闭，无宿主回退。"""
        config = ExtensionConfig(
            enabled=True, mcp=[McpConfig(name="local", argv=[sys.executable, "-c", MCP_SERVER])]
        )
        async with environment(tmp_path, config) as (executor, _, manager):
            listed = await executor.execute(call("mcp_list", {"server": "local"}))
            assert not listed.is_error and "echo" in listed.content, listed.content
            invalid = await executor.execute(
                call(
                    "mcp_call",
                    {"server": "local", "tool": "echo", "arguments": {"bad": 1}},
                    "invalid",
                )
            )
            assert invalid.is_error
            echoed = await executor.execute(
                call(
                    "mcp_call",
                    {"server": "local", "tool": "echo", "arguments": {"text": "hello"}},
                    "valid",
                )
            )
            assert not echoed.is_error and echoed.content == "hello", echoed.content
            assert len(executor.audits) == 3
            for resource in manager.session.get("resources", []):
                if resource["kind"] == "process":
                    assert not process_identity(resource["pid"])

    asyncio.run(scenario())


def test_full_product_uses_skill_child_background_hook_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """统一 Application 完成扩展联合任务，独立核对文件、权限、共享请求与清理。"""
    import codepilot.app.service as service
    from codepilot.app import CodePilotApplication
    from codepilot.contracts import Message
    from codepilot.sessions.store import SessionStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = workspace / ".codepilot/skills/check/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: check\ndescription: check project\n---\nBODY_ONLY_AFTER_LOADING")
    (workspace / "user-note").write_text("keep")

    class AdaptiveProvider:
        """根据规范工具结果推进固定流程，不伪装真实模型解题。"""

        def __init__(self) -> None:
            """保存阶段和任务 ID，等待由真实任务状态决定。"""
            self.phase, self.sequence = 0, 0
            self.job = ""

        async def complete(self, messages: Any) -> Message:
            """仅固定工具选择，真实后端和生命周期保持正式实现。"""
            self.sequence += 1
            args: dict[str, Any]
            last = messages[-1].tool_result
            if self.phase == 0:
                assert "BODY_ONLY_AFTER_LOADING" not in str(messages)
                name, args = "load_skill", {"name": "check"}
            elif self.phase == 1:
                assert "BODY_ONLY_AFTER_LOADING" in last.content
                name, args = "delegate", {"task": "只读列出项目文件并报告来源", "role": "explore"}
            elif self.phase == 2:
                name, args = "task_start", {"command": "printf background-done"}
            elif self.phase == 3:
                if not self.job:
                    self.job = json.loads(last.content)["task_id"]
                if (
                    last
                    and "state" in last.content
                    and json.loads(last.content).get("state") == "succeeded"
                ):
                    self.phase = 4
                    name, args = "mcp_list", {"server": "local"}
                else:
                    await asyncio.sleep(0.01)
                    return Message(
                        role="assistant",
                        tool_calls=(
                            call("task_status", {"task_id": self.job}, str(self.sequence)),
                        ),
                    )
            elif self.phase == 5:
                name, args = (
                    "mcp_call",
                    {"server": "local", "tool": "echo", "arguments": {"text": "verified"}},
                )
            elif self.phase == 6:
                assert last.content == "verified"
                name, args = "write_file", {"path": "answer.txt", "content": last.content}
            else:
                return Message(role="assistant", content="扩展流程执行完毕，验收由控制器核对。")
            self.phase += 1
            return Message(role="assistant", tool_calls=(call(name, args, str(self.sequence)),))

    def factory(*args: Any, **kwargs: Any) -> service._ProviderHandle:
        """仅替换不确定模型，所有扩展、审批和资源保持真实。"""
        return service._ProviderHandle(AdaptiveProvider())

    monkeypatch.setattr(service, "_create_provider", factory)
    config = ProductConfig(
        runtime=RunLimits(max_steps=32, max_tool_calls=64),
        state_directory=tmp_path / "state",
        permission="controlled-edit",
        extensions=ExtensionConfig(
            enabled=True,
            hooks=[
                HookConfig(
                    name="check",
                    event="before_tool",
                    argv=[sys.executable, "-c", 'print(\'{"decision":"continue"}\')'],
                )
            ],
            mcp=[McpConfig(name="local", argv=[sys.executable, "-c", MCP_SERVER])],
        ),
    )
    result = asyncio.run(
        CodePilotApplication(config, workspace, approval=ScriptedApproval([True] * 20)).run(
            "执行扩展联合任务", session_id="integration"
        )
    )
    assert result.report.reason == "completed", result.report
    assert (workspace / "answer.txt").read_text() == "verified"
    assert (workspace / "user-note").read_text() == "keep"
    with SessionStore(tmp_path / "state", "integration", create=False) as stored:
        tasks = stored.get("tasks")
        assert len(tasks) == 2 and all(row["state"] == "succeeded" for row in tasks.values())
        assert any("source_calls" in row["output"] for row in tasks.values())
        assert stored.get("request_attempts") == result.report.steps + 2
        assert stored.get("status") == "ready" and not stored.get("resources")
    assert result.resources.backend_closed


@pytest.mark.parametrize(
    "server",
    ["import sys; sys.exit(7)", "import sys; print('X'*70000,flush=True); sys.stdin.read()"],
)
def test_mcp_disconnect_and_oversized_message_stop_without_hanging(
    tmp_path: Path, server: str
) -> None:
    """真实服务端退出/巨大帧时结束连接，保留未知结论且不无限等待。"""

    async def scenario() -> None:
        """有限时限内清理进程，原始服务端错误不直接进入结果。"""
        config = ExtensionConfig(
            enabled=True,
            mcp=[McpConfig(name="fault", argv=[sys.executable, "-c", server], timeout_seconds=1.0)],
        )
        async with environment(tmp_path, config) as (executor, _, manager):
            result = await asyncio.wait_for(
                executor.execute(call("mcp_list", {"server": "fault"})), 5
            )
            assert result.is_error and "outcome unknown" in result.content
            assert all(
                not process_identity(row["pid"])
                for row in manager.session.get("resources", [])
                if row["kind"] == "process"
            )

    asyncio.run(scenario())


def test_hook_script_changed_during_approval_is_rejected(tmp_path: Path) -> None:
    """审批中替换仓库 Hook 脚本会改变来源指纹，不执行新脚本或原工具。"""

    async def scenario() -> None:
        """在真实审批回调中改文件，独立核对副作用没有发生。"""
        config = ExtensionConfig(
            enabled=True,
            hooks=[
                HookConfig(name="guard", event="before_tool", argv=[sys.executable, "guard.py"])
            ],
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        script = workspace / "guard.py"
        script.write_text('print(\'{"decision":"continue"}\')')
        async with environment(tmp_path, config) as (executor, router, _):

            class MutatingApproval(ApprovalProvider):
                """确认前改变被批准脚本，验证执行前来源复核。"""

                async def approve(self, request: Any) -> bool:
                    """模拟审批界面期间仓库内容被外部编辑。"""
                    script.write_text(
                        "from pathlib import Path; Path('forbidden').write_text('bad')"
                    )
                    return True

            executor.registry.approval = MutatingApproval()
            result = await executor.execute(call("write_file", {"path": "answer", "content": "x"}))
            assert (
                result.is_error
                and not (router.root / "answer").exists()
                and not (router.root / "forbidden").exists()
            )

    asyncio.run(scenario())


def test_shared_child_request_budget_cannot_reset_parent_ledger() -> None:
    """子额度和父总额度共同限制真实尝试，结束子任务不能返还已发请求。"""
    from codepilot.providers.budget import BudgetExceededError
    from codepilot.providers.openai_compatible import RequestBudget
    from codepilot.tasks.children import ChildBudget

    parent = RequestBudget(2)
    child = ChildBudget(parent, 1)
    identity = child.consume(50, 20)
    with pytest.raises(BudgetExceededError, match="child"):
        child.consume()
    from codepilot.contracts.stream import Usage

    child.settle(Usage(input_tokens=40, output_tokens=10), identity)
    parent.consume(30, 20)
    with pytest.raises(BudgetExceededError):
        ChildBudget(parent, 2).consume()
    assert parent.ledger.snapshot.requests == 2 and parent.ledger.snapshot.input_tokens == 40


@pytest.mark.parametrize("key", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_mcp_schema_cannot_fetch_remote_references(key: str) -> None:
    """Schema 校验不允许暗中联网获取引用，无论服务器声明何种只读注释。"""
    from codepilot.extensions.mcp import check_schema

    with pytest.raises(ToolFailure, match="external"):
        check_schema({key: "https://untrusted.invalid/schema"})


def test_private_control_mailbox_cancels_active_owned_task(tmp_path: Path) -> None:
    """第二终端的取消是持久控制事件，不抢会话锁也不进入模型。"""
    from codepilot.tasks.controls import task_command, watch_controls

    async def scenario() -> None:
        """真实后台进程被所有者消费控制事件后清理，旧事件不能作用于新 run。"""
        async with environment(tmp_path, ExtensionConfig(enabled=True)) as (
            executor,
            router,
            manager,
        ):

            async def deliver(identity: str, data: str) -> None:
                """该测试仅验证取消，不允许隐式终端输入。"""
                raise ValueError("not interactive")

            watcher = asyncio.create_task(watch_controls(manager, deliver))
            try:
                started = await executor.execute(
                    call("task_start", {"command": "printf ready > marker; sleep 60"})
                )
                identity = json.loads(started.content)["task_id"]
                async with asyncio.timeout(5):
                    while not (router.root / "marker").exists():
                        await asyncio.sleep(0.01)
                response = task_command(tmp_path / "state", "b4", "cancel", identity)
                assert response["control_queued"] == "cancel"
                await asyncio.wait_for(asyncio.shield(manager.handles[identity]), 5)
            except asyncio.CancelledError:
                # 取消终态本来就会使任务句柄取消，控制器仍需独立核对持久状态。
                pass
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            assert manager.status(identity)["state"] == "cancelled"

    asyncio.run(scenario())


def test_crashed_background_is_unknown_and_never_restarted(tmp_path: Path) -> None:
    """控制器在后台写入后真实退出，重开保留任务与资源身份，不自动复活。"""
    import os
    import signal
    import subprocess
    import time

    from codepilot.sessions.store import ReconciliationRequired

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    script = r'''
import asyncio, os, sys
from pathlib import Path
from codepilot.app import CodePilotApplication
from codepilot.config import ProductConfig
from codepilot.contracts import Message,ToolCall
from codepilot.extensions.models import ExtensionConfig
from codepilot.permissions import ScriptedApproval
import codepilot.app.service as service
workspace=Path(sys.argv[1])
class Provider:
    """第一次启动后台，之后等真实副作用发生再模拟断电。"""
    def __init__(self):
        """保存一次性调用计数。"""
        self.count=0
    async def complete(self,messages):
        """真实后台追加文件，不能以模型自述替代副作用屏障。"""
        self.count+=1
        if self.count==1:
            tool=ToolCall(call_id='background',name='task_start',
                          arguments={'command':'printf once >> counter; sleep 60'})
            return Message(role='assistant',tool_calls=(tool,))
        async with asyncio.timeout(10):
            while not (workspace/'counter').exists(): await asyncio.sleep(0.01)
        os._exit(42)
def factory(*args,**kwargs):
    """替换模型但保持真实 Application 与后台后端。"""
    return service._ProviderHandle(Provider())
service._create_provider=factory
config=ProductConfig(state_directory=Path(sys.argv[2]),permission='controlled-edit',extensions=ExtensionConfig(enabled=True))
asyncio.run(CodePilotApplication(config,workspace,approval=ScriptedApproval([True])).run('后台任务',session_id='crash'))
'''
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(workspace), str(state)], capture_output=True, timeout=20
    )
    assert crashed.returncode == 42, crashed.stderr
    with SessionStore(state, "crash", create=False) as stored:
        resources = stored.get("resources")
        assert any(row["state"] == "running" for row in stored.get("tasks").values())
        with pytest.raises(ReconciliationRequired):
            stored.check(workspace, "local:False")
        for resource in resources:
            if (
                resource["kind"] == "process"
                and process_identity(resource["pid"]) == resource["start"]
            ):
                os.killpg(resource["pid"], signal.SIGTERM)
                deadline = time.monotonic() + 5
                while process_identity(resource["pid"]) and time.monotonic() < deadline:
                    time.sleep(0.01)
        stored.reconcile(workspace)
        assert all(row["state"] == "unknown" for row in stored.get("tasks").values())
        stored.check(workspace, "local:False")
    assert (workspace / "counter").read_text() == "once"


@pytest.mark.parametrize("failure", ["block", "continue"])
def test_hook_timeout_obeys_failure_policy_without_late_write(
    tmp_path: Path, failure: Literal["block", "continue"]
) -> None:
    """超时 Hook 必须结束进程，严格/宽容策略只影响原操作是否继续。"""

    async def scenario() -> None:
        """使用真实进程超时，不以 mock 异常代替清理路径。"""
        config = ExtensionConfig(
            enabled=True,
            hooks=[
                HookConfig(
                    name="slow",
                    event="before_tool",
                    argv=[sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout_seconds=0.1,
                    failure=failure,
                )
            ],
        )
        async with environment(tmp_path, config) as (executor, router, manager):
            result = await executor.execute(
                call("write_file", {"path": "answer", "content": "done"})
            )
            assert result.is_error == (failure == "block")
            assert (router.root / "answer").exists() == (failure == "continue")
            assert executor.budget.used == 2
            assert not manager.handles

    asyncio.run(scenario())


def test_owner_close_cancels_all_tasks_and_waits_for_cleanup(tmp_path: Path) -> None:
    """父退出同时取消子任务和后台任务，所有清理完成后才能释放会话。"""

    async def scenario() -> None:
        """用事件屏障验证多个子任务的所有权与退出顺序。"""
        with SessionStore(tmp_path / "state", "owner") as stored:
            manager = TaskManager(stored, "parent", 2)
            started = [asyncio.Event(), asyncio.Event()]
            cleaned: set[int] = set()

            async def operation(index: int) -> str:
                """模拟各自持有资源的工作，取消必须进入 finally。"""
                started[index].set()
                try:
                    await asyncio.Event().wait()
                    return "unreachable"
                finally:
                    cleaned.add(index)

            manager.start("child", "explore", lambda: operation(0))
            manager.start("background", "process", lambda: operation(1))
            await asyncio.gather(*(event.wait() for event in started))
            await manager.close()
            assert cleaned == {0, 1}
            assert all(row["state"] == "cancelled" for row in manager.records.values())
            assert all(task.done() for task in manager.handles.values())

    asyncio.run(scenario())


def test_child_does_not_hold_outer_read_lease_against_parent_hook(tmp_path: Path) -> None:
    """子任务外层不占读取租约，父 Hook 与后续子读取不能互相等待。"""

    async def scenario() -> None:
        """延迟子读取到父进程排队之后，稳定复现真实模型发现的依赖顺序。"""
        async with environment(tmp_path, ExtensionConfig(enabled=True)) as (
            executor,
            router,
            manager,
        ):
            proceed = asyncio.Event()

            async def child(arguments: Any) -> str:
                """子 Runner 的读工具自行获取租约，不继承外层委派租约。"""
                await proceed.wait()
                result = await ReadOnlyExecutor(executor).execute(call("glob", {"pattern": "*"}))
                assert not result.is_error
                return "child finished"

            router.delegate_handler = child
            result = await executor.execute(call("delegate", {"task": "read", "role": "explore"}))
            identity = json.loads(result.content)["task_id"]
            parent = asyncio.create_task(executor.execute(call("shell", {"command": "pwd"})))
            await asyncio.sleep(0)
            proceed.set()
            async with asyncio.timeout(5):
                await parent
                await manager.handles[identity]
            assert manager.status(identity)["state"] == "succeeded"

    asyncio.run(scenario())


def test_scoped_rules_order_limits_and_skill_size(tmp_path: Path) -> None:
    """外层到内层规则有来源且受限，巨大技能不能挤占上下文。"""
    from codepilot.extensions.skills import scoped_rules

    (tmp_path / "pkg/sub").mkdir(parents=True)
    (tmp_path / "pkg/AGENTS.md").write_text("outer rule")
    inner = tmp_path / "pkg/sub/AGENTS.md"
    inner.write_text("inner rule")
    text = scoped_rules(tmp_path, "pkg/sub/file.py")
    assert text.index("outer rule") < text.index("inner rule")
    assert "pkg/AGENTS.md" in text and "pkg/sub/AGENTS.md" in text
    inner.write_text("X" * 8193)
    with pytest.raises(ToolFailure, match="size limit"):
        scoped_rules(tmp_path, "pkg/sub/file.py")
    skill = tmp_path / ".codepilot/skills/huge/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("X" * 32769)
    with pytest.raises(ToolFailure, match="size limit"):
        SkillCatalog(tmp_path)


@pytest.mark.parametrize("duplicate", [False, True])
def test_mcp_catalog_drift_or_duplicate_never_reaches_call(tmp_path: Path, duplicate: bool) -> None:
    """同名目录和首次发现后的 Schema 漂移均在远端调用之前拒绝。"""

    async def scenario() -> None:
        """服务端读取受控标记变化，验证跨新进程目录快照而非 SDK mock。"""
        server = MCP_SERVER.replace(
            "import sys,json",
            "import sys,json\nfrom pathlib import Path",
        ).replace(
            "    print(json.dumps(",
            "    if method=='tools/list' and Path('change').exists():\n"
            + (
                "        result['tools'].append(result['tools'][0].copy())\n"
                if duplicate
                else "        result['tools'][0]['inputSchema']['description']='changed'\n"
            )
            + "    if method=='tools/call': Path('called').write_text('side effect')\n"
            "    print(json.dumps(",
        )
        config = ExtensionConfig(
            enabled=True, mcp=[McpConfig(name="local", argv=[sys.executable, "-c", server])]
        )
        async with environment(tmp_path, config) as (executor, router, manager):
            listed = await executor.execute(call("mcp_list", {"server": "local"}))
            assert not listed.is_error
            (router.root / "change").touch()
            result = await executor.execute(
                call("mcp_call", {"server": "local", "tool": "echo", "arguments": {"text": "ok"}})
            )
            assert result.is_error and not (router.root / "called").exists()
            assert "do not replay" in result.content

    asyncio.run(scenario())
