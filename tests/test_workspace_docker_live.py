"""在显式启用时用真实 Docker daemon 验证任务沙箱边界。"""

import asyncio
import os
import time
import uuid
from pathlib import Path

import pytest

from codepilot.contracts import ToolCall
from codepilot.workspace import BackendLimits, DockerExecutionBackend
from codepilot.workspace.docker import _run_docker

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1",
    reason="需要显式设置 CODEPILOT_DOCKER_LIVE=1 并提供隔离测试 daemon",
)

IMAGE = "codepilot-sandbox:0.1.0"


async def _docker(*arguments: str) -> tuple[int, str, str]:
    """通过产品使用的同一 Docker 传输执行测试准备和只读核对。"""
    code, stdout, stderr = await _run_docker(tuple(arguments), None, 262_144)
    return code, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def test_live_docker_workspace_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实容器共享工作区，同时执行断网、凭据和进程权限限制。"""

    async def scenario() -> None:
        """依次执行正常写入、系统边界探测、资源采样和产物导出。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        destination = tmp_path / "artifacts" / "answer.txt"
        monkeypatch.setenv("CODEPILOT_LIVE_SECRET", "synthetic-secret-must-stay-on-host")
        backend = DockerExecutionBackend(
            workspace,
            IMAGE,
            limits=BackendLimits(memory_bytes=64 * 1024 * 1024),
        )
        async with backend:
            written = await backend.execute(
                ToolCall(
                    call_id="write-live",
                    name="write_file",
                    arguments={"path": "answer.txt", "content": "forty-two\n"},
                )
            )
            read_back = await backend.run_argv(
                ("python", "-c", "print(open('answer.txt').read().strip())")
            )
            network = await backend.run_argv(
                (
                    "python",
                    "-c",
                    "import socket; s=socket.socket(); s.settimeout(.2); result='open';\n"
                    "try: s.connect(('1.1.1.1', 53))\n"
                    "except OSError: result='blocked'\n"
                    "print(result)",
                ),
                timeout_seconds=2,
            )
            secret = await backend.run_argv(
                (
                    "python",
                    "-c",
                    "import os; print(os.getenv('CODEPILOT_LIVE_SECRET', 'absent'))",
                )
            )
            root_layer = await backend.run_argv(
                (
                    "python",
                    "-c",
                    "from pathlib import Path; Path('/forbidden').write_text('x')",
                )
            )
            process_security = await backend.run_argv(
                (
                    "python",
                    "-c",
                    "print('|'.join(x.strip() for x in open('/proc/self/status') "
                    "if x.startswith(('CapEff:', 'NoNewPrivs:'))))",
                )
            )
            await backend.sample_resources()
            exported = await backend.export_artifact("answer.txt", destination)

        assert not written.is_error
        assert read_back.exit_code == 0 and read_back.stdout.strip() == "forty-two"
        assert network.exit_code == 0 and network.stdout.strip() == "blocked"
        assert secret.exit_code == 0 and secret.stdout.strip() == "absent"
        assert root_layer.exit_code != 0 and "Read-only file system" in root_layer.stderr
        assert "CapEff:\t0000000000000000" in process_security.stdout
        assert "NoNewPrivs:\t1" in process_security.stdout
        assert exported.read_text(encoding="utf-8") == "forty-two\n"
        assert any(event.kind == "resource_sampled" for event in backend.events)
        assert backend.events[-1].kind == "backend_closed"
        code, _, _ = await _docker("inspect", backend.container_name)
        assert code != 0

    asyncio.run(scenario())


def test_live_docker_shell_can_use_git_in_a_dirty_workspace(tmp_path: Path) -> None:
    """真实沙箱应内置 Git，并允许在已有文件的普通目录初始化和提交。"""

    async def scenario() -> None:
        """通过正式 Shell 工具执行 init、commit 和 branch，同时保留用户文件。"""
        (tmp_path / "user-note.txt").write_text("keep\n", encoding="utf-8")
        backend = DockerExecutionBackend(tmp_path, IMAGE)
        async with backend:
            result = await backend.execute(
                ToolCall(
                    call_id="docker-git-live",
                    name="shell",
                    arguments={
                        "command": (
                            "git init -q -b main && "
                            "git config user.name 'CodePilot Test' && "
                            "git config user.email codepilot@example.invalid && "
                            "printf 'agent\\n' > agent.txt && git add agent.txt && "
                            "git commit -qm agent && git switch -qc feature && "
                            "git status --short --branch"
                        )
                    },
                )
            )

        assert not result.is_error, result.content
        assert "## feature" in result.content
        assert "?? user-note.txt" in result.content
        assert (tmp_path / "user-note.txt").read_text(encoding="utf-8") == "keep\n"

    asyncio.run(scenario())


