"""通过 Docker CLI 实现受限、可取消且可清理的任务执行后端。"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType
from typing import Self

from codepilot.contracts import ToolCall, ToolResult
from codepilot.tools import create_basic_registry
from codepilot.tools.base import ToolSpec
from codepilot.workspace.common import BackendJournal, export_file, read_limited
from codepilot.workspace.models import BackendEvent, BackendLimits, CommandResult

DockerRunner = Callable[[tuple[str, ...], bytes | None, int], Awaitable[tuple[int, bytes, bytes]]]


class DockerExecutionBackend:
    """让全部工具在一个非特权、默认断网的任务容器中执行。"""

    def __init__(
        self,
        root: Path,
        image: str,
        *,
        limits: BackendLimits | None = None,
        network: str = "none",
        owner: str | None = None,
        lifetime_seconds: int = 3600,
        runner: DockerRunner | None = None,
        audit_directory: Path | None = None,
    ) -> None:
        """保存显式镜像和隔离策略；直到 start 才创建容器。"""
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")
        if not image.strip():
            raise ValueError("docker image must not be empty")
        if network != "none":
            raise ValueError("network access requires a future explicit policy")
        if lifetime_seconds < 60:
            raise ValueError("container lifetime must be at least 60 seconds")
        self.image = image
        self.limits = limits or BackendLimits()
        self.network = network
        # 默认 owner 跨控制器进程保持稳定，崩溃后的新进程才能找到本用户遗留容器。
        self.owner = owner or f"codepilot-uid-{os.getuid()}"
        self.backend_id = f"docker-{uuid.uuid4().hex}"
        self._journal = BackendJournal(audit_directory, self.root, self.backend_id)
        self.container_name = f"codepilot-{uuid.uuid4().hex}"
        self.expires_at = int(time.time()) + lifetime_seconds
        self._runner = runner or _run_docker
        self._specs = create_basic_registry(self.root).specs
        self._events: list[BackendEvent] = []
        self._container_id = ""
        self._started = False
        self._cancelled = False
        self._closed = False

    @property
    def is_container(self) -> bool:
        """说明工具和命令在 Docker 容器边界内执行。"""
        return True

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """公开与容器内同版本基础工具一致的声明。"""
        return self._specs

    @property
    def events(self) -> tuple[BackendEvent, ...]:
        """返回控制器侧不可变的生命周期事件。"""
        return tuple(self._events)

    async def start(self) -> None:
        """解析镜像为不可变 ID，清理过期任务后创建并启动容器。"""
        if self._closed:
            raise RuntimeError("backend is closed")
        if self._started:
            return
        await self.cleanup_stale(self._runner, self.owner, self._record_stale)
        code, stdout, stderr = await self._docker(
            ("image", "inspect", "--format", "{{.Id}}", self.image)
        )
        if code != 0 or not stdout.strip().startswith(b"sha256:"):
            raise RuntimeError(f"docker image unavailable: {_safe_error(stderr)}")
        image_id = stdout.decode().strip()
        container_user = await self._container_user()
        command = self._create_command(image_id, container_user)
        code, stdout, stderr = await self._docker(command)
        if code != 0:
            raise RuntimeError(f"docker create failed: {_safe_error(stderr)}")
        self._container_id = stdout.decode().strip()
        code, _, stderr = await self._docker(("start", self._container_id))
        if code != 0:
            # 创建后启动失败仍必须删除，不能留下无人管理的容器。
            await self._docker(("rm", "-f", self._container_id))
            self._container_id = ""
            raise RuntimeError(f"docker start failed: {_safe_error(stderr)}")
        try:
            await self._verify_worker()
        except BaseException:
            # 镜像内容与控制器不一致时不能继续执行模型请求。
            await asyncio.shield(self._docker(("rm", "-f", "-v", self._container_id)))
            self._container_id = ""
            raise
        self._started = True
        self._record("backend_started", outcome="ready")

    async def execute(self, call: ToolCall) -> ToolResult:
        """把完整工具调用经 stdin 发送给容器内同版本 worker。"""
        self._ensure_ready()
        started = time.monotonic()
        payload = json.dumps(
            {"kind": "tool", "call": call.model_dump(mode="json")}, ensure_ascii=False
        ).encode()
        try:
            code, stdout, stderr = await self._docker(
                ("exec", "-i", self._container_id, "python", "-m", "codepilot.workspace.worker"),
                payload,
            )
        except asyncio.CancelledError:
            # docker exec 客户端退出不保证容器内进程退出，因此取消整个任务容器。
            await asyncio.shield(self.cancel())
            raise
        if code != 0:
            oom = await self._was_oom_killed()
            detail = "container_oom" if oom else f"container_exec_error: {_safe_error(stderr)}"
            result = ToolResult(call_id=call.call_id, content=detail, is_error=True)
        else:
            result = self._decode_tool_result(call, stdout)
        self._record(
            "tool_executed",
            call_id=call.call_id,
            outcome="error" if result.is_error else "success",
            duration_seconds=time.monotonic() - started,
        )
        await self.sample_resources()
        return result

    async def run_argv(
        self, command: tuple[str, ...], *, timeout_seconds: float | None = None
    ) -> CommandResult:
        """在同一容器中执行固定 argv，超时会杀死整个任务容器。"""
        self._ensure_ready()
        if not command:
            raise ValueError("command must not be empty")
        timeout = timeout_seconds or self.limits.command_timeout_seconds
        payload = json.dumps(
            {
                "kind": "command",
                "argv": command,
                "timeout_seconds": timeout,
                "output_bytes": self.limits.output_bytes,
            },
            ensure_ascii=False,
        ).encode()
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout + 1.0):
                code, stdout, stderr = await self._docker(
                    (
                        "exec",
                        "-i",
                        self._container_id,
                        "python",
                        "-m",
                        "codepilot.workspace.worker",
                    ),
                    payload,
                )
        except TimeoutError:
            await self.cancel()
            result = CommandResult(exit_code=None, timed_out=True)
        except asyncio.CancelledError:
            await asyncio.shield(self.cancel())
            raise
        else:
            if code == 0:
                result = CommandResult.model_validate_json(stdout)
            else:
                result = CommandResult(
                    exit_code=code,
                    stderr=_safe_error(stderr),
                    oom_killed=await self._was_oom_killed(),
                )
        self._record(
            "command_executed",
            outcome=(
                "oom"
                if result.oom_killed
                else "timeout"
                if result.timed_out
                else f"exit_{result.exit_code}"
            ),
            duration_seconds=time.monotonic() - started,
        )
        return result

    async def cancel(self) -> None:
        """杀死整个任务容器，确保 exec 后代不会在取消后继续执行。"""
        if self._cancelled or self._closed:
            return
        self._cancelled = True
        if self._container_id:
            code, _, stderr = await self._docker(("kill", self._container_id))
            if code != 0:
                self._record("backend_cancelled", outcome="kill_failed")
                raise RuntimeError(f"docker kill failed: {_safe_error(stderr)}")
        self._record("backend_cancelled", outcome="killed")

    async def export_artifact(self, relative_path: str, destination: Path) -> Path:
        """从唯一绑定工作区导出普通文件并记录结果。"""
        self._ensure_ready()
        target = export_file(self.root, relative_path, destination, self.limits.artifact_bytes)
        self._record("artifact_exported", outcome="success")
        return target

    async def sample_resources(self) -> None:
        """尽力采集一次 CPU 与内存快照；采样失败不改变工具结果。"""
        if not self._container_id or self._cancelled:
            return
        code, stdout, _ = await self._docker(
            ("stats", "--no-stream", "--format", "{{json .}}", self._container_id)
        )
        if code != 0:
            return
        try:
            record = json.loads(stdout)
            cpu = float(str(record["CPUPerc"]).rstrip("%"))
            memory = _parse_bytes(str(record["MemUsage"]).split("/")[0].strip())
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        self._record("resource_sampled", outcome="sampled", cpu_percent=cpu, memory_bytes=memory)

    async def close(self) -> None:
        """强制删除任务容器和匿名卷；任意先前状态下都可重复调用。"""
        if self._closed:
            return
        if self._container_id:
            code, _, stderr = await self._docker(("rm", "-f", "-v", self._container_id))
            if code != 0:
                self._record("backend_closed", outcome="remove_failed")
                raise RuntimeError(f"docker remove failed: {_safe_error(stderr)}")
        self._closed = True
        self._record("backend_closed", outcome="removed")

    async def __aenter__(self) -> Self:
        """创建并启动任务容器。"""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出任务范围时强制删除容器。"""
        await self.close()

    @staticmethod
    async def cleanup_stale(
        runner: DockerRunner,
        owner: str,
        on_removed: Callable[[str], None] | None = None,
    ) -> tuple[str, ...]:
        """查找同控制器所有者且已经过期的遗留容器并强制删除。"""
        code, stdout, _ = await runner(
            (
                "ps",
                "-a",
                "--filter",
                "label=codepilot.managed=true",
                "--filter",
                f"label=codepilot.owner={owner}",
                "--format",
                '{{.ID}} {{.Label "codepilot.expires_at"}}',
            ),
            None,
            262_144,
        )
        if code != 0:
            return ()
        removed: list[str] = []
        now = int(time.time())
        for line in stdout.decode(errors="replace").splitlines():
            parts = line.split(maxsplit=1)
            # 标签缺失或损坏时不误删；仅清理由本协议创建且明确过期的容器。
            if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) > now:
                continue
            container_id = parts[0]
            result, _, _ = await runner(("rm", "-f", "-v", container_id), None, 262_144)
            if result == 0:
                removed.append(container_id)
                if on_removed is not None:
                    on_removed(container_id)
        return tuple(removed)

    def _create_command(self, image_id: str, container_user: str) -> tuple[str, ...]:
        """构造不转发宿主环境、断网、只读根层和受资源限制的创建命令。"""
        return (
            "create",
            "--name",
            self.container_name,
            "--label",
            "codepilot.managed=true",
            "--label",
            f"codepilot.owner={self.owner}",
            "--label",
            f"codepilot.expires_at={self.expires_at}",
            "--network",
            self.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.limits.pids),
            "--memory",
            str(self.limits.memory_bytes),
            "--memory-swap",
            str(self.limits.memory_bytes),
            "--cpus",
            str(self.limits.cpus),
            "--user",
            container_user,
            "--workdir",
            "/workspace",
            "--mount",
            # bind 挂载默认可写；Docker 29 的 --mount 语法不接受独立的 rw 字段。
            f"type=bind,src={self.root},dst=/workspace",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864",
            "--init",
            "--stop-timeout",
            "1",
            image_id,
            "python",
            "-c",
            "import time; time.sleep(2147483647)",
        )

    async def _container_user(self) -> str:
        """根据 daemon 是否 rootless 选择能安全访问 bind 工作区的容器身份。"""
        code, stdout, stderr = await self._docker(("info", "--format", "{{json .SecurityOptions}}"))
        if code != 0:
            # 身份模式未知时拒绝创建，避免在 rootful daemon 中意外使用 root。
            raise RuntimeError(f"docker security options unavailable: {_safe_error(stderr)}")
        try:
            options = json.loads(stdout)
            if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
                raise ValueError("security options must be strings")
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            # 非预期响应同样采用失败关闭，不能凭本地环境猜测远端 daemon 类型。
            raise RuntimeError("docker returned invalid security options") from error
        if "name=rootless" in options:
            # rootless 的容器 0 映射为宿主普通用户，才能写该用户拥有的 bind 工作区。
            return "0:0"
        # rootful daemon 中必须显式降到控制器身份，避免容器以宿主 root 写入工作区。
        return f"{_sandbox_uid()}:{_sandbox_gid()}"

    async def _docker(
        self, arguments: tuple[str, ...], stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        """调用可替换的 Docker 传输并统一输出容量上限。"""
        return await self._runner(arguments, stdin, self.limits.output_bytes)

    async def _was_oom_killed(self) -> bool:
        """从容器状态读取内核 OOM 标记，避免依赖错误文本猜测。"""
        if not self._container_id:
            return False
        code, stdout, _ = await self._docker(
            ("inspect", "--format", "{{.State.OOMKilled}}", self._container_id)
        )
        return code == 0 and stdout.decode().strip().lower() == "true"

    async def _verify_worker(self) -> None:
        """启动时比较工具声明，拒绝控制器与沙箱镜像版本漂移。"""
        code, stdout, stderr = await self._docker(
            ("exec", "-i", self._container_id, "python", "-m", "codepilot.workspace.worker"),
            b'{"kind":"describe"}',
        )
        if code != 0:
            raise RuntimeError(f"sandbox worker unavailable: {_safe_error(stderr)}")
        try:
            raw_specs = json.loads(stdout)
            if not isinstance(raw_specs, list):
                raise ValueError("worker specs must be a list")
            # 严格契约从 Python 对象校验时拒绝 list；JSON 边界允许数组映射为 tuple。
            worker_specs = tuple(
                ToolSpec.model_validate_json(json.dumps(item)) for item in raw_specs
            )
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            raise ValueError("sandbox worker returned invalid specs") from error
        if worker_specs != self._specs:
            raise RuntimeError("sandbox tool specs do not match controller")

    def _decode_tool_result(self, call: ToolCall, payload: bytes) -> ToolResult:
        """校验 worker 回包并把协议损坏转成与调用配对的错误。"""
        try:
            result = ToolResult.model_validate_json(payload)
        except ValueError:
            return ToolResult(
                call_id=call.call_id,
                content="container_protocol_error: invalid tool result",
                is_error=True,
            )
        if result.call_id != call.call_id:
            return ToolResult(
                call_id=call.call_id,
                content="container_protocol_error: call ID mismatch",
                is_error=True,
            )
        return result

    def _ensure_ready(self) -> None:
        """阻止容器启动前、取消后或清理后的新调用。"""
        if not self._started:
            raise RuntimeError("backend is not started")
        if self._cancelled:
            raise RuntimeError("backend is cancelled")
        if self._closed:
            raise RuntimeError("backend is closed")

    def _record_stale(self, container_id: str) -> None:
        """记录由本次启动清理的过期容器 ID。"""
        self._record("stale_container_removed", container_id=container_id, outcome="removed")

    def _record(self, kind: str, **payload: object) -> None:
        """追加不包含工具参数、命令和环境内容的后端事件。"""
        event = BackendEvent.model_validate(
            {
                "sequence": len(self._events) + 1,
                "kind": kind,
                "backend_id": self.backend_id,
                "container_id": self._container_id,
                **payload,
            }
        )
        self._events.append(event)
        self._journal.append(event)


async def _run_docker(
    arguments: tuple[str, ...], stdin: bytes | None, output_limit: int
) -> tuple[int, bytes, bytes]:
    """执行 Docker CLI，并在协程取消时终止 CLI 子进程。"""
    process = await asyncio.create_subprocess_exec(
        "docker",
        *arguments,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if stdin is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Docker CLI 提前失败时仍读取其 stderr，由上层形成稳定错误。
            pass
        finally:
            process.stdin.close()
    stdout_task = asyncio.create_task(read_limited(process.stdout, output_limit))
    stderr_task = asyncio.create_task(read_limited(process.stderr, output_limit))
    try:
        await process.wait()
    except asyncio.CancelledError:
        # 这里终止的是 CLI；DockerExecutionBackend 还会杀死对应任务容器。
        process.kill()
        await process.wait()
        for reader in (stdout_task, stderr_task):
            reader.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    stdout, _ = await stdout_task
    stderr, _ = await stderr_task
    return process.returncode or 0, stdout, stderr


def _safe_error(stderr: bytes) -> str:
    """只保留 Docker 错误尾部并折叠换行，防止日志无界扩张。"""
    return stderr.decode("utf-8", errors="replace")[-1000:].replace("\n", " ").strip() or "unknown"


def _parse_bytes(value: str) -> int:
    """解析 docker stats 使用的二进制或十进制容量单位。"""
    units = {
        "B": 1,
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }
    for unit in sorted(units, key=len, reverse=True):
        if value.endswith(unit):
            return int(float(value[: -len(unit)].strip()) * units[unit])
    raise ValueError("unknown memory unit")


def _sandbox_uid() -> int:
    """沿用非 root 控制器 UID；root 控制器固定降权到无特权 UID。"""
    return os.getuid() or 65532


def _sandbox_gid() -> int:
    """沿用非 root 控制器 GID；root 主组固定降权到无特权 GID。"""
    return os.getgid() or 65532
