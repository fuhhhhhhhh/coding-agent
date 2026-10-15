"""M02 循环：模型 → 调用 → 结果 → 模型，并提供明确的停止机制。"""

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Literal, Protocol
from uuid import uuid4

from codepilot.context import ContextManager, ContextPreparationError
from codepilot.contracts import Message, RunState, ToolCall, ToolResult
from codepilot.contracts.run import RunLimits, RunObservation, RunReport, StopReason
from codepilot.contracts.stream import StreamChunk, Usage
from codepilot.observability.trace import CURRENT_RECORDER, CURRENT_TURN, EventRecorder, emit
from codepilot.providers.base import Provider
from codepilot.providers.stream import (
    OutputLimitError,
    StreamContractError,
    StreamingProvider,
    collect_reply,
)

LiteralObservation = Literal[
    "model_requested", "model_replied", "tool_requested", "tool_result", "run_stopped"
]


class ToolExecutor(Protocol):
    """约束 Runtime 调用工具时依赖的唯一执行接口。"""

    async def execute(self, call: ToolCall) -> ToolResult:
        """在副作用前校验参数，并用相同调用 ID 返回错误。"""
        ...


class _Stop(Exception):
    """在循环内部携带预期停止原因，不代表未处理的程序错误。"""

    def __init__(self, reason: StopReason, detail: str = "") -> None:
        """将预期的 Runtime 停止原因从内部辅助函数传递给 ``Runner.run``。"""
        self.reason = reason
        self.detail = detail


