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
