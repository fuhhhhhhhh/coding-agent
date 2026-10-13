"""实现有结果上限的工作区 glob 工具。"""

from typing import Annotated

from pydantic import Field

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool
from codepilot.tools.discovery_common import _matches
from codepilot.tools.workspace import Workspace


class GlobArguments(Contract):
    """定义工作区 glob 模式及最大返回数量。"""

    pattern: Annotated[str, Field(min_length=1)]
    max_results: Annotated[int, Field(ge=1, le=1000)] = 200


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
