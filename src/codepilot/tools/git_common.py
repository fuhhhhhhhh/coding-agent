"""提供只读 Git 工具共享的有界、无 Shell 子进程入口。"""

import asyncio
from pathlib import Path

from codepilot.tools.base import ToolFailure

MAX_GIT_OUTPUT_BYTES = 256 * 1024


async def run_readonly_git(root: Path, arguments: tuple[str, ...]) -> str:
    """运行禁用外部 diff 和文本转换的 Git argv，并返回有界 UTF-8 输出。"""
    process = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        *arguments,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if len(stdout) > MAX_GIT_OUTPUT_BYTES or len(stderr) > MAX_GIT_OUTPUT_BYTES:
        raise ToolFailure("git output exceeds 256 KiB limit")
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
        raise ToolFailure(f"git command failed: {detail or process.returncode}")
    return stdout.decode("utf-8", errors="replace")
