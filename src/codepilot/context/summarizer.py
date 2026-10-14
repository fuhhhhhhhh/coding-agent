"""定义历史摘要协议，并提供复用现有 Provider 的摘要适配器。"""

from collections.abc import Sequence
from typing import Protocol

from codepilot.context.models import SummaryResult
from codepilot.contracts import Message
from codepilot.contracts.stream import Usage
from codepilot.providers.base import Provider
from codepilot.providers.stream import StreamingProvider, collect_reply


class Summarizer(Protocol):
    """约束上下文压缩所需的最小异步摘要能力。"""

    async def summarize(self, messages: Sequence[Message], target_tokens: int) -> SummaryResult:
        """把完整的旧消息组压缩为结构化摘要，并返回本次调用用量。"""
        ...


class ModelSummarizer:
    """使用独立的无工具 Provider 请求生成结构化历史摘要。"""

    def __init__(self, provider: Provider | StreamingProvider) -> None:
        """保存专用于摘要的 Provider；调用预算仍由 Provider 自己强制执行。"""
        self.provider = provider

    async def summarize(self, messages: Sequence[Message], target_tokens: int) -> SummaryResult:
        """请求模型保留目标、约束、待办、修改与测试证据，并拒绝工具回复。"""
        transcript = "\n".join(message.model_dump_json() for message in messages)
        request = (
            Message(
                role="system",
                content=(
                    "你是上下文压缩器。只总结给定历史，不执行工具。必须分为：用户目标与约束、"
                    "待办、已修改内容、测试与故障证据、未解决风险。保留文件名、错误类型和结论；"
                    "不把推测写成事实。"
                ),
            ),
            Message(
                role="user",
                content=f"目标不超过约 {target_tokens} Token。以下是待压缩历史：\n{transcript}",
            ),
        )
        if isinstance(self.provider, StreamingProvider):
            # 摘要流不转发到主 Runtime，避免把内部压缩文本显示成 Agent 正式回答。
            reply, usage = await collect_reply(self.provider, request, lambda chunk: None)
        else:
            reply = await self.provider.complete(request)
            usage = Usage()
        if reply.role != "assistant" or reply.tool_calls or not reply.content.strip():
            raise ValueError("summarizer must return nonempty assistant text without tools")
        return SummaryResult(content=reply.content, usage=usage)
