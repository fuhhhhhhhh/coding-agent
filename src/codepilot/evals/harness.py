"""实现从干净工作区启动 Agent、验证和反馈纠错的本地 Harness。"""

import difflib
import shutil
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, cast

from codepilot.contracts import Message
from codepilot.evals.models import AttemptRecord, TaskSpec, TrialResult
from codepilot.evals.verifier import IndependentVerifier
from codepilot.providers.base import Provider
from codepilot.providers.stream import StreamingProvider
from codepilot.runtime import Runner, ToolExecutor

ProviderFactory = Callable[[TaskSpec, int, str], Provider | StreamingProvider]


class LocalHarness:
    """在临时副本中运行任务，并由工作区外验证器决定成功与否。"""

    def __init__(
        self,
        verifier: IndependentVerifier,
        executor_factory: Callable[[Path], ToolExecutor],
    ) -> None:
        """保存验证器和每次试验的工具执行器工厂。"""
        self.verifier = verifier
        self.executor_factory = executor_factory

    async def run(self, task: TaskSpec, provider_factory: ProviderFactory) -> TrialResult:
        """从干净目录开始运行一个任务，并循环传递验证反馈。"""
        source = Path(task.repository).resolve(strict=True)
        if not source.is_dir():
            raise ValueError("task repository must be a directory")
        with tempfile.TemporaryDirectory(prefix=f"codepilot-eval-{task.task_id}-") as directory:
            workspace = Path(directory) / "workspace"
            shutil.copytree(source, workspace)
            before = _snapshot(workspace)
            feedback = ""
            records: list[AttemptRecord] = []
            for attempt in range(1, task.max_attempts + 1):
                provider = provider_factory(task, attempt, feedback)
                report = await Runner(
                    provider,
                    self.executor_factory(workspace),
                    task.limits,
                ).run((Message(role="user", content=task.description),))
                development = await self._development_feedback(task, workspace)
                verification = await self.verifier.verify(workspace)
                feedback = "\n".join(item for item in (development, verification.detail) if item)[
                    -4000:
                ]
                records.append(
                    AttemptRecord(
                        attempt=attempt,
                        report=report,
                        verification=verification,
                        feedback=feedback,
                    )
                )
                # 模型提前结束不代表任务通过，必须先看独立验证器。
                if verification.passed:
                    return TrialResult(
                        task_id=task.task_id,
                        status="success",
                        attempts=tuple(records),
                        changed_files=tuple(_changed_files(before, _snapshot(workspace))),
                        patch=_patch(before, _snapshot(workspace)),
                        detail="independent verifier passed",
                    )
                # 验证器不可评测或运行超时，不应伪装成可继续修复的代码反馈。
                if verification.status == "error":
                    status = "unevaluable"
                    detail = "independent verifier error"
                    return self._result(task, records, before, workspace, status, detail)
                if verification.status == "timeout":
                    return self._result(
                        task, records, before, workspace, "timeout", "verifier timeout"
                    )
                # Runner 已经因为资源或 Provider 故障停止，继续请求只会掩盖根因。
                if report.reason in {"time_limit", "cancelled"}:
                    return self._result(task, records, before, workspace, "timeout", report.reason)
                if report.reason in {"provider_error", "contract_error"}:
                    return self._result(
                        task, records, before, workspace, "infrastructure_error", report.reason
                    )
            return self._result(
                task, records, before, workspace, "unresolved", "attempt budget exhausted"
            )

    async def run_batch(
        self, tasks: Sequence[TaskSpec], provider_factory: ProviderFactory
    ) -> tuple[TrialResult, ...]:
        """逐个从干净工作区运行任务，避免批量结果共享文件状态。"""
        results: list[TrialResult] = []
        for task in tasks:
            results.append(await self.run(task, provider_factory))
        return tuple(results)

    async def _development_feedback(self, task: TaskSpec, workspace: Path) -> str:
        """运行可被 Agent 修改的开发测试，并只把结果作为下一次反馈。"""
        if not task.development_command:
            return ""
        result = await IndependentVerifier(
            task.development_command, task.development_timeout_seconds
        ).verify(workspace)
        return f"development_tests: {result.status}\n{result.detail}"

    def _result(
        self,
        task: TaskSpec,
        records: Sequence[AttemptRecord],
        before: dict[str, str],
        workspace: Path,
        status: str,
        detail: str,
    ) -> TrialResult:
        """统一构造失败分类，确保补丁和变更文件仍然可审查。"""
        return TrialResult(
            task_id=task.task_id,
            status=cast(
                Literal["success", "unresolved", "timeout", "unevaluable", "infrastructure_error"],
                status,
            ),
            attempts=tuple(records),
            changed_files=tuple(_changed_files(before, _snapshot(workspace))),
            patch=_patch(before, _snapshot(workspace)),
            detail=detail,
        )


def _snapshot(root: Path) -> dict[str, str]:
    """读取临时工作区中的文本文件快照，供补丁和空补丁判断使用。"""
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """返回新增、删除或内容变化的相对路径。"""
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def _patch(before: dict[str, str], after: dict[str, str]) -> str:
    """生成可观察的文本补丁；二进制内容已在快照层降级为替换文本。"""
    lines: list[str] = []
    for path in _changed_files(before, after):
        lines.extend(
            difflib.unified_diff(
                before.get(path, "").splitlines(keepends=True),
                after.get(path, "").splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    return "".join(lines)
