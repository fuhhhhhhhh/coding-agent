"""通过 Docker CLI 实现受限、可取消且可清理的任务执行后端。

本模块是控制器一侧的 Docker 适配层。真正的工具代码运行在容器内
`codepilot.workspace.worker`，本模块只负责构造边界、传输内部契约、记录轨迹
以及在异常和取消时回收资源。这样可以把“允许执行什么”和“在哪里执行”分开。
"""

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
from codepilot.sessions.outputs import CURRENT_OUTPUT, FileCapture
from codepilot.tools import create_basic_registry
from codepilot.tools.base import ToolSpec
from codepilot.tools.process import settle
from codepilot.workspace.common import BackendJournal, export_file, read_limited
from codepilot.workspace.models import BackendEvent, BackendLimits, CommandResult
from codepilot.workspace.output_stream import stream_worker

DockerRunner = Callable[[tuple[str, ...], bytes | None, int], Awaitable[tuple[int, bytes, bytes]]]


class DockerExecutionBackend:
    """让一项任务的全部工具在同一个受限容器中执行。

    一个 backend 对应一个 task/trial，而不是一个单独的工具调用。容器持续存在
    的意义是：先写文件、再运行测试时，两个动作看到同一个 `/workspace`；取消时
    也能以整个容器为边界停止当前命令和它产生的子进程。容器不挂载 Docker socket，
    不继承控制器环境变量，默认使用断网、只读根层和资源限制。
    """

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
        """校验并保存任务配置，但暂不访问 Docker daemon。

        `root` 是控制器和容器共同看到的唯一工作区；`image` 是用户选择的镜像
        标签，真正创建容器前会解析成不可变的 image ID。`runner` 是 Docker CLI
        的可替换传输，生产环境使用 `_run_docker`，单元测试用 fake runner，避免
        测试依赖真实 daemon。所有状态字段先初始化为“尚未启动”，因此构造对象
        本身不会产生容器或副作用。
        """
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
        # bind/宿主目录没有可强制的文件系统容量边界，请求硬配额必须失败关闭。
        if self.limits.workspace_bytes is not None:
            raise ValueError("hard workspace disk quota is unavailable for this backend")
        self.network = network
        # 默认 owner 跨控制器进程保持稳定，崩溃后的新进程才能找到本用户遗留容器。
        self.owner = owner or f"codepilot-uid-{os.getuid()}"
        self.backend_id = f"docker-{uuid.uuid4().hex}"
        self._journal = BackendJournal(audit_directory, self.root, self.backend_id)
        self.container_name = f"codepilot-{uuid.uuid4().hex}"
        self.expires_at = int(time.time()) + lifetime_seconds
        self._runner = runner or _run_docker
        self._container_specs = create_basic_registry(self.root).specs
        self._specs = self._container_specs
        self._events: list[BackendEvent] = []
        self._container_id = ""
        self._started = False
        self._cancelled = False
        self._closed = False

    @property
    def is_container(self) -> bool:
        """返回当前后端是否提供真实容器边界。

        权限层和评测 Harness 会读取这个属性，区分 Docker 后端与只能提供进程
        级约束的 Local 后端。它不是对容器“绝对安全”的承诺；Docker 仍共享宿主
        内核，具体限制由 `_create_command` 和真实验收共同定义。
        """
        return True

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """返回控制器侧向模型公开的工具声明。

        这些声明来自控制器里的基础注册表，启动时还会和容器 worker 的声明逐项
        比较。调用方因此可以在容器启动后使用同一份 `ToolSpec`，而不会出现模型
        看到新参数、容器却运行旧工具实现的版本漂移。
        """
        return self._specs

    @property
    def events(self) -> tuple[BackendEvent, ...]:
        """返回按发生顺序冻结的后端事件快照。

        事件只保存状态、结果、耗时和资源采样，不保存工具参数、命令正文、环境
        变量或输出内容；调用者拿到 tuple 后不能修改内部事件列表。
        """
        return tuple(self._events)

    async def start(self) -> None:
        """完成一次幂等启动，并在工具执行前验证 worker 契约。

        顺序很重要：先清理同 owner 的过期容器，再把镜像标签解析为 digest/ID，
        然后查询 daemon 类型、构造受限 `docker create` 参数、启动容器，最后发送
        `describe` 握手。任何一步失败都不把 backend 标成 started；如果容器已经
        创建但启动或握手失败，立即强制删除，避免泄漏无人管理的容器。
        """
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
        """把一个完整 `ToolCall` 交给容器内 worker，并校验配对结果。

        控制器只发送 JSON 请求，工具参数的最终校验仍在容器内注册表执行。worker
        返回的 `ToolResult` 必须带回相同 call ID；Docker exec 自身失败会被转换成
        稳定错误，协程取消则不仅停止本地 CLI，还会调用 `cancel()` 杀死整个任务
        容器，防止容器内的 Shell 继续运行。
        """
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
        """在同一容器中执行不经过 Shell 展开的固定 argv 命令。

        该入口主要给测试和独立验证器使用，因此参数会作为数组传给 worker，不会
        重新拼接成 Shell 字符串。worker 返回结构化 `CommandResult`；控制器外层
        还设置一个比 worker 时限略长的 asyncio 超时，用来处理 worker 卡死或
        Docker CLI 无响应。超时和取消都会停止整个 backend，后续调用被拒绝。
        """
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
        """取消任务并封闭后续执行入口。

        `docker exec` 是控制器启动的客户端进程，容器内命令是另一棵进程树；只
        取消客户端不能证明命令已经停止。因此这里设置 `_cancelled` 后发送
        `docker kill`，让容器及其后代一起退出。kill 失败会保留失败事件并抛出
        错误，因为此时不能假装资源已经清理。
        """
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
        """把工作区内的普通文件复制到控制器指定的产物位置。

        路径规范化、符号链接检查、目标必须位于工作区外以及文件大小上限都由
        `export_file` 负责；本方法只保证导出发生在已启动且未取消的 backend 中，
        并把成功事实写入后端轨迹。
        """
        self._ensure_ready()
        target = export_file(self.root, relative_path, destination, self.limits.artifact_bytes)
        self._record("artifact_exported", outcome="success")
        return target

    async def sample_resources(self) -> None:
        """读取一次容器级 CPU/内存快照并写入可观察轨迹。

        资源采样是诊断信息，不是执行结果的一部分。容器刚被取消、已经被清理或
        daemon 暂时无法返回 stats 时直接跳过，不能因为监控失败而把一个已经成功
        的工具调用改成失败；解析失败也只丢弃本次样本。
        """
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
        """删除当前 backend 拥有的容器和匿名卷，并使关闭操作幂等。

        `rm -f -v` 同时覆盖运行中、已停止和带匿名卷的容器。关闭失败时不设置
        `_closed`，因为资源可能仍然存在，调用方可以根据错误重试；成功后才记录
        `backend_closed`。上下文管理器的 `__aexit__` 会调用本方法，所以正常返回、
        异常和取消都走同一个清理出口。
        """
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
        """进入异步上下文时启动并返回当前 backend。"""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """离开异步上下文时无条件尝试清理任务容器。

        `exc_type`、`exc` 和 `traceback` 由 Python 传入，清理策略不依赖异常类型；
        即使任务内部抛错，也不能把容器留给下一次进程误用。
        """
        await self.close()

    @staticmethod
    async def cleanup_stale(
        runner: DockerRunner,
        owner: str,
        on_removed: Callable[[str], None] | None = None,
    ) -> tuple[str, ...]:
        """按 managed/owner/expiry 标签清理本控制器留下的过期容器。

        这是崩溃恢复路径：上一个控制器可能来不及执行 `close()`。查询只筛选本
        协议的 managed 标签和同一个 owner；每行必须同时包含合法容器 ID 与数字化
        过期时间，标签缺失、损坏或尚未到期都跳过，避免误删公共 Docker 中的其他
        容器。删除成功后才通过 `on_removed` 把清理事实交给当前 backend 记录。
        """
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
        """把隔离策略编码为完整的 `docker create` 参数数组。

        这里刻意使用 tuple 而不是 Shell 字符串：每个参数保持独立，避免引号和
        空格被重新解释。策略包括默认断网、只读根层、删除全部 capability、
        `no-new-privileges`、CPU/内存/PID 上限、固定工作目录和唯一 bind mount。
        `container_user` 由 `_container_user` 决定；镜像使用已解析的 ID，避免同一
        标签在任务执行期间漂移到另一份镜像。
        """
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
        """读取 daemon 安全模式并选择容器内 UID/GID。

        rootful daemon 中必须显式降权为控制器 UID/GID，避免容器 root 写宿主工作区；
        rootless daemon 的用户命名空间会把容器 `0:0` 映射为宿主启动用户，把普通
        容器 UID 映射成 subordinate UID。后者通常无法写入宿主用户拥有的 bind
        目录，因此 rootless 分支反而使用 namespace 内 root；这仍不会获得宿主 root
        权限，因为 daemon 本身就是普通用户运行的。
        """
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
        """通过 runner 执行一条 Docker CLI 调用，并统一限制 stdout/stderr。

        所有 Docker 操作都经过这里，生产 runner 和 fake runner 因而共享同一个
        参数、输入和输出契约；输出上限防止 daemon 的错误信息或 stats 无限增长。
        """
        factory = CURRENT_OUTPUT.get()
        if (
            self._runner is _run_docker
            and factory is not None
            and stdin is not None
            and "codepilot.workspace.worker" in arguments
        ):
            request = json.loads(stdin)
            if request.get("kind") == "command" or (
                request.get("kind") == "tool" and request.get("call", {}).get("name") == "shell"
            ):
                call_id = request.get("call", {}).get("call_id", f"argv-{uuid.uuid4().hex}")
                capture = factory.begin(call_id)
                if not isinstance(capture, FileCapture):
                    raise ValueError("controller output factory must own files")
                request.update(stream_output=True, output_id=capture.output_id)
                try:
                    return await stream_worker(arguments, json.dumps(request).encode(), capture)
                except BaseException:
                    # 写盘失败或传输中断也可能留下容器内进程，不能只杀本地客户端。
                    await settle(self.cancel())
                    raise
        return await self._runner(
            arguments, stdin, max(1024 * 1024, self.limits.output_bytes * 8 + 65536)
        )

    async def _was_oom_killed(self) -> bool:
        """读取容器主进程的 Docker OOM 标记。

        该检查只能覆盖容器主进程被内核 OOM 杀死的情况。常驻主进程仍在运行时，
        `docker exec` 里的子进程 OOM 不一定反映到这里；worker 会额外比较 cgroup
        `memory.events`，所以本方法不能单独作为所有 OOM 场景的判断依据。
        """
        if not self._container_id:
            return False
        code, stdout, _ = await self._docker(
            ("inspect", "--format", "{{.State.OOMKilled}}", self._container_id)
        )
        return code == 0 and stdout.decode().strip().lower() == "true"

    async def _verify_worker(self) -> None:
        """用 describe 握手比较控制器和容器内的完整工具 Schema。

        worker 返回 JSON 数组，控制器逐项还原为 `ToolSpec` 后与本地注册表比较。
        只要 worker 不可用、返回格式损坏或工具顺序/参数声明不同，启动就失败并由
        `start()` 删除容器；宁可整项任务停止，也不让模型按错误 Schema 发起调用。
        """
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
        if worker_specs != self._container_specs:
            raise RuntimeError("sandbox tool specs do not match controller")

    def _decode_tool_result(self, call: ToolCall, payload: bytes) -> ToolResult:
        """把 worker 的 JSON 回包转换成受信任的 `ToolResult`。

        Pydantic 校验负责字段类型和必填项，额外的 call ID 检查负责防止旧响应、
        串线响应或错误容器响应被交给当前调用方。协议损坏会返回带当前 call ID 的
        结构化错误，而不是把原始响应正文写入日志或继续执行。
        """
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
        """在每个执行入口统一检查 backend 生命周期状态。

        未启动表示还没有合法容器；cancelled 表示容器已经被停止，closed 表示
        资源已经回收。三种状态都拒绝新调用，避免调用方在错误恢复路径中重新使用
        已失效的工作区或把结果写入另一项任务。
        """
        if not self._started:
            raise RuntimeError("backend is not started")
        if self._cancelled:
            raise RuntimeError("backend is cancelled")
        if self._closed:
            raise RuntimeError("backend is closed")

    def _record_stale(self, container_id: str) -> None:
        """把 stale cleanup 删除的容器 ID转成当前 backend 的审计事件。"""
        self._record("stale_container_removed", container_id=container_id, outcome="removed")

    def _record(self, kind: str, **payload: object) -> None:
        """追加一条脱敏生命周期事件，并同步写入外部审计日志。

        sequence 在控制器内递增，backend/container ID 用于关联资源；调用参数、
        命令正文、环境变量和输出正文都不会进入事件。先更新内存列表再交给
        `BackendJournal`，因此调用方可以立即读取本次轨迹，同时日志文件保留持久
        化审计记录。
        """
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
    """执行不经 Shell 的 Docker CLI，并有界读取 stdout/stderr。

    使用 `create_subprocess_exec` 可以让每个 Docker 参数保持原样，避免 Shell 注入。
    两个读取任务会持续排空管道，即使只保留前 N 字节也不会因为子进程写满 pipe
    而死锁。若协程被取消，只终止本地 Docker CLI；上层 `DockerExecutionBackend`
    随后负责 kill 容器，因为这里无法知道容器内 exec 是否已经结束。
    """
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
    """把 Docker stderr 压缩成适合异常和事件的有限错误摘要。

    保留尾部通常能看到真正的失败原因，折叠换行让一条错误不会污染事件格式；
    只返回摘要，不把完整 stderr 写入模型上下文或持久化轨迹。
    """
    return stderr.decode("utf-8", errors="replace")[-1000:].replace("\n", " ").strip() or "unknown"


def _parse_bytes(value: str) -> int:
    """把 `docker stats` 的内存字符串转换为字节数。

    Docker 可能返回 `MiB` 等二进制单位，也可能返回 `MB` 等十进制单位；按单位
    长度倒序匹配，避免先把 `MiB` 错误识别成末尾的 `B`。未知格式抛出异常，调用方
    会丢弃该次采样而不影响工具结果。
    """
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
    """返回 rootful daemon 下容器应使用的 UID。

    普通用户运行时沿用其 UID，让 bind 工作区的宿主所有权保持一致；若控制器
    意外以 root 启动，则使用 65532 这一无特权回退值，避免把宿主 root 直接暴露
    给任务容器。rootless 分支不使用此值，而由 `_container_user` 选择 `0:0`。
    """
    return os.getuid() or 65532


def _sandbox_gid() -> int:
    """返回 rootful daemon 下容器应使用的 GID。

    规则与 `_sandbox_uid` 对称：普通用户沿用主组，root 控制器回退到 65532，
    确保任务不会以宿主 root 主组访问工作区。
    """
    return os.getgid() or 65532
