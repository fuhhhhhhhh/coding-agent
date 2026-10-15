"""向模型公开当前会话原始输出的有界只读工具。"""

from typing import Annotated, Literal

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.sessions.outputs import OutputStore
from codepilot.tools.base import Tool, ToolFailure


class ReadOutputArguments(Contract):
    """限定输出 ID、管道和字节范围，禁止宿主路径参数。"""

    output_id: Annotated[str, Field(pattern=r"^output-[a-f0-9]{32}$")]
    stream: Literal["stdout", "stderr"] = "stdout"
    offset: Annotated[int, Field(ge=0)] = 0
    max_bytes: Annotated[int, Field(ge=1, le=65536)] = 8000


class SearchOutputArguments(Contract):
    """限定单路输出的字面查询，避免任意正则造成无界计算。"""

    output_id: Annotated[str, Field(pattern=r"^output-[a-f0-9]{32}$")]
    stream: Literal["stdout", "stderr"] = "stdout"
    query: Annotated[str, Field(min_length=1, max_length=256)]


class ReadOutputTool(Tool[ReadOutputArguments]):
    """按会话索引读取曾保存的原始输出。"""

    name = "read_output"
    description = "按 output_id、stream、offset 字节偏移读取原始命令输出，最多 65536 字节"
    effect = "read"
    resources = ("context:read",)
    arguments_model = ReadOutputArguments

    def __init__(self, store: OutputStore) -> None:
        """绑定当前会话唯一输出存储。"""
        self.store = store

    async def run(self, arguments: ReadOutputArguments) -> str:
        """返回有限片段，缺失或已过期引用形成可诊断错误。"""
        try:
            return self.store.read(
                arguments.output_id, arguments.stream, arguments.offset, arguments.max_bytes
            )
        except (OSError, ValueError) as error:
            raise ToolFailure(f"output_unavailable: {type(error).__name__}") from error


class SearchOutputTool(Tool[SearchOutputArguments]):
    """在已授权原文中分块定位中段关键字节。"""

    name = "search_output"
    description = "在 output_id 的单路原始命令输出中搜索字面文本，返回最多二十个字节偏移"
    effect = "read"
    resources = ("context:read",)
    arguments_model = SearchOutputArguments

    def __init__(self, store: OutputStore) -> None:
        """绑定会话输出索引，不接受任意文件系统路径。"""
        self.store = store

    async def run(self, arguments: SearchOutputArguments) -> str:
        """执行有界搜索，索引缺失时不制造空匹配假象。"""
        try:
            return self.store.search(arguments.output_id, arguments.stream, arguments.query)
        except (OSError, ValueError) as error:
            raise ToolFailure(f"output_unavailable: {type(error).__name__}") from error
