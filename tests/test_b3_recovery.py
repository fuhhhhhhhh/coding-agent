"""验收迁移回滚、索引快照、控制器事实、上下文撤回与定价归属。"""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from codepilot.config import ProviderConfig
from codepilot.context import ContextArtifactStore, ContextLimits, ContextManager, ModelSummarizer
from codepilot.contracts import Message
from codepilot.contracts.stream import Usage
from codepilot.providers.capabilities import provider_profile
from codepilot.providers.fake import FakeProvider
from codepilot.sessions.context import context_command
from codepilot.sessions.migrations import maintain_database
from codepilot.sessions.store import SessionStore
from codepilot.tools import create_basic_registry


def legacy_database(root: Path) -> Path:
    """创建可由旧程序读取的真实 v1 库，保存历史和 running 状态。"""
    with SessionStore(root, "legacy") as stored:
        stored.begin("old-run", (Message(role="user", content="保留历史"),))
        stored.db.execute("DROP TABLE snapshots")
        stored.db.execute("PRAGMA user_version=1")
    return root / "legacy"


def test_migration_backs_up_before_upgrade_and_preserves_unknown(tmp_path: Path) -> None:
    """升级前备份能独立读取，迁移不清除 running 和未知执行。"""
    directory = legacy_database(tmp_path)
    with pytest.raises(ValueError, match="sessions migrate"):
        SessionStore(tmp_path, "legacy", create=False)
    result = maintain_database(directory, migrate=True)
    assert result["schema_before"] == 1 and result["schema_after"] == 2
    backup = Path(str(result["backup"]))
    assert backup.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    with SessionStore(tmp_path, "legacy", create=False) as stored:
        assert stored.get("status") == "running"
        assert stored.history()[0].content == "保留历史"


