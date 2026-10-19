"""把 Responses 语义事件转换为同一个内部流契约，不引入第二个 Runtime。"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from codepilot.contracts import Message
from codepilot.contracts.stream import StreamChunk, Usage
from codepilot.providers.openai_compatible import OpenAICompatibleProvider
from codepilot.providers.stream import OutputLimitError, StreamContractError


class ResponsesProvider(OpenAICompatibleProvider):
    """复用客户端、预算和退避，仅替换消息编码与语义事件适配。"""

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamChunk]:
        """完整消费响应并核对函数参数及终态，断流或 incomplete 均不执行工具。"""
        from openai.types.responses import ResponseInputParam, ToolParam

        payload = response_input(messages)
        tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "strict": False,
            }
            for tool in self.tools
        ]

        async def create() -> Any:
            """通过 SDK 调用无服务端会话状态的 Responses 接口。"""
            return await self.client.responses.create(
                model=self.model,
                input=cast(ResponseInputParam, payload),
                tools=cast(list[ToolParam], tools),
                max_output_tokens=self.max_tokens,
                stream=True,
                store=False,
                extra_body=self.extra_body,
            )

        stream, reservation = await self.open_stream(create, {"input": payload, "tools": tools})
        calls: dict[int, dict[str, str]] = {}
        completed = False
        sequence = -1
        async with stream:
            async for event in stream:
                number = getattr(event, "sequence_number", None)
                # 已完成或乱序的流不能再拼装成另一个可执行回复。
                if not isinstance(number, int) or number <= sequence or completed:
                    raise StreamContractError("invalid Responses sequence or data after completion")
                sequence = number
                kind = event.type
                if kind == "response.output_text.delta":
                    yield StreamChunk(kind="text_delta", text=event.delta)
                elif kind == "response.output_item.added" and event.item.type == "function_call":
                    index = event.output_index
                    # 相同索引重用会覆盖先前参数，必须在执行前拒绝。
                    if index in calls:
                        raise StreamContractError("duplicate Responses function item")
                    calls[index] = {
                        "id": event.item.id or "",
                        "call_id": event.item.call_id,
                        "name": event.item.name,
                        "arguments": "",
                        "done": "",
                    }
                    yield StreamChunk(
                        kind="tool_call_delta",
                        index=index,
                        call_id=event.item.call_id,
                        name=event.item.name,
                    )
                elif kind == "response.function_call_arguments.delta":
                    index = event.output_index
                    # 只接受已声明且身份一致的分片，不能把未知参数归给别的调用。
                    if index not in calls or calls[index]["id"] != event.item_id:
                        raise StreamContractError("unknown Responses argument item")
                    calls[index]["arguments"] += event.delta
                    yield StreamChunk(
                        kind="tool_call_delta", index=index, arguments_delta=event.delta
                    )
                elif kind == "response.output_item.done" and event.item.type == "function_call":
                    index = event.output_index
                    call = calls.get(index)
                    # 终项必须与所有已消费分片相符，不能以最终正文静默替换参数。
                    if (
                        call is None
                        or call["done"]
                        or any(
                            call[key] != getattr(event.item, key)
                            for key in ("id", "call_id", "name", "arguments")
                        )
                    ):
                        raise StreamContractError("Responses function item mismatch")
                    call["done"] = "yes"
                elif kind == "response.incomplete":
                    # 截断意味着调用参数不可信，输出上限不能视为工具回合完成。
                    raise OutputLimitError("Responses output incomplete")
                elif kind in {"response.failed", "error"}:
                    # 协议错误正文可能包含秘密，仅记录稳定内部类别。
                    raise StreamContractError("Responses failed")
                elif kind == "response.completed":
                    # 服务端整轮与每个调用都必须确认完成，局部完成不足以执行。
                    if event.response.status != "completed" or any(
                        not call["done"] for call in calls.values()
                    ):
                        raise StreamContractError("Responses completed with unfinished calls")
                    output_calls = [
                        item for item in event.response.output if item.type == "function_call"
                    ]
                    expected = {call["call_id"]: call for call in calls.values()}
                    # 最终清单连参数也必须吻合，集合比较本身会掩盖重复项和篡改。
                    if (
                        len(output_calls) != len(calls)
                        or any(
                            item.call_id not in expected
                            or any(
                                expected[item.call_id][key] != getattr(item, key)
                                for key in ("id", "call_id", "name", "arguments")
                            )
                            for item in output_calls
                        )
                        or {item.call_id for item in output_calls} != set(expected)
                    ):
                        raise StreamContractError("Responses final calls mismatch")
                    if event.response.usage is not None:
                        usage = Usage(
                            input_tokens=event.response.usage.input_tokens,
                            output_tokens=event.response.usage.output_tokens,
                            source="provider",
                        )
                        self.budget.settle(usage, reservation)
                        yield StreamChunk(kind="usage_updated", usage=usage)
                    completed = True
                elif kind.startswith(
                    ("response.custom_tool", "response.web_search", "response.tool_search")
                ):
                    # 未暴露的服务端工具不允许静默执行并当成本地工具成功。
                    raise StreamContractError("unsupported Responses tool type")
        # EOF 不等于正常终态，完整参数但没有 completed 也不能执行。
        if not completed:
            raise StreamContractError("Responses stream ended without completion")
        yield StreamChunk(kind="turn_finished", finish_reason="tool_calls" if calls else "stop")


def response_input(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """转换完整历史并保留函数调用/结果 ID，恢复时不依赖 previous_response_id。"""
    result: list[dict[str, Any]] = []
    for message in messages:
        if message.tool_result is not None:
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_result.call_id,
                    "output": message.tool_result.content,
                }
            )
            continue
        if message.content:
            result.append({"role": message.role, "content": message.content})
        for call in message.tool_calls:
            result.append(
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                }
            )
    return result
