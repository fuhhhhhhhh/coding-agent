"""验收持久会话的连续历史、跨进程检查与未知副作用阻断。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import codepilot.app.service as service
from codepilot.app import CodePilotApplication
from codepilot.config import ProductConfig
from codepilot.contracts import Message, ToolCall
from codepilot.providers.fake import FakeProvider
from codepilot.sessions.store import (
    ReconciliationRequired,
    SessionStore,
    manifest_changes,
    workspace_manifest,
)
from codepilot.ui.cli import main


def test_session_continues_history_without_replaying_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多次产品运行共享历史并刷新规则，旧工具不会再次执行。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    config = ProductConfig(state_directory=state, permission="controlled-edit")
    calls = 0
    captured: list[FakeProvider] = []

    def factory(*args: object, **kwargs: object) -> service._ProviderHandle:
        """第一任务创建文件，第二任务只回答并检查收到旧结果。"""
        nonlocal calls
        calls += 1
        script = (
            (
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id="write-once",
                            name="write_file",
                            arguments={"path": "answer", "content": "42"},
                        ),
                    ),
                ),
                Message(role="assistant", content="已创建 42"),
            )
            if calls == 1
            else (Message(role="assistant", content="旧任务创建了 42"),)
        )
        provider = FakeProvider(script)
        captured.append(provider)
        return service._ProviderHandle(provider)

    monkeypatch.setattr(service, "_create_provider", factory)
    first = asyncio.run(
        CodePilotApplication(config, workspace).run("创建答案", session_id="conversation")
    )
    second = asyncio.run(
        CodePilotApplication(config, workspace).run("刚才结果是什么", session_id="conversation")
    )
    assert first.report.tool_calls == 1 and second.report.tool_calls == 0
    assert second.report.reason == "completed"
    assert (workspace / "answer").read_text() == "42"
    with SessionStore(state, "conversation", create=False) as stored:
        history = stored.history()
        assert len([item for item in history if item.role == "user"]) == 2
        assert len([item for item in history if item.tool_result]) == 1
        assert stored.get("request_attempts") == 3
        assert stored.get("status") == "ready"
        expected = [message.model_dump(mode="json") for message in history]
        stored.db.execute("DELETE FROM state")
        stored.db.commit()
        stored.rebuild()
        stored._messages = None
        assert [message.model_dump(mode="json") for message in stored.history()] == expected
    assert (state / "conversation/session.db").stat().st_mode & 0o777 == 0o600


def test_run_records_private_workspace_baseline_and_owned_changes(tmp_path: Path) -> None:
    """每次任务保存私有基线，并独立记录新增、修改和删除路径。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "user.txt").write_text("before", encoding="utf-8")
    baseline = workspace_manifest(workspace)
    (workspace / "user.txt").write_text("after", encoding="utf-8")
    (workspace / "agent.txt").write_text("new", encoding="utf-8")
    changes = manifest_changes(baseline, workspace_manifest(workspace))
    assert {item["path"] for item in changes} == {"user.txt", "agent.txt"}
    assert {item["kind"] for item in changes} == {"modified", "added"}

    state = tmp_path / "state"
    with SessionStore(state, "baseline") as session:
        session.begin("run-1", (Message(role="user", content="检查"),), workspace)
        assert (session.directory / "workspace-baseline.json").stat().st_mode & 0o777 == 0o600
        assert session.get("workspace_baseline")["count"] == 2


