"""实现有结果和输出上限的工作区纯文本搜索工具。"""

from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure
from codepilot.tools.discovery_common import _matches
from codepilot.tools.file_common import MAX_FILE_BYTES, _read_text
from codepilot.tools.workspace import Workspace

MAX_SEARCH_OUTPUT_BYTES = 65_536


class SearchArguments(Contract):
    """定义文本查询、文件模式、大小和结果数量限制。"""

    query: Annotated[str, Field(min_length=1)]
    pattern: Annotated[str, Field(min_length=1)] = "**/*"
    case_sensitive: bool = True
    max_results: Annotated[int, Field(ge=1, le=1000)] = 200
    max_file_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = 262_144


class SearchTool(Tool[SearchArguments]):
    """在有界 UTF-8 文件集合中执行逐行字面文本搜索。"""

    name = "search"
    description = "在工作区 UTF-8 文本文件中搜索字面文本"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = SearchArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存文本搜索使用的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: SearchArguments) -> str:
        """搜索普通文件，并报告结果截断及无法读取的文件数量。"""
        needle = arguments.query if arguments.case_sensitive else arguments.query.casefold()
        results: list[str] = []
        skipped = 0
        output_bytes = 0
        truncated = False
        for path in _matches(self.workspace, arguments.pattern):
            if not path.is_file():
                continue
            try:
                content = _read_text(path, arguments.max_file_bytes)
            except ToolFailure:
                skipped += 1
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                haystack = line if arguments.case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                # 单行裁剪避免生成文件中的极长行独占全部模型上下文。
                display = line if len(line) <= 500 else line[:500] + "…"
                entry = f"{self.workspace.relative(path)}:{line_number}:{display}"
                entry_bytes = len((entry + "\n").encode("utf-8"))
                if len(results) >= arguments.max_results or (
                    output_bytes + entry_bytes > MAX_SEARCH_OUTPUT_BYTES
                ):
                    truncated = True
                    break
                results.append(entry)
                output_bytes += entry_bytes
            if truncated:
                break
        lines = results if results else ["no matches"]
        if truncated:
            lines.append("[truncated: result or output limit reached]")
        if skipped:
            lines.append(f"[skipped unreadable files: {skipped}]")
        return "\n".join(lines)
