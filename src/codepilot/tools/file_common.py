"""提供文件工具共享的哈希、原子写入、大小和编码校验策略。"""

import hashlib
import os
import tempfile
from pathlib import Path

from codepilot.tools.base import ToolFailure

MAX_FILE_BYTES = 1_048_576
DEFAULT_READ_BYTES = 65_536


def _read_text(path: Path, max_bytes: int) -> str:
    """有界读取 UTF-8 文本，并明确区分过大、二进制和编码错误。"""
    if not path.is_file():
        raise ToolFailure("not_a_file: requested path is not a regular file")
    with path.open("rb") as file:
        data = file.read(max_bytes + 1)
    # 多读一个字节即可判断超限，无需把整个大文件载入内存。
    if len(data) > max_bytes:
        raise ToolFailure(f"file_too_large: content exceeds {max_bytes} bytes")
    if b"\x00" in data:
        raise ToolFailure("binary_file: NUL byte detected")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolFailure("encoding_error: file is not valid UTF-8") from error


def _validate_write(path: Path, content: str) -> bytes:
    """在写入前校验父目录、目标类型和 UTF-8 字节大小。"""
    if not path.parent.is_dir():
        raise ToolFailure("missing_parent: destination directory does not exist")
    if path.exists() and not path.is_file():
        raise ToolFailure("not_a_file: destination is not a regular file")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ToolFailure(f"file_too_large: content exceeds {MAX_FILE_BYTES} bytes")
    return data


def _sha256(path: Path) -> str:
    """分块计算文件哈希，避免为版本检查额外复制完整内容。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_expected_hash(path: Path, expected_sha256: str | None) -> None:
    """在有版本前置条件时拒绝已被外部修改的目标文件。"""
    if expected_sha256 is None:
        return
    if not path.exists():
        raise ToolFailure("conflict: expected an existing file")
    actual = _sha256(path)
    if actual != expected_sha256:
        raise ToolFailure(f"conflict: expected_sha256={expected_sha256} actual_sha256={actual}")


def _atomic_write(path: Path, data: bytes) -> None:
    """在目标同目录写入临时文件后原子替换，避免留下半写入文件。"""
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        # 替换失败时清理临时文件，不影响原目标内容。
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
