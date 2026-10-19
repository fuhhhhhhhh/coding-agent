"""用 OpenAI SDK 适配采用 Chat Completions 协议的兼容网关。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from codepilot.contracts import Message
from codepilot.contracts.stream import StreamChunk, Usage
from codepilot.observability.trace import emit
from codepilot.providers.budget import BudgetLedger
from codepilot.providers.reliability import RetryPolicy, error_category, retry_delay
from codepilot.providers.stream import OutputLimitError, StreamContractError
from codepilot.tools.base import ToolSpec

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam


class RequestBudget:
    """跨 Provider 实例共享请求上限，失败请求同样计入额度。"""

    def __init__(
        self,
        max_requests: int = 3,
        *,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        """创建有限请求额度，防止配置错误或循环持续产生费用。"""
        if max_requests < 1:
            raise ValueError("request budget must be positive")
        self.max_requests = max_requests
        self.used = 0
        self.ledger = BudgetLedger(
            max_requests=max_requests,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
        )
        self.on_reserve: Callable[[int, int, str], str] | None = None
        self.on_settle: Callable[[str, Usage], None] | None = None
        self._session_ids: dict[str, str] = {}

    def consume(self, input_tokens: int = 0, output_tokens: int = 0) -> str:
        """在网络请求前消耗一次额度，额度耗尽时不接触远端。"""
        identity = self.ledger.reserve_request(input_tokens, output_tokens)
        self.used = self.ledger.snapshot.requests
        if self.on_reserve is not None:
            self._session_ids[identity] = self.on_reserve(input_tokens, output_tokens, identity)
        return identity

    def settle(self, usage: Usage, reservation_id: str) -> None:
        """把 Provider usage 结算到同一账本，缺失字段保持保守占用。"""
        # 先保存服务端已发生事实，再更新内存；持久化失败时不能继续发送请求。
        if self.on_settle is not None:
            self.on_settle(self._session_ids[reservation_id], usage)
        self.ledger.settle(usage.input_tokens, usage.output_tokens, reservation_id=reservation_id)
        emit("provider_attempt", attempt_id=reservation_id, outcome="settled", usage=usage)


class OpenAICompatibleProvider:
    """把兼容 Chat Completions 的流转换为内部严格分片契约。"""

    def __init__(
        self,
        client: AsyncOpenAI,
        tools: Sequence[ToolSpec] = (),
        *,
        model: str,
        budget: RequestBudget | None = None,
        max_tokens: int = 256,
        extra_body: Mapping[str, object] | None = None,
        retry: RetryPolicy | None = None,
        context_tokens: int | None = None,
    ) -> None:
        """绑定客户端、工具目录和有界输出，不在构造阶段发起网络请求。"""
        if not model.strip():
            raise ValueError("model must not be empty")
        if not 1 <= max_tokens <= 65_536:
            raise ValueError("max_tokens must be between 1 and 65536")
        # SDK 自动重试会绕过显式请求额度，因此在适配边界统一关闭。
        self.client = client.with_options(max_retries=0)
        self.tools = tuple(tool.model_copy(deep=True) for tool in tools)
        self.model = model
        self.budget = budget if budget is not None else RequestBudget()
        self.max_tokens = max_tokens
        self.extra_body = dict(extra_body) if extra_body is not None else None
        self.retry = retry or RetryPolicy()
        self.context_tokens = context_tokens

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamChunk]:
        """发起一次有限流式请求，并在完整结束前不产生可执行工具调用。"""
        # SDK 只在实际请求时加载，打开提示符和离线工具不承担联网依赖的导入成本。
        from openai import omit

        payload = _messages(messages)
        tools = cast(
            "list[ChatCompletionToolParam]",
            [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in self.tools
            ],
        )

        async def create() -> Any:
            """在协议边界构造真实 SDK 请求，重试共用唯一入口。"""
            return await self.client.chat.completions.create(
                model=self.model,
                messages=payload,
                tools=tools or omit,
                max_tokens=self.max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=self.extra_body,
            )

        response, reservation = await self.open_stream(
            create, {"messages": payload, "tools": tools}
        )
        finish: str | None = None
        usage_seen = False
        async with response:
            async for chunk in response:
                # 兼容独立 usage 尾片段和附着在 finish 片段的两种实现。
                if chunk.usage is not None:
                    if usage_seen:
                        raise StreamContractError("duplicate provider usage")
                    usage_seen = True
                    usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                        source="provider",
                    )
                    self.budget.settle(usage, reservation)
                    yield StreamChunk(
                        kind="usage_updated",
                        usage=usage,
                    )
                for choice in chunk.choices:
                    if choice.index != 0 or finish is not None:
                        raise StreamContractError("unexpected choice or data after finish")
                    delta = choice.delta
                    if delta.content:
                        yield StreamChunk(kind="text_delta", text=delta.content)
                    for call in delta.tool_calls or ():
                        function = call.function
                        call_id = call.id or ""
                        name = function.name or "" if function else ""
                        arguments = function.arguments or "" if function else ""
                        # 某些兼容网关会发送只有 index 的占位项；它不推进组装，应直接忽略。
                        if not (call_id or name or arguments):
                            continue
                        # 少数兼容网关在单工具续片段省略 index；单调用只能归入第零组。
                        # 若网关随后发送不同 ID，collect_reply 仍会按冲突契约拒绝。
                        call_index = call.index if call.index is not None else 0
                        yield StreamChunk(
                            kind="tool_call_delta",
                            index=call_index,
                            call_id=call_id,
                            name=name,
                            arguments_delta=arguments,
                        )
                    if choice.finish_reason is not None:
                        finish = choice.finish_reason
        # 输出额度耗尽时参数可能只有半段，必须分类停止且绝不能执行工具。
        if finish == "length":
            raise OutputLimitError("provider output reached max_tokens")
        # 其他供应商专用结束原因同样不能被误报为完整回答。
        if finish not in {"stop", "tool_calls"}:
            raise StreamContractError("incomplete provider finish")
        yield StreamChunk(
            kind="turn_finished", finish_reason=cast(Literal["stop", "tool_calls"], finish)
        )

    async def open_stream(
        self, create: Callable[[], Awaitable[Any]], payload: object
    ) -> tuple[Any, str]:
        """两个协议共用请求预估、持久预留和明确拒绝后的退避；不重试已开始的流。"""
        serialized = json.dumps(payload, ensure_ascii=False)
        ascii_count = sum(char.isascii() for char in serialized)
        estimated_input = (ascii_count + 2) // 3 + len(serialized) - ascii_count + 256
        if (
            self.context_tokens is not None
            and estimated_input + self.max_tokens > self.context_tokens
        ):
            raise ValueError("request exceeds configured context including tools and output")
        attempt = 0
        while True:
            attempt += 1
            reservation = self.budget.consume(estimated_input, self.max_tokens)
            emit("provider_attempt", attempt_id=reservation, outcome="started", call_index=attempt)
            try:
                return await create(), reservation
            except Exception as error:
                delay = retry_delay(error, attempt, self.retry)
                category = error_category(error)
                emit(
                    "provider_attempt",
                    outcome=category,
                    call_index=attempt,
                    error_type=type(error).__name__,
                    attempt_id=reservation,
                )
                # 明确拒绝才释放预留；服务端是否接受未知的错误保留 pending。
                if getattr(error, "status_code", None) == 429 or delay is not None:
                    self.budget.settle(
                        Usage(input_tokens=0, output_tokens=0, source="provider"), reservation
                    )
                if delay is None:
                    raise
                emit(
                    "provider_retry",
                    attempt_id=reservation,
                    outcome=category,
                    call_index=attempt,
                    duration_seconds=delay,
                )
                await asyncio.sleep(delay)


def messages_to_openai(messages: Sequence[Message]) -> list[ChatCompletionMessageParam]:
    """转换内部四类消息，并保留工具调用和结果之间的 ID 关联。"""
    payload: list[dict[str, object]] = []
    for message in messages:
        if message.role == "tool":
            result = message.tool_result
            if result is None:
                raise ValueError("missing tool result")
            payload.append(
                {"role": "tool", "tool_call_id": result.call_id, "content": result.content}
            )
            continue
        item: dict[str, object] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        payload.append(item)
    return cast("list[ChatCompletionMessageParam]", payload)


# 保留模块内短别名，使协议转换在流式热路径中仍容易辨认。
_messages = messages_to_openai
