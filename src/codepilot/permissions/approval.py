"""提供 CLI 审批接口及离线测试审批实现。"""

import asyncio
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
        prompt = (
            f"允许工具 {request.tool.name} ({request.tool.effect})? "
            f"参数={request.call.arguments} 原因={request.reason} [y/N] "
        )
        # 一个终端同一时间只能可靠回答一个完整审批问题。
        async with self._lock:
            answer = await asyncio.to_thread(input, prompt)
        return answer.strip().lower() in {"y", "yes"}