def test_migration_sql_failure_rolls_back_version_and_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DDL 中途失败后版本和原事件不变，完整备份保留。"""
    import codepilot.sessions.migrations as migrations

    directory = legacy_database(tmp_path)
    monkeypatch.setattr(migrations, "SNAPSHOT_SQL", "CREATE TABLE events (duplicate TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        maintain_database(directory, migrate=True)
    with closing(sqlite3.connect(directory / "session.db")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert len(list((directory / "backups").glob("*.db"))) == 1


def test_backup_failure_and_live_lock_never_modify_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘故障发生在迁移前；活跃写锁不可被维护命令抢占。"""
    import codepilot.sessions.migrations as migrations

    directory = legacy_database(tmp_path)

    def fail_backup(*args: object) -> Path:
        """模拟备份磁盘不可写，禁止继续进入 DDL。"""
        raise OSError("synthetic disk full")

    monkeypatch.setattr(migrations, "backup_database", fail_backup)
    with pytest.raises(OSError):
        maintain_database(directory, migrate=True)
    with closing(sqlite3.connect(directory / "session.db")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    with SessionStore(tmp_path, "active"):
        with pytest.raises(ValueError, match="already in use"):
            maintain_database(tmp_path / "active", migrate=True)


@pytest.mark.parametrize(
    "field,value,reason",
    [("state", "{}", "digest mismatch"), ("sequence", 999, "coverage mismatch")],
)
def test_snapshot_replays_tail_and_rejects_state_or_offset_corruption(
    tmp_path: Path, field: str, value: str | int, reason: str
) -> None:
    """覆盖点之后的 running 不能被旧快照遮盖，损坏快照必须停止。"""
    with SessionStore(tmp_path, "snapshot") as stored:
        stored.commit("first", {}, status="ready")
        stored.save_snapshot()
        stored.commit("later", {}, status="running")
        stored.db.execute("DELETE FROM state")
        stored.db.commit()
    with SessionStore(tmp_path, "snapshot", create=False) as stored:
        assert stored.get("status") == "running"
        stored.db.execute(f"UPDATE snapshots SET {field}=?", (value,))
        stored.db.commit()
    with pytest.raises(ValueError, match=reason):
        with SessionStore(tmp_path, "snapshot", create=False):
            pass


def test_context_reset_archives_and_disables_until_forced(tmp_path: Path) -> None:
    """撤回保留原文并暂停自动压缩，检查点损坏也能显式归档。"""
    root = tmp_path / "state"
    with SessionStore(root, "context") as stored:
        stored.begin("run", (Message(role="user", content="保留目标"),))
        stored.commit("ended", {}, status="ready")
        directory = stored.directory / "checkpoints"
        directory.mkdir()
        (directory / "latest.json").write_text('{"broken":')
    with pytest.raises(ValueError):
        context_command(root, "context", "show")
    result = context_command(root, "context", "reset")
    assert result["compaction_disabled"] and result["archived"]
    with SessionStore(root, "context", create=False) as stored:
        assert stored.history()[0].content == "保留目标"
        assert stored.get("compaction_disabled") is True
        assert not (stored.directory / "checkpoints/latest.json").exists()


@pytest.mark.parametrize("content", ['{"summary":"x","source_indices":[99]}', "not json"])
def test_structured_summary_rejects_forged_sources_or_invalid_json(content: str) -> None:
    """摘要格式或来源非法时不能降级为可信工作事实。"""
    summarizer = ModelSummarizer(
        FakeProvider((Message(role="assistant", content=content),)), structured=True
    )
    with pytest.raises(ValueError):
        asyncio.run(summarizer.summarize((Message(role="user", content="真实消息"),), 100))


def test_withdrawn_context_does_not_call_summary_behind_user(tmp_path: Path) -> None:
    """撤回后直接使用规范历史，超限明确停止而非自动重新压缩。"""
    from codepilot.context import ContextPreparationError

    limits = ContextLimits(
        max_prompt_tokens=1000,
        response_reserve_tokens=100,
        compact_trigger_tokens=700,
        compact_target_tokens=500,
    )
    manager = ContextManager(
        create_basic_registry(tmp_path),
        ContextArtifactStore(tmp_path / "artifacts"),
        limits=limits,
        allow_compaction=False,
    )
    with pytest.raises(ContextPreparationError, match="disabled"):
        asyncio.run(manager.prepare((Message(role="user", content="字" * 2000),)))


def test_provider_profile_and_cost_keep_endpoint_and_attempt_identity(tmp_path: Path) -> None:
    """相同模型名在未知站点无价格承诺，换模型后旧尝试按发送时价格结算。"""
    official = ProviderConfig(
        kind="openai-compatible", base_url="https://api.deepseek.com", model="deepseek-flash"
    )
    profile = provider_profile(official, 1_000_000)
    assert profile["input_usd_per_million"] == 0.3
    mirror = official.model_copy(update={"base_url": "https://proxy.invalid"})
    assert provider_profile(mirror, 1_000_000)["input_usd_per_million"] is None
    with pytest.raises(ValueError, match="capability"):
        provider_profile(official, 2_000_000)
    with SessionStore(tmp_path, "cost") as stored:
        stored.unit_prices = [0.3, 1.2]
        request = stored.reserve_provider_attempt(100, 100)
        stored.unit_prices = [10.0, 20.0]
        stored.settle_provider_attempt(request, Usage(input_tokens=100, output_tokens=50))
        assert float(stored.get("provider_budget")["known_cost_usd"]) == 0.00009
        assert "estimate_only" in stored.get("provider_budget")["cost_status"]


@pytest.mark.parametrize("point", ["before_model", "during_shell", "after_tool"])
def test_real_controller_crash_matrix_preserves_side_effects_without_replay(
    tmp_path: Path, point: str
) -> None:
    """控制器真实退出覆盖请求前、进程副作用中及结果落库前，恢复绝不重放。"""
    import os
    import signal
    import subprocess
    import sys
    import time

    from codepilot.sessions.store import ReconciliationRequired, process_identity

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "state"
    script = r'''
import asyncio, os, shlex, sys, time
from pathlib import Path
from codepilot.app import CodePilotApplication
from codepilot.config import ProductConfig
from codepilot.contracts import Message, ToolCall
from codepilot.permissions import ScriptedApproval
from codepilot.providers.fake import FakeProvider
from codepilot.sessions.store import SessionStore
import codepilot.app.service as service
point, workspace, root = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
observe, resource = SessionStore.observe, SessionStore.resource
def crash_observation(self, observation):
    """在真实生命周期屏障注入控制器退出，不模拟运行终态。"""
    if point == 'after_tool' and observation.kind == 'tool_result':
        os._exit(73)
    observe(self, observation)
    if point == 'before_model' and observation.kind == 'model_requested':
        os._exit(73)
def crash_resource(self, kind, identity):
    """资源先持久化，等待真实子进程写文件后再退出。"""
    resource(self, kind, identity)
    if point == 'during_shell' and kind == 'process':
        deadline = time.monotonic() + 5
        while not (workspace / 'counter').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not (workspace / 'counter').exists():
            os._exit(74)
        os._exit(73)
def factory(*args, **kwargs):
    """固定一次带可观察副作用的 Shell 调用，结果后不会再发送工具。"""
    code = "from pathlib import Path; import time; Path('counter').open('a').write('once\\n')"
    if point == 'during_shell':
        code += '; time.sleep(60)'
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
    return service._ProviderHandle(FakeProvider((Message(role='assistant', tool_calls=(
        ToolCall(call_id='effect', name='shell', arguments={'command': command}),
    )),)))
SessionStore.observe, SessionStore.resource = crash_observation, crash_resource
service._create_provider = factory
config = ProductConfig(state_directory=root, permission='controlled-edit')
asyncio.run(CodePilotApplication(config, workspace, approval=ScriptedApproval([True])).run(
    '执行一次', session_id='crash-matrix'))
'''
    controller = subprocess.run(
        [sys.executable, "-c", script, point, str(workspace), str(root)],
        capture_output=True,
        timeout=20,
        env=dict(os.environ),
    )
    assert controller.returncode == 73, controller.stderr.decode()
    pid = None
    try:
        with SessionStore(root, "crash-matrix", create=False) as stored:
            assert stored.diagnose()["automatic_replay"] is False
            with pytest.raises(ReconciliationRequired):
                stored.check(workspace, "local:False")
            if point == "during_shell":
                entry = next(item for item in stored.get("resources") if item["kind"] == "process")
                pid = entry["pid"]
                assert process_identity(pid) == entry["start"]
                with pytest.raises(ValueError, match="still active"):
                    stored.reconcile(workspace)
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while process_identity(pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
            stored.reconcile(workspace)
            stored.check(workspace, "local:False")
            if point != "before_model":
                result = stored.history()[-1].tool_result
                assert result is not None and result.is_error
                assert "Do not replay" in result.content
        counter = workspace / "counter"
        assert counter.exists() == (point != "before_model")
        if counter.exists():
            before = counter.read_bytes()
            # 新任务仅回答，独立核对文件未被恢复过程再次写入。
            config = tmp_path / "config.yaml"
            config.write_text("provider:\n  kind: fake\n  model: fake-recovery\n")
            continued = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "codepilot.ui.cli",
                    "resume",
                    "crash-matrix",
                    "回顾历史",
                    "--workspace",
                    str(workspace),
                    "--state-directory",
                    str(root),
                    "--config",
                    str(config),
                    "--jsonl",
                ],
                capture_output=True,
                timeout=20,
                env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "empty-config")},
            )
            assert continued.returncode == 0, continued.stderr.decode()
            assert counter.read_bytes() == before
    finally:
        # 只清理本测试从持久资源清单确认的进程组，失败也不能留下睡眠进程。
        if pid is not None and process_identity(pid):
            os.killpg(pid, signal.SIGKILL)


