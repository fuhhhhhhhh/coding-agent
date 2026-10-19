"""验证 B3 的共享预算账本和持久上下文检查点。"""

from pathlib import Path

import pytest

from codepilot.context.models import SummaryResult
from codepilot.contracts.stream import Usage
from codepilot.providers.budget import BudgetLedger
from codepilot.sessions.checkpoints import CheckpointStore


def test_budget_ledger_reserves_requests_and_settles_tokens() -> None:
    """请求和输入输出 Token 共享同一账本，任一额度超限都在副作用前失败。"""
    ledger = BudgetLedger(max_requests=2, max_input_tokens=100, max_output_tokens=20)
    ledger.reserve_request(40)
    ledger.settle(40, 8)
    ledger.reserve_request(20)
    ledger.settle(20, 7)
    assert ledger.snapshot.requests == 2
    assert ledger.snapshot.input_tokens == 60
    assert ledger.snapshot.output_tokens == 15
    with pytest.raises(RuntimeError, match="request budget"):
        ledger.reserve_request()


def test_budget_ledger_does_not_release_unknown_usage() -> None:
    """缺失 usage 不能把不确定的服务端成本释放成可无限重试额度。"""
    ledger = BudgetLedger(max_requests=1, max_output_tokens=10)
    ledger.reserve_request()
    ledger.settle(None, None)
    with pytest.raises(RuntimeError, match="request budget"):
        ledger.reserve_request()


def test_checkpoint_store_persists_structured_working_state(tmp_path: Path) -> None:
    """检查点原子落盘并从摘要标题提取目标、待办和风险。"""
    store = CheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.save(
        [{"role": "user", "content": "修复解析器"}],
        SummaryResult(
            content=(
                "用户目标与约束：修复 parser.py\n"
                "待办：补充空输入测试\n"
                "测试与故障证据：pytest 失败 1 项\n"
                "未解决风险：Windows 路径尚未验证"
            ),
            usage=Usage(input_tokens=12, output_tokens=8, source="provider"),
        ),
        tokens_before=900,
        tokens_after=240,
    )
    loaded = store.load()
    assert loaded is not None and loaded.checkpoint_id == checkpoint.checkpoint_id
    assert loaded.working_state.pending == ("补充空输入测试",)
    assert loaded.working_state.risks == ("Windows 路径尚未验证",)
    assert (tmp_path / "checkpoints/latest.json").stat().st_mode & 0o777 == 0o600


def test_concurrent_reservations_settle_by_identity_and_keep_unknown() -> None:
    """乱序响应不能结算别人的额度，缺失 usage 的预留仍阻止超发。"""
    ledger = BudgetLedger(max_requests=10, max_input_tokens=100, max_output_tokens=100)
    first = ledger.reserve_request(40, 30)
    second = ledger.reserve_request(50, 50)
    ledger.settle(45, 15, reservation_id=second)
    assert ledger.snapshot.reserved_input_tokens == 40
    ledger.settle(None, None, reservation_id=first)
    assert ledger.snapshot.reserved_output_tokens == 30
    with pytest.raises(RuntimeError, match="input token"):
        ledger.reserve_request(20)
    with pytest.raises(ValueError, match="unknown or settled"):
        ledger.settle(45, 15, reservation_id=second)


def test_session_attempts_survive_reopen_and_include_summary(tmp_path: Path) -> None:
    """摘要和重试逐次消耗持久额度，重开后未结算请求不清零。"""
    from codepilot.sessions.store import SessionStore

    root = tmp_path / "state"
    with SessionStore(root, "budget") as session:
        session.request_limit = 3
        first = session.reserve_provider_attempt(100, 200)
        session.settle_provider_attempt(
            first, Usage(input_tokens=50, output_tokens=20, source="provider")
        )
        session.reserve_provider_attempt(100, 200)
    with SessionStore(root, "budget", create=False) as session:
        session.request_limit = 3
        assert session.get("request_attempts") == 2
        assert session.get("provider_budget")["input"] == 50
        assert len(session.get("provider_budget")["pending"]) == 1
        session.reserve_provider_attempt(100, 200)
        with pytest.raises(RuntimeError, match="session request"):
            session.reserve_provider_attempt(100, 200)


def test_controller_crash_keeps_reserved_attempt_and_blocks_resume(tmp_path: Path) -> None:
    """真实控制器进程在请求预留后崩溃，恢复不能清空预算或自动重放。"""
    import os
    import subprocess
    import sys

    from codepilot.sessions.store import ReconciliationRequired, SessionStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    script = """import os,sys
from pathlib import Path
from codepilot.sessions.store import SessionStore
from codepilot.contracts import Message
with SessionStore(Path(sys.argv[1]), 'crash-budget') as stored:
    stored.check(Path(sys.argv[2]), 'local:False')
    stored.begin('crashed-run', (Message(role='user',content='检查'),), Path(sys.argv[2]))
    stored.reserve_provider_attempt(200, 300)
    os._exit(23)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(state), str(workspace)], env=dict(os.environ), timeout=15
    )
    assert process.returncode == 23
    with SessionStore(state, "crash-budget", create=False) as stored:
        assert stored.get("request_attempts") == 1
        assert len(stored.get("provider_budget")["pending"]) == 1
        with pytest.raises(ReconciliationRequired):
            stored.check(workspace, "local:False")
        stored.reconcile(workspace)
        # 明确协调旧 run 也不释放不确定 Provider 费用或伪造请求未发生。
        assert len(stored.get("provider_budget")["pending"]) == 1
        stored.check(workspace, "local:False")


def test_session_overreported_usage_is_saved_before_stopping(tmp_path: Path) -> None:
    """服务端用量超过预留时先保留实际事实，再阻止本轮继续执行工具。"""
    from codepilot.sessions.store import SessionStore

    with SessionStore(tmp_path / "state", "overreported") as stored:
        stored.input_token_limit = 100
        identity = stored.reserve_provider_attempt(50, 20)
        with pytest.raises(RuntimeError, match="input token"):
            stored.settle_provider_attempt(
                identity, Usage(input_tokens=101, output_tokens=10, source="provider")
            )
        assert stored.get("provider_budget")["input"] == 101
        assert not stored.get("provider_budget")["pending"]
