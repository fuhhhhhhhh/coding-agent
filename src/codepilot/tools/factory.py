"""组装 M03 六个基础工具的默认注册表。"""

from pathlib import Path
from typing import Any

from codepilot.tools.base import Tool
from codepilot.tools.edit_file import EditFileTool
from codepilot.tools.git_diff import GitDiffTool
from codepilot.tools.git_status import GitStatusTool
from codepilot.tools.glob import GlobTool
from codepilot.tools.read_file import ReadFileTool
from codepilot.tools.registry import ToolRegistry
from codepilot.tools.search import SearchTool
from codepilot.tools.shell import ShellTool
from codepilot.tools.workspace import Workspace
from codepilot.tools.write_file import WriteFileTool


def create_basic_registry(root: Path, *, include_git: bool = False) -> ToolRegistry:
    """让基础工具共享工作区，并按产品链需要加入只读 Git 工具。"""
    workspace = Workspace(root)
    tools: list[Tool[Any]] = [
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        EditFileTool(workspace),
        GlobTool(workspace),
        SearchTool(workspace),
        ShellTool(workspace),
    ]
    if include_git:
        # Git 状态由控制器只读执行；文件与 Shell 是否进容器仍由后端决定。
        tools.extend((GitStatusTool(workspace), GitDiffTool(workspace)))
    return ToolRegistry(tools)
