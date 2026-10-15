"""演示两个任务 worktree 隔离、补丁导出和干净起点重放。"""

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from codepilot.workspace.git import GitTaskWorkspace


def _git(root: Path, *arguments: str) -> str:
    """在演示仓库运行固定 Git 命令，失败时终止演示。"""
    process = subprocess.run(
        ("git", *arguments), cwd=root, capture_output=True, check=True, text=True
    )
    return process.stdout


def _create_repository(root: Path) -> None:
    """创建一个只有固定基线文件的临时 Git 仓库。"""
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "CodePilot Demo")
    _git(root, "config", "user.email", "codepilot@example.invalid")
    (root / "value.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "value.txt")
    _git(root, "commit", "-qm", "base")


async def run_demo() -> dict[str, object]:
    """运行两项隔离修改，并返回不依赖模型随机性的验收摘要。"""
    with tempfile.TemporaryDirectory(prefix="codepilot-m11-") as directory:
        root = Path(directory)
        source = root / "repository"
        _create_repository(source)
        managers = tuple(
            GitTaskWorkspace(
                source,
                root / "state" / name,
                session_id="session-demo",
                task_id=f"task-{name}",
                run_id=f"run-{name}",
            )
            for name in ("left", "right")
        )
        workspaces = await asyncio.gather(
            *(asyncio.to_thread(manager.prepare) for manager in managers)
        )
        for workspace, content in zip(workspaces, ("left\n", "right\n"), strict=True):
            workspace.joinpath("value.txt").write_text(content, encoding="utf-8")
        deliveries = await asyncio.gather(*(manager.finish() for manager in managers))
        return {
            "source_unchanged": source.joinpath("value.txt").read_text() == "base\n",
            "task_contents": [
                workspace.joinpath("value.txt").read_text() for workspace in workspaces
            ],
            "replay_statuses": [delivery.replay_status for delivery in deliveries],
            "patches_distinct": deliveries[0].patch_sha256 != deliveries[1].patch_sha256,
            "source_clean": _git(source, "status", "--porcelain") == "",
        }


def main() -> int:
    """执行演示并输出一条便于保存证据的 JSON。"""
    print(json.dumps(asyncio.run(run_demo()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
