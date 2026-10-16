"""验证权限预设、拒绝优先、审批取消和审计记录。"""

import asyncio
from pathlib import Path

from pydantic import JsonValue

from codepilot.contracts import ToolCall
from codepilot.contracts.models import ToolResult
from codepilot.permissions import (
    PermissionedExecutor,
    PermissionEngine,
    PermissionPolicy,
    PermissionRule,
    ScriptedApproval,
    ask_edits_policy,
    controlled_edit_policy,
    read_only_policy,
)
from codepilot.tools import create_basic_registry
from codepilot.workspace import LocalExecutionBackend


def run(executor: PermissionedExecutor, call: ToolCall) -> ToolResult:
    """同步执行权限包装器，简化策略测试。"""
    return asyncio.run(executor.execute(call))


def call(name: str, **arguments: JsonValue) -> ToolCall:
    """构造带稳定调用 ID 的测试工具调用。"""
    return ToolCall.model_validate({"call_id": name, "name": name, "arguments": arguments})


def test_read_only_denies_writes_before_side_effect(tmp_path: Path) -> None:
    """只读预设允许读取并在写入前拒绝，因此目标文件不会创建。"""
    registry = create_basic_registry(tmp_path)
    executor = PermissionedExecutor(registry, PermissionEngine(read_only_policy()))
    denied = run(executor, call("write_file", path="value.txt", content="blocked"))
    assert denied.is_error and "permission_denied" in denied.content
    assert not (tmp_path / "value.txt").exists()
    assert executor.audits[0].outcome == "denied"


def test_ask_edit_uses_full_call_and_cancel_has_no_side_effect(tmp_path: Path) -> None:
    """逐次审批记录完整参数，取消后工具不执行。"""
    registry = create_basic_registry(tmp_path)
    approval = ScriptedApproval((False, True))
    executor = PermissionedExecutor(registry, PermissionEngine(ask_edits_policy()), approval)
    first = run(executor, call("write_file", path="value.txt", content="first"))
    assert first.is_error and "permission_cancelled" in first.content
    assert not (tmp_path / "value.txt").exists()
    assert approval.requests[0].call.arguments["content"] == "first"
    second = run(executor, call("write_file", path="value.txt", content="second"))
    assert not second.is_error and (tmp_path / "value.txt").read_text() == "second"
    assert [audit.outcome for audit in executor.audits] == ["cancelled", "approved"]


def test_controlled_edit_allows_files_but_asks_shell(tmp_path: Path) -> None:
    """受控编辑预设允许文件工具，但 Shell 必须经过审批。"""
    registry = create_basic_registry(tmp_path)
    approval = ScriptedApproval((False,))
    executor = PermissionedExecutor(registry, PermissionEngine(controlled_edit_policy()), approval)
    write = run(executor, call("write_file", path="value.txt", content="ok"))
    shell = run(executor, call("shell", command="printf unsafe"))
    assert not write.is_error and shell.is_error and "permission_cancelled" in shell.content
    assert len(approval.requests) == 1 and approval.requests[0].tool.name == "shell"


def test_controlled_edit_auto_allows_strict_read_shell(tmp_path: Path) -> None:
    """受控编辑预设自动放行严格白名单查询，并留下可解释审计原因。"""
    registry = create_basic_registry(tmp_path)
    approval = ScriptedApproval(())
    executor = PermissionedExecutor(registry, PermissionEngine(controlled_edit_policy()), approval)
    result = run(executor, call("shell", command="pwd"))
    assert not result.is_error
    assert not approval.requests
    assert executor.audits[0].reason == "safe_process_read"


def test_compound_shell_still_requires_approval(tmp_path: Path) -> None:
    """复合命令即使表面只读也不能绕过审批白名单。"""
    registry = create_basic_registry(tmp_path)
    approval = ScriptedApproval((False,))
    executor = PermissionedExecutor(registry, PermissionEngine(controlled_edit_policy()), approval)
    result = run(executor, call("shell", command="ls -la && git status --short"))
    assert result.is_error and "permission_cancelled" in result.content
    assert approval.requests


def test_container_mode_rejects_local_backend(tmp_path: Path) -> None:
    """策略要求容器时，本地注册表即使有允许规则也必须拒绝。"""
    registry = create_basic_registry(tmp_path)
    policy = read_only_policy().model_copy(update={"container_mode": True})
    executor = PermissionedExecutor(registry, PermissionEngine(policy))
    result = run(executor, call("read_file", path="missing.txt"))
    assert result.is_error and "container_backend_unavailable" in result.content
    assert executor.audits[0].reason == "container_backend_unavailable"


def test_container_mode_accepts_explicit_container_backend(tmp_path: Path) -> None:
    """执行链明确提供容器能力时，容器要求不应覆盖原有允许规则。"""

    class ContainerProbeBackend(LocalExecutionBackend):
        """仅用于验证能力发现，并复用 Local 的工具契约。"""

        @property
        def is_container(self) -> bool:
            """向权限层声明测试执行链具有容器边界。"""
            return True

    async def scenario() -> None:
        """启动能力探针并通过权限包装器执行允许的读取。"""
        (tmp_path / "value.txt").write_text("ok", encoding="utf-8")
        backend = ContainerProbeBackend(tmp_path)
        policy = read_only_policy().model_copy(update={"container_mode": True})
        executor = PermissionedExecutor(backend, PermissionEngine(policy))
        async with backend:
            result = await executor.execute(call("read_file", path="value.txt"))
        assert not result.is_error
        assert result.content.startswith("ok")

    asyncio.run(scenario())


def test_path_rule_and_deny_priority_are_explicit(tmp_path: Path) -> None:
    """路径规则只放行指定前缀，重叠规则中的 deny 始终优先。"""
    (tmp_path / "safe").mkdir()
    (tmp_path / "safe/blocked").mkdir()
    policy = PermissionPolicy(
        name="path-test",
        default_decision="deny",
        rules=(
            PermissionRule(decision="allow", effects=("write",), path_prefix="safe"),
            PermissionRule(decision="deny", effects=("write",), path_prefix="safe/blocked"),
        ),
    )
    executor = PermissionedExecutor(create_basic_registry(tmp_path), PermissionEngine(policy))
    allowed = run(executor, call("write_file", path="safe/value.txt", content="ok"))
    denied = run(executor, call("write_file", path="safe/blocked/value.txt", content="no"))
    assert not allowed.is_error
    assert denied.is_error and "permission_denied" in denied.content
    assert not (tmp_path / "safe/blocked/value.txt").exists()
