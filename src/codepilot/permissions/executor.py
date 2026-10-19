"""将权限判定、CLI 审批、审计和下游工具执行器组合为执行入口。"""

import hashlib
import time
from typing import Protocol

from codepilot.contracts import ToolCall, ToolResult
from codepilot.contracts.authorization import EXECUTION_GUARD
from codepilot.observability.trace import emit
from codepilot.permissions.action import build_action
from codepilot.permissions.approval import ApprovalProvider
from codepilot.permissions.models import ApprovalRequest, AuditOutcome, Decision, PermissionAudit
from codepilot.permissions.policy import PermissionEngine
from codepilot.tools.base import ToolSpec
from codepilot.workspace import find_backend


class CatalogExecutor(Protocol):
    """约束权限层需要的工具声明和执行能力。"""

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """返回权限判定所需的工具声明。"""
        ...

    async def execute(self, call: ToolCall) -> ToolResult:
        """执行已经通过权限层的单次工具调用。"""
        ...


class PermissionedExecutor:
    """在实际 ToolRegistry 前强制执行权限判定，拒绝路径不产生工具副作用。"""

    def __init__(
        self,
        registry: CatalogExecutor,
        engine: PermissionEngine,
        approval: ApprovalProvider | None = None,
    ) -> None:
        """绑定注册表、策略引擎和可选审批提供者。"""
        self.registry = registry
        self.engine = engine
        self.approval = approval
        self.audits: list[PermissionAudit] = []

    @property
    def supports_parallel(self) -> bool:
        """仅在下游调度器明确保证资源安全时允许 Runner 并发。"""
        return bool(getattr(self.registry, "supports_parallel", False))

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """转发唯一工具目录，使权限层可以继续被 Context 等外层包装。"""
        return self.registry.specs

    async def execute(self, call: ToolCall) -> ToolResult:
        """先判定并按需审批，只有允许路径才委托给真实工具注册表。"""
        spec = self._spec_for(call.name)
        if spec is None:
            # 注册表负责 unknown_tool 语义；权限层不应为未知工具创建审批。
            return await self.registry.execute(call)
        backend = find_backend(self.registry)
        decision, reason = self.engine.decide(
            spec,
            call,
            container_available=backend is not None and backend.is_container,
        )
        request = self.engine.request(spec, call, reason, backend=backend)
        if decision == "deny":
            self._audit(request, decision, "denied", reason)
            return self._error(call, f"permission_denied: {reason}")
        if decision == "ask":
            if self.approval is None:
                self._audit(request, decision, "cancelled", "approval_provider_missing")
                return self._error(call, "permission_cancelled: approval provider unavailable")
            emit("approval_requested", call_id=call.call_id)
            try:
                approved = await self.approval.approve(request)
            except BaseException:
                # 取消/输入错误也要结束界面的审批状态，不能让下一轮结果一直被缓冲。
                emit("approval_resolved", call_id=call.call_id, outcome="cancelled")
                raise
            emit(
                "approval_resolved",
                call_id=call.call_id,
                outcome="approved" if approved else "cancelled",
            )
            if not approved:
                self._audit(request, decision, "cancelled", "approval_cancelled")
                return self._error(call, "permission_cancelled: approval declined")
            self._audit(request, decision, "approved", reason)
        else:
            self._audit(request, decision, "allowed", reason)

        def validate() -> ToolResult | None:
            """在审批返回及真正获得执行槽后复核同一次授权，排队不能延长批准寿命。"""
            if (
                time.monotonic() - request.issued_at > self.engine.policy.approval_ttl_seconds
                or hashlib.sha256(self.engine.policy.model_dump_json().encode()).hexdigest()
                != request.policy_digest
            ):
                self._audit(request, "deny", "denied", "approval_expired_or_policy_changed")
                return self._error(call, "permission_denied: approval expired or policy changed")
            current_spec = self._spec_for(call.name)
            try:
                current_backend = find_backend(self.registry)
                fingerprint = (
                    build_action(current_spec, call, current_backend).fingerprint
                    if current_spec is not None
                    else None
                )
            except (OSError, ValueError):
                # 根目录被移走或元数据无法读取时失效授权，不能凭旧身份继续。
                fingerprint = None
            if current_spec is None or fingerprint != request.action.fingerprint:
                self._audit(request, "deny", "denied", "action_changed_after_approval")
                return self._error(call, "permission_denied: action changed after approval")
            return None

        denied = validate()
        if denied is not None:
            return denied
        token = EXECUTION_GUARD.set(validate)
        try:
            return await self.registry.execute(call)
        finally:
            # ContextVar 按协程隔离并在退出恢复，不能把父调用批准传给下一次工具。
            EXECUTION_GUARD.reset(token)

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
