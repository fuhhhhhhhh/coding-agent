"""有限的脚本化回复；耗尽时明确失败，不重复旧回复。"""

from collections import deque
from collections.abc import Sequence

from codepilot.contracts import Message, ToolCall


class FakeProviderExhausted(RuntimeError):
    """测试脚本中已没有剩余回复。"""


class FakeProvider:
    """按构造顺序一次性返回预设 assistant 消息，供离线测试使用。"""

    def __init__(self, responses: Sequence[Message] | None = None) -> None:
        """快照有限的 assistant 回复脚本；未提供时使用 M01 默认脚本。"""
        script = (
            responses
            if responses is not None
            else (
                Message(role="assistant", content="M01 fake: ready to inspect a file."),
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id="call-1", name="read_file", arguments={"path": "README.md"}
                        ),
                    ),
                ),
            )
        )
        # 在此边界上，Provider 只能产生 assistant 消息。
        if any(message.role != "assistant" for message in script):
            raise ValueError("fake responses must be assistant messages")
        # 同时快照脚本和每条回复，调用方后续修改不会影响未来输出。
        self._responses = deque(message.model_copy(deep=True) for message in script)

    async def complete(self, messages: Sequence[Message]) -> Message:
        """确认请求历史非空后，消费一条脚本回复。"""
        # 非法请求不会消费回复，修正后的重试仍会看到同一条脚本数据。
        if not messages:
            raise ValueError("provider request requires at least one message")
        # 耗尽必须显式报错；静默重复最后一条回复可能制造无限循环。
        if not self._responses:
            raise FakeProviderExhausted("fake response script exhausted")
        return self._responses.popleft()
