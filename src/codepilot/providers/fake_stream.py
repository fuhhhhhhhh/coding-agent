"""提供可注入断流异常的有限离线流式脚本。"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence

from codepilot.contracts import Message
from codepilot.contracts.stream import StreamChunk


class FakeStreamProvider:
    """逐请求回放分片；脚本与运行时脱敏 Trace 分开保存。"""

    def __init__(self, turns: Sequence[Sequence[StreamChunk | Exception]]) -> None:
        """快照每个回合的分片脚本，允许注入网络故障。"""
        self._turns = deque(
            tuple(
                item.model_copy(deep=True) if isinstance(item, StreamChunk) else item
                for item in turn
            )
            for turn in turns
        )

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[StreamChunk]:
        """逐片让出执行权以接收取消，耗尽脚本时明确失败。"""
        if not messages or not self._turns:
            raise RuntimeError("invalid request or exhausted stream script")
        for item in self._turns.popleft():
            await asyncio.sleep(0)
            if isinstance(item, Exception):
                raise item
            yield item.model_copy(deep=True)
