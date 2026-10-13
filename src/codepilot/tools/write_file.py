"""实现受工作区限制的 UTF-8 文本写入工具。"""

from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool
from codepilot.tools.file_common import _atomic_write, _check_expected_hash, _validate_write
from codepilot.tools.workspace import Workspace


class WriteFileArguments(Contract):
    """定义写入路径和有大小上限的 UTF-8 文本。"""

    path: Annotated[str, Field(min_length=1)]
    content: str
    expected_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None


class WriteFileTool(Tool[WriteFileArguments]):
    """在工作区内创建或覆盖一个有大小上限的 UTF-8 文件。"""

    name = "write_file"
    description = "在工作区中写入 UTF-8 文本文件"
    effect = "write"
    resources = ("workspace:write",)
    arguments_model = WriteFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: WriteFileArguments) -> str:
        """完成全部边界校验后写入文本，并返回写入字节数。"""
        path = self.workspace.resolve(arguments.path, must_exist=False)
        data = _validate_write(path, arguments.content)
        _check_expected_hash(path, arguments.expected_sha256)
        _atomic_write(path, data)
        return f"written: path={self.workspace.relative(path)} bytes={len(data)}"
