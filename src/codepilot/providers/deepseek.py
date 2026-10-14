"""使用官方 OpenAI SDK 适配 DeepSeek 的非推理 Chat Completions 流。"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Literal, cast

from openai import AsyncOpenAI, omit
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from codepilot.contracts import Message
from codepilot.contracts.stream import StreamChunk, Usage
from codepilot.providers.stream import StreamContractError
from codepilot.tools.base import ToolSpec


class RequestBudget:
    """跨 Provider 实例共享请求上限，计入失败请求且不自动重试。"""

    def __init__(self, max_requests: int = 3) -> None:
        """创建显式请求额度，不用可无限增长的默认计数。"""
        if max_requests < 1:
            raise ValueError("request budget must be positive")
        self.max_requests = max_requests
        self.used = 0

    def consume(self) -> None:
        """在发起网络请求前消耗额度，耗尽时拒绝。"""
        if self.used >= self.max_requests:
            raise RuntimeError("provider request budget exhausted")
        self.used += 1


class DeepSeekProvider:
    """只在适配层接触 SDK；工具 Schema 来自实际注册表声明。"""

    def __init__(
        self,
        client: AsyncOpenAI,
        tools: Sequence[ToolSpec] = (),
        *,
        model: str = "deepseek-flash",
        budget: RequestBudget | None = None,
        max_tokens: int = 256,
    ) -> None:
        """配置非推理模型和输出预算；客户端需由调用方关闭。"""
        if not 1 <= max_tokens <= 4096:
            raise ValueError("max_tokens must be between 1 and 4096")
        # 禁用 SDK 自动重试，失败也只能消费一次真实请求额度。
        self.client = client.with_options(max_retries=0)
        self.tools = tuple(tool.model_copy(deep=True) for tool in tools)
        self.model = model
        self.budget = budget if budget is not None else RequestBudget()
        self.max_tokens = max_tokens

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamChunk]:
        """转换消息与分片；只有正常完成整个 SDK 流后才发出结束标记。"""
        payload = _messages(messages)
        tools = cast(
            list[ChatCompletionToolParam],
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
            extra_body={"thinking": {"type": "disabled"}},
        )
        finish: str | None = None
        async with response:
            async for chunk in response:
                # 兼容末尾独立 usage 分片和 usage 附着在 finish 分片两种布局。
                if chunk.usage is not None:
                    yield StreamChunk(
                        kind="usage_updated",
                        usage=Usage(
                            input_tokens=chunk.usage.prompt_tokens,
                            output_tokens=chunk.usage.completion_tokens,
                            source="provider",
                        ),
                    )
                for choice in chunk.choices:
                    if choice.index != 0 or finish is not None:
                        raise StreamContractError("unexpected choice or data after finish")
                    delta = choice.delta
                    if delta.content:
                        yield StreamChunk(kind="text_delta", text=delta.content)
                    for call in delta.tool_calls or ():
                        function = call.function
                        yield StreamChunk(
                            kind="tool_call_delta",
                            index=call.index,
                            call_id=call.id or "",
                            name=function.name or "" if function else "",
                            arguments_delta=function.arguments or "" if function else "",
                        )
                    if choice.finish_reason is not None:
                        finish = choice.finish_reason
        # 长度截断、过滤、服务端中断不可当作正常完整回复。
        if finish not in {"stop", "tool_calls"}:
            raise StreamContractError("incomplete provider finish")
        yield StreamChunk(
            kind="turn_finished", finish_reason=cast(Literal["stop", "tool_calls"], finish)
        )


def _messages(messages: Sequence[Message]) -> list[ChatCompletionMessageParam]:
    """转换内部四类消息，保留工具调用与结果的 ID 配对。"""
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
    return cast(list[ChatCompletionMessageParam], payload)
