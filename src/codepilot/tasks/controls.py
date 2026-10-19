"""为另一个终端提供私有任务控制信箱，不抢占正在运行的会话锁。"""

import asyncio
import json
from itertools import islice
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.sessions.outputs import atomic_json, private_directory
from codepilot.sessions.store import SessionStore, safe_id
from codepilot.tasks.manager import TaskManager


def task_command(
    root: Path, session_id: str, action: str, task_id: str | None = None, data: str = ""
) -> dict[str, Any]:
    """元数据只读查看；取消或输入只写当前拥有任务的私有控制事件。"""
    snapshot = SessionStore.inspect(root, session_id)
    tasks = snapshot.get("tasks") or {}
    if action == "list":
        return {
            key: {name: value for name, value in record.items() if name != "output"}
            for key, record in tasks.items()
        }
    identity = safe_id(task_id or "")
    if identity not in tasks:
        raise ValueError("unknown task")
    if action == "show":
        return dict(tasks[identity])
    if snapshot.get("status") != "running" or tasks[identity]["state"] not in {"queued", "running"}:
        raise ValueError("task is not owned by an active controller")
    if len(data.encode()) > 4096:
        raise ValueError("task input exceeds 4096 bytes")
    directory = root / safe_id(session_id) / "task-controls"
    private_directory(directory)
    atomic_json(
        directory / f"control-{uuid4().hex}.json",
        {
            "task_id": identity,
            "parent_run": tasks[identity]["parent_run"],
            "action": action,
            "data": data,
        },
    )
    return {"task_id": identity, "control_queued": action}


async def watch_controls(manager: TaskManager, deliver: Any) -> None:
    """任务所有者在安全边界消费信箱，旧 run 的请求不进入新任务。"""
    directory = manager.session.directory / "task-controls"
    private_directory(directory)
    while True:
        for path in islice(directory.glob("control-*.json"), 32):
            try:
                if path.is_symlink() or path.stat().st_size > 16384:
                    raise ValueError("unsafe control message")
                record = json.loads(path.read_bytes())
                identity = record["task_id"]
                if record["parent_run"] != manager.run_id or identity not in manager.handles:
                    continue
                if record["action"] == "cancel":
                    await manager.cancel(identity)
                elif record["action"] == "input":
                    await deliver(identity, record["data"])
                manager.session.commit(
                    "task_control_consumed", {"task_id": identity, "action": record["action"]}
                )
            except Exception:
                # 输入正文可能含秘密，故障只记录稳定事件，不能把整份控制信箱回显。
                manager.session.commit("task_control_rejected", {})
            finally:
                path.unlink(missing_ok=True)
        await asyncio.sleep(0.1)
