"""实现权限规则匹配和只读、审批、受控编辑预设。"""

from pathlib import PurePosixPath
from uuid import uuid4

from codepilot.contracts import ToolCall
from codepilot.permissions.models import (
    ApprovalRequest,
    Decision,
    PermissionPolicy,
    PermissionRule,
)
from codepilot.tools.base import ToolSpec


def _path_matches(prefix: str | None, call: ToolCall) -> bool:
    """按规范化的模型路径匹配规则，缺少路径时不放宽权限。"""
    if prefix is None:
        return True
    raw_path = call.arguments.get("path")
    if not isinstance(raw_path, str):
        return False
    candidate = PurePosixPath(raw_path)
    expected = PurePosixPath(prefix)
    return candidate == expected or expected in candidate.parents


def _rule_matches(rule: PermissionRule, spec: ToolSpec, call: ToolCall) -> bool:
    """判断一条规则是否同时命中工具、影响类型和路径条件。"""
    if rule.tool_names and spec.name not in rule.tool_names:
        return False
    if rule.effects and spec.effect not in rule.effects:
        return False
    return _path_matches(rule.path_prefix, call)


class PermissionEngine:
    """按拒绝优先、规则顺序和默认值计算一次调用的权限决策。"""

    def __init__(self, policy: PermissionPolicy) -> None:
        """保存不可变策略快照，后续审批不会改变既有请求内容。"""
        self.policy = policy

    def decide(
        self, spec: ToolSpec, call: ToolCall, *, container_available: bool = False
    ) -> tuple[Decision, str]:
        """返回决策及可写入审计记录的理由。"""
        # 请求容器模式但执行链没有真实容器后端时继续拒绝，不能只相信策略名称。
        if self.policy.container_mode and not container_available:
            return "deny", "container_backend_unavailable"
        matches = [rule for rule in self.policy.rules if _rule_matches(rule, spec, call)]
        # 任意匹配到的 deny 都优先于 allow 或 ask，避免规则顺序意外放权。
        for rule in matches:
            if rule.decision == "deny":
                return "deny", "matched_deny_rule"
        if matches:
            rule = matches[0]
            return rule.decision, f"matched_{rule.decision}_rule"
        return self.policy.default_decision, "policy_default"

    def request(self, spec: ToolSpec, call: ToolCall, reason: str) -> ApprovalRequest:
        """为 ask 决策创建绑定完整调用参数的审批请求。"""
        return ApprovalRequest(
            request_id=str(uuid4()), tool=spec, call=call.model_copy(deep=True), reason=reason
        )


def read_only_policy() -> PermissionPolicy:
    """创建只允许读取工具的最小预设。"""
    return PermissionPolicy(
        name="read-only",
        default_decision="deny",
        rules=(PermissionRule(decision="allow", effects=("read",)),),
    )


def ask_edits_policy() -> PermissionPolicy:
    """创建读取自动允许、写入和进程调用逐次审批的预设。"""
    return PermissionPolicy(
        name="ask-edits",
        default_decision="deny",
        rules=(
            PermissionRule(decision="ask", effects=("write", "process")),
            PermissionRule(decision="allow", effects=("read",)),
        ),
    )


def controlled_edit_policy() -> PermissionPolicy:
    """创建允许工作区编辑但仍审批 Shell 的受控编辑预设。"""
    return PermissionPolicy(
        name="controlled-edit",
        default_decision="deny",
        rules=(
            PermissionRule(decision="allow", effects=("read", "write")),
            PermissionRule(decision="ask", effects=("process",)),
        ),
    )
