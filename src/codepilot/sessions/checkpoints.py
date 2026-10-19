"""持久化上下文检查点和结构化工作状态。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from codepilot.contracts.models import Contract
from codepilot.contracts.stream import Usage

if TYPE_CHECKING:
    from codepilot.context.models import SummaryResult


class WorkingState(Contract):
    """保存可继续工作的目标、约束、待办和证据摘要。"""

    goal: str = ""
    constraints: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


class ContextCheckpoint(Contract):
    """保存一次已验证的压缩视图及其覆盖范围，不替代规范消息历史。"""

    checkpoint_id: str
    source_digest: str
    created_at: float
    tokens_before: int
    tokens_after: int
    usage: Usage
    summary: str
    working_state: WorkingState
    schema_version: int = 1
    covered_messages: int = 0
    summary_digest: str = ""


def source_digest(messages: object) -> str:
    """对规范 JSON 消息序列稳定计算来源哈希，不接受 repr 代替事实。"""
    serialized = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class CheckpointStore:
    """在私有目录中原子保存和读取最新检查点。"""

    def __init__(self, directory: Path) -> None:
        """绑定会话私有检查点目录，不读取工作区文件。"""
        from codepilot.sessions.outputs import private_directory

        self.directory = directory
        private_directory(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)

    def save(
        self,
        messages: object,
        summary: SummaryResult,
        *,
        tokens_before: int,
        tokens_after: int,
    ) -> ContextCheckpoint:
        """原子写入检查点；写失败时不替换旧检查点。"""
        digest = source_digest(messages)
        checkpoint_id = f"checkpoint-{digest[:24]}"
        checkpoint = ContextCheckpoint(
            checkpoint_id=checkpoint_id,
            source_digest=digest,
            created_at=time.time(),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            usage=summary.usage,
            summary=summary.content,
            working_state=_extract_working_state(summary.content),
            schema_version=2,
            covered_messages=len(messages) if isinstance(messages, list) else 0,
            summary_digest=hashlib.sha256(summary.content.encode("utf-8")).hexdigest(),
        )
        target = self.directory / "latest.json"
        temporary = self.directory / f".{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(checkpoint.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            # 替换是最后一步，摘要失败或磁盘异常不会破坏上一次可用检查点。
            os.replace(temporary, target)
        finally:
            # 替换失败也不留下可被误认为有效检查点的临时文件。
            temporary.unlink(missing_ok=True)
        return checkpoint

    def load(self) -> ContextCheckpoint | None:
        """读取最新检查点；缺失返回空，损坏则明确失败。"""
        target = self.directory / "latest.json"
        # 检查点只能位于私有存储，拒绝符号链接跳转到工作区或其他文件。
        if target.is_symlink():
            raise ValueError("unsafe checkpoint symbolic link")
        # 新会话没有检查点是正常状态，不人为生成空摘要。
        if not target.exists():
            return None
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            # 本地损坏或异常大文件不能在恢复时无限占用内存。
            if os.fstat(stream.fileno()).st_size > 1024 * 1024:
                raise ValueError("checkpoint exceeds 1 MiB")
            checkpoint = ContextCheckpoint.model_validate_json(stream.read(1024 * 1024 + 1))
        # 旧程序不猜测未来格式，防止把覆盖范围解释错后丢失历史。
        if checkpoint.schema_version not in {1, 2}:
            raise ValueError("unsupported checkpoint version")
        # 摘要与元数据意外分离时停止；校验和不是对恶意文件所有者的认证。
        if checkpoint.schema_version == 2 and (
            hashlib.sha256(checkpoint.summary.encode("utf-8")).hexdigest()
            != checkpoint.summary_digest
        ):
            raise ValueError("checkpoint summary checksum mismatch")
        return checkpoint


def _extract_working_state(summary: str) -> WorkingState:
    """按摘要器约定的中文/英文标题提取有界结构化工作状态。"""
    sections: dict[str, list[str]] = {}
    current = ""
    aliases = {
        "用户目标与约束": "goal",
        "目标与约束": "goal",
        "待办": "pending",
        "已修改内容": "modified",
        "测试与故障证据": "tests",
        "未解决风险": "risks",
    }
    for line in summary.splitlines():
        cleaned = line.strip().lstrip("#*- ")
        heading, separator, remainder = cleaned.partition("：")
        if not separator:
            heading, separator, remainder = cleaned.partition(":")
        heading = heading.rstrip(":：")
        if heading in aliases:
            current = aliases[heading]
            sections.setdefault(current, [])
            if remainder.strip():
                sections[current].append(remainder.strip())
        elif current and line.strip():
            sections[current].append(line.strip())
    goal_parts = sections.get("goal", [])
    return WorkingState(
        goal=" ".join(goal_parts)[:4000],
        constraints=tuple(goal_parts[1:21]),
        pending=tuple(sections.get("pending", [])[:20]),
        modified=tuple(sections.get("modified", [])[:20]),
        tests=tuple(sections.get("tests", [])[:20]),
        risks=tuple(sections.get("risks", [])[:20]),
    )
