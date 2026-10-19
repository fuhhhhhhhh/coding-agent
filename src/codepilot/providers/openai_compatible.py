"""用 OpenAI SDK 适配采用 Chat Completions 协议的兼容网关。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, cast

from codepilot.contracts import Message
from codepilot.contracts.stream import StreamChunk, Usage
from codepilot.providers.budget import BudgetLedger
from codepilot.providers.stream import OutputLimitError, StreamContractError
from codepilot.tools.base import ToolSpec

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam


class RequestBudget:
    """跨 Provider 实例共享请求上限，失败请求同样计入额度。"""

    def __init__(self, max_requests: int = 3) -> None:
        """创建有限请求额度，防止配置错误或循环持续产生费用。"""
        if max_requests < 1:
            raise ValueError("request budget must be positive")
        self.max_requests = max_requests
        self.used = 0
        self.ledger = BudgetLedger(max_requests=max_requests)

    def consume(self) -> None:
        """在网络请求前消耗一次额度，额度耗尽时不接触远端。"""
        self.ledger.reserve_request()
        self.used = self.ledger.snapshot.requests

    def settle(self, usage: Usage) -> None:
        """把 Provider usage 结算到同一账本，缺失字段保持保守占用。"""
        self.ledger.settle(usage.input_tokens, usage.output_tokens)


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
        self.budget.consume()
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=payload,
            tools=tools or omit,
            max_tokens=self.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=self.extra_body,
        )
        finish: str | None = None
        async with response:
            async for chunk in response:
                # 兼容独立 usage 尾片段和附着在 finish 片段的两种实现。
                if chunk.usage is not None:
                    usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                        source="provider",
                    )
                    self.budget.settle(usage)
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
