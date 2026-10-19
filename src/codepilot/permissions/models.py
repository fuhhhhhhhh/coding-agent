"""定义权限决策、规则、审批请求和审计记录的内部契约。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts import ToolCall
from codepilot.contracts.models import Contract, Identifier
from codepilot.tools.base import ToolSpec

Decision = Literal["allow", "deny", "ask"]
AuditOutcome = Literal["allowed", "denied", "approved", "cancelled"]


class Action(Contract):
    """冻结一次工具调用的影响摘要，供审批展示、复核和审计关联。"""

    tool_name: Identifier
    effect: Literal["read", "write", "process"]
    call_id: Identifier
    arguments_digest: Annotated[str, Field(min_length=64, max_length=64)]
    argument_summary: str
    cwd: str
    backend: Literal["local", "container", "unknown"]
    backend_id: Identifier
    resources: tuple[str, ...]
    network: bool = False
    secrets: tuple[str, ...] = ()
    environment_names: tuple[str, ...] = ()
    workspace_identity: tuple[int, int] | None = None
    fingerprint: Annotated[str, Field(min_length=64, max_length=64)]


class PermissionRule(Contract):
    """声明匹配工具、影响类型和路径前缀后的权限结果。"""

    decision: Decision
    tool_names: tuple[str, ...] = ()
    effects: tuple[Literal["read", "write", "process"], ...] = ()
    path_prefix: str | None = None


class PermissionPolicy(Contract):
    """保存默认决策、优先规则和是否强制要求容器后端。"""

    name: Identifier
    default_decision: Decision = "deny"
    rules: tuple[PermissionRule, ...] = ()
    container_mode: bool = False
    safe_process_reads: bool = False
    approval_ttl_seconds: Annotated[float, Field(gt=0, le=3600)] = 300.0


class ApprovalRequest(Contract):
    """冻结一次待审批调用的完整工具声明和参数。"""

    request_id: Identifier
    tool: ToolSpec
    call: ToolCall
    action: Action
    reason: str
    issued_at: float = 0.0
    policy_digest: str = ""


class PermissionAudit(Contract):
    """记录一次权限判定或审批结果，不保存模型密钥。"""

    sequence: Annotated[int, Field(ge=1)]
    request_id: Identifier
    call_id: Identifier
    tool_name: Identifier
    decision: Decision
    outcome: AuditOutcome
    reason: str
