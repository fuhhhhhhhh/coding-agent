"""提供执行后端共享的有界进程读取与安全产物复制。"""

import asyncio
import os
import shutil
from pathlib import Path

from codepilot.workspace.models import BackendEvent


class BackendJournal:
    """把不含正文的后端事件追加到工作区外的仅所有者 JSONL。"""

    def __init__(self, directory: Path | None, workspace: Path, backend_id: str) -> None:
        """校验审计目录不在 Agent 可写工作区内，并延迟创建文件。"""
        self.directory = directory.resolve() if directory is not None else None
        self.workspace = workspace.resolve()
        self.backend_id = backend_id
        self.path: Path | None = None
        if self.directory is not None and self.directory.is_relative_to(self.workspace):
            raise ValueError("backend audit directory must be outside workspace")

    def append(self, event: BackendEvent) -> None:
        """首次事件时安全创建文件，随后逐条立即落盘。"""
        if self.directory is None:
            return
        if self.path is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path = self.directory / f"backend-{self.backend_id}.jsonl"
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")


async def read_limited(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    """排空进程管道并只保留前 limit 字节，避免内存无限增长。"""
    if stream is None:
        return b"", False
    data = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        remaining = limit - len(data)
        if remaining > 0:
            data.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    return bytes(data), truncated


def export_file(root: Path, relative_path: str, destination: Path, limit: int) -> Path:
    """校验来源与大小后，以不跟随工作区符号链接的方式导出单个文件。"""
    from codepilot.tools.workspace import Workspace

    source = Workspace(root).resolve(relative_path, must_exist=True)
    # 产物仅允许普通文件，目录和设备文件不能越过容量与读取边界。
    if not source.is_file() or source.is_symlink():
        raise ValueError("artifact must be a regular file")
    if source.stat().st_size > limit:
        raise ValueError("artifact exceeds configured size limit")
    if destination.is_symlink():
        raise ValueError("artifact destination may not be a symlink")
    target = destination.parent.resolve() / destination.name
    if target.is_relative_to(root.resolve()):
        raise ValueError("artifact destination must be outside workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target
