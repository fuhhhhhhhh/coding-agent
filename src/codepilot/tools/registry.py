"""实现可直接交给 Runner 的工具注册与统一参数校验入口。"""

import asyncio
import json
from collections.abc import Iterable
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from codepilot.contracts import ToolCall, ToolResult
from codepilot.tools.base import Tool, ToolFailure, ToolSpec


class ToolRegistry:
    """保存名称唯一的工具，并在任何副作用前完成查找和参数校验。"""

    def __init__(self, tools: Iterable[Tool[Any]] = ()) -> None:
        """按给定顺序注册工具，重复名称立即失败。"""
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools:
            self.register(tool)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """按注册顺序返回不可变的工具声明快照。"""
        return tuple(tool.spec for tool in self._tools.values())

    def register(self, tool: Tool[Any]) -> None:
        """注册一个工具，并拒绝覆盖已经存在的同名实现。"""
        # 静默覆盖会让模型看到的声明与最终执行实现取决于导入顺序。
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        # 注册时构建一次声明，使非法名称、超时和 Schema 在接受工具前暴露。
        _ = tool.spec
        self._tools[tool.name] = tool

    async def execute(self, call: ToolCall) -> ToolResult:
        """查找、校验并限时执行一次调用，预期失败统一返回 ToolResult。"""
        tool = self._tools.get(call.name)
        # 未知工具属于模型可修正错误，不能作为 Runtime 自身异常终止运行。
        if tool is None:
            return self._error(call, f"unknown_tool: {call.name}")
        try:
            arguments = tool.arguments_model.model_validate(call.arguments)
        except ValidationError as error:
            # 不回显原始输入，避免把文件内容或凭据复制进错误轨迹。
            details = json.dumps(error.errors(include_input=False), ensure_ascii=False)
            return self._error(call, f"invalid_arguments: {details}")
        try:
            async with asyncio.timeout(tool.default_timeout):
                content = await self._run(tool, arguments)
        except TimeoutError:
            # timeout 上下文会先取消工具协程，再把超时转换为可反馈结果。
            return self._error(call, f"tool_timeout: {tool.default_timeout:g}s")
        except ToolFailure as error:
            return self._error(call, f"tool_error: {error}")
        return ToolResult(call_id=call.call_id, content=content)

    async def _run(self, tool: Tool[Any], arguments: BaseModel) -> str:
        """在参数类型由注册项确定后调用工具，集中隔离泛型擦除。"""
        return await tool.run(cast(Any, arguments))

    @staticmethod
    def _error(call: ToolCall, content: str) -> ToolResult:
        """构造与原调用 ID 配对的模型可见错误结果。"""
        return ToolResult(call_id=call.call_id, content=content, is_error=True)