def test_live_docker_timeout_oom_and_cancel_cleanup(tmp_path: Path) -> None:
    """真实 cgroup 区分超时与 OOM，协程取消后不留下任务容器。"""

    async def scenario() -> None:
        """先复用容器验证资源故障，再单独取消一个活动命令。"""
        resource_backend = DockerExecutionBackend(
            tmp_path,
            IMAGE,
            limits=BackendLimits(
                memory_bytes=64 * 1024 * 1024,
                command_timeout_seconds=0.2,
            ),
        )
        async with resource_backend:
            timeout = await resource_backend.run_argv(("python", "-c", "while True: pass"))
            oom = await resource_backend.run_argv(
                ("python", "-c", "x=bytearray(512*1024*1024); print(len(x))"),
                timeout_seconds=5,
            )
            tool_oom = await resource_backend.execute(
                ToolCall(
                    call_id="shell-oom-live",
                    name="shell",
                    arguments={"command": "python -c 'x=bytearray(512*1024*1024)'"},
                )
            )
        assert timeout.timed_out and not timeout.oom_killed
        assert oom.oom_killed and not oom.timed_out
        assert tool_oom.is_error and tool_oom.content == "container_oom"

        cancel_backend = DockerExecutionBackend(tmp_path, IMAGE)
        await cancel_backend.start()
        container_id = cancel_backend._container_id
        task = asyncio.create_task(
            cancel_backend.run_argv(("python", "-c", "import time; time.sleep(60)"))
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await cancel_backend.close()
        code, _, _ = await _docker("inspect", container_id)
        assert code != 0
        assert any(event.kind == "backend_cancelled" for event in cancel_backend.events)

    asyncio.run(scenario())


def test_live_docker_removes_expired_owned_container(tmp_path: Path) -> None:
    """新任务只清理由同一 owner 标记且已经过期的真实遗留容器。"""

    async def scenario() -> None:
        """预建过期容器，启动后端触发清理并核对删除轨迹。"""
        owner = f"codepilot-live-{uuid.uuid4().hex}"
        stale_name = f"codepilot-stale-{uuid.uuid4().hex}"
        code, stale_id, stderr = await _docker(
            "create",
            "--name",
            stale_name,
            "--label",
            "codepilot.managed=true",
            "--label",
            f"codepilot.owner={owner}",
            "--label",
            f"codepilot.expires_at={int(time.time()) - 1}",
            IMAGE,
            "python",
            "-c",
            "import time; time.sleep(60)",
        )
        assert code == 0, stderr
        backend = DockerExecutionBackend(tmp_path, IMAGE, owner=owner)
        try:
            await backend.start()
            removed = [event for event in backend.events if event.kind == "stale_container_removed"]
            assert len(removed) == 1
            # docker ps 默认返回短 ID；它必须是准备阶段完整 ID 的唯一前缀。
            assert stale_id.strip().startswith(removed[0].container_id)
            inspect_code, _, _ = await _docker("inspect", stale_id.strip())
            assert inspect_code != 0
        finally:
            # 即使断言失败，也同时清理任务容器和准备阶段的遗留容器。
            await backend.close()
            await _docker("rm", "-f", "-v", stale_name)

    asyncio.run(scenario())


def test_live_streaming_output_and_persistent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """统一产品链跨容器保留输出引用，后续任务可读取此前中段原文。"""
    import json

    import codepilot.app.service as service
    from codepilot.app import CodePilotApplication
    from codepilot.config import BackendConfig, ProductConfig
    from codepilot.contracts import Message
    from codepilot.providers.fake import FakeProvider
    from codepilot.sessions.store import SessionStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    sequence = 0
    output_id = ""

    def factory(*args: object, **kwargs: object) -> service._ProviderHandle:
        """第一任务输出日志，第二任务按先前 ID 读取，结果由控制器核对。"""
        nonlocal sequence
        sequence += 1
        call = (
            ToolCall(
                call_id="logs",
                name="shell",
                arguments={
                    "command": (
                        'python -c "import os; '
                        "os.write(1,b'x'*100000+b'DOCKER_MIDDLE'+b'y'*100000); "
                        "os.write(2,b'err')\""
                    )
                },
            )
            if sequence == 1
            else ToolCall(
                call_id="read-old",
                name="read_output",
                arguments={"output_id": output_id, "offset": 100000, "max_bytes": 13},
            )
        )
        return service._ProviderHandle(
            FakeProvider(
                (
                    Message(role="assistant", tool_calls=(call,)),
                    Message(role="assistant", content="结束"),
                )
            )
        )

    monkeypatch.setattr(service, "_create_provider", factory)
    config = ProductConfig(
        backend=BackendConfig(kind="docker", docker_image=IMAGE),
        permission="controlled-edit",
        state_directory=state,
    )
    from codepilot.permissions import ScriptedApproval

    first = asyncio.run(
        CodePilotApplication(config, workspace, approval=ScriptedApproval((True,))).run(
            "生成日志", session_id="docker-session"
        )
    )
    index = next((state / "docker-session/outputs").glob("output-*.json"))
    record = json.loads(index.read_bytes())
    output_id = record["output_id"]
    assert record["state"] == "complete" and record["saved"]["stdout"] == 200013
    second = asyncio.run(
        CodePilotApplication(config, workspace, approval=ScriptedApproval((True,))).run(
            "读取先前输出", session_id="docker-session"
        )
    )
    assert first.report.reason == second.report.reason == "completed"
    assert any(
        message.tool_result and "DOCKER_MIDDLE" in message.tool_result.content
        for message in second.report.messages
    )
    assert second.resources.backend_closed
    with SessionStore(state, "docker-session", create=False) as stored:
        assert stored.get("status") == "ready" and stored.get("resources") == []


def test_live_docker_streams_over_100mib_with_bounded_previews(tmp_path: Path) -> None:
    """真实容器通过分块协议传输大日志，控制器只保留有限预览。"""
    import json

    from codepilot.sessions.outputs import CURRENT_OUTPUT, OutputStore

    async def scenario() -> None:
        """命令输出由 worker 到控制器逐块传输，中段可跨 store 重读。"""
        output = OutputStore(tmp_path / "outputs")
        token = CURRENT_OUTPUT.set(output)
        try:
            backend = DockerExecutionBackend(
                tmp_path, IMAGE, limits=BackendLimits(output_bytes=1024)
            )
            async with backend:
                result = await backend.run_argv(
                    (
                        "python",
                        "-c",
                        "import os; b=b'x'*65536; "
                        "[(os.write(1,b),os.write(2,b)) for _ in range(420)]; "
                        "os.write(1,b'MIDDLE_LIVE'); "
                        "[(os.write(1,b),os.write(2,b)) for _ in range(420)]",
                    ),
                    timeout_seconds=90,
                )
        finally:
            CURRENT_OUTPUT.reset(token)
        assert result.exit_code == 0 and len(result.stdout) == 1024
        record = json.loads(next(output.directory.glob("*.json")).read_bytes())
        assert record["state"] == "complete"
        assert sum(record["saved"].values()) > 100 * 1024**2
        assert str(420 * 65536) in output.search(record["output_id"], "stdout", "MIDDLE_LIVE")

    asyncio.run(scenario())


def test_live_stream_capture_quota_and_cancellation_stop_container(tmp_path: Path) -> None:
    """真实传输在配额耗尽和主动取消时都停止容器，不能留下后台写进程。"""
    import json

    from codepilot.sessions.outputs import CURRENT_OUTPUT, OutputStore

    async def scenario() -> None:
        """分别验证持续大输出被配额停止和长命令被用户取消。"""
        output = OutputStore(tmp_path / "outputs", command_bytes=16384)
        token = CURRENT_OUTPUT.set(output)
        try:
            async with DockerExecutionBackend(tmp_path, IMAGE) as backend:
                with pytest.raises(OSError):
                    await backend.run_argv(
                        ("python", "-c", "import os;\nwhile True: os.write(1,b'x'*8192)")
                    )
            records = [json.loads(path.read_bytes()) for path in output.directory.glob("*.json")]
            assert records[0]["state"] == "quota_exceeded"
            async with DockerExecutionBackend(tmp_path, IMAGE) as cancelled_backend:
                task = asyncio.create_task(
                    cancelled_backend.run_argv(
                        ("python", "-c", "import time; print('ready',flush=True); time.sleep(60)")
                    )
                )
                while len(list(output.directory.glob("*.json"))) < 2:
                    await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            records = [json.loads(path.read_bytes()) for path in output.directory.glob("*.json")]
            assert {record["state"] for record in records} == {"partial", "quota_exceeded"}
            assert backend.events[-1].kind == cancelled_backend.events[-1].kind == "backend_closed"
        finally:
            CURRENT_OUTPUT.reset(token)

    asyncio.run(scenario())
