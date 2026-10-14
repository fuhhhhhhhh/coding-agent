"""实现有并发上限、资源冲突判断和可取消排队的工具执行器。"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from codepilot.contracts import ToolCall, ToolResult
from codepilot.observability.trace import emit
from codepilot.scheduler.models import ExecutionPlan
from codepilot.tools.base import ToolSpec


class SchedulableExecutor(Protocol):
    """声明调度器下游所需的工具目录和执行能力。"""

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """返回资源规划所需的工具声明。"""
        ...

    async def execute(self, call: ToolCall) -> ToolResult:
        """执行已获得调度资源的一次工具调用。"""
        ...


@dataclass(frozen=True)
class _Waiter:
    """把排队顺序和资源计划绑定，取消时可精确移除。"""

    ticket: int
    plan: ExecutionPlan


class ScheduledExecutor:
    """允许无冲突调用并发，并让进程或未知资源调用独占执行。"""

    supports_parallel = True

    def __init__(self, registry: SchedulableExecutor, max_concurrency: int = 4) -> None:
        """绑定工具注册表并设置整个执行器的并发硬上限。"""
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.registry = registry
        self.max_concurrency = max_concurrency
        self._condition = asyncio.Condition()
        self._waiting: list[_Waiter] = []
        self._active: list[ExecutionPlan] = []
        self._next_ticket = 1

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """公开底层工具声明，使权限层仍可在调度前完成判定。"""
        return self.registry.specs

    async def execute(self, call: ToolCall) -> ToolResult:
        """等待并获取资源槽，执行完成或取消后始终释放。"""
        plan = self.plan(call)
        queued_at = time.monotonic()
        waiter = await self._enqueue(plan)
        try:
            await self._acquire(waiter, queued_at)
        except asyncio.CancelledError:
            # 排队或锁等待取消必须先移除等待项，后续释放不能再次启动它。
            await self._cancel_waiter(waiter)
            emit(
                "tool_queue_cancelled",
                call_id=call.call_id,
                resources=plan.resources,
                outcome=plan.mode,
                duration_seconds=time.monotonic() - queued_at,
            )
            raise
        try:
            return await self.registry.execute(call)
        finally:
            # 正常、错误、超时和运行中取消都通过同一出口唤醒候选调用。
            await self._release(plan)

    def plan(self, call: ToolCall) -> ExecutionPlan:
        """根据工具声明和实际路径参数生成保守的动态资源计划。"""
        spec = next((item for item in self.specs if item.name == call.name), None)
        if spec is None or spec.effect == "process":
            return ExecutionPlan(call_id=call.call_id, mode="exclusive", resources=("workspace",))
        path = _safe_path(call.arguments.get("path"))
        if path is not None:
            return ExecutionPlan(
                call_id=call.call_id,
                mode=spec.effect,
                resources=(f"path:{path}",),
            )
        resources = _declared_resources(spec)
        # 缺少精确路径的写入无法证明独立，必须独占而不是乐观并发。
        if spec.effect == "write" or not resources:
            return ExecutionPlan(call_id=call.call_id, mode="exclusive", resources=("workspace",))
        return ExecutionPlan(call_id=call.call_id, mode="read", resources=resources)

    async def _enqueue(self, plan: ExecutionPlan) -> _Waiter:
        """按到达顺序登记等待项并发布不含参数正文的排队事件。"""
        async with self._condition:
            waiter = _Waiter(self._next_ticket, plan)
            self._next_ticket += 1
            self._waiting.append(waiter)
        emit(
            "tool_queued",
            call_id=plan.call_id,
            resources=plan.resources,
            outcome=plan.mode,
        )
        return waiter

    async def _acquire(self, waiter: _Waiter, queued_at: float) -> None:
        """等待容量和资源均可用，并阻止同资源后来者越过。"""
        async with self._condition:
            await self._condition.wait_for(lambda: self._can_start(waiter))
            self._waiting.remove(waiter)
            self._active.append(waiter.plan)
        emit(
            "tool_scheduled",
            call_id=waiter.plan.call_id,
            resources=waiter.plan.resources,
            outcome=waiter.plan.mode,
            duration_seconds=time.monotonic() - queued_at,
        )

    def _can_start(self, waiter: _Waiter) -> bool:
        """检查全局容量、活动冲突和同资源先到等待项。"""
        if len(self._active) >= self.max_concurrency:
            return False
        if any(_conflicts(waiter.plan, active) for active in self._active):
            return False
        return not any(
            earlier.ticket < waiter.ticket and _conflicts(waiter.plan, earlier.plan)
            for earlier in self._waiting
        )

    async def _cancel_waiter(self, waiter: _Waiter) -> None:
        """仅在仍排队时移除等待项，并唤醒可能被顺序约束的后来者。"""
        async with self._condition:
            if waiter in self._waiting:
                self._waiting.remove(waiter)
                self._condition.notify_all()

    async def _release(self, plan: ExecutionPlan) -> None:
        """释放活动资源，并重新计算所有等待调用。"""
        async with self._condition:
            self._active.remove(plan)
            self._condition.notify_all()


def _safe_path(raw: object) -> str | None:
    """只为可解释的工作区相对路径生成动态资源键。"""
    if not isinstance(raw, str) or not raw:
        return None
    path = PurePosixPath(raw)
    # 非法路径仍由工具报告参数错误；调度层对它采用独占策略。
    if path.is_absolute() or ".." in path.parts:
        return None
    normalized = PurePosixPath(*(part for part in path.parts if part != "."))
    return normalized.as_posix() if normalized.parts else None


def _declared_resources(spec: ToolSpec) -> tuple[str, ...]:
    """去掉声明末尾的访问属性，使读写声明能映射到同一资源域。"""
    resources = []
    for resource in spec.resources:
        prefix, separator, suffix = resource.rpartition(":")
        resources.append(
            prefix if separator and suffix in {"read", "write", "process"} else resource
        )
    return tuple(dict.fromkeys(resources))


def _conflicts(left: ExecutionPlan, right: ExecutionPlan) -> bool:
    """判断两个计划能否同时活动；纯读取之间允许共享。"""
    if "exclusive" in {left.mode, right.mode}:
        return True
    if left.mode == right.mode == "read":
        return False
    if "workspace" in left.resources or "workspace" in right.resources:
        return True
    return bool(set(left.resources) & set(right.resources))
