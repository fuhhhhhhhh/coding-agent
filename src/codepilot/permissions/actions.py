"""识别可以自动放行的严格只读 Shell Action。"""

import shlex
from pathlib import PurePosixPath

from codepilot.contracts import ToolCall

_SHELL_META = frozenset(";&|<>`$()\n\r")


def is_safe_process_read(call: ToolCall) -> bool:
    """判断 Shell 调用是否属于无组合语法、无写入和无网络的只读命令。"""
    if call.name != "shell":
        return False
    command = call.arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    # 只接受由 && 连接的安全查询；其他组合符号仍然回到人工审批。
    if "&&" in command:
        parts = command.split("&&")
        return len(parts) > 1 and all(_is_safe_single_command(part) for part in parts)
    return _is_safe_single_command(command)


def _is_safe_single_command(command: str) -> bool:
    """判断一段不含组合操作符的命令是否属于严格查询白名单。"""
    if any(char in command for char in _SHELL_META):
        return False
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        # 引号不完整时 Shell 的真实解释不确定，必须回到人工审批。
        return False
    if not argv or any(not item or item.startswith("--") and "=" in item for item in argv):
        return False
    executable = PurePosixPath(argv[0]).name
    args = argv[1:]
    if executable in {"pwd", "true"}:
        return not args
    if executable in {"ls", "dir"}:
        # 仅允许列目录；禁止递归、命令执行和输出重定向类选项。
        return all(
            not item.startswith(("-R", "--recursive", "--quoting-style"))
            and not item.startswith("/")
            and ".." not in PurePosixPath(item).parts
            for item in args
        )
    if executable in {"git", "git.exe"}:
        return _safe_git_args(args)
    return False


def _safe_git_args(args: list[str]) -> bool:
    """限制 Git 只执行不会写仓库、联网或运行用户脚本的查询子命令。"""
    if not args or any(item in {"-c", "--config-env", "--exec-path"} for item in args):
        return False
    # 全局选项只接受 pager 禁用，避免配置触发外部程序或交互分页器。
    while args and args[0] in {"--no-pager", "--no-optional-locks"}:
        args = args[1:]
    if not args:
        return False
    subcommand = args[0]
    rest = args[1:]
    if subcommand in {"status", "log", "branch", "rev-parse", "ls-files"}:
        # 查询也不能借白名单读取工作区外的显式路径；Shell cwd 仍由后端固定。
        if any(item.startswith("/") or ".." in PurePosixPath(item).parts for item in rest):
            return False
        if any(item in {"--upload-pack", "--receive-pack", "--paginate"} for item in rest):
            return False
        if subcommand == "branch":
            return rest in (["--show-current"], ["--list"], [])
        if subcommand == "rev-parse":
            allowed = {"--show-toplevel", "--git-dir", "--is-inside-work-tree"}
            return bool(rest) and all(item in allowed for item in rest)
        return True
    return False