def test_resume_cli_in_another_process_and_external_change_requires_reconciliation(
    tmp_path: Path,
) -> None:
    """安装入口可跨进程重开，外部文件变更必须显式接受后才继续。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "config.toml"
    config.write_text('permission="read-only"\n[provider]\nkind="fake"\nmodel="fake-session"\n')
    args = [
        sys.executable,
        "-m",
        "codepilot.ui.cli",
        "--workspace",
        str(workspace),
        "--config",
        str(config),
        "--state-directory",
        str(tmp_path / "state"),
    ]
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "empty")}
    first = subprocess.run(
        [*args, "run", "first", "--session-id", "across"], capture_output=True, env=env
    )
    assert first.returncode == 0, first.stderr
    second = subprocess.run([*args, "resume", "across", "second"], capture_output=True, env=env)
    assert second.returncode == 0, second.stderr
    (workspace / "user.txt").write_text("preserve")
    blocked = subprocess.run([*args, "resume", "across", "third"], capture_output=True, env=env)
    assert blocked.returncode == 2 and b"workspace changed" in blocked.stderr
    accepted = subprocess.run(
        [*args, "sessions", "reconcile", "across", "--accept-workspace"],
        capture_output=True,
        env=env,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert (
        subprocess.run(
            [*args, "resume", "across", "third"], capture_output=True, env=env
        ).returncode
        == 0
    )
    assert (workspace / "user.txt").read_text() == "preserve"


def test_crash_after_file_side_effect_never_replays_unknown_call(tmp_path: Path) -> None:
    """真实进程在写入后、结果提交前退出，重开不能自动再执行一次写入。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    script = '''
import asyncio, os, sys
from pathlib import Path
from codepilot.app import CodePilotApplication
from codepilot.config import ProductConfig
from codepilot.contracts import Message, ToolCall
from codepilot.providers.fake import FakeProvider
import codepilot.app.service as service
from codepilot.sessions.store import SessionStore
original = SessionStore.observe
def crash(self, observation):
    """在文件已经写入但结果尚未提交时模拟控制器掉电。"""
    if observation.kind == "tool_result":
        os._exit(71)
    original(self, observation)
def factory(*args, **kwargs):
    """确定性请求一次追加操作，其重复副作用可以独立核对。"""
    return service._ProviderHandle(FakeProvider((Message(role="assistant", tool_calls=(ToolCall(
        call_id="write", name="write_file", arguments={"path":"counter", "content":"once"}),)),)))
SessionStore.observe = crash
service._create_provider = factory
config = ProductConfig(state_directory=Path(sys.argv[2]), permission="controlled-edit")
asyncio.run(CodePilotApplication(config, Path(sys.argv[1])).run("write", session_id="crash"))
'''
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(workspace), str(state)], capture_output=True
    )
    assert crashed.returncode == 71, crashed.stderr
    assert (workspace / "counter").read_text() == "once"
    app = CodePilotApplication(ProductConfig(state_directory=state), workspace)
    with pytest.raises(ReconciliationRequired, match="previous run"):
        asyncio.run(app.run("continue", session_id="crash"))
    with SessionStore(state, "crash", create=False) as stored:
        stored.reconcile(workspace)
        history = stored.history()
        assert history[-1].tool_result is not None and history[-1].tool_result.is_error
        assert "Do not replay" in history[-1].tool_result.content
    assert (workspace / "counter").read_text() == "once"


def test_session_lock_schema_and_cli_metadata_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """独占写锁、未知 Schema、路径穿越与默认无正文查看均保持边界。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    with SessionStore(state, "locked") as first:
        first.commit("test", {}, status="ready", private_text="synthetic-private")
        with pytest.raises(ValueError, match="already in use"):
            with SessionStore(state, "locked"):
                pass
    with pytest.raises(ValueError, match="invalid"):
        SessionStore(state, "../escape")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "--state-directory",
                str(state),
                "sessions",
                "show",
                "locked",
            ]
        )
        == 0
    )
    assert "synthetic-private" not in capsys.readouterr().out
    with SessionStore(state, "locked") as stored:
        stored.db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="unsupported"):
        SessionStore(state, "locked", create=False)


def test_private_storage_symlink_and_sqlite_failure_do_not_execute_tools(tmp_path: Path) -> None:
    """不安全存储和事务失败不会降级为无持久记录的工具执行。"""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        SessionStore(link, "unsafe")
    with SessionStore(tmp_path / "state", "readonly") as stored:
        stored.db.execute("PRAGMA query_only=ON")
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            stored.begin("run", (Message(role="user", content="must not execute"),))
    assert not (tmp_path / "state/readonly/side-effect").exists()


def test_retention_and_deletion_preserve_unknown_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """过期会话不能继续，prune 仅删除安全结束会话而保留未知副作用。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name, status in (("expired", "ready"), ("unknown", "needs_reconciliation")):
        with SessionStore(state, name) as stored:
            stored.commit("fixture", {}, status=status, expires_at=0)
    with SessionStore(state, "expired") as stored:
        with pytest.raises(ValueError, match="expired"):
            stored.check(workspace, "local:False")
    assert (
        main(["--workspace", str(workspace), "--state-directory", str(state), "sessions", "prune"])
        == 0
    )
    assert not (state / "expired").exists()
    assert (state / "unknown/session.db").exists()


