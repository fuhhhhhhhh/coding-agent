"""显式 Rootless Docker 验收后台取消、交互终端与真实 MCP 组合。"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from test_b4 import MCP_SERVER, call

from codepilot.context import ContextArtifactStore, ContextManager
from codepilot.contracts import Message
from codepilot.extensions.executor import ManagedExecutor, OperationBudget
from codepilot.extensions.models import ExtensionConfig, McpConfig
from codepilot.extensions.router import ExtensionRouter
from codepilot.permissions import (
    PermissionedExecutor,
    PermissionEngine,
    ScriptedApproval,
    controlled_edit_policy,
)
from codepilot.scheduler import ScheduledExecutor
from codepilot.sessions.outputs import CURRENT_OUTPUT, OutputStore
from codepilot.sessions.store import SessionStore, process_identity
from codepilot.tasks.manager import TaskManager
from codepilot.workspace import DockerExecutionBackend

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="需要显式 Rootless Docker live 开关"
)


def test_background_cancel_leaves_foreground_alive_and_mcp_and_pty_work(tmp_path: Path) -> None:
    """真实独立容器任务取消不杀主后端，MCP/终端均保留原审批与持久资源链。"""

    async def scenario() -> None:
        """在单个有界测试中复用主后端，独立检查资源清理和用户文件。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "server.py").write_text(MCP_SERVER)
        (workspace / "user-note").write_text("keep")
        with SessionStore(tmp_path / "state", "docker") as stored:
            stored.check(workspace, "docker:False")
            stored.begin("run", (Message(role="user", content="验收"),), workspace)
            output = OutputStore(stored.directory / "outputs")
            output.on_process = lambda pid: stored.resource(
                "process", {"pid": pid, "start": process_identity(pid)}
            )
            token = CURRENT_OUTPUT.set(output)
            main = DockerExecutionBackend(workspace, "codepilot-sandbox:0.1.0")
            manager = TaskManager(stored, "run", 2)
            config = ExtensionConfig(
                enabled=True,
                mcp=[
                    McpConfig(
                        name="local", argv=["python", "-u", "server.py"], timeout_seconds=45.0
                    )
                ],
            )
            router = ExtensionRouter(
                main,
                config,
                manager,
                lambda name: DockerExecutionBackend(workspace, "codepilot-sandbox:0.1.0"),
            )
            context = ContextManager(router, ContextArtifactStore(stored.directory / "artifacts"))
            scheduler = ScheduledExecutor(context, 4)
            executor = ManagedExecutor(
                PermissionedExecutor(
                    scheduler,
                    PermissionEngine(controlled_edit_policy()),
                    ScriptedApproval([True] * 10),
                ),
                router,
                OperationBudget(30),
            )
            scheduler.deferred = manager.by_call.get
            await main.start()
            try:
                started = await executor.execute(
                    call("task_start", {"command": "printf ready > marker; sleep 60"})
                )
                identity = json.loads(started.content)["task_id"]
                async with asyncio.timeout(45):
                    while not (workspace / "marker").exists():
                        await asyncio.sleep(0.02)
                await executor.execute(call("task_cancel", {"task_id": identity}))
                assert manager.status(identity)["state"] == "cancelled"
                assert not (
                    await executor.execute(call("read_file", {"path": "user-note"}))
                ).is_error
                listed = await executor.execute(call("mcp_list", {"server": "local"}))
                assert not listed.is_error, listed.content
                echoed = await executor.execute(
                    call(
                        "mcp_call",
                        {"server": "local", "tool": "echo", "arguments": {"text": "docker-echo"}},
                    )
                )
                assert echoed.content == "docker-echo", echoed.content
                started = await executor.execute(
                    call(
                        "task_start",
                        {
                            "command": (
                                'python -u -c \'print("READY",flush=True); '
                                "x=input(); print(len(x),flush=True)'"
                            ),
                            "interactive": True,
                        },
                        "terminal",
                    )
                )
                terminal = json.loads(started.content)["task_id"]
                async with asyncio.timeout(45):
                    while "READY" not in manager.status(terminal)["output"]:
                        await asyncio.sleep(0.02)
                await executor.execute(
                    call("task_input", {"task_id": terminal, "data": "synthetic-secret\n"})
                )
                await asyncio.wait_for(manager.handles[terminal], 45)
                assert manager.status(terminal)["state"] == "succeeded"
                assert "synthetic-secret" not in manager.status(terminal)["output"]
                raw = b"".join(path.read_bytes() for path in output.directory.glob("*.bin"))
                assert b"READY" in raw
                assert b"synthetic-secret" not in raw
                assert "16" in manager.status(terminal)["output"]
            finally:
                await manager.close()
                await scheduler.close()
                await main.close()
                CURRENT_OUTPUT.reset(token)
        assert (workspace / "user-note").read_text() == "keep"

    asyncio.run(scenario())
