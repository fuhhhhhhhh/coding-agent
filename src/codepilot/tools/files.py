"""实现受工作区限制的读取、写入和唯一文本替换工具。"""

from pathlib import Path
from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure
from codepilot.tools.workspace import Workspace

MAX_FILE_BYTES = 1_048_576
DEFAULT_READ_BYTES = 65_536


class ReadFileArguments(Contract):
    """定义读取路径和允许返回的最大字节数。"""

    path: Annotated[str, Field(min_length=1)]
    max_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = DEFAULT_READ_BYTES


class WriteFileArguments(Contract):
    """定义写入路径和有大小上限的 UTF-8 文本。"""

    path: Annotated[str, Field(min_length=1)]
    content: str


class EditFileArguments(Contract):
    """定义必须唯一出现的旧文本及其替换文本。"""

    path: Annotated[str, Field(min_length=1)]
    old_text: Annotated[str, Field(min_length=1)]
    new_text: str


def _read_text(path: Path, max_bytes: int) -> str:
    """有界读取 UTF-8 文本，并明确区分过大、二进制和编码错误。"""
    if not path.is_file():
        raise ToolFailure("not_a_file: requested path is not a regular file")
    with path.open("rb") as file:
        data = file.read(max_bytes + 1)
    # 多读一个字节即可判断超限，无需把整个大文件载入内存。
    if len(data) > max_bytes:
        raise ToolFailure(f"file_too_large: content exceeds {max_bytes} bytes")
    if b"\x00" in data:
        raise ToolFailure("binary_file: NUL byte detected")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolFailure("encoding_error: file is not valid UTF-8") from error


def _validate_write(path: Path, content: str) -> bytes:
    """在写入前校验父目录、目标类型和 UTF-8 字节大小。"""
    if not path.parent.is_dir():
        raise ToolFailure("missing_parent: destination directory does not exist")
    if path.exists() and not path.is_file():
        raise ToolFailure("not_a_file: destination is not a regular file")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ToolFailure(f"file_too_large: content exceeds {MAX_FILE_BYTES} bytes")
    return data


class ReadFileTool(Tool[ReadFileArguments]):
    """读取一个有大小上限的工作区 UTF-8 文本文件。"""

    name = "read_file"
    description = "读取工作区中的 UTF-8 文本文件"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = ReadFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存所有文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: ReadFileArguments) -> str:
        """解析路径并返回完整且未超过调用上限的文本。"""
        path = self.workspace.resolve(arguments.path, must_exist=True)
        return _read_text(path, arguments.max_bytes)


class WriteFileTool(Tool[WriteFileArguments]):
    """在工作区内创建或覆盖一个有大小上限的 UTF-8 文件。"""

    name = "write_file"
    description = "在工作区中写入 UTF-8 文本文件"
    effect = "write"
    resources = ("workspace:write",)
    arguments_model = WriteFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存所有文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: WriteFileArguments) -> str:
        """完成全部边界校验后写入文本，并返回写入字节数。"""
        path = self.workspace.resolve(arguments.path, must_exist=False)
        data = _validate_write(path, arguments.content)
        path.write_bytes(data)
        return f"written: path={self.workspace.relative(path)} bytes={len(data)}"


class EditFileTool(Tool[EditFileArguments]):
    """仅在旧文本恰好出现一次时修改工作区文本文件。"""

    name = "edit_file"
    description = "唯一匹配旧文本后替换工作区文件内容"
    effect = "write"
    resources = ("workspace:write",)
    arguments_model = EditFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存所有文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: EditFileArguments) -> str:
        """拒绝零匹配和多匹配，成功时只替换唯一位置。"""
        path = self.workspace.resolve(arguments.path, must_exist=True)
        content = _read_text(path, MAX_FILE_BYTES)
        matches = content.count(arguments.old_text)
        # 零匹配可能表示模型依据旧内容操作，多匹配则无法确定修改目标。
        if matches != 1:
            raise ToolFailure(f"ambiguous_edit: expected 1 match, found {matches}")
        updated = content.replace(arguments.old_text, arguments.new_text, 1)
        data = _validate_write(path, updated)
        path.write_bytes(data)
        return f"edited: path={self.workspace.relative(path)} bytes={len(data)}"
