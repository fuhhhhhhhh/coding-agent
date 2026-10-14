"""作为容器内窄入口执行一个工具调用或固定 argv 命令。"""

import asyncio
import json
import sys
from pathlib import Path

from codepilot.contracts import ToolCall
from codepilot.tools import create_basic_registry
from codepilot.workspace.local import LocalExecutionBackend
from codepilot.workspace.models import BackendLimits


def _read_oom_kill_count() -> int | None:
    """读取当前 cgroup 的 OOM 杀进程计数；非 cgroup v2 环境返回未知。"""
    try:
        lines = Path("/sys/fs/cgroup/memory.events").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        # worker 也可被单独运行；没有 memory controller 时不能武断判定 OOM。
        return None
    for line in lines:
        key, separator, value = line.partition(" ")
        if key == "oom_kill" and separator and value.isdigit():
            return int(value)
    # 内核文件存在但字段缺失时视为无法判定，避免把普通 SIGKILL 误报为 OOM。
    return None


def _oom_kill_increased(before: int | None, after: int | None) -> bool:
    """仅在两次 cgroup 采样都有效且计数增加时确认发生过 OOM kill。"""
    return before is not None and after is not None and after > before


async def dispatch(payload: bytes) -> bytes:
    """校验单个控制器请求，并序列化严格的内部结果契约。"""
    try:
        request = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid worker JSON") from error
    if not isinstance(request, dict):
        raise ValueError("worker request must be an object")
    kind = request.get("kind")
    if kind == "describe":
        specs = create_basic_registry(Path("/workspace")).specs
        return json.dumps([spec.model_dump(mode="json") for spec in specs]).encode()
    if kind == "tool":
        call = ToolCall.model_validate(request.get("call"))
        # Shell 会启动独立子进程；容器主进程存活时 Docker 状态不会报告子进程 OOM。
        oom_before = _read_oom_kill_count() if call.name == "shell" else None
        tool_result = await create_basic_registry(Path("/workspace")).execute(call)
        oom_after = _read_oom_kill_count() if call.name == "shell" else None
        if (
            tool_result.is_error
            and ("exit_code=-9" in tool_result.content or "exit_code=137" in tool_result.content)
            and _oom_kill_increased(oom_before, oom_after)
        ):
            # 稳定分类供 Runtime 纠错，避免模型从平台相关退出码自行猜测。
            tool_result = tool_result.model_copy(update={"content": "container_oom"})
        return tool_result.model_dump_json().encode()
    if kind == "command":
        raw_argv = request.get("argv")
        # 固定 argv 必须是非空字符串数组，不能由 worker 隐式交给 Shell 解释。
        if (
            not isinstance(raw_argv, list)
            or not raw_argv
            or not all(isinstance(item, str) and item for item in raw_argv)
        ):
            raise ValueError("command argv must contain nonempty strings")
        timeout = request.get("timeout_seconds")
        output_bytes = request.get("output_bytes", 262_144)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("command timeout must be positive")
        if isinstance(output_bytes, bool) or not isinstance(output_bytes, int):
            raise ValueError("command output limit must be an integer")
        limits = BackendLimits(
            command_timeout_seconds=float(timeout),
            output_bytes=output_bytes,
        )
        backend = LocalExecutionBackend(Path("/workspace"), limits=limits)
        oom_before = _read_oom_kill_count()
        async with backend:
            command_result = await backend.run_argv(tuple(raw_argv), timeout_seconds=float(timeout))
        oom_after = _read_oom_kill_count()
        if command_result.exit_code in {-9, 137} and _oom_kill_increased(oom_before, oom_after):
            # Docker exec 子进程被杀时容器主进程仍存活，只能由 cgroup 事件识别 OOM。
            command_result = command_result.model_copy(update={"oom_killed": True})
        return command_result.model_dump_json().encode()
    raise ValueError("unknown worker request kind")


def main() -> int:
    """从 stdin 读取一次请求；协议错误只返回固定错误类型到 stderr。"""
    try:
        output = asyncio.run(dispatch(sys.stdin.buffer.read()))
    except Exception as error:
        # 不回显请求正文，避免命令或工具参数进入 Docker 控制面日志。
        print(f"worker_error: {type(error).__name__}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
