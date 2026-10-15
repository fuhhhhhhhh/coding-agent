"""把大工具输出保存在控制器私有目录，并按字符范围读取。"""

import hashlib
import os
import uuid
from pathlib import Path

from codepilot.context.models import ArtifactReference, ArtifactSlice
from codepilot.sessions.outputs import atomic_json, private_directory


class ArtifactNotFoundError(LookupError):
    """表示请求的 artifact ID 不属于当前 ContextArtifactStore。"""


class ContextArtifactStore:
    """管理原始输出文件及可选的跨进程持久索引。

    文件名只使用随机 UUID，不使用 call ID 或模型输入，避免路径注入和敏感名称
    泄漏。独立调用默认临时索引，产品通过 persistent 模式保存并重载私有索引。
    """

    def __init__(
        self, directory: Path, *, persistent: bool = False, max_bytes: int = 32 * 1024**2
    ) -> None:
        """创建权限收紧的外部目录，但不提前创建任何 artifact 文件。"""
        private_directory(directory)
        self.directory = directory
        self.persistent = persistent
        self.max_bytes = max_bytes
        self._references: dict[str, ArtifactReference] = {}
        self._paths: dict[str, Path] = {}
        if persistent:
            for index in directory.glob("artifact-*.json"):
                if index.is_symlink():
                    raise ValueError("invalid artifact index")
                reference = ArtifactReference.model_validate_json(index.read_bytes())
                path = directory / f"{reference.artifact_id}.txt"
                if path.is_symlink():
                    raise ValueError("invalid artifact path")
                self._references[reference.artifact_id] = reference
                self._paths[reference.artifact_id] = path

    def write(self, call_id: str, content: str) -> ArtifactReference:
        """以 0600 新文件原子保存完整正文，并返回不含宿主路径的引用。"""
        encoded = content.encode("utf-8")
        if (
            sum(path.stat().st_size for path in self.directory.glob("*.txt")) + len(encoded)
            > self.max_bytes
        ):
            raise OSError("context artifact quota exceeded")
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
        if self.persistent:
            atomic_json(self.directory / f"{artifact_id}.json", reference.model_dump(mode="json"))
        self._references[artifact_id] = reference
        self._paths[artifact_id] = path
        return reference

    def read(self, artifact_id: str, start: int, max_chars: int) -> ArtifactSlice:
        """读取已登记 artifact 的有界字符区间，拒绝任意路径和未知 ID。"""
        reference = self._references.get(artifact_id)
        path = self._paths.get(artifact_id)
        if reference is None or path is None or not path.is_file():
            raise ArtifactNotFoundError(artifact_id)
        # 顺序跳过字符前缀但只保留所需片段，内存不随原文增长。
        remaining = start
        parts: list[str] = []
        needed = max_chars
        with path.open(encoding="utf-8") as source:
            while chunk := source.read(8192):
                skipped = min(remaining, len(chunk))
                remaining -= skipped
                chunk = chunk[skipped:]
                if needed and chunk:
                    parts.append(chunk[:needed])
                    needed -= min(needed, len(chunk))
                if not remaining and not needed:
                    break
        if start > reference.characters:
            raise ValueError("artifact start exceeds content length")
        content = "".join(parts)
        end = start + len(content)
        return ArtifactSlice(
            artifact_id=artifact_id,
            start=start,
            end=end,
            total_characters=reference.characters,
            content=content,
            has_more=end < reference.characters,
        )

    def cleanup(self) -> None:
        """只删除当前 store 创建并登记的文件，使显式生命周期结束可重复调用。"""
        for path in self._paths.values():
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
        self._references.clear()
        self._paths.clear()
