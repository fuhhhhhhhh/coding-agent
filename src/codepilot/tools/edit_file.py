"""实现要求唯一匹配的工作区文本编辑工具。"""

from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure
from codepilot.tools.file_common import (
    MAX_FILE_BYTES,
    _atomic_write,
    _check_expected_hash,
    _read_text_with_hash,
    _validate_write,
)
from codepilot.tools.workspace import Workspace


class EditFileArguments(Contract):
    """定义必须唯一出现的旧文本及其替换文本。"""

    path: Annotated[str, Field(min_length=1)]
    old_text: Annotated[str, Field(min_length=1)]
    new_text: str
    expected_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None


class EditFileTool(Tool[EditFileArguments]):
    """仅在旧文本恰好出现一次时修改工作区文本文件。"""

    name = "edit_file"
    description = "唯一匹配旧文本后替换工作区文件内容"
    effect = "write"
    resources = ("workspace:write",)
    arguments_model = EditFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: EditFileArguments) -> str:
        """拒绝零匹配和多匹配，成功时只替换唯一位置。"""
        path = self.workspace.resolve(arguments.path, must_exist=True)
        _check_expected_hash(path, arguments.expected_sha256)
        content, read_hash = _read_text_with_hash(path, MAX_FILE_BYTES)
        matches = content.count(arguments.old_text)
        # 零匹配可能表示模型依据旧内容操作，多匹配则无法确定修改目标。
        if matches != 1:
            raise ToolFailure(f"ambiguous_edit: expected 1 match, found {matches}")
        updated = content.replace(arguments.old_text, arguments.new_text, 1)
        data = _validate_write(path, updated)
        # 二次检查覆盖“读取后、写入前”这段窗口，检测外部并发修改。
        _check_expected_hash(path, arguments.expected_sha256 or read_hash)
        _atomic_write(path, data)
        diff = _diff(self.workspace.relative(path), content, updated)
        return f"edited: path={self.workspace.relative(path)} bytes={len(data)}\n{diff}"


def _diff(path: object, before: str, after: str) -> str:
    """生成面向人和轨迹的统一 diff，不参与文件写入判定。"""
    import difflib

    name = str(path)
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=name,
        tofile=name,
    )
    return "".join(lines) or "diff: no changes"
