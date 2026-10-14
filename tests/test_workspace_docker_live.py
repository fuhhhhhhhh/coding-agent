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
