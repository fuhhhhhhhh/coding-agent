"""组装完整流式消息；整个回复结束并校验之前不允许调用工具。"""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from codepilot.contracts import Message, ToolCall
from codepilot.contracts.stream import StreamChunk, Usage

_MAX_STREAM_BYTES = 16 * 1024 * 1024


@runtime_checkable
class StreamingProvider(Protocol):
    """约束供应商适配器输出统一分片，不暴露 SDK 对象。"""

    def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamChunk]:
        """返回一次回复的异步分片迭代器，必须包含结束标记。"""
        ...


class StreamContractError(ValueError):
    """表示断流以外的非法分片、结束原因或 JSON 参数。"""


class OutputLimitError(StreamContractError):
    """表示供应商因输出额度耗尽而截断了可能不完整的回复。"""


def _invalid_constant(value: str) -> None:
    """拒绝 JSON 标准不允许的 NaN 和无穷大。"""
    raise StreamContractError("non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝重复 JSON 键，避免工具参数被静默覆盖。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StreamContractError("duplicate JSON key")
        result[key] = value
    return result


async def collect_reply(
    provider: StreamingProvider,
    messages: Sequence[Message],
    on_chunk: Callable[[StreamChunk], None],
) -> tuple[Message, Usage]:
    """按调用索引组装有界分片，正常关闭流后统一校验全部调用。"""
    content: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    usage = Usage()
    finished = False
    finish_reason = None
    size = 0
    stream = provider.stream(messages)
    try:
        async for raw_chunk in stream:
            try:
                chunk = StreamChunk.model_validate(raw_chunk.model_dump())
            except (ValidationError, AttributeError) as error:
                raise StreamContractError("invalid stream chunk") from error
            if finished:
                raise StreamContractError("chunk after turn finished")
            size += len(chunk.model_dump_json().encode("utf-8"))
            # 64K 输出模型会产生大量小分片；仍以 16 MiB 累计硬限制阻止无限流耗尽内存。
            if size > _MAX_STREAM_BYTES:
                raise StreamContractError("stream size limit exceeded")
            on_chunk(chunk)
            if chunk.kind == "text_delta":
                content.append(chunk.text)
            elif chunk.kind == "tool_call_delta":
                call = calls.setdefault(chunk.index, {"call_id": "", "name": "", "arguments": ""})
                for field in ("call_id", "name"):
                    value = getattr(chunk, field)
                    if value:
                        if call[field] and call[field] != value:
                            raise StreamContractError("conflicting call identity")
                        call[field] = value
                call["arguments"] += chunk.arguments_delta
            elif chunk.kind == "usage_updated":
                if chunk.usage is None:
                    raise StreamContractError("missing usage payload")
                usage = chunk.usage
            else:
                finished = True
                finish_reason = chunk.finish_reason
    finally:
        # 取消和异常必须关闭供应商流，释放连接或异步生成器。
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
    if not finished:
        raise StreamContractError("stream ended without finish marker")
    if finish_reason != ("tool_calls" if calls else "stop"):
        raise StreamContractError("inconsistent finish reason")
    try:
        tool_calls = tuple(
            ToolCall(
                call_id=call["call_id"],
                name=call["name"],
                arguments=json.loads(
                    call["arguments"],
                    parse_constant=_invalid_constant,
                    object_pairs_hook=_unique_object,
                ),
            )
            for _, call in sorted(calls.items())
        )
        return Message(role="assistant", content="".join(content), tool_calls=tool_calls), usage
    except (ValueError, TypeError, RecursionError) as error:
        raise StreamContractError("invalid assembled reply") from error
