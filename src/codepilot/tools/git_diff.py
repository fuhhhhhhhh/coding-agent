"""实现面向模型的只读 Git 差异工具。"""

from typing import Literal

from codepilot.contracts.models import Contract
from codepilot.tools.base import Tool
from codepilot.tools.git_common import run_readonly_git
from codepilot.tools.workspace import Workspace


class GitDiffArguments(Contract):
    """选择查看完整正文或仅统计，并可切换到已暂存差异。"""

    mode: Literal["full", "stat"] = "full"
    cached: bool = False


class GitDiffTool(Tool[GitDiffArguments]):
    """展示任务 worktree 的受限 Git diff，不执行外部 diff 驱动。"""

    name = "git_diff"
    description = "查看当前任务相对固定起点的 Git 差异；未跟踪文件先在 git_status 中显示"
    effect = "read"
    resources = ("workspace:read",)
    arguments_model = GitDiffArguments

    def __init__(self, workspace: Workspace) -> None:
        """保存只允许在任务目录中运行的工作区边界。"""
        self.workspace = workspace

    async def run(self, arguments: GitDiffArguments) -> str:
        """按参数返回正文或统计；禁用仓库自定义 diff 与 textconv。"""
        command = ["diff", "--no-ext-diff", "--no-textconv"]
        if arguments.cached:
            command.append("--cached")
        if arguments.mode == "stat":
            command.append("--stat")
        output = await run_readonly_git(self.workspace.root, tuple(command))
        return output or "no tracked diff; use git_status to inspect untracked files"