def test_database_restore_keeps_code_and_requires_reconciliation(tmp_path: Path) -> None:
    """恢复数据库前备份当前库；恢复不回滚用户文件且必须重新协调。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "state"
    with SessionStore(root, "restore") as stored:
        stored.check(workspace, "local:False")
        stored.commit("old", {}, marker="old")
        first = stored.reserve_provider_attempt(30, 30)
        stored.settle_provider_attempt(first, Usage(input_tokens=20, output_tokens=10))
    backup = maintain_database(root / "restore")
    with SessionStore(root, "restore", create=False) as stored:
        stored.commit("later", {}, marker="new")
        unknown = stored.reserve_provider_attempt(100, 20)
        # 维护命令也必须从事件重建预算，不能被损坏或清空的索引绕过。
        stored.db.execute("DELETE FROM state")
        stored.db.commit()
    (workspace / "user.txt").write_text("current user content")
    result = maintain_database(root / "restore", restore_name=Path(str(backup["backup"])).name)
    assert result["status"] == "needs_reconciliation"
    with SessionStore(root, "restore", create=False) as stored:
        assert stored.get("marker") == "old"
        assert stored.get("request_attempts") == 2
        assert stored.get("provider_budget")["input"] == 20
        assert unknown in stored.get("provider_budget")["pending"]
        assert stored.get("status") == "needs_reconciliation"
        stored.reconcile(workspace)
        stored.check(workspace, "local:False")
    assert (workspace / "user.txt").read_text() == "current user content"


def test_cli_maintenance_and_context_never_start_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """真正 CLI 可迁移、诊断和查看上下文，未接入任何模型请求。"""
    from codepilot.ui.cli import main

    root = tmp_path / "state"
    root.mkdir()
    legacy_database(root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    args = ["--workspace", str(tmp_path), "--state-directory", str(root)]
    assert main([*args, "sessions", "migrate", "legacy"]) == 0
    assert '"schema_after": 2' in capsys.readouterr().out
    assert main([*args, "sessions", "diagnose", "legacy"]) == 0
    assert '"automatic_replay": false' in capsys.readouterr().out
    assert main([*args, "context", "legacy"]) == 0
    assert "canonical_estimated_tokens" in capsys.readouterr().out
    assert main([*args, "context", "legacy", "reset"]) == 2
    assert "reconcile" in capsys.readouterr().err


def test_controller_facts_precede_latest_user_without_splitting_tools(tmp_path: Path) -> None:
    """事实补充不能把请求尾部变为 assistant，也不能拆开工具调用与结果。"""
    from codepilot.contracts import ToolCall, ToolResult

    manager = ContextManager(
        create_basic_registry(tmp_path),
        ContextArtifactStore(tmp_path / "artifacts"),
        facts_provider=lambda: Message(role="assistant", content="控制器记录"),
    )
    for suffix in (
        (),
        (
            Message(
                role="assistant", tool_calls=(ToolCall(call_id="pair", name="glob", arguments={}),)
            ),
            Message(role="tool", tool_result=ToolResult(call_id="pair", content="result")),
        ),
    ):
        history = (Message(role="user", content="最新修订"), *suffix)
        view = asyncio.run(manager.prepare(history))
        assert view.messages[0].content == "控制器记录"
        assert view.messages[1].content == "最新修订"
        assert view.messages[2:] == suffix
