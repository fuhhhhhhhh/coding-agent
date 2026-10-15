"""实现面向模型的只读 Git 状态工具。"""

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool
from codepilot.tools.git_common import run_readonly_git
from codepilot.tools.workspace import Workspace


class GitStatusArguments(Contract):
    """表示无需参数的 Git 状态查询。"""


class GitStatusTool(Tool[GitStatusArguments]):
    """展示任务 worktree 的分支和文件状态，不修改 index。"""

    name = "git_status"
    description = "查看当前任务 Git worktree 的简洁状态"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = GitStatusArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存只允许在任务目录中运行的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: GitStatusArguments) -> str:
        """返回 porcelain 状态；无修改时显式说明工作树干净。"""
        del arguments
        output = await run_readonly_git(
            self.workspace.root,
            ("status", "--short", "--branch", "--untracked-files=all"),
        )
        return output or "working tree clean"
