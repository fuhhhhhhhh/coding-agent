"""验证文件工具的工作区边界、内容限制和编辑唯一性。"""

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue

from codepilot.contracts import ToolCall, ToolResult
from codepilot.tools import EditFileTool, ReadFileTool, ToolRegistry, Workspace, WriteFileTool


def execute(registry: ToolRegistry, name: str, **arguments: JsonValue) -> ToolResult:
    """同步执行单次工具调用，减少文件行为测试中的样板代码。"""
    call = ToolCall(call_id="call", name=name, arguments=arguments)
    return asyncio.run(registry.execute(call))


def registry_for(root: Path) -> ToolRegistry:
    """为临时目录建立三个共享同一边界的文件工具。"""
    workspace = Workspace(root)
    return ToolRegistry(
        (ReadFileTool(workspace), WriteFileTool(workspace), EditFileTool(workspace))
    )


def test_read_write_and_unique_edit(tmp_path: Path) -> None:
    """完成创建、读取和唯一替换的正常流程。"""
    registry = registry_for(tmp_path)
    written = execute(registry, "write_file", path="src/value.py", content="x = 1\n")
    # 工具不会隐式创建目录，调用方必须先提供清楚的现有目标范围。
    assert written.is_error and "missing_parent" in written.content
    (tmp_path / "src").mkdir()
    written = execute(registry, "write_file", path="src/value.py", content="x = 1\n")
    read = execute(registry, "read_file", path="src/value.py")
    edited = execute(registry, "edit_file", path="src/value.py", old_text="x = 1", new_text="x = 2")
    assert not written.is_error and "bytes=6" in written.content
    assert read.content == "x = 1\n" and not read.is_error
    assert not edited.is_error
    assert (tmp_path / "src/value.py").read_text(encoding="utf-8") == "x = 2\n"


@pytest.mark.parametrize("old_text,count", [("missing", 0), ("same", 2)])
def test_ambiguous_edit_preserves_original(tmp_path: Path, old_text: str, count: int) -> None:
    """零匹配和多匹配都拒绝写入，并报告实际匹配次数。"""
    path = tmp_path / "value.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    before = path.read_bytes()
    result = execute(
        registry_for(tmp_path), "edit_file", path="value.txt", old_text=old_text, new_text="new"
    )
    assert result.is_error and f"found {count}" in result.content
    assert path.read_bytes() == before


def test_parent_and_absolute_paths_are_rejected(tmp_path: Path) -> None:
    """拒绝父目录片段和绝对路径，且不修改工作区外文件。"""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("safe", encoding="utf-8")
    registry = registry_for(tmp_path)
    parent = execute(registry, "write_file", path="../outside.txt", content="changed")
    absolute = execute(registry, "read_file", path=str(outside))
    assert parent.is_error and absolute.is_error
    assert outside.read_text(encoding="utf-8") == "safe"


def test_symlink_components_are_rejected(tmp_path: Path) -> None:
    """拒绝指向工作区内外的符号链接，避免文件身份在校验后变化。"""
    target = tmp_path / "target"
    target.mkdir()
    (target / "value.txt").write_text("safe", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("当前文件系统不允许创建符号链接")
    result = execute(registry_for(tmp_path), "read_file", path="link/value.txt")
    assert result.is_error and "symbolic links" in result.content


@pytest.mark.parametrize(
    "data,error",
    [(b"a\x00b", "binary_file"), (b"\xff\xfe", "encoding_error"), (b"abcdef", "file_too_large")],
)
def test_binary_encoding_and_size_errors_are_distinct(
    tmp_path: Path, data: bytes, error: str
) -> None:
    """为二进制、非法 UTF-8 和超过调用上限返回不同错误。"""
    (tmp_path / "data.txt").write_bytes(data)
    result = execute(registry_for(tmp_path), "read_file", path="data.txt", max_bytes=5)
    assert result.is_error and error in result.content


def test_invalid_arguments_cannot_truncate_existing_file(tmp_path: Path) -> None:
    """未知参数在工具执行前被注册层拒绝，原文件保持不变。"""
    path = tmp_path / "value.txt"
    path.write_text("original", encoding="utf-8")
    result = execute(
        registry_for(tmp_path),
        "write_file",
        path="value.txt",
        content="changed",
        unexpected=True,
    )
    assert result.is_error and result.content.startswith("invalid_arguments:")
    assert path.read_text(encoding="utf-8") == "original"
