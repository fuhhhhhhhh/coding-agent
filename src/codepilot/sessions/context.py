"""提供无模型调用的上下文检查与显式撤回服务。"""

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.context import estimate_messages
from codepilot.sessions.checkpoints import CheckpointStore
from codepilot.sessions.store import SessionStore


def inspect_context(session: SessionStore, *, summary: bool = False) -> dict[str, Any]:
    """区分规范历史估算与上次压缩估算；摘要正文需显式请求。"""
    checkpoint = CheckpointStore(session.directory / "checkpoints").load()
    result: dict[str, Any] = {
        "canonical_estimated_tokens": estimate_messages(session.history()),
        "estimate_source": "approximate_messages_only; provider also counts schemas and output",
        "compaction_disabled": session.get("compaction_disabled", False),
        "checkpoint": None,
        "provider_budget": session.get("provider_budget"),
    }
    if checkpoint is not None:
        result["checkpoint"] = {
            "id": checkpoint.checkpoint_id,
            "schema_version": checkpoint.schema_version,
            "covered_messages": checkpoint.covered_messages,
            "source_digest": checkpoint.source_digest,
            "last_estimated_tokens": checkpoint.tokens_after,
            "usage": checkpoint.usage.model_dump(),
            "semantic_accuracy": "model_candidate_not_independent_verification",
        }
        if summary:
            # 私有摘要包含项目内容，只有显式命令才展示正文。
            result["checkpoint"]["summary"] = checkpoint.summary
    return result


def reset_context(session: SessionStore) -> dict[str, Any]:
    """归档当前检查点并暂停自动压缩；不回滚代码、不删除规范消息。"""
    if session.get("status") in {"running", "needs_reconciliation"}:
        raise ValueError("reconcile session before resetting context")
    store = CheckpointStore(session.directory / "checkpoints")
    target = store.directory / "latest.json"
    archived = None
    # 即使检查点损坏，也允许用户显式撤回；链接则不能当作有效文件搬运。
    if target.is_symlink():
        raise ValueError("unsafe checkpoint symbolic link")
    if target.exists():
        archived = store.directory / f"withdrawn-{uuid4().hex}.json"
        os.replace(target, archived)
    try:
        session.commit("context_withdrawn", {}, compaction_disabled=True)
    except BaseException:
        # 事件无法提交时恢复原检查点，不能悄悄改变用户选择。
        if archived is not None:
            os.replace(archived, target)
        raise
    return {"compaction_disabled": True, "archived": archived.name if archived else None}


def context_command(root: Path, session_id: str, action: str) -> dict[str, Any]:
    """让 CLI 与交互斜杠命令共用同一个持锁服务。"""
    with SessionStore(root, session_id, create=False) as session:
        if action == "reset":
            return reset_context(session)
        return inspect_context(session, summary=action == "summary")
