"""通过实际包源码守护 M01 核心层不依赖供应商 SDK 的边界。"""

import ast
import sys
from pathlib import Path

import codepilot.contracts


def test_contracts_only_import_stdlib_pydantic_and_contracts() -> None:
    """确保供应商 SDK 和无关应用层不会进入核心契约。"""
    package_file = codepilot.contracts.__file__
    assert package_file is not None
    package_dir = Path(package_file).parent
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name}: use explicit imports to expose dependencies"
                assert node.module is not None
                modules = [node.module]
            for module in modules:
                assert (
                    module.split(".")[0] in sys.stdlib_module_names
                    or module.split(".")[0] == "pydantic"
                    or module == "codepilot.contracts"
                    or module.startswith("codepilot.contracts.")
                ), f"{path.name}: core contracts must not depend on {module}"
