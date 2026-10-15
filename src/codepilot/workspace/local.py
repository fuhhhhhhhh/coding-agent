"""实现与现有工具兼容的本地 ExecutionBackend。"""

import asyncio
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Self

from codepilot.contracts import ToolCall, ToolResult
from codepilot.sessions.outputs import CURRENT_OUTPUT
from codepilot.tools import ToolRegistry, create_basic_registry
from codepilot.tools.base import ToolSpec
from codepilot.tools.process import collect, spawn
from codepilot.workspace.common import BackendJournal, export_file
from codepilot.workspace.models import BackendEvent, BackendLimits, CommandResult


class LocalExecutionBackend:
    """在一个固定本地目录中统一执行工具和验证命令。"""

    def __init__(
        self,
        root: Path,
        *,
        limits: BackendLimits | None = None,
        registry: ToolRegistry | None = None,
        audit_directory: Path | None = None,
    ) -> None:
        """绑定已存在的工作区，并准备单任务生命周期状态。"""
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")
        self.limits = limits or BackendLimits()
        self.registry = registry or create_basic_registry(self.root)
        self.backend_id = f"local-{uuid.uuid4().hex}"
        self._journal = BackendJournal(audit_directory, self.root, self.backend_id)
        self._events: list[BackendEvent] = []
        self._active: set[asyncio.Task[object]] = set()
        self._processes: set[asyncio.subprocess.Process] = set()
        self._started = False
        self._cancelled = False
        self._closed = False

    @property
    def is_container(self) -> bool:
        """说明本地后端不提供容器隔离。"""
        return False

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """转发唯一工具注册表的声明。"""
        return self.registry.specs

    @property
    def events(self) -> tuple[BackendEvent, ...]:
        """返回调用方不能修改的事件快照。"""
        return tuple(self._events)

    async def start(self) -> None:
        """标记本地任务环境可用；重复启动保持幂等。"""
        if self._closed:
            raise RuntimeError("backend is closed")
        if not self._started:
            self._started = True
            self._record("backend_started", outcome="ready")

    async def execute(self, call: ToolCall) -> ToolResult:
        """跟踪当前协程并在同一工作区执行现有工具。"""
        self._ensure_ready()
        started = time.monotonic()
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            result = await self.registry.execute(call)
            self._record(
                "tool_executed",
                call_id=call.call_id,
                outcome="error" if result.is_error else "success",
                duration_seconds=time.monotonic() - started,
            )
            return result
        finally:
            if task is not None:
                self._active.discard(task)

    async def run_argv(
        self, command: tuple[str, ...], *, timeout_seconds: float | None = None
    ) -> CommandResult:
        """运行固定 argv；超时或取消时终止整个 POSIX 进程组。"""
        self._ensure_ready()
        if not command:
            raise ValueError("command must not be empty")
        started = time.monotonic()
        task = asyncio.current_task()
        factory = CURRENT_OUTPUT.get()
        call_id = f"argv-{uuid.uuid4().hex}"
        capture = factory.begin(call_id) if factory is not None else None
        process = await spawn(command, cwd=str(self.root), capture=capture)
        if task is not None:
            self._active.add(task)
        self._processes.add(process)
        timed_out = False
        cancelled = False
        output = asyncio.create_task(collect(process, self.limits.output_bytes, call_id, capture))
        timed_out = False
        cancelled = False
        try:
            async with asyncio.timeout(timeout_seconds or self.limits.command_timeout_seconds):
                stdout, stdout_truncated, stderr, stderr_truncated, reference = await output
        except TimeoutError:
            timed_out = True
            # collect 在超时取消中已经完成进程回收；没有完整结果时保留索引里的 partial。
            stdout, stderr, stdout_truncated, stderr_truncated, reference = b"", b"", True, True, ""
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._processes.discard(process)
            if task is not None:
                self._active.discard(task)
        result = CommandResult(
            exit_code=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=timed_out,
            cancelled=cancelled,
            output_reference=reference,
        )
        self._record(
            "command_executed",
            outcome="timeout" if timed_out else f"exit_{process.returncode}",
            duration_seconds=time.monotonic() - started,
        )
        return result

    async def cancel(self) -> None:
        """先封闭入口，再终止活动进程并取消仍在运行的工具协程。"""
        if self._cancelled or self._closed:
            return
        self._cancelled = True
        current = asyncio.current_task()
        tasks = [task for task in self._active if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*(self._stop_process(item) for item in self._processes))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._record("backend_cancelled", outcome="cancelled")

    async def export_artifact(self, relative_path: str, destination: Path) -> Path:
        """从同一任务工作区导出一个受容量限制的普通文件。"""
        self._ensure_ready()
        target = export_file(self.root, relative_path, destination, self.limits.artifact_bytes)
        self._record("artifact_exported", outcome="success")
        return target

    async def close(self) -> None:
        """终止尚存执行并只记录一次关闭事件。"""
        if self._closed:
            return
        if self._active or self._processes:
            await self.cancel()
        self._closed = True
        self._record("backend_closed", outcome="cleaned")

    async def __aenter__(self) -> Self:
        """启动本地后端并返回自身。"""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """无论任务是否抛错都清理活动子进程。"""
        await self.close()

    def _ensure_ready(self) -> None:
        """拒绝未启动、已取消或已关闭环境中的新工作。"""
        if not self._started:
            raise RuntimeError("backend is not started")
        if self._cancelled:
            raise RuntimeError("backend is cancelled")
        if self._closed:
            raise RuntimeError("backend is closed")

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        """先温和终止进程组，短暂等待后再强制杀死。"""
        from codepilot.tools.process import settle, stop_process

        await settle(stop_process(process))

    def _record(self, kind: str, **payload: object) -> None:
        """追加一条严格校验且不含正文的后端事件。"""
        event = BackendEvent.model_validate(
            {
                "sequence": len(self._events) + 1,
                "kind": kind,
                "backend_id": self.backend_id,
                **payload,
            }
        )
        self._events.append(event)
        self._journal.append(event)
