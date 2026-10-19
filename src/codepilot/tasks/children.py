"""只读子任务复用同一个 Runner、协议适配器与持久请求总账本。"""

from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.contracts import Message, ToolCall
from codepilot.contracts.run import RunLimits, RunObservation
from codepilot.contracts.stream import Usage
from codepilot.extensions.executor import ManagedExecutor, ReadOnlyExecutor
from codepilot.extensions.models import ExtensionConfig
from codepilot.extensions.router import DelegateArguments
from codepilot.observability import EventRecorder
from codepilot.providers.budget import BudgetExceededError
from codepilot.providers.fake import FakeProvider
from codepilot.providers.openai_compatible import OpenAICompatibleProvider, RequestBudget
from codepilot.providers.responses import ResponsesProvider
from codepilot.runtime import Runner
from codepilot.sessions.store import SessionStore
from codepilot.tools.base import ToolFailure


class ChildBudget(RequestBudget):
    """子额度仅限次数，总账本与所有尝试结算仍由父对象持有。"""

    def __init__(self, parent: RequestBudget, maximum: int) -> None:
        """不创建第二份 Token 账本，避免主/子请求分别获得免费额度。"""
        self.parent, self.max_requests, self.used = parent, maximum, 0
        self.ledger = parent.ledger

    def consume(self, input_tokens: int = 0, output_tokens: int = 0) -> str:
        """先核对子额度，再在真实网络前消耗父共享额度。"""
        if self.used >= self.max_requests:
            raise BudgetExceededError("child request budget exhausted")
        identity = self.parent.consume(input_tokens, output_tokens)
        self.used += 1
        return identity

    def settle(self, usage: Usage, reservation_id: str) -> None:
        """usage 只结算一次到父账本与 SQLite，子任务不自己释放预留。"""
        self.parent.settle(usage, reservation_id)


async def run_child(
    arguments: DelegateArguments,
    parent_provider: Any,
    parent: ManagedExecutor,
    session: SessionStore,
    config: ExtensionConfig,
    traces: Path,
) -> str:
    """用固定只读能力开启新 Runner，结果是有来源的模型报告而非独立结论。"""
    executor = ReadOnlyExecutor(parent)
    provider: Any
    if isinstance(parent_provider, OpenAICompatibleProvider):
        adapter = (
            ResponsesProvider
            if isinstance(parent_provider, ResponsesProvider)
            else OpenAICompatibleProvider
        )
        provider = adapter(
            parent_provider.client,
            executor.specs,
            model=parent_provider.model,
            budget=ChildBudget(parent_provider.budget, config.child_requests),
            # 推理 Token 也占服务端输出额度，不能硬编码过低上限导致只读报告持续截断。
            max_tokens=min(parent_provider.max_tokens, config.child_output_tokens),
            retry=parent_provider.retry,
            context_tokens=parent_provider.context_tokens,
            extra_body=parent_provider.extra_body,
        )
    else:
        # fake 只用于确定性产品验收，不把固定报告冒充模型解题能力。
        provider = FakeProvider(
            (
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            call_id=f"child-read-{uuid4().hex}",
                            name="glob",
                            arguments={"pattern": "*"},
                        ),
                    ),
                ),
                Message(role="assistant", content="只读检查已结束，结论需父任务核对。"),
            )
        )

    def observation(item: RunObservation) -> None:
        """子运行事实独立保存，不能混入父规范历史或复用父工具调用 ID。"""
        if item.kind == "model_requested" and not isinstance(
            parent_provider, OpenAICompatibleProvider
        ):
            identity = session.reserve_provider_attempt(0, 0)
            session.settle_provider_attempt(
                identity, Usage(input_tokens=0, output_tokens=0, source="provider")
            )
        session.commit("child_observation", {"observation": item.model_dump(mode="json")})

    report = await Runner(
        provider,
        executor,
        RunLimits(
            max_steps=config.child_requests,
            max_tool_calls=config.child_tool_calls,
            timeout_seconds=config.task_timeout_seconds,
        ),
        recorder=EventRecorder(traces),
        on_observation=observation,
    ).run(
        (
            Message(
                role="system",
                content=(
                    "你是只读 explore/review 子任务。只按工具证据定位或审查，"
                    "引用文件路径和调用来源。"
                    "不得执行 Shell、写文件、配置扩展或继续委派；结论由父任务独立核对。"
                ),
            ),
            Message(role="user", content=arguments.task),
        )
    )
    if report.reason != "completed":
        raise ToolFailure(f"child stopped: {report.reason}")
    answer = next(
        (
            message.content
            for message in reversed(report.messages)
            if message.role == "assistant" and message.content
        ),
        "",
    )
    sources = [item.result.call_id for item in report.observations if item.result]
    return f"model_report; independent_verdict=not_run; source_calls={sources}\n{answer[:12000]}"