class Runner:
    """一个实例只管理一次运行；取消会向上传播，但报告仍可检查。"""

    def __init__(
        self,
        provider: Provider | StreamingProvider,
        executor: ToolExecutor,
        limits: RunLimits | None = None,
        *,
        run_id: str | None = None,
        recorder: EventRecorder | None = None,
        context: ContextManager | None = None,
    ) -> None:
        """为一次运行创建隔离状态，并绑定可选的 prompt 上下文治理器。"""
        self.provider = provider
        self.recorder = recorder if recorder is not None else EventRecorder()
        self.context = context
        self._turn_started = 0.0
        self._turn_open = False
        self._turn_usage = False
        self.executor = executor
        self.limits = limits if limits is not None else RunLimits()
        self.run_id = run_id if run_id is not None else str(uuid4())
        # 每条观察记录都会携带 ID，若接受纯空白值，轨迹将无法清楚识别。
        if not self.run_id.strip():
            raise ValueError("run_id must contain a non-whitespace character")
        self.state = RunState.CREATED
        self.report: RunReport | None = None
        self._messages: list[Message] = []
        self._observations: list[RunObservation] = []
        self._steps = 0
        self._tool_calls = 0
        self._call_ids: set[str] = set()
        self._failed_signature: str | None = None
        self._failure_count = 0
        self._deadline = 0.0

    def _observe(
        self,
        kind: LiteralObservation,
        *,
        message: Message | None = None,
        result: ToolResult | None = None,
        call: ToolCall | None = None,
        state: RunState | None = None,
        reason: StopReason | None = None,
        detail: str = "",
    ) -> None:
        """以运行内的下一个序号追加一条深拷贝观察记录。"""
        self._observations.append(
            RunObservation(
                run_id=self.run_id,
                sequence=len(self._observations) + 1,
                step=self._steps,
                kind=kind,
                message=message,
                result=result,
                call=call,
                state=state,
                reason=reason,
                detail=detail,
            ).model_copy(deep=True)
        )

    def _finish(self, reason: StopReason, detail: str = "") -> RunReport:
        """将停止原因映射为唯一终态，并冻结最终运行报告。"""
        # 只有模型正常结束才记为完成；取消必须与失败保持区分。
        self.state = (
            RunState.COMPLETED
            if reason == "completed"
            else RunState.CANCELLED
            if reason == "cancelled"
            else RunState.FAILED
        )
        self._finish_turn(reason)
        self.recorder.finish(reason, detail)
        self._observe("run_stopped", state=self.state, reason=reason, detail=detail)
        self.report = RunReport(
            run_id=self.run_id,
            state=self.state,
            reason=reason,
            steps=self._steps,
            tool_calls=self._tool_calls,
            messages=tuple(self._messages),
            observations=tuple(self._observations),
        ).model_copy(deep=True)
        return self.report

    async def _checkpoint(self) -> None:
        """主动让出执行权以接收取消，然后检查基于单调时钟的截止时间。"""
        # 即使 fake provider 或 fixture 操作内部没有 await，也要给取消信号执行机会。
        await asyncio.sleep(0)
        if asyncio.get_running_loop().time() >= self._deadline:
            raise _Stop("time_limit")

    async def run(self, messages: Sequence[Message]) -> RunReport:
        """从一组初始 system/user 历史运行，最终只产生一份终态报告。"""
        # 重用实例会混合消息历史、调用 ID、预算和终态观察记录。
        if self.state != RunState.CREATED:
            raise RuntimeError("Runner is single-use; create another Runner for another run")
        # M02 只启动全新运行；接受 assistant/tool 历史会暗示尚未实现的恢复能力。
        if not messages or any(message.role not in ("system", "user") for message in messages):
            raise ValueError("a new run requires nonempty system/user messages")
        # 重新校验可变嵌套数据，并与调用方持有的对象解除共享。
        self._messages = [Message.model_validate(message.model_dump()) for message in messages]
        self.state = RunState.RUNNING
        self._deadline = asyncio.get_running_loop().time() + self.limits.timeout_seconds
        self.recorder.start(self.run_id)
        recorder_token = CURRENT_RECORDER.set(self.recorder)
        turn_token = CURRENT_TURN.set("")
        try:
            async with asyncio.timeout_at(self._deadline):
                await self._loop()
        except asyncio.CancelledError:
            # 保留可诊断报告，同时继续把取消传播给调用方。
            self._finish("cancelled")
            raise
        except TimeoutError:
            # asyncio 会在超时上下文边界把内部取消转换为 TimeoutError。
            return self._finish("time_limit")
        except _Stop as stop:
            # 预期限制与契约失败共用一个集中的终态转换入口。
            return self._finish(stop.reason, stop.detail)
        else:
            return self._finish("completed")
        finally:
            CURRENT_TURN.reset(turn_token)
            CURRENT_RECORDER.reset(recorder_token)

    async def _loop(self) -> None:
        """交替执行 Provider 请求与顺序工具调用，直到满足停止条件。"""
        while True:
            await self._checkpoint()
            # 在递增前检查，使 ``steps`` 始终只统计真正尝试过的请求。
            if self._steps >= self.limits.max_steps:
                raise _Stop("step_limit")
            next_step = self._steps + 1
            CURRENT_TURN.set(f"{self.run_id}:{next_step}")
            self._turn_started = time.monotonic()
            self._turn_open = True
            self._turn_usage = False
            try:
                # ContextManager 只生成 Provider 视图，不修改报告持有的规范历史。
                history = (
                    (await self.context.prepare(self._messages)).messages
                    if self.context is not None
                    else tuple(message.model_copy(deep=True) for message in self._messages)
                )
            except ContextPreparationError as error:
                # 预算或摘要失败发生在网络请求前，因此不计入 Provider 步数。
                raise _Stop("context_limit", str(error)) from error
            self._steps = next_step
            emit("model_requested")
            self._observe("model_requested")
            try:
                if isinstance(self.provider, StreamingProvider):
                    reply, usage = await collect_reply(self.provider, history, self._chunk)
                else:
                    reply = await self.provider.complete(history)
                    usage = Usage()
                if not self._turn_usage:
                    emit("usage_updated", usage=usage)
                    self._turn_usage = True
            except OutputLimitError as error:
                # 截断的工具参数保持零执行，并与真正协议违规分开供用户调大额度。
                raise _Stop("output_limit", type(error).__name__) from error
            except StreamContractError as error:
                raise _Stop("contract_error", type(error).__name__) from error
            except Exception as error:
                # 不泄露 Provider 异常正文，其中可能包含凭据。
                raise _Stop("provider_error", type(error).__name__) from error
            await self._checkpoint()
            try:
                reply = Message.model_validate(reply.model_dump())
                # Message 内部校验通过，并不代表角色满足 Provider 边界约束。
                if reply.role != "assistant":
                    raise ValueError("provider must return an assistant message")
                # 调用 ID 在整个运行中保持唯一，确保每个结果只有一个明确来源。
                if any(call.call_id in self._call_ids for call in reply.tool_calls):
                    raise ValueError("provider reused a call ID from this run")
            except Exception as error:
                # 非法 Provider 数据中不存在可信工具请求，因此必须直接终止。
                raise _Stop("contract_error", type(error).__name__) from error
            self._messages.append(reply)
            self._observe("model_replied", message=reply)
            for ready_call in reply.tool_calls:
                emit("tool_call_ready", call_id=ready_call.call_id, call=ready_call)
            # 在 M02 中，不含工具调用的回复就是模型正常结束信号。
            if not reply.tool_calls:
                self._finish_turn("completed")
                return
            # 整批调用超出额度时，在执行第一个调用前整体拒绝。
            if self._tool_calls + len(reply.tool_calls) > self.limits.max_tool_calls:
                raise _Stop("tool_budget")
            self._call_ids.update(call.call_id for call in reply.tool_calls)
            await self._execute_calls(reply.tool_calls)
            self._finish_turn("tools_completed")

    async def _execute_calls(self, calls: Sequence[ToolCall]) -> None:
        """按执行器能力顺序或并发执行，并按模型调用顺序写回结果。"""
        if not getattr(self.executor, "supports_parallel", False) or len(calls) == 1:
            for call in calls:
                await self._checkpoint()
                self._tool_calls += 1
                self._observe("tool_requested", call=call)
                self._record_tool_result(call, await self._execute_call(call))
            return
        # 并发批次中的每个调用都会立即进入调度器，因此统一计为已尝试。
        for call in calls:
            self._tool_calls += 1
            self._observe("tool_requested", call=call)
        tasks = [asyncio.create_task(self._execute_call(call)) for call in calls]
        try:
            # gather 保持输入顺序，完成事件仍由各任务按真实结束时间发布。
            results = await asyncio.gather(*tasks)
        except (asyncio.CancelledError, Exception):
            # 任一契约故障或外部取消都要停止仍排队或运行中的同批调用。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for call, result in zip(calls, results, strict=True):
            self._record_tool_result(call, result)

    def _record_tool_result(self, call: ToolCall, result: ToolResult) -> None:
        """按模型调用顺序追加结果，并应用连续失败停止规则。"""
        self._messages.append(Message(role="tool", tool_result=result))
        self._observe("tool_result", result=result)
        self._track_failure(call, result)

    async def _execute_call(self, call: ToolCall) -> ToolResult:
        """执行一个调用，验证结果配对并发布真实完成顺序事件。"""
        tool_started = time.monotonic()
        try:
            result = await self.executor.execute(call.model_copy(deep=True))
        except asyncio.CancelledError:
            emit(
                "tool_finished",
                call_id=call.call_id,
                outcome="cancelled",
                duration_seconds=time.monotonic() - tool_started,
            )
            raise
        except Exception as error:
            # 普通异常转换为模型可见反馈；取消异常不会进入此分支。
            result = ToolResult(
                call_id=call.call_id,
                is_error=True,
                content=f"tool_exception: {type(error).__name__}",
            )
        try:
            result = ToolResult.model_validate(result.model_dump())
            # ID 不匹配可能把副作用或错误错误关联到另一个请求。
            if result.call_id != call.call_id:
                raise ValueError("executor returned a mismatched call ID")
        except Exception as error:
            # 无法配对的执行结果不能写入对话历史。
            emit(
                "tool_finished",
                call_id=call.call_id,
                outcome="contract_error",
                duration_seconds=time.monotonic() - tool_started,
            )
            raise _Stop("contract_error", type(error).__name__) from error
        emit(
            "tool_finished",
            call_id=call.call_id,
            outcome="error" if result.is_error else "success",
            duration_seconds=time.monotonic() - tool_started,
        )
        return result

    def _chunk(self, chunk: StreamChunk) -> None:
        """实时转发增量；结束事件要等完整校验和本回合工具结束后发布。"""
        if chunk.kind == "text_delta":
            emit("text_delta", text=chunk.text)
        elif chunk.kind == "usage_updated":
            emit("usage_updated", usage=chunk.usage)
            self._turn_usage = True
        elif chunk.kind == "tool_call_delta":
            emit(
                "tool_call_delta",
                call_id=chunk.call_id,
                call_index=chunk.index,
                text=chunk.arguments_delta,
            )

    def _finish_turn(self, outcome: str) -> None:
        """每个已开始回合只发布一次终态，包含失败和取消。"""
        if self._turn_open:
            if not self._turn_usage:
                emit("usage_updated", usage=Usage())
            emit(
                "turn_finished",
                outcome=outcome,
                duration_seconds=time.monotonic() - self._turn_started,
            )
            self._turn_open = False

    def _track_failure(self, call: ToolCall, result: ToolResult) -> None:
        """同名同参数连续失败时停止；变化的调用 ID 不影响判断。"""
        signature = json.dumps([call.name, call.arguments], sort_keys=True, ensure_ascii=False)
        # 任意一次成功都会打断连续失败计数。
        if not result.is_error:
            self._failed_signature = None
            self._failure_count = 0
            return
        self._failure_count = self._failure_count + 1 if signature == self._failed_signature else 1
        self._failed_signature = signature
        # 达到阈值后立即停止，不再启动同批次的下一个调用。
        if self._failure_count >= self.limits.max_consecutive_failures:
            raise _Stop("repeated_failure")
