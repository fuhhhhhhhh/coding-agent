"""把大工具输出保存在控制器私有目录，并按字符范围读取。"""

import hashlib
import os
import uuid
from pathlib import Path

from codepilot.context.models import ArtifactReference, ArtifactSlice


class ArtifactNotFoundError(LookupError):
    """表示请求的 artifact ID 不属于当前 ContextArtifactStore。"""


class ContextArtifactStore:
    """管理本次进程创建的原始输出文件及其内存索引。

    文件名只使用随机 UUID，不使用 call ID 或模型输入，避免路径注入和敏感名称
    泄漏。索引当前只存在于内存；跨进程恢复属于后续会话恢复阶段。
    """

    def __init__(self, directory: Path) -> None:
        """创建权限收紧的外部目录，但不提前创建任何 artifact 文件。"""
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self._references: dict[str, ArtifactReference] = {}
        self._paths: dict[str, Path] = {}

    def write(self, call_id: str, content: str) -> ArtifactReference:
        """以 0600 新文件原子保存完整正文，并返回不含宿主路径的引用。"""
        encoded = content.encode("utf-8")
        artifact_id = f"artifact-{uuid.uuid4().hex}"
        path = self.directory / f"{artifact_id}.txt"
        # O_EXCL 防止随机 ID 极端碰撞时覆盖既有证据；写完 fsync 再公开索引。
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # 写入中断时删除不完整文件，调用方会保留原始 ToolResult 作为降级路径。
            path.unlink(missing_ok=True)
            raise
        reference = ArtifactReference(
            artifact_id=artifact_id,
            call_id=call_id,
            characters=len(content),
            bytes=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )
        self._references[artifact_id] = reference
        self._paths[artifact_id] = path
        return reference

    def read(self, artifact_id: str, start: int, max_chars: int) -> ArtifactSlice:
        """读取已登记 artifact 的有界字符区间，拒绝任意路径和未知 ID。"""
        reference = self._references.get(artifact_id)
        path = self._paths.get(artifact_id)
        if reference is None or path is None or not path.is_file():
            raise ArtifactNotFoundError(artifact_id)
        # 当前输出上限为数 MiB，完整解码能保证不会从 UTF-8 多字节字符中间切开。
        content = path.read_text(encoding="utf-8")
        if start > len(content):
            raise ValueError("artifact start exceeds content length")
        end = min(len(content), start + max_chars)
        return ArtifactSlice(
            artifact_id=artifact_id,
            start=start,
            end=end,
            total_characters=len(content),
            content=content[start:end],
            has_more=end < len(content),
        )

    def cleanup(self) -> None:
        """只删除当前 store 创建并登记的文件，使显式生命周期结束可重复调用。"""
        for path in self._paths.values():
            path.unlink(missing_ok=True)
        self._references.clear()
        self._paths.clear()
