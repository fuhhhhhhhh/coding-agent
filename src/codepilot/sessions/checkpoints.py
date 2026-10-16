"""持久化上下文检查点和结构化工作状态。"""

import hashlib
import json
import os
import time
from pathlib import Path

from codepilot.context.models import SummaryResult
from codepilot.contracts.models import Contract
from codepilot.contracts.stream import Usage


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


class CheckpointStore:
    """在私有目录中原子保存和读取最新检查点。"""

    def __init__(self, directory: Path) -> None:
        """绑定会话私有检查点目录，不读取工作区文件。"""
        self.directory = directory
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
        serialized = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
        source_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        checkpoint_id = f"checkpoint-{source_digest[:24]}"
        checkpoint = ContextCheckpoint(
            checkpoint_id=checkpoint_id,
            source_digest=source_digest,
            created_at=time.time(),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            usage=summary.usage,
            summary=summary.content,
            working_state=_extract_working_state(summary.content),
        )
        target = self.directory / "latest.json"
        temporary = self.directory / f".{checkpoint_id}.tmp"
        temporary.write_text(checkpoint.model_dump_json(), encoding="utf-8")
        temporary.chmod(0o600)
        # 替换是最后一步，摘要失败或磁盘异常不会破坏上一次可用检查点。
        os.replace(temporary, target)
        return checkpoint

    def load(self) -> ContextCheckpoint | None:
        """读取最新检查点；缺失返回空，损坏则明确失败。"""
        target = self.directory / "latest.json"
        if not target.exists():
            return None
        return ContextCheckpoint.model_validate_json(target.read_text(encoding="utf-8"))


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