def test_session_cleanup_never_deletes_retained_code_or_patch(tmp_path: Path) -> None:
    """过期和显式会话删除都必须保护仍保留的任务代码与交付补丁。"""
    import json

    with SessionStore(tmp_path, "retained") as stored:
        stored.commit("fixture", {}, status="ready")
        task = stored.directory / "task/run"
        task.mkdir(parents=True)
        (task / "workspace.json").write_text(json.dumps({"state": "changes_retained"}))
        (task / "changes.patch").write_text("keep")
        with pytest.raises(ValueError, match="refusing deletion"):
            stored.ensure_deletable()
        assert (task / "changes.patch").read_text() == "keep"


def test_rebuilt_index_cannot_hide_unknown_run(tmp_path: Path) -> None:
    """索引落后或被清空时根据事务事件恢复 running，不能误判新会话。"""
    with SessionStore(tmp_path / "state", "unknown") as stored:
        stored.begin("run", (Message(role="user", content="未完成"),))
        stored.db.execute("DELETE FROM state")
        stored.db.commit()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with SessionStore(tmp_path / "state", "unknown", create=False) as stored:
        assert stored.get("status") == "running"
        with pytest.raises(ReconciliationRequired):
            stored.check(workspace, "local:False")


def test_continuous_session_uses_failed_test_then_repairs_and_rechecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实失败测试进入后续任务历史，修复后由控制器独立复验且保留用户改动。"""
    from codepilot.permissions import ScriptedApproval

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "user.txt").write_text("keep")
    (workspace / "answer.py").write_text("answer = 0\n")
    command = f'{sys.executable} -c "from answer import answer; assert answer == 42"'
    invocation = 0

    def factory(*args: object, **kwargs: object) -> service._ProviderHandle:
        """固定三个任务：发现失败、修复复测、依据历史回答。"""
        nonlocal invocation
        invocation += 1
        script: tuple[Message, ...]
        if invocation == 1:
            script = (
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id="failed-test", name="shell", arguments={"command": command}
                        ),
                    ),
                ),
                Message(role="assistant", content="测试失败，尚未修复"),
            )
        elif invocation == 2:
            script = (
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id="read-answer", name="read_file", arguments={"path": "answer.py"}
                        ),
                    ),
                ),
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id="fix",
                            name="edit_file",
                            arguments={
                                "path": "answer.py",
                                "old_text": "answer = 0",
                                "new_text": "answer = 42",
                            },
                        ),
                    ),
                ),
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id="passed-test", name="shell", arguments={"command": command}
                        ),
                    ),
                ),
                Message(role="assistant", content="修复后测试成功"),
            )
        else:
            script = (Message(role="assistant", content="先失败，后修复并复测"),)
        return service._ProviderHandle(FakeProvider(script))

    monkeypatch.setattr(service, "_create_provider", factory)
    config = ProductConfig(state_directory=tmp_path / "state", permission="controlled-edit")
    results = []
    for task in ("运行测试查找错误", "根据刚才失败修复并复测", "解释刚才验证了什么"):
        results.append(
            asyncio.run(
                CodePilotApplication(
                    config, workspace, approval=ScriptedApproval((True, True))
                ).run(task, session_id="repair")
            )
        )
    assert all(result.report.reason == "completed" for result in results)
    tool_results = [
        message.tool_result for message in results[-1].report.messages if message.tool_result
    ]
    assert tool_results[0].is_error and not tool_results[-1].is_error
    with SessionStore(tmp_path / "state", "repair", create=False) as stored:
        facts = stored.facts_view(workspace)
        assert facts["goal_revision"] == 3
        executions = {item["call_id"]: item for item in facts["executions"]}
        assert executions["failed-test"]["exit_code"] != 0
        assert executions["failed-test"]["current"] is False
        assert executions["passed-test"]["exit_code"] == 0
        assert executions["passed-test"]["current"] is True
        assert executions["passed-test"]["independent_verdict"] == "not_run"
        (workspace / "user.txt").write_text("later user edit")
        assert not any(item["current"] for item in stored.facts_view(workspace)["executions"])
    assert (workspace / "user.txt").read_text() == "later user edit"
    verifier = subprocess.run(
        [sys.executable, "-c", "from answer import answer; assert answer == 42"],
        cwd=workspace,
        capture_output=True,
    )
    assert verifier.returncode == 0, verifier.stderr


def test_reconcile_refuses_live_owned_process_and_corrupt_event_is_not_discarded(
    tmp_path: Path,
) -> None:
    """协调独立核验进程启动身份；损坏已提交事件不静默截断。"""
    import json

    from codepilot.sessions.store import process_identity

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with SessionStore(tmp_path / "state", "owned") as stored:
            stored.check(workspace, "local:False")
            stored.resource("process", {"pid": process.pid, "start": process_identity(process.pid)})
            with pytest.raises(ValueError, match="still active"):
                stored.reconcile(workspace)
    finally:
        process.terminate()
        process.wait(timeout=5)
    with SessionStore(tmp_path / "state", "corrupt") as stored:
        stored.commit("committed", {}, status="running")
        stored.db.execute("UPDATE events SET payload='{' WHERE seq=1")
        stored.db.commit()
    with pytest.raises(json.JSONDecodeError):
        with SessionStore(tmp_path / "state", "corrupt", create=False):
            pass


def test_shared_request_limit_stops_with_one_terminal_event(tmp_path: Path) -> None:
    """共享额度拒绝后正常形成 session_limit 终态，不把未发送请求计为已用。"""
    from codepilot.config import SessionConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    config = ProductConfig(
        state_directory=state, permission="read-only", sessions=SessionConfig(max_requests=1)
    )
    result = asyncio.run(CodePilotApplication(config, workspace).run("budget", session_id="budget"))
    assert result.report.reason == "session_limit" and result.report.steps == 1
    assert len([event for event in result.report.observations if event.kind == "run_stopped"]) == 1
    with SessionStore(state, "budget", create=False) as stored:
        assert stored.get("request_attempts") == 1


def test_read_only_inspection_does_not_block_active_session_writer(tmp_path: Path) -> None:
    """活跃会话持有执行锁时仍可查看元数据，读操作不抢占或修改执行权。"""
    root = tmp_path / "state"
    with SessionStore(root, "active") as writer:
        writer.begin("run", (Message(role="user", content="private"),))
        snapshot = SessionStore.inspect(root, "active")
        assert snapshot["status"] == "running" and "messages" not in snapshot
        body = SessionStore.inspect(root, "active", messages=True)
        assert body["messages"][0]["content"] == "private"
        assert writer.get("status") == "running"


def test_known_secret_is_redacted_before_json_escaping(tmp_path: Path) -> None:
    """已知秘密含特殊字符时也不能因 JSON 编码绕过私有事件脱敏。"""
    secret = 'synthetic"secret\\with-escape'
    with SessionStore(tmp_path / "state", "redacted") as stored:
        stored.secrets = (secret,)
        stored.commit(
            "fixture", {}, messages=[Message(role="user", content=secret).model_dump(mode="json")]
        )
    snapshot = SessionStore.inspect(tmp_path / "state", "redacted", messages=True)
    assert snapshot["messages"][0]["content"] == "<redacted>"
