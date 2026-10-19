"""定义历史摘要协议，并提供复用现有 Provider 的摘要适配器。"""

import asyncio
from collections.abc import Sequence
from typing import Annotated, Protocol

from pydantic import Field

from codepilot.context.models import SummaryResult
from codepilot.contracts import Message
from codepilot.contracts.models import Contract
from codepilot.contracts.stream import Usage
from codepilot.providers.base import Provider
from codepilot.providers.stream import StreamingProvider, collect_reply


class Summarizer(Protocol):
    """约束上下文压缩所需的最小异步摘要能力。"""

    async def summarize(self, messages: Sequence[Message], target_tokens: int) -> SummaryResult:
        """把完整的旧消息组压缩为结构化摘要，并返回本次调用用量。"""
        ...


class SummaryCandidate(Contract):
    """模型只提出有来源的候选摘要，不能直接写入控制器事实。"""

    summary: Annotated[str, Field(min_length=1, max_length=32000)]
    source_indices: Annotated[tuple[int, ...], Field(min_length=1)]


class ModelSummarizer:
    """使用独立的无工具 Provider 请求生成结构化历史摘要。"""

    def __init__(
        self,
        provider: Provider | StreamingProvider,
        *,
        timeout_seconds: float = 60.0,
        structured: bool = False,
    ) -> None:
        """保存专用 Provider 和整次摘要总时限；请求额度仍由 Provider 强制执行。"""
        if timeout_seconds <= 0:
            raise ValueError("summary timeout must be positive")
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.structured = structured

    async def summarize(self, messages: Sequence[Message], target_tokens: int) -> SummaryResult:
        """请求模型保留目标、约束、待办、修改与测试证据，并拒绝工具回复。"""
        transcript = "\n".join(
            f"source_index={index} {message.model_dump_json()}"
            for index, message in enumerate(messages)
        )
        request = (
            Message(
                role="system",
                content=(
                    "你是上下文压缩器。只总结给定历史，不执行工具。必须分为：用户目标与约束、"
                    "待办、已修改内容、测试与故障证据、未解决风险。保留文件名、错误类型和结论；"
                    "不把推测写成事实。"
                    + (
                        '只返回严格 JSON：{"summary":"按上述标题组织的摘要",'
                        '"source_indices":[0]}。source_indices 必须引用实际输入消息编号；'
                        "不加 Markdown 围栏、未知字段或其他说明。"
                        if self.structured
                        else ""
                    )
                ),
            ),
            Message(
                role="user",
                content=f"目标不超过约 {target_tokens} Token。以下是待压缩历史：\n{transcript}",
            ),
        )
        # SDK 的读超时通常按单次网络操作计算；这里额外限制整个摘要调用的墙钟时间。
        async with asyncio.timeout(self.timeout_seconds):
            if isinstance(self.provider, StreamingProvider):
                # 摘要流不转发到主 Runtime，避免把内部压缩文本显示成 Agent 正式回答。
                reply, usage = await collect_reply(self.provider, request, lambda chunk: None)
            else:
                reply = await self.provider.complete(request)
                usage = Usage()
        if reply.role != "assistant" or reply.tool_calls or not reply.content.strip():
            raise ValueError("summarizer must return nonempty assistant text without tools")
        content = reply.content
        if self.structured:
            # 非法 JSON 或不存在的来源会停止压缩，不能用自由文本偷偷降级。
            candidate = SummaryCandidate.model_validate_json(content)
            if any(index < 0 or index >= len(messages) for index in candidate.source_indices):
                raise ValueError("summary references unknown source")
            content = candidate.summary
        return SummaryResult(content=content, usage=usage)
