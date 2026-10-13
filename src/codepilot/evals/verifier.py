"""实现不受 Agent 工作区修改影响的本地独立验证器。"""

import asyncio
import time
from pathlib import Path

from codepilot.evals.models import VerificationResult


class IndependentVerifier:
    """用固定命令在 Agent 结束后独立验证工作区。"""

    def __init__(self, command: tuple[str, ...], timeout_seconds: float = 10.0) -> None:
        """保存不可由任务工作区修改的 argv 和验证时限。"""
        if not command:
            raise ValueError("verifier command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("verifier timeout must be positive")
        self.command = command
        self.timeout_seconds = timeout_seconds

    async def verify(self, workspace: Path) -> VerificationResult:
        """在工作区运行固定验证命令，并把进程故障转成明确状态。"""
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            # 验证器本身无法启动时不能把任务误判为代码未解决。
            return VerificationResult(
                status="error",
                passed=False,
                detail=f"verifier_start_error: {type(error).__name__}",
                duration_seconds=time.monotonic() - started,
            )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout_seconds)
        except TimeoutError:
            # 验证超时属于环境/验证状态，不能继续消耗纠错轮次。
            process.kill()
            await process.wait()
            return VerificationResult(
                status="timeout",
                passed=False,
                detail=f"verifier_timeout: {self.timeout_seconds:g}s",
                duration_seconds=time.monotonic() - started,
            )
        output = (stdout + stderr).decode("utf-8", errors="replace")
        return VerificationResult(
            status="passed" if process.returncode == 0 else "failed",
            passed=process.returncode == 0,
            detail=output[-4000:] or f"exit_code={process.returncode}",
            exit_code=process.returncode,
            duration_seconds=time.monotonic() - started,
        )
