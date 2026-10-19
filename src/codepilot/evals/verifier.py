"""实现使用明确执行后端、独立材料与受控生命周期的验证器。"""

import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path

from codepilot.evals.models import VerificationResult
from codepilot.workspace import ExecutionBackend, LocalExecutionBackend


class IndependentVerifier:
    """用固定命令在 Agent 结束后独立验证工作区。"""

    def __init__(
        self,
        command: tuple[str, ...],
        timeout_seconds: float = 10.0,
        *,
        backend_factory: Callable[[Path], ExecutionBackend] | None = None,
        on_started: Callable[[ExecutionBackend], None] | None = None,
    ) -> None:
        """保存不可由任务工作区修改的 argv 和验证时限。"""
        if not command:
            raise ValueError("verifier command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("verifier timeout must be positive")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.backend_factory = backend_factory or LocalExecutionBackend
        self.on_started = on_started
        self.backend: ExecutionBackend | None = None

    async def verify(self, workspace: Path) -> VerificationResult:
        """在工作区运行固定验证命令，并把进程故障转成明确状态。"""
        started = time.monotonic()
        try:
            # 明确后端决定隔离边界，Docker 验收不得悄悄在控制器宿主执行候选代码。
            async with AsyncExitStack() as resources:
                backend = self.backend_factory(workspace)
                self.backend = backend
                resources.push_async_callback(backend.close)
                await backend.start()
                if self.on_started is not None:
                    self.on_started(backend)
                result = await backend.run_argv(self.command, timeout_seconds=self.timeout_seconds)
        except (OSError, RuntimeError, ValueError) as error:
            return VerificationResult(
                status="error",
                passed=False,
                detail=f"verifier_start_error: {type(error).__name__}",
                duration_seconds=time.monotonic() - started,
            )
        if result.timed_out:
            return VerificationResult(
                status="timeout",
                passed=False,
                detail=f"verifier_timeout: {self.timeout_seconds:g}s",
                duration_seconds=time.monotonic() - started,
            )
        if result.oom_killed:
            return VerificationResult(
                status="error",
                passed=False,
                detail="verifier OOM",
                duration_seconds=time.monotonic() - started,
            )
        output = result.stdout + result.stderr
        return VerificationResult(
            status="passed" if result.exit_code == 0 else "failed",
            passed=result.exit_code == 0,
            detail=output[-4000:] or f"exit_code={result.exit_code}",
            exit_code=result.exit_code,
            duration_seconds=time.monotonic() - started,
        )


class IsolatedVerifier:
    """从同一初始代码重建候选，并在 Agent 未见的材料注入后验证。"""

    def __init__(
        self,
        source: Path,
        validation_material: Path,
        command: tuple[str, ...],
        backend_factory: Callable[[Path], ExecutionBackend],
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        """固定可信源、外部验证材料、命令和独立环境工厂。"""
        self.source = source.resolve(strict=True)
        self.validation_material = validation_material.resolve(strict=True)
        if not self.source.is_dir() or not self.validation_material.is_dir():
            raise ValueError("source and validation material must be directories")
        if not command:
            raise ValueError("verifier command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("verifier timeout must be positive")
        self.command = command
        self.backend_factory = backend_factory
        self.timeout_seconds = timeout_seconds

    async def verify(self, candidate: Path) -> VerificationResult:
        """把候选差异应用到干净副本，最后注入隐藏材料并执行验证。"""
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="codepilot-verify-") as directory:
                root = Path(directory) / "workspace"
                shutil.copytree(self.source, root, symlinks=True)
                _apply_candidate(self.source, candidate.resolve(strict=True), root)
                hidden = root / ".codepilot-validation"
                # 候选不得预占注入位置，否则可能用伪造验证材料影响得分。
                if hidden.exists() or hidden.is_symlink():
                    raise ValueError("candidate occupies reserved validation path")
                shutil.copytree(self.validation_material, hidden, symlinks=False)
                backend = self.backend_factory(root)
                async with backend:
                    result = await backend.run_argv(
                        self.command, timeout_seconds=self.timeout_seconds
                    )
        except (OSError, RuntimeError, ValueError) as error:
            return VerificationResult(
                status="error",
                passed=False,
                detail=f"isolated_verifier_error: {type(error).__name__}",
                duration_seconds=time.monotonic() - started,
            )
        if result.timed_out:
            return VerificationResult(
                status="timeout",
                passed=False,
                detail="isolated verifier timeout",
                duration_seconds=time.monotonic() - started,
            )
        if result.oom_killed:
            return VerificationResult(
                status="error",
                passed=False,
                detail="isolated verifier OOM",
                duration_seconds=time.monotonic() - started,
            )
        detail = (result.stdout + result.stderr)[-4000:]
        return VerificationResult(
            status="passed" if result.exit_code == 0 else "failed",
            passed=result.exit_code == 0,
            detail=detail or f"exit_code={result.exit_code}",
            exit_code=result.exit_code,
            duration_seconds=time.monotonic() - started,
        )


def _apply_candidate(source: Path, candidate: Path, target: Path) -> None:
    """按字节差异覆盖干净副本，并拒绝候选中的符号链接与特殊文件。"""
    source_files = _safe_files(source)
    candidate_files = _safe_files(candidate)
    for relative in sorted(set(source_files) - set(candidate_files)):
        (target / relative).unlink(missing_ok=True)
    for relative, candidate_path in candidate_files.items():
        source_path = source_files.get(relative)
        if source_path is not None and source_path.read_bytes() == candidate_path.read_bytes():
            continue
        destination = target / relative
        # 干净源中同名符号链接也不能成为控制器写入的跳板。
        if destination.is_symlink():
            destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(candidate_path.read_bytes())


def _safe_files(root: Path) -> dict[Path, Path]:
    """收集普通文件，并拒绝可能逃逸或改变复制语义的符号链接。"""
    files: dict[Path, Path] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("candidate and source may not contain symlinks")
        if path.is_file():
            files[relative] = path
    return files
