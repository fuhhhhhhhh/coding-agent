"""组装 M03 六个基础工具的默认注册表。"""

from pathlib import Path
from typing import Any

from codepilot.tools.base import Tool
from codepilot.tools.edit_file import EditFileTool
from codepilot.tools.glob import GlobTool
from codepilot.tools.read_file import ReadFileTool
from codepilot.tools.registry import ToolRegistry
from codepilot.tools.search import SearchTool
from codepilot.tools.shell import ShellTool
from codepilot.tools.workspace import Workspace
from codepilot.tools.write_file import WriteFileTool


def create_basic_registry(root: Path) -> ToolRegistry:
    """组装共享同一工作区的六个基础工具，Git 操作统一经 Shell 执行。"""
    workspace = Workspace(root)
    tools: list[Tool[Any]] = [
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        EditFileTool(workspace),
        GlobTool(workspace),
        SearchTool(workspace),
        ShellTool(workspace),
    ]
    return ToolRegistry(tools)
