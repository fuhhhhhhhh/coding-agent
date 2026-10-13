"""定义工具声明、参数模型和执行失败的通用契约。"""

from abc import ABC, abstractmethod
from typing import Annotated, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, Field, JsonValue

from codepilot.contracts.models import Contract

ArgumentsT = TypeVar("ArgumentsT", bound=BaseModel)


class ToolSpec(Contract):
    """向模型和调度层描述工具能力，不包含具体执行状态。"""

    name: Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    description: str
    input_schema: dict[str, JsonValue]
    effect: Literal["read", "write", "process"]
    # resources 是保守的资源声明，后续调度器可据此决定是否允许并发。
    resources: tuple[str, ...]
    default_timeout: Annotated[float, Field(gt=0)]
    supports_cancel: bool


class ToolFailure(Exception):
    """表示可预期且适合反馈给模型的工具业务失败。"""


class ToolOutput(Contract):
    """允许工具在保留正文时标记命令退出等业务失败。"""

    content: str
    is_error: bool = False


class Tool(ABC, Generic[ArgumentsT]):
    """把一份参数模型同时用于公开 Schema 和执行前校验。"""

    name: str
    description: str
    effect: Literal["read", "write", "process"]
    resources: tuple[str, ...]
    default_timeout: float = 10.0
    supports_cancel: bool = True
    arguments_model: type[ArgumentsT]

    @property
    def spec(self) -> ToolSpec:
        """从实际参数模型生成公开声明，避免 Schema 与执行参数漂移。"""
        # Pydantic 生成的数据符合 JSON 值约束，此处转换只用于收窄静态类型。
        schema = cast(dict[str, JsonValue], self.arguments_model.model_json_schema())
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=schema,
            effect=self.effect,
            resources=self.resources,
            default_timeout=self.default_timeout,
            supports_cancel=self.supports_cancel,
        )

    @abstractmethod
    async def run(self, arguments: ArgumentsT) -> str | ToolOutput:
        """执行已经通过对应 Pydantic 模型校验的参数并返回文本结果。"""
        raise NotImplementedError
