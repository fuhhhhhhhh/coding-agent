"""在通用 OpenAI-compatible 适配器上固定 DeepSeek 非推理参数。"""

from collections.abc import Sequence

from openai import AsyncOpenAI

from codepilot.providers.openai_compatible import (
    OpenAICompatibleProvider,
)
from codepilot.providers.openai_compatible import (
    RequestBudget as RequestBudget,
)
from codepilot.tools.base import ToolSpec


class DeepSeekProvider(OpenAICompatibleProvider):
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
        """复用通用流式契约，并加入 DeepSeek 关闭思考模式的请求字段。"""
        super().__init__(
            client,
            tools,
            model=model,
            budget=budget,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
