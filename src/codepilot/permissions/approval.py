"""提供 CLI 审批接口及离线测试审批实现。"""

import asyncio
import os
import sys
from collections.abc import Sequence

from codepilot.permissions.models import ApprovalRequest


class ApprovalProvider:
    """定义审批层只回答是否批准，不接触工具执行细节。"""

    async def approve(self, request: ApprovalRequest) -> bool:
        """根据完整请求返回批准或取消。"""
        raise NotImplementedError


class ScriptedApproval(ApprovalProvider):
    """按预设布尔序列回答审批，供不依赖终端输入的测试使用。"""

    def __init__(self, decisions: Sequence[bool]) -> None:
        """快照有限的审批答案，耗尽后默认取消。"""
        self._decisions = tuple(decisions)
        self.requests: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> bool:
        """记录完整请求并消费一个答案，耗尽时拒绝。"""
        self.requests.append(request.model_copy(deep=True))
        return (
            self._decisions[len(self.requests) - 1]
            if len(self.requests) <= len(self._decisions)
            else False
        )


class ConsoleApproval(ApprovalProvider):
    """通过标准输入逐次询问用户，非明确 y/yes 均视为取消。"""

    def __init__(self) -> None:
        """创建单终端输入锁，避免并发工具把审批提示交错显示。"""
        self._lock = asyncio.Lock()

    async def approve(self, request: ApprovalRequest) -> bool:
        """显示工具、影响、参数和理由，并等待一次终端回答。"""
        # 管道输入属于任务或脚本数据，不能被审批线程吞掉或无限等待。
        if not sys.stdin.isatty():
            return False
        prompt = (
            f"允许 {request.action.tool_name} ({request.action.effect})? "
            f"目录={request.action.cwd} 后端={request.action.backend} "
            f"参数摘要={request.action.argument_summary} "
            f"资源={','.join(request.action.resources) or '-'} "
            f"指纹={request.action.fingerprint[:12]} 原因={request.reason} [y/N] "
        )
        # 一个终端同一时间只能可靠回答一个完整审批问题。
        async with self._lock:
            loop = asyncio.get_running_loop()
            ready: asyncio.Future[str] = loop.create_future()
            descriptor = sys.stdin.fileno()
            buffer = bytearray()
            oversized = False

            def readable() -> None:
                """规范模式只在一行或 EOF 就绪时读取，取消后不留下占用输入的线程。"""
                nonlocal oversized
                if ready.done():
                    return
                try:
                    # 不使用 TextIO 预读：粘贴多行答案时，下一行必须仍留在 fd 中可被监听。
                    chunk = os.read(descriptor, 1)
                    if not chunk or chunk == b"\n":
                        ready.set_result(
                            "" if oversized else buffer.decode("ascii", errors="replace")
                        )
                    elif len(buffer) < 64:
                        buffer.extend(chunk)
                    else:
                        # 丢弃到完整行结束并拒绝，不能把长输入截断成前缀 y/yes 而误批准。
                        oversized = True
                except OSError:
                    ready.set_exception(ValueError("approval input unavailable"))

            sys.stdout.write(prompt)
            sys.stdout.flush()
            loop.add_reader(descriptor, readable)
            try:
                answer = await ready
            finally:
                # 取消审批必须先解除 stdin 监听，下一轮输入才能交还交互外壳。
                loop.remove_reader(descriptor)
                sys.stdout.write("\n")
                sys.stdout.flush()
        return answer.strip().lower() in {"y", "yes"}
