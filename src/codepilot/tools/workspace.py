"""集中实现文件工具共享的工作区路径边界。"""

from pathlib import Path

from codepilot.tools.base import ToolFailure


class Workspace:
    """把用户相对路径解析为不能逃出根目录且不经过符号链接的路径。"""

    def __init__(self, root: Path) -> None:
        """绑定到一个已经存在的真实目录。"""
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")

    def resolve(self, relative: str, *, must_exist: bool) -> Path:
        """校验相对路径、符号链接和最终边界后返回绝对路径。"""
        requested = Path(relative)
        # 只接受非空相对路径，避免绝对路径绕过工作区拼接。
        if not relative.strip() or requested.is_absolute():
            raise ToolFailure("invalid_path: expected a nonempty relative path")
        lexical = self.root.joinpath(requested)
        current = self.root
        for part in requested.parts:
            # 提前拒绝父目录片段，使错误原因稳定且不依赖目标是否存在。
            if part in ("", ".", ".."):
                raise ToolFailure("invalid_path: parent and empty segments are not allowed")
            current /= part
            # 任一路径分量成为符号链接都会让文件身份随外部状态变化。
            if current.is_symlink():
                raise ToolFailure("invalid_path: symbolic links are not allowed")
        try:
            resolved = lexical.resolve(strict=must_exist)
            resolved.relative_to(self.root)
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise ToolFailure("invalid_path: path is missing or outside workspace") from error
        return resolved

    def relative(self, path: Path) -> str:
        """把已校验的绝对路径转换为使用正斜杠的工作区相对路径。"""
        return path.relative_to(self.root).as_posix()
