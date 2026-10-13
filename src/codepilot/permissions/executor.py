"""将权限判定、CLI 审批、审计和工具注册表组合为执行入口。"""

from codepilot.contracts import ToolCall, ToolResult
from codepilot.permissions.approval import ApprovalProvider
from codepilot.permissions.models import ApprovalRequest, AuditOutcome, Decision, PermissionAudit
from codepilot.permissions.policy import PermissionEngine
from codepilot.tools.base import ToolSpec
from codepilot.tools.registry import ToolRegistry


class PermissionedExecutor:
    """在实际 ToolRegistry 前强制执行权限判定，拒绝路径不产生工具副作用。"""

    def __init__(
        self,
        registry: ToolRegistry,
        engine: PermissionEngine,
        approval: ApprovalProvider | None = None,
    ) -> None:
        """绑定注册表、策略引擎和可选审批提供者。"""
        self.registry = registry
        self.engine = engine
        self.approval = approval
        self.audits: list[PermissionAudit] = []

    async def execute(self, call: ToolCall) -> ToolResult:
        """先判定并按需审批，只有允许路径才委托给真实工具注册表。"""
        spec = self._spec_for(call.name)
        if spec is None:
            # 注册表负责 unknown_tool 语义；权限层不应为未知工具创建审批。
            return await self.registry.execute(call)
        decision, reason = self.engine.decide(spec, call)
        request = self.engine.request(spec, call, reason)
        if decision == "deny":
            self._audit(request, decision, "denied", reason)
            return self._error(call, f"permission_denied: {reason}")
        if decision == "ask":
            if self.approval is None:
                self._audit(request, decision, "cancelled", "approval_provider_missing")
                return self._error(call, "permission_cancelled: approval provider unavailable")
            approved = await self.approval.approve(request)
            if not approved:
                self._audit(request, decision, "cancelled", "approval_cancelled")
                return self._error(call, "permission_cancelled: approval declined")
            self._audit(request, decision, "approved", reason)
        else:
            self._audit(request, decision, "allowed", reason)
        return await self.registry.execute(call)

    def _spec_for(self, name: str) -> ToolSpec | None:
        """从注册表声明快照查找工具，避免复制注册表内部实现。"""
        return next((spec for spec in self.registry.specs if spec.name == name), None)

    def _audit(
        self,
        request: ApprovalRequest,
        decision: Decision,
        outcome: AuditOutcome,
        reason: str,
    ) -> None:
        """追加不含秘密正文的顺序审计记录。"""
        self.audits.append(
            PermissionAudit(
                sequence=len(self.audits) + 1,
                request_id=request.request_id,
                call_id=request.call.call_id,
                tool_name=request.tool.name,
                decision=decision,
                outcome=outcome,
                reason=reason,
            )
        )

    @staticmethod
    def _error(call: ToolCall, content: str) -> ToolResult:
        """构造保持原调用 ID 的权限错误结果。"""
        return ToolResult(call_id=call.call_id, content=content, is_error=True)
