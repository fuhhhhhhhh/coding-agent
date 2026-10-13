"""实现有结果上限的工作区文件发现与纯文本搜索。"""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool, ToolFailure
from codepilot.tools.files import MAX_FILE_BYTES, _read_text
from codepilot.tools.workspace import Workspace

DEFAULT_EXCLUDES = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"})
MAX_SEARCH_OUTPUT_BYTES = 65_536


class GlobArguments(Contract):
    """定义工作区 glob 模式及最大返回数量。"""

    pattern: Annotated[str, Field(min_length=1)]
    max_results: Annotated[int, Field(ge=1, le=1000)] = 200


class SearchArguments(Contract):
    """定义文本查询、文件模式、大小和结果数量限制。"""

    query: Annotated[str, Field(min_length=1)]
    pattern: Annotated[str, Field(min_length=1)] = "**/*"
    case_sensitive: bool = True
    max_results: Annotated[int, Field(ge=1, le=1000)] = 200
    max_file_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = 262_144


def _validate_pattern(pattern: str) -> None:
    """拒绝可能离开工作区或交给绝对路径解释的 glob 模式。"""
    candidate = Path(pattern)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ToolFailure("invalid_pattern: pattern must stay inside workspace")


def _matches(workspace: Workspace, pattern: str) -> Iterator[Path]:
    """按稳定顺序产生非排除目录中的真实路径，并跳过所有符号链接。"""
    _validate_pattern(pattern)
    try:
        candidates = sorted(workspace.root.glob(pattern), key=lambda path: path.as_posix())
    except (OSError, ValueError) as error:
        raise ToolFailure("invalid_pattern: glob pattern cannot be evaluated") from error
    for path in candidates:
        relative = path.relative_to(workspace.root)
        # 默认排除版本库和缓存目录，避免噪声、重复内容和意外大遍历。
        if any(part in DEFAULT_EXCLUDES for part in relative.parts):
            continue
        try:
            resolved = workspace.resolve(relative.as_posix(), must_exist=True)
        except ToolFailure:
            # 发现类工具跳过符号链接，并让结果只包含可由文件工具再次访问的路径。
            continue
        yield resolved


class GlobTool(Tool[GlobArguments]):
    """列出符合模式且能安全再次访问的工作区路径。"""

    name = "glob"
    description = "按 glob 模式列出工作区路径"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = GlobArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存文件发现使用的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: GlobArguments) -> str:
        """返回稳定排序的路径列表，并明确标记数量截断。"""
        results: list[str] = []
        truncated = False
        for path in _matches(self.workspace, arguments.pattern):
            if len(results) >= arguments.max_results:
                truncated = True
                break
            suffix = "/" if path.is_dir() else ""
            results.append(self.workspace.relative(path) + suffix)
        if not results:
            return "no matches"
        footer = f"\n[truncated after {arguments.max_results} results]" if truncated else ""
        return "\n".join(results) + footer


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
