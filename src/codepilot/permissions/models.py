"""定义权限决策、规则、审批请求和审计记录的内部契约。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts import ToolCall
from codepilot.contracts.models import Contract, Identifier
from codepilot.tools.base import ToolSpec

Decision = Literal["allow", "deny", "ask"]
AuditOutcome = Literal["allowed", "denied", "approved", "cancelled"]


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


class ApprovalRequest(Contract):
    """冻结一次待审批调用的完整工具声明和参数。"""

    request_id: Identifier
    tool: ToolSpec
    call: ToolCall
    reason: str


class PermissionAudit(Contract):
    """记录一次权限判定或审批结果，不保存模型密钥。"""

    sequence: Annotated[int, Field(ge=1)]
    request_id: Identifier
    call_id: Identifier
    tool_name: Identifier
    decision: Decision
    outcome: AuditOutcome
    reason: str
