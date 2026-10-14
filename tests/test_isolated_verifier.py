"""验证候选补丁重建、隐藏材料注入和防篡改边界。"""

import asyncio
import sys
from pathlib import Path

from codepilot.evals import IsolatedVerifier
from codepilot.workspace import LocalExecutionBackend


def _backend(root: Path) -> LocalExecutionBackend:
    """为独立验证临时目录创建本地契约后端。"""
    return LocalExecutionBackend(root)


def test_isolated_verifier_applies_candidate_then_injects_hidden_material(tmp_path: Path) -> None:
    """验证环境必须从可信源重建，并在候选完成后才加入验收脚本。"""

    async def scenario() -> None:
        """候选修复源文件后，由未曾暴露给 Agent 的脚本检查结果。"""
        source = tmp_path / "source"
        candidate = tmp_path / "candidate"
        hidden = tmp_path / "hidden"
        source.mkdir()
        candidate.mkdir()
        hidden.mkdir()
        (source / "answer.py").write_text("VALUE = 1\n", encoding="utf-8")
        (candidate / "answer.py").write_text("VALUE = 42\n", encoding="utf-8")
        (hidden / "verify.py").write_text(
            "import runpy, sys\n"
            "value = runpy.run_path('answer.py')['VALUE']\n"
            "sys.exit(0 if value == 42 else 1)\n",
            encoding="utf-8",
        )
        verifier = IsolatedVerifier(
            source,
            hidden,
            (sys.executable, "-I", ".codepilot-validation/verify.py"),
            _backend,
        )
        result = await verifier.verify(candidate)
        assert result.passed
        assert not (candidate / ".codepilot-validation").exists()

    asyncio.run(scenario())


def test_isolated_verifier_rejects_reserved_path_tampering(tmp_path: Path) -> None:
    """候选预建隐藏验证目录时应不可评测，不能运行伪造材料。"""

    async def scenario() -> None:
        """构造占位攻击并检查验证器在执行命令前拒绝。"""
        source = tmp_path / "source"
        candidate = tmp_path / "candidate"
        hidden = tmp_path / "hidden"
        source.mkdir()
        candidate.mkdir()
        hidden.mkdir()
        (source / "answer.py").write_text("VALUE = 1\n", encoding="utf-8")
        (candidate / "answer.py").write_text("VALUE = 42\n", encoding="utf-8")
        forged = candidate / ".codepilot-validation"
        forged.mkdir()
        (forged / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (hidden / "verify.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        verifier = IsolatedVerifier(
            source,
            hidden,
            (sys.executable, ".codepilot-validation/verify.py"),
            _backend,
        )
        result = await verifier.verify(candidate)
        assert result.status == "error"
        assert result.detail == "isolated_verifier_error: ValueError"

    asyncio.run(scenario())


def test_isolated_verifier_rejects_candidate_symlink(tmp_path: Path) -> None:
    """候选符号链接不能让控制器在应用补丁时读取工作区外文件。"""

    async def scenario() -> None:
        """把候选文件链接到外部并检查安全失败。"""
        source = tmp_path / "source"
        candidate = tmp_path / "candidate"
        hidden = tmp_path / "hidden"
        source.mkdir()
        candidate.mkdir()
        hidden.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (candidate / "leak.txt").symlink_to(outside)
        (hidden / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        verifier = IsolatedVerifier(
            source,
            hidden,
            (sys.executable, ".codepilot-validation/verify.py"),
            _backend,
        )
        result = await verifier.verify(candidate)
        assert result.status == "error"

    asyncio.run(scenario())
