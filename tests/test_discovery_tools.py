"""验证 glob 与文本搜索的排除规则、稳定输出和限制。"""

import asyncio
from pathlib import Path

from codepilot.contracts import ToolCall, ToolResult
from codepilot.tools import ToolRegistry, create_basic_registry


def execute(registry: ToolRegistry, name: str, arguments: dict[str, object]) -> ToolResult:
    """执行一个发现类工具调用并返回结构化结果。"""
    call = ToolCall.model_validate({"call_id": "call", "name": name, "arguments": arguments})
    return asyncio.run(registry.execute(call))


def test_glob_is_sorted_limited_and_excludes_cache_directories(tmp_path: Path) -> None:
    """glob 只返回稳定排序的安全路径，并明确报告数量截断。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/b.py").write_text("b", encoding="utf-8")
    (tmp_path / "src/a.py").write_text("a", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/secret.py").write_text("secret", encoding="utf-8")
    result = execute(
        create_basic_registry(tmp_path), "glob", {"pattern": "**/*.py", "max_results": 1}
    )
    assert not result.is_error
    assert result.content.splitlines()[0] == "src/a.py"
    assert "truncated" in result.content and "secret.py" not in result.content


def test_search_reports_matches_and_skipped_files(tmp_path: Path) -> None:
    """搜索返回文件与行号，同时汇总无法作为 UTF-8 文本读取的文件。"""
    (tmp_path / "a.py").write_text("Alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("NEEDLE too\n", encoding="utf-8")
    (tmp_path / "binary.py").write_bytes(b"needle\x00binary")
    result = execute(
        create_basic_registry(tmp_path),
        "search",
        {"query": "needle", "pattern": "*.py", "case_sensitive": False},
    )
    assert not result.is_error
    assert "a.py:2:needle here" in result.content
    assert "b.py:1:NEEDLE too" in result.content
    assert "skipped unreadable files: 1" in result.content


def test_search_and_glob_reject_parent_patterns(tmp_path: Path) -> None:
    """发现工具拒绝包含父目录片段的模式。"""
    registry = create_basic_registry(tmp_path)
    glob_result = execute(registry, "glob", {"pattern": "../*.py"})
    search_result = execute(registry, "search", {"query": "x", "pattern": "../*"})
    assert glob_result.is_error and "invalid_pattern" in glob_result.content
    assert search_result.is_error and "invalid_pattern" in search_result.content


def test_search_stops_at_result_limit(tmp_path: Path) -> None:
    """达到结果数量限制时停止遍历并留下截断标记。"""
    (tmp_path / "many.txt").write_text("x\nx\nx\n", encoding="utf-8")
    result = execute(
        create_basic_registry(tmp_path),
        "search",
        {"query": "x", "pattern": "*.txt", "max_results": 2},
    )
    assert result.content.count("many.txt:") == 2
    assert "truncated" in result.content
