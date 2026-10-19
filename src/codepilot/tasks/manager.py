"""当前运行拥有后台任务；退出时清理，未知任务不会在重开时复活。"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from codepilot.sessions.store import SessionStore
from codepilot.tools.base import ToolFailure
from codepilot.tools.process import settle


class TaskManager:
    """维护内存运行句柄和 SQLite 事实，模型只使用控制器生成的任务 ID。"""

    def __init__(self, session: SessionStore, run_id: str, maximum: int) -> None:
        """绑定父运行与并发容量；历史任务只读保留，不恢复协程。"""
        self.session, self.run_id, self.maximum = session, run_id, maximum
        self.handles: dict[str, asyncio.Task[None]] = {}
        self.by_call: dict[str, asyncio.Task[None]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.cleanup_failed = False

    def save(self, identity: str, **updates: Any) -> None:
        """以唯一任务记录更新事件索引，正文留在私有会话。"""
        record = self.records[identity]
        record.update(updates)
        tasks = self.session.get("tasks", {})
        tasks[identity] = record.copy()
        self.session.commit("task_state", {"task_id": identity}, tasks=tasks)

    def start(self, call_id: str, kind: str, operation: Callable[[], Awaitable[str]]) -> str:
        """先保存排队意图再创建协程，容量不足时不启动任何副作用。"""
        if sum(not task.done() for task in self.handles.values()) >= self.maximum:
            raise ToolFailure("task capacity exhausted")
        identity = f"job-{uuid4().hex}"
        self.records[identity] = {
            "task_id": identity,
            "parent_run": self.run_id,
            "parent_call": call_id,
            "kind": kind,
            "state": "queued",
            "created_at": time.time(),
            "output": "",
            "error_type": "",
        }
        self.save(identity)
        task = asyncio.create_task(self._run(identity, operation), name=identity)
        self.handles[identity] = self.by_call[call_id] = task
        return identity

    async def _run(self, identity: str, operation: Callable[[], Awaitable[str]]) -> None:
        """所有终态恰好由此处提交，取消清理由操作的 finally 完成。"""
        try:
            self.save(identity, state="running")
            output = await operation()
            # PTY 已有的有界日志不能被最后的退出码和原文引用覆盖。
            prefix = self.records[identity]["output"]
            combined = prefix[:12000] + "\n" + output[:4000] if prefix else output[:16000]
            self.save(identity, state="succeeded", output=combined)
        except asyncio.CancelledError:
            self.save(identity, state="cancelled")
            raise
        except Exception as error:
            # 外部异常正文可能含敏感参数，仅保留类型并标明结果不确定。
            self.save(identity, state="failed", error_type=type(error).__name__)

    def status(self, identity: str, cursor: int = 0) -> dict[str, Any]:
        """分页返回任务日志；默认索引不导出正文，工具主动查看才给出内容。"""
        if identity not in self.records:
            raise ToolFailure("unknown task in current run")
        record = self.records[identity].copy()
        output = record.pop("output", "")
        record.update(
            output=output[cursor : cursor + 4000],
            cursor=cursor,
            next_cursor=min(cursor + 4000, len(output)),
            has_more=cursor + 4000 < len(output),
        )
        return record

    async def cancel(self, identity: str) -> None:
        """只取消当前运行拥有的任务，等待资源清理后才向调用方返回。"""
        owner = asyncio.current_task()
        cancellation_count = owner.cancelling() if owner is not None else 0
        task = self.handles.get(identity)
        if task is None:
            raise ToolFailure("unknown task in current run")
        if not task.done() and not task.cancelling():
            task.cancel()
        await settle(asyncio.gather(task, return_exceptions=True))
        # 清理屏蔽只延迟取消；调用方自己的取消仍须传播，否则控制监听器无法退出。
        if owner is not None and owner.cancelling() > cancellation_count:
            raise asyncio.CancelledError
        # 尚未进入协程的 queued 任务也必须有终态。
        if self.records[identity]["state"] == "queued":
            self.save(identity, state="cancelled")

    async def close(self) -> None:
        """前台退出取消所有未结束任务，禁止留下脱管进程或模型请求。"""
        for identity in self.handles:
            await self.cancel(identity)
        if self.cleanup_failed:
            raise RuntimeError("managed task cleanup failed; reconciliation required")
