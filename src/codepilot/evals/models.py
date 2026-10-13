"""定义 M06 评测任务、验证结果和尝试记录的数据契约。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.contracts.run import RunLimits, RunReport

EvalStatus = Literal["success", "unresolved", "timeout", "unevaluable", "infrastructure_error"]
VerificationStatus = Literal["passed", "failed", "error", "timeout"]


class TaskSpec(Contract):
    """固定一次评测任务的输入、预算和最大纠错次数。"""

    task_id: Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
    repository: str
    repository_commit: str = "working-tree"
    description: str
    environment: dict[str, str] = {}
    development_command: tuple[str, ...] = ()
    development_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    limits: RunLimits = RunLimits()
    max_attempts: Annotated[int, Field(ge=1, le=8)] = 2


class VerificationResult(Contract):
    """保存独立验证器的结果，区分代码失败、验证器失败和超时。"""

    status: VerificationStatus
    passed: bool
    detail: str
    exit_code: int | None = None
    duration_seconds: float = 0.0


class AttemptRecord(Contract):
    """记录一次 Agent 尝试及其之后的独立验证结果。"""

    attempt: Annotated[int, Field(ge=1)]
    report: RunReport
    verification: VerificationResult
    feedback: str


class TrialResult(Contract):
    """汇总一次从干净工作区开始的任务试验和最终分类。"""

    task_id: str
    status: EvalStatus
    attempts: tuple[AttemptRecord, ...]
    changed_files: tuple[str, ...]
    patch: str
    detail: str
