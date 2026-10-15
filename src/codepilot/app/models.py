"""定义产品入口使用的运行身份、资源归属和最终结果。"""

from pathlib import Path

from codepilot.contracts.models import Contract, Identifier
from codepilot.contracts.run import RunReport
from codepilot.permissions import PermissionAudit
from codepilot.workspace import BackendEvent


class RunIdentity(Contract):
    """把一次执行稳定关联到会话、任务和单次运行三个层级。"""

    session_id: Identifier
    task_id: Identifier
    run_id: Identifier


class ResourceOwnership(Contract):
    """记录本进程为一次运行创建、保留或清理的资源。"""

    workspace: Path
    state_directory: Path
    trace_path: Path | None = None
    backend_id: Identifier
    backend_closed: bool
    context_artifacts_cleaned: bool


class ProductRunResult(Contract):
    """汇总 Runtime 报告、权限审计、后端轨迹和资源清理事实。"""

    identity: RunIdentity
    report: RunReport
    resources: ResourceOwnership
    permission_audits: tuple[PermissionAudit, ...]
    backend_events: tuple[BackendEvent, ...]
