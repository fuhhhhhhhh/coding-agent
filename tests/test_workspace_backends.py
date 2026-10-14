"""验证统一执行后端、本地生命周期和 Docker 隔离命令契约。"""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from codepilot.contracts import ToolCall, ToolResult
from codepilot.workspace import BackendLimits, DockerExecutionBackend, LocalExecutionBackend
from codepilot.workspace.worker import _oom_kill_increased, _read_oom_kill_count


def test_local_backend_shares_workspace_and_exports_artifact(tmp_path: Path) -> None:
    """文件工具、固定命令和产物导出必须观察同一份任务内容。"""

    async def scenario() -> None:
        """写入文件后由独立 argv 命令读取，再导出给控制器。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        destination = tmp_path / "artifacts" / "answer.txt"
        backend = LocalExecutionBackend(workspace)
        async with backend:
            result = await backend.execute(
                ToolCall(
                    call_id="write-1",
                    name="write_file",
                    arguments={"path": "answer.txt", "content": "forty-two\n"},
                )
            )
            command = await backend.run_argv(
                (sys.executable, "-c", "print(open('answer.txt').read().strip())")
            )
            exported = await backend.export_artifact("answer.txt", destination)
        assert not result.is_error
        assert command.exit_code == 0
        assert command.stdout.strip() == "forty-two"
        assert exported.read_text(encoding="utf-8") == "forty-two\n"
        assert [event.kind for event in backend.events] == [
            "backend_started",
            "tool_executed",
            "command_executed",
            "artifact_exported",
            "backend_closed",
        ]

        another = LocalExecutionBackend(workspace)
        async with another:
            with pytest.raises(ValueError, match="outside workspace"):
                await another.export_artifact("answer.txt", workspace / "copy.txt")

    asyncio.run(scenario())


def test_backend_journal_is_external_private_and_redacted(tmp_path: Path) -> None:
    """后端审计写在工作区外、权限为 0600，且不包含工具参数正文。"""

    async def scenario() -> None:
        """执行含敏感正文的写入并检查持久化字段。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        audit = tmp_path / "audit"
        backend = LocalExecutionBackend(workspace, audit_directory=audit)
        async with backend:
            await backend.execute(
                ToolCall(
                    call_id="secret-write",
                    name="write_file",
                    arguments={"path": "secret.txt", "content": "never-persist-this"},
                )
            )
        path = next(audit.glob("*.jsonl"))
        content = path.read_text(encoding="utf-8")
        assert "never-persist-this" not in content
        assert "secret.txt" not in content
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(ValueError, match="outside workspace"):
            LocalExecutionBackend(workspace, audit_directory=workspace / "audit")

    asyncio.run(scenario())


def test_local_backend_timeout_stops_process_group(tmp_path: Path) -> None:
    """CPU 型长任务达到期限后返回明确超时且直接进程已经退出。"""

    async def scenario() -> None:
        """以无限循环模拟 CPU 故障并检查后端仍可清理。"""
        backend = LocalExecutionBackend(
            tmp_path, limits=BackendLimits(command_timeout_seconds=0.05)
        )
        async with backend:
            result = await backend.run_argv((sys.executable, "-c", "while True: pass"))
            assert result.timed_out
            assert result.exit_code is not None
            assert not backend._processes

    asyncio.run(scenario())


