"""保持仓库中面向学习的模块、类、函数和分支说明完整。"""

import ast
import io
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = (
    PROJECT_ROOT / "src" / "codepilot",
    PROJECT_ROOT / "tests",
    PROJECT_ROOT / "examples" / "fixture_repo",
)


def test_every_module_class_function_and_method_has_a_docstring() -> None:
    """报告所有缺少 docstring 的模块、类、函数和方法。"""
    missing: list[str] = []
    for root in PYTHON_ROOTS:
        # 对路径排序，使失败输出在不同文件系统和 Python 版本间保持稳定。
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if ast.get_docstring(tree, clean=False) is None:
                location = path.relative_to(PROJECT_ROOT)
                missing.append(f"{location}:1 <module>")
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 类和嵌套辅助函数也是学习材料，因此同样纳入检查。
                    if ast.get_docstring(node, clean=False) is None:
                        location = path.relative_to(PROJECT_ROOT)
                        missing.append(f"{location}:{node.lineno} {node.name}")
    assert not missing, "缺少 docstring：\n" + "\n".join(missing)


def test_docstrings_and_comments_contain_chinese() -> None:
    """确保已有 docstring 和行注释使用中文说明。"""
    non_chinese: list[str] = []
    for root in PYTHON_ROOTS:
        # 同时检查模块、类和函数说明，避免局部中文而文件顶部仍是英文。
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            documented_nodes = [tree, *ast.walk(tree)]
            for node in documented_nodes:
                if not isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                docstring = ast.get_docstring(node, clean=False)
                if docstring is not None and not any(
                    "\u4e00" <= char <= "\u9fff" for char in docstring
                ):
                    location = path.relative_to(PROJECT_ROOT)
                    line = getattr(node, "lineno", 1)
                    non_chinese.append(f"{location}:{line} docstring")

            # tokenize 可以区分真正的行注释和字符串中的 ``#`` 字符。
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for token in tokens:
                if token.type == tokenize.COMMENT and not any(
                    "\u4e00" <= char <= "\u9fff" for char in token.string
                ):
                    location = path.relative_to(PROJECT_ROOT)
                    non_chinese.append(f"{location}:{token.start[0]} comment")
    assert not non_chinese, "非中文 docstring 或注释：\n" + "\n".join(non_chinese)
