"""实现要求唯一匹配的工作区文本编辑工具。"""

import json
from typing import Annotated

from pydantic import Field, field_validator

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure, ToolOutput
from codepilot.tools.file_common import (
    MAX_FILE_BYTES,
    _atomic_write,
    _check_expected_hash,
    _read_text_with_hash,
    _validate_write,
)
from codepilot.tools.workspace import Workspace


class FileEdit(Contract):
    """定义必须唯一出现的旧文本及其替换文本。"""

    path: Annotated[str, Field(min_length=1)]
    old_text: Annotated[str, Field(min_length=1)]
    new_text: str
    expected_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None


class EditFileArguments(FileEdit):
    """主编辑与最多十五个附加文件共同预检，保留原单文件接口。"""

    additional_edits: Annotated[tuple[FileEdit, ...], Field(max_length=15)] = ()

    @field_validator("additional_edits", mode="before")
    @classmethod
    def parse_edits(cls, value: object) -> object:
        """将模型 JSON 数组转换为不可变批次。"""
        return tuple(value) if isinstance(value, list) else value


class EditFileTool(Tool[EditFileArguments]):
    """仅在旧文本恰好出现一次时修改工作区文本文件。"""

    name = "edit_file"
    description = (
        "唯一匹配后替换；additional_edits 支持最多16文件整批预检与部分失败清单，"
        "已有文件需要读取版本"
    )
    effect = "write"
    resources = ("workspace:write",)
    arguments_model = EditFileArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存文件工具共享的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: EditFileArguments) -> str | ToolOutput:
        """拒绝零匹配和多匹配，成功时只替换唯一位置。"""
        # 附加文件要求整批检查，避免第二个歧义直到第一个已写后才发现。
        if arguments.additional_edits:
            return self._batch(arguments)
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

    def _batch(self, arguments: EditFileArguments) -> ToolOutput:
        """整批预检唯一目标与版本，再逐文件复核写入，故障返回完整部分结果。"""
        edits = (
            FileEdit.model_validate(arguments.model_dump(exclude={"additional_edits"})),
            *arguments.additional_edits,
        )
        prepared = []
        seen: set[str] = set()
        for edit in edits:
            path = self.workspace.resolve(edit.path, must_exist=True)
            relative = self.workspace.relative(path)
            # 同一路径的多次修改容易混淆前置版本，批次中要求每个目标只出现一次。
            if relative in seen:
                raise ToolFailure("invalid_batch: duplicate file path")
            seen.add(relative)
            content, sha = _read_text_with_hash(path, MAX_FILE_BYTES)
            _check_expected_hash(path, edit.expected_sha256)
            if content.count(edit.old_text) != 1:
                raise ToolFailure(f"ambiguous_edit: expected 1 match: {relative}")
            data = _validate_write(path, content.replace(edit.old_text, edit.new_text, 1))
            prepared.append((relative, path, data, sha))
        applied: list[str] = []
        for index, (relative, path, data, sha) in enumerate(prepared):
            try:
                # 预检查后仍可能被外部参与者修改，不能用批次起始检查代替逐写检查。
                self.workspace.resolve(relative, must_exist=True)
                _check_expected_hash(path, sha)
                _atomic_write(path, data)
            except (OSError, ToolFailure) as error:
                return ToolOutput(
                    is_error=True,
                    content=json.dumps(
                        {
                            "kind": "edit_batch",
                            "applied": applied,
                            "failed": relative,
                            "error": type(error).__name__,
                            "unattempted": [item[0] for item in prepared[index + 1 :]],
                        }
                    ),
                )
            applied.append(relative)
        return ToolOutput(
            content=json.dumps(
                {"kind": "edit_batch", "applied": applied, "failed": None, "unattempted": []}
            )
        )


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
