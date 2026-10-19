"""可信用户配置声明扩展命令与预算，仓库数据不授予执行权限。"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from codepilot.contracts.models import Contract


class HookConfig(Contract):
    """限制生命周期事件、命令、超时及失败策略，不允许改工具参数。"""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    event: Literal["before_tool", "after_tool"]
    argv: Annotated[list[str], Field(min_length=1, max_length=32)]
    timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0
    failure: Literal["block", "continue"] = "block"


class McpConfig(Contract):
    """仅支持受控 stdio，远程 URL 与任意秘密环境注入不在此阶段开放。"""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    argv: Annotated[list[str], Field(min_length=1, max_length=32)]
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 15.0


class ExtensionConfig(Contract):
    """所有主动执行默认关闭；后台与子任务仍受父级权限及统一预算约束。"""

    enabled: bool = False
    skills: bool = True
    hooks: Annotated[list[HookConfig], Field(max_length=8)] = []
    mcp: Annotated[list[McpConfig], Field(max_length=8)] = []
    max_tasks: Annotated[int, Field(ge=1, le=8)] = 2
    task_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] = 120.0
    child_requests: Annotated[int, Field(ge=1, le=8)] = 3
    child_tool_calls: Annotated[int, Field(ge=1, le=32)] = 8
    child_output_tokens: Annotated[int, Field(ge=256, le=32768)] = 8192

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        """同命名空间重复配置不能静默覆盖执行目标。"""
        for records in (self.hooks, self.mcp):
            if len({item.name for item in records}) != len(records):
                raise ValueError("duplicate extension name")
        return self
