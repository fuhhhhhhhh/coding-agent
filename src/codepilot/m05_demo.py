"""演示 M05 的只读、逐次审批、受控编辑和未实现容器模式拒绝。"""

import asyncio
import json
import tempfile
from pathlib import Path

from codepilot.contracts import ToolCall, ToolResult
from codepilot.permissions import (
    PermissionedExecutor,
    PermissionEngine,
    ScriptedApproval,
    ask_edits_policy,
    read_only_policy,
)
from codepilot.tools import create_basic_registry


def execute(executor: PermissionedExecutor, name: str, **arguments: object) -> ToolResult:
    """同步执行一个权限包装器调用并返回 ToolResult。"""
    return asyncio.run(
        executor.execute(
            ToolCall.model_validate({"call_id": name, "name": name, "arguments": arguments})
        )
    )


def report(name: str, result: ToolResult, executor: PermissionedExecutor) -> None:
    """打印结果和不含敏感正文的权限审计摘要。"""
    print(
        json.dumps(
            {
                "scenario": name,
                "error": result.is_error,
                "content": result.content,
                "audit": [audit.model_dump() for audit in executor.audits],
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    """在临时目录运行四个 M05 权限场景。"""
    with tempfile.TemporaryDirectory(prefix="codepilot-m05-") as temporary:
        root = Path(temporary)
        read_only = PermissionedExecutor(
            create_basic_registry(root), PermissionEngine(read_only_policy())
        )
        report(
            "read_only_denied", execute(read_only, "write_file", path="x", content="no"), read_only
        )

        cancelled = PermissionedExecutor(
            create_basic_registry(root),
            PermissionEngine(ask_edits_policy()),
            ScriptedApproval((False,)),
        )
        report(
            "approval_cancelled",
            execute(cancelled, "write_file", path="x", content="no"),
            cancelled,
        )

        approved = PermissionedExecutor(
            create_basic_registry(root),
            PermissionEngine(ask_edits_policy()),
            ScriptedApproval((True,)),
        )
        report(
            "approval_granted", execute(approved, "write_file", path="x", content="yes"), approved
        )

        container = PermissionedExecutor(
            create_basic_registry(root),
            PermissionEngine(read_only_policy().model_copy(update={"container_mode": True})),
        )
        report("container_mode_rejected", execute(container, "read_file", path="x"), container)


if __name__ == "__main__":
    main()
