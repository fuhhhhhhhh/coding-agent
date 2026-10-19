"""扩展共享实际调用额度与权限入口，Hook 不构成第二套执行循环。"""

from typing import Any
from uuid import uuid4

from codepilot.contracts import ToolCall, ToolResult
from codepilot.extensions.router import ExtensionRouter
from codepilot.permissions.executor import PermissionedExecutor
from codepilot.tools.base import ToolSpec


class OperationBudget:
    """主工具、Hook 与子任务读操作共享一次运行的尝试上限。"""

    def __init__(self, maximum: int) -> None:
        """只创建计数器，主循环仍拥有时间和主工具调用限制。"""
        self.maximum, self.used = maximum, 0

    def consume(self) -> bool:
        """在实际授权/执行前同步预扣，防止并发扩展绕过额度。"""
        if self.used >= self.maximum:
            return False
        self.used += 1
        return True


class ManagedExecutor:
    """Hook 在原权限入口外组织，实际进程仍在权限与资源调度之后执行。"""

    supports_parallel = True

    def __init__(
        self, registry: PermissionedExecutor, router: ExtensionRouter, budget: OperationBudget
    ) -> None:
        """绑定不可变 Hook 配置和共享调用额度，不向模型暴露内部 Hook 工具。"""
        self.registry, self.router, self.budget = registry, router, budget

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """隐藏内部生命周期执行器；模型不能直接伪造 Hook 控制事件。"""
        return tuple(spec for spec in self.registry.specs if spec.name != "run_hook")

    @property
    def audits(self) -> list[Any]:
        """审计由原 PermissionedExecutor 持有，扩展不建立平行审计源。"""
        return self.registry.audits

    async def execute(self, call: ToolCall) -> ToolResult:
        """未知/未公开工具先拒绝；配置 Hook 只包裹普通工作区工具。"""
        if call.name not in {spec.name for spec in self.specs}:
            return ToolResult(call_id=call.call_id, content="unknown tool", is_error=True)
        if self.router.manager.cleanup_failed:
            return ToolResult(
                call_id=call.call_id, content="cleanup failed; stop and reconcile", is_error=True
            )
        if not self.budget.consume():
            return ToolResult(
                call_id=call.call_id, content="shared operation budget exhausted", is_error=True
            )
        hooked = self.router.config.enabled and call.name in {
            item.name for item in self.router.registry.specs
        }
        if hooked:
            blocked = await self._hooks("before_tool", call)
            if blocked is not None:
                return blocked
        result = await self.registry.execute(call)
        if hooked:
            blocked = await self._hooks("after_tool", call)
            if blocked is not None:
                return ToolResult(
                    call_id=call.call_id,
                    is_error=True,
                    content=(
                        "Original tool already finished; do not blindly replay. "
                        f"original_error={result.is_error}; hook_error={blocked.content}"
                    ),
                )
        return result

    async def _hooks(self, event: str, call: ToolCall) -> ToolResult | None:
        """固定 argv 的 Hook 单独授权、计数并落事实，禁止递归触发 Hook。"""
        for hook in self.router.config.hooks:
            if hook.event != event:
                continue
            if not self.budget.consume():
                return ToolResult(
                    call_id=call.call_id, content="shared hook budget exhausted", is_error=True
                )
            internal = ToolCall(
                call_id=f"hook-{uuid4().hex}",
                name="run_hook",
                arguments={
                    "name": hook.name,
                    "event": event,
                    "call_id": call.call_id,
                    "tool": call.name,
                },
            )
            stored = self.router.manager.session
            calls = stored.get("calls", {})
            calls[internal.call_id] = "planned"
            stored.commit("hook_intent", {"call": internal.model_dump(mode="json")}, calls=calls)
            result = await self.registry.execute(internal)
            calls = stored.get("calls", {})
            calls[internal.call_id] = "failed" if result.is_error else "succeeded"
            stored.commit("hook_result", {"result": result.model_dump(mode="json")}, calls=calls)
            if result.is_error and hook.failure == "block":
                return ToolResult(
                    call_id=call.call_id, content=f"hook blocked: {hook.name}", is_error=True
                )
        return None


class ReadOnlyExecutor:
    """子任务只看到固定内置读能力，任何伪造 Shell/写入/MCP/递归调用都拒绝。"""

    supports_parallel = True

    def __init__(self, parent: ManagedExecutor) -> None:
        """共用父授权与调度器，但不运行需要 process 权限的 Hook。"""
        self.parent, self.registry = parent, parent.registry
        self.allowed = {
            "read_file",
            "glob",
            "search",
            "read_output",
            "search_output",
            "read_context_artifact",
            "load_skill",
            "load_rules",
        }

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """取父工具目录与只读集合交集，未来扩展不会自动进入子权限。"""
        return tuple(
            spec
            for spec in self.parent.specs
            if spec.name in self.allowed and spec.effect == "read"
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        """即使模型没有看到危险工具 Schema，执行入口仍再次强制拒绝。"""
        if call.name not in {spec.name for spec in self.specs} or not self.parent.budget.consume():
            return ToolResult(
                call_id=call.call_id, content="child permission or budget denied", is_error=True
            )
        return await self.registry.execute(call)
