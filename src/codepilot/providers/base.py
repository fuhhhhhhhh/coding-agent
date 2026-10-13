"""最小结构化接口；不包含供应商专用请求或回复类型。"""

from collections.abc import Sequence
from typing import Protocol

from codepilot.contracts import Message


class Provider(Protocol):
    """约束 Runtime 所需的最小模型供应商能力。"""

    async def complete(self, messages: Sequence[Message]) -> Message:
        """返回一条完整 assistant 消息；失败时抛出异常。"""
        ...
