"""提供 glob 和搜索工具共享的模式校验与安全遍历策略。"""

from collections.abc import Iterator
from pathlib import Path

from codepilot.tools.base import ToolFailure
from codepilot.tools.workspace import Workspace

DEFAULT_EXCLUDES = frozenset({".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"})
MAX_SEARCH_OUTPUT_BYTES = 65_536


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
