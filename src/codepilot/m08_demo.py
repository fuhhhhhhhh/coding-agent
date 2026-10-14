"""演示 M08 的并发读取、同资源串行和排队取消。"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Literal

from codepilot.contracts import ToolCall
from codepilot.contracts.models import Contract
from codepilot.scheduler import ScheduledExecutor
from codepilot.tools import Tool, ToolRegistry


class ProbeArguments(Contract):
    """定义演示调用的资源路径、标签和等待时间。"""

    path: str
    label: str
    delay: float


@dataclass
class ProbeState:
    """记录调度演示中的开始顺序和最大并发数。"""

    started: list[str] = field(default_factory=list)
    active: int = 0
    maximum: int = 0


class ReadProbe(Tool[ProbeArguments]):
    """用短暂异步等待表示独立只读工作。"""

    name = "read_probe"
    description = "演示可并发读取"
    effect: Literal["read", "write", "process"] = "read"
    resources = ("workspace:read",)
    arguments_model = ProbeArguments

    def __init__(self, state: ProbeState) -> None:
        """保存共享状态以测量真实重叠。"""
        self.state = state

    async def run(self, arguments: ProbeArguments) -> str:
        """记录活动区间并在延迟后返回标签。"""
        self.state.started.append(arguments.label)
        self.state.active += 1
        self.state.maximum = max(self.state.maximum, self.state.active)
        try:
            await asyncio.sleep(arguments.delay)
            return arguments.label
        finally:
            self.state.active -= 1


class WriteProbe(ReadProbe):
    """用相同等待行为表示按实际路径串行的写工作。"""

    name = "write_probe"
    description = "演示同资源串行写入"
    effect: Literal["read", "write", "process"] = "write"
    resources = ("workspace:write",)


def _call(call_id: str, name: str, path: str, delay: float) -> ToolCall:
    """创建演示使用的固定工具调用。"""
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments={"path": path, "label": call_id, "delay": delay},
    )


async def _measure(scheduler: ScheduledExecutor, calls: tuple[ToolCall, ...]) -> float:
    """并发提交一组调用并返回单调时钟耗时。"""
    started = time.monotonic()
    await asyncio.gather(*(scheduler.execute(call) for call in calls))
    return time.monotonic() - started


async def main_async() -> None:
    """运行三个确定性场景并输出 JSON 摘要。"""
    read_state = ProbeState()
    reads = ScheduledExecutor(ToolRegistry((ReadProbe(read_state),)), max_concurrency=2)
    read_elapsed = await _measure(
        reads,
        (
            _call("read-a", "read_probe", "a.txt", 0.08),
            _call("read-b", "read_probe", "b.txt", 0.08),
        ),
    )
    print(
        json.dumps(
            {
                "scenario": "parallel_reads",
                "max_concurrency": 2,
                "observed_parallel": read_state.maximum,
                "elapsed_seconds": round(read_elapsed, 3),
            }
        )
    )

    write_state = ProbeState()
    writes = ScheduledExecutor(ToolRegistry((WriteProbe(write_state),)), max_concurrency=2)
    write_elapsed = await _measure(
        writes,
        (
            _call("write-a", "write_probe", "same.txt", 0.08),
            _call("write-b", "write_probe", "same.txt", 0.08),
        ),
    )
    print(
        json.dumps(
            {
                "scenario": "same_path_writes",
                "resource": "path:same.txt",
                "observed_parallel": write_state.maximum,
                "elapsed_seconds": round(write_elapsed, 3),
            }
        )
    )

    cancel_state = ProbeState()
    limited = ScheduledExecutor(ToolRegistry((ReadProbe(cancel_state),)), max_concurrency=1)
    first = asyncio.create_task(limited.execute(_call("first", "read_probe", "a", 0.05)))
    while cancel_state.started != ["first"]:
        await asyncio.sleep(0)
    queued = asyncio.create_task(limited.execute(_call("cancelled", "read_probe", "b", 0.01)))
    await asyncio.sleep(0)
    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    await first
    print(
        json.dumps(
            {
                "scenario": "queued_cancel",
                "started": cancel_state.started,
                "cancelled_started": "cancelled" in cancel_state.started,
            }
        )
    )


def main() -> None:
    """从同步模块入口运行异步调度演示。"""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