def test_worker_oom_counter_reader_handles_kernel_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 只读取 memory.events 中精确的 oom_kill 数字。"""

    def fake_read_text(path: Path, *, encoding: str) -> str:
        """返回与 cgroup v2 相同的键值行，隔离测试不依赖宿主计数。"""
        assert str(path) == "/sys/fs/cgroup/memory.events"
        assert encoding == "utf-8"
        return "low 0\nhigh 0\noom 3\noom_kill 2\n"

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    assert _read_oom_kill_count() == 2
    assert _oom_kill_increased(1, 2)
    assert not _oom_kill_increased(None, 2)


def test_local_backend_cancel_stops_running_command_and_closes_entry(tmp_path: Path) -> None:
    """显式取消会停止活动命令，并拒绝在同一任务环境继续启动工作。"""

    async def scenario() -> None:
        """等待子进程出现后调用后端取消，而非只取消输出读取。"""
        backend = LocalExecutionBackend(tmp_path)
        await backend.start()
        task = asyncio.create_task(
            backend.run_argv((sys.executable, "-c", "import time; time.sleep(60)"))
        )
        for _ in range(100):
            if backend._processes:
                break
            await asyncio.sleep(0.005)
        await backend.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="cancelled"):
            await backend.execute(ToolCall(call_id="read", name="glob", arguments={"pattern": "*"}))
        await backend.close()
        assert not backend._processes

    asyncio.run(scenario())


class FakeDockerRunner:
    """记录 Docker argv，并为生命周期和 worker 返回受控响应。"""

    def __init__(self) -> None:
        """初始化调用记录和可覆盖的 exec 响应。"""
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []
        self.exec_result = ToolResult(call_id="read-1", content="ok").model_dump_json().encode()
        self.exec_code = 0
        self.oom_killed = False
        self.block_exec = False
        self.specs_mismatch = False
        self.exec_started = asyncio.Event()

    async def __call__(
        self, arguments: tuple[str, ...], stdin: bytes | None, output_limit: int
    ) -> tuple[int, bytes, bytes]:
        """按子命令返回最小有效 Docker 响应。"""
        self.calls.append((arguments, stdin))
        if arguments[0] == "ps":
            return 0, b"", b""
        if arguments[:2] == ("image", "inspect"):
            return 0, b"sha256:fixed-image\n", b""
        if arguments[0] == "info":
            return 0, b'["name=seccomp,profile=builtin"]\n', b""
        if arguments[0] == "create":
            return 0, b"container-123\n", b""
        if arguments[0] in {"start", "kill", "rm"}:
            return 0, b"", b""
        if arguments[0] == "stats":
            return 0, b'{"CPUPerc":"2.5%","MemUsage":"12MiB / 256MiB"}', b""
        if arguments[0] == "exec":
            self.exec_started.set()
            if self.block_exec:
                await asyncio.Event().wait()
            if stdin is not None and json.loads(stdin).get("kind") == "describe":
                from codepilot.tools import create_basic_registry

                specs = create_basic_registry(Path.cwd()).specs
                if self.specs_mismatch:
                    specs = specs[:-1]
                return 0, json.dumps([item.model_dump(mode="json") for item in specs]).encode(), b""
            return self.exec_code, self.exec_result, b"worker failed"
        if arguments[0] == "inspect":
            return 0, b"true\n" if self.oom_killed else b"false\n", b""
        raise AssertionError(f"unexpected docker call: {arguments}")


def test_docker_backend_uses_fixed_image_and_restricted_create(tmp_path: Path) -> None:
    """容器创建必须使用解析后的镜像 ID，并包含核心隔离与资源参数。"""

    async def scenario() -> None:
        """启动假容器并检查没有宿主环境或 Docker socket 转发参数。"""
        runner = FakeDockerRunner()
        backend = DockerExecutionBackend(tmp_path, "codepilot-sandbox:0.1.0", runner=runner)
        async with backend:
            result = await backend.execute(
                ToolCall(call_id="read-1", name="read_file", arguments={"path": "a.txt"})
            )
        assert result.content == "ok"
        create = next(call for call, _ in runner.calls if call[0] == "create")
        joined = " ".join(create)
        assert "sha256:fixed-image" in create
        assert "--network none" in joined
        assert "--read-only" in create
        assert "--cap-drop ALL" in joined
        assert "no-new-privileges" in create
        assert "--pids-limit" in create
        assert "--memory" in create
        assert "--cpus" in create
        assert "--user" in create
        assert create[create.index("--user") + 1].split(":")[0] != "0"
        mount = create[create.index("--mount") + 1]
        assert mount.startswith("type=bind,src=")
        assert mount.endswith(",dst=/workspace")
        assert ",rw" not in mount
        assert "/var/run/docker.sock" not in joined
        assert "--env" not in create and "-e" not in create
        payload = next(
            stdin
            for call, stdin in runner.calls
            if call[0] == "exec" and stdin is not None and json.loads(stdin).get("kind") == "tool"
        )
        assert payload is not None
        assert json.loads(payload)["call"]["call_id"] == "read-1"
        assert runner.calls[-1][0][:3] == ("rm", "-f", "-v")

    asyncio.run(scenario())


def test_docker_backend_uses_namespace_root_with_rootless_daemon(tmp_path: Path) -> None:
    """rootless 容器的根用户映射为宿主普通用户，才能写用户拥有的 bind 工作区。"""

    class RootlessDockerRunner(FakeDockerRunner):
        """只把 daemon 安全选项改为 rootless，其余生命周期沿用假传输。"""

        async def __call__(
            self, arguments: tuple[str, ...], stdin: bytes | None, output_limit: int
        ) -> tuple[int, bytes, bytes]:
            """返回 rootless 标记，并继续记录全部 Docker 命令。"""
            if arguments[0] == "info":
                self.calls.append((arguments, stdin))
                return 0, b'["name=seccomp,profile=builtin","name=rootless"]\n', b""
            return await super().__call__(arguments, stdin, output_limit)

    async def scenario() -> None:
        """启动 rootless 假 daemon 并检查容器身份映射。"""
        runner = RootlessDockerRunner()
        backend = DockerExecutionBackend(tmp_path, "image", runner=runner)
        async with backend:
            pass
        create = next(call for call, _ in runner.calls if call[0] == "create")
        assert create[create.index("--user") + 1] == "0:0"

    asyncio.run(scenario())


def test_docker_backend_rejects_worker_schema_drift_and_removes_container(
    tmp_path: Path,
) -> None:
    """旧镜像缺少工具声明时必须在任务开始前失败并删除已建容器。"""

    async def scenario() -> None:
        """模拟 worker 工具集合漂移并检查清理命令。"""
        runner = FakeDockerRunner()
        runner.specs_mismatch = True
        backend = DockerExecutionBackend(tmp_path, "image", runner=runner)
        with pytest.raises(RuntimeError, match="specs do not match"):
            await backend.start()
        assert any(call[:3] == ("rm", "-f", "-v") for call, _ in runner.calls)

    asyncio.run(scenario())


def test_docker_backend_reports_kernel_oom_state(tmp_path: Path) -> None:
    """worker 异常退出后应读取 Docker OOMKilled 状态，而非猜测 stderr 文本。"""

    async def scenario() -> None:
        """模拟退出码 137 与内核 OOM 标记并检查模型可见分类。"""
        runner = FakeDockerRunner()
        runner.exec_code = 137
        runner.oom_killed = True
        backend = DockerExecutionBackend(tmp_path, "image", runner=runner)
        async with backend:
            result = await backend.execute(
                ToolCall(call_id="read-1", name="read_file", arguments={"path": "a.txt"})
            )
        assert result.is_error
        assert result.content == "container_oom"
        assert any(call[0] == "inspect" for call, _ in runner.calls)

    asyncio.run(scenario())


def test_docker_cancel_kills_container_before_cleanup(tmp_path: Path) -> None:
    """取消等待中的 docker exec 时必须发出容器 kill，而非只结束 CLI。"""

    async def scenario() -> None:
        """阻塞 worker 调用，取消协程并检查 kill 与后续拒绝。"""
        runner = FakeDockerRunner()
        backend = DockerExecutionBackend(tmp_path, "image", runner=runner)
        await backend.start()
        runner.block_exec = True
        runner.exec_started.clear()
        task = asyncio.create_task(
            backend.execute(
                ToolCall(call_id="read-1", name="read_file", arguments={"path": "a.txt"})
            )
        )
        await runner.exec_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(call[0] == "kill" for call, _ in runner.calls)
        with pytest.raises(RuntimeError, match="cancelled"):
            await backend.execute(
                ToolCall(call_id="later", name="read_file", arguments={"path": "a.txt"})
            )
        await backend.close()

    asyncio.run(scenario())


def test_cleanup_stale_only_removes_expired_owned_containers() -> None:
    """崩溃恢复清理只处理带管理标签、同 owner 且已经过期的容器。"""

    async def scenario() -> None:
        """返回一个过期和一个未过期容器并核对删除范围。"""
        calls: list[tuple[str, ...]] = []

        async def runner(
            arguments: tuple[str, ...], stdin: bytes | None, output_limit: int
        ) -> tuple[int, bytes, bytes]:
            """模拟 Docker 列表和删除响应。"""
            calls.append(arguments)
            if arguments[0] == "ps":
                return 0, f"old {int(time.time()) - 1}\nnew {int(time.time()) + 60}\n".encode(), b""
            return 0, b"", b""

        removed = await DockerExecutionBackend.cleanup_stale(runner, "stable-owner")
        assert removed == ("old",)
        assert any(call[-1] == "old" for call in calls if call[0] == "rm")
        assert not any(call[-1] == "new" for call in calls if call[0] == "rm")

    asyncio.run(scenario())
