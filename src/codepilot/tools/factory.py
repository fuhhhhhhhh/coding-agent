"""组装 M03 六个基础工具的默认注册表。"""

from pathlib import Path

from codepilot.tools.edit_file import EditFileTool
from codepilot.tools.glob import GlobTool
from codepilot.tools.read_file import ReadFileTool
from codepilot.tools.registry import ToolRegistry
from codepilot.tools.search import SearchTool
from codepilot.tools.shell import ShellTool
from codepilot.tools.workspace import Workspace
from codepilot.tools.write_file import WriteFileTool


def create_basic_registry(root: Path) -> ToolRegistry:
    """让六个基础工具共享同一个已校验工作区并返回统一执行入口。"""
    workspace = Workspace(root)
    return ToolRegistry(
        (
            ReadFileTool(workspace),
            WriteFileTool(workspace),
            EditFileTool(workspace),
            GlobTool(workspace),
            SearchTool(workspace),
            ShellTool(workspace),
        )
    )
