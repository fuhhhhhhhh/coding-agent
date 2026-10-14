"""记录关联事件，默认持久化元数据而不保存提示词、参数和工具正文。"""

import json
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from codepilot.contracts.stream import RuntimeEvent

CURRENT_RECORDER: ContextVar["EventRecorder | None"] = ContextVar("event_recorder", default=None)
CURRENT_TURN: ContextVar[str] = ContextVar("event_turn", default="")


class EventRecorder:
    """保存内存事件，并按可选路径立即追加脱敏 JSONL。"""

    def __init__(self, directory: Path | None = None) -> None:
        """显式开启本地落盘；目录应位于 Agent 工作区外。"""
        self.directory = directory
        self.events: list[RuntimeEvent] = []
        self.run_id = ""
        self.started = 0.0
        self._path: Path | None = None

    def start(self, run_id: str) -> None:
        """绑定一次运行，独占新建文件以防覆盖既有轨迹。"""
        if self.run_id:
            raise RuntimeError("recorder is single-use")
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            # 不使用外部传入的运行 ID 拼路径，防止路径穿越。
            import uuid

            self._path = self.directory / f"trace-{uuid.uuid4()}.jsonl"
            self._path.touch(mode=0o600, exist_ok=False)
        self.run_id = run_id
        self.started = time.monotonic()

    def emit(self, kind: str, **payload: Any) -> None:
        """追加深拷贝事件；落盘仅保留固定元数据字段。"""
        event = RuntimeEvent.model_validate(
            dict(
                run_id=self.run_id,
                turn_id=CURRENT_TURN.get(),
                sequence=len(self.events) + 1,
                kind=kind,
                **payload,
            )
        ).model_copy(deep=True)
        self.events.append(event)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.redacted(event), ensure_ascii=False) + "\n")

    @staticmethod
    def redacted(event: RuntimeEvent) -> dict[str, Any]:
        """正文与参数采用整字段省略，避免依赖关键词猜测秘密。"""
        return event.model_dump(exclude={"text", "call"}, mode="json")

    def finish(self, reason: str, error_type: str = "") -> None:
        """发出唯一运行终态并保存摘要；费用和缺失用量不按零计算。"""
        duration = time.monotonic() - self.started
        self.emit("run_finished", outcome=reason, error_type=error_type, duration_seconds=duration)
        if self._path is None:
            return
        usages = [event for event in self.events if event.kind == "usage_updated"]
        summary = {
            "run_id": self.run_id,
            "reason": reason,
            "error_type": error_type,
            "duration_seconds": duration,
            "events": len(self.events),
            "usage_by_turn": [self.redacted(event) for event in usages],
            "cost": None,
            "price_source": None,
            "price_date": None,
            "cost_is_estimate": None,
            "content_policy": "metadata_only",
        }
        # 摘要同样使用独占创建；不把完整 RunReport 中的历史直接序列化。
        with self._path.with_suffix(".summary.json").open("x", encoding="utf-8") as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2)

    def normalized(self) -> tuple[dict[str, Any], ...]:
        """规范化运行、回合、调用 ID 和时间，用于离线行为比较。"""
        ids: dict[str, str] = {}
        records = []
        for event in self.events:
            record = self.redacted(event)
            for field in ("run_id", "turn_id", "call_id"):
                value = record[field]
                if value:
                    record[field] = ids.setdefault(value, f"id-{len(ids) + 1}")
            record["duration_seconds"] = None
            records.append(record)
        return tuple(records)


def emit(kind: str, **payload: Any) -> None:
    """在当前运行上下文中发布事件，独立调用执行器时保持兼容。"""
    recorder = CURRENT_RECORDER.get()
    if recorder is not None:
        recorder.emit(kind, **payload)
