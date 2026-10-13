"""实现受工作区限制的 UTF-8 文本读取工具。"""

from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool
from codepilot.tools.file_common import DEFAULT_READ_BYTES, MAX_FILE_BYTES, _read_text_with_hash
from codepilot.tools.workspace import Workspace


class ReadFileArguments(Contract):
    """定义读取路径和允许返回的最大字节数。"""

    path: Annotated[str, Field(min_length=1)]
    max_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = DEFAULT_READ_BYTES
    include_sha256: bool = False


class ReadFileTool(Tool[ReadFileArguments]):
    """读取一个有大小上限的工作区 UTF-8 文本文件。"""

    name = "read_file"
    description = "读取工作区中的 UTF-8 文本文件"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = ReadFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: ReadFileArguments) -> str:
        """解析路径并返回完整且未超过调用上限的文本。"""
        path = self.workspace.resolve(arguments.path, must_exist=True)
        content, sha256 = _read_text_with_hash(path, arguments.max_bytes)
        # 版本元数据按需返回，保持普通读取结果向后兼容。
        if arguments.include_sha256:
            return f"sha256={sha256}\ncontent:\n{content}"
        return content
