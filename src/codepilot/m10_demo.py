"""演示大输出引用、按需读取和完整回合压缩，不调用真实模型。"""

import asyncio
import json
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from codepilot.context import (
    ContextArtifactStore,
    ContextLimits,
    ContextManager,
    SummaryResult,
    estimate_messages,
)
from codepilot.contracts import Message, ToolCall, ToolResult
from codepilot.contracts.stream import Usage
from codepilot.runtime import Runner
from codepilot.tools.base import ToolSpec


class DemoExecutor:
    """为演示返回包含中段错误的固定 110KB 工具输出。"""

    def __init__(self) -> None:
        """创建一个只读演示工具声明和可核对的原始正文。"""
        self.raw = "H" * 55_000 + "\nMIDDLE_ERROR: parser failed at row 42\n" + "T" * 55_000
        self.specs = (
            ToolSpec(
                name="collect_logs",
                description="返回固定的大型测试日志",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effect="read",
                resources=("demo:read",),
                default_timeout=1.0,
                supports_cancel=True,
            ),
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        """返回与调用 ID 配对的大日志。"""
        return ToolResult(call_id=call.call_id, content=self.raw)


class AdaptiveDemoProvider:
    """先请求大日志，再按引用读取中段，最后给出固定结论。"""

    def __init__(self, middle_start: int) -> None:
        """保存错误所在字符位置和当前脚本步骤。"""
        self.middle_start = middle_start
        self.step = 0
        self.prompt_tokens: list[int] = []

    async def complete(self, messages: Sequence[Message]) -> Message:
        """根据上一步短引用提取 artifact ID，构造下一次读取调用。"""
        self.prompt_tokens.append(estimate_messages(messages))
        self.step += 1
        if self.step == 1:
            return Message(
                role="assistant",
                tool_calls=(ToolCall(call_id="logs", name="collect_logs", arguments={}),),
            )
        if self.step == 2:
            result = messages[-1].tool_result
            if result is None:
                raise ValueError("演示缺少大输出工具结果")
            matched = re.search(r"artifact_id=(artifact-[0-9a-f]{32})", result.content)
            if matched is None:
                raise ValueError("演示缺少 artifact 引用")
            return Message(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        call_id="read-middle",
                        name="read_context_artifact",
                        arguments={
                            "artifact_id": matched.group(1),
                            "start": self.middle_start,
                            "max_chars": 160,
                        },
                    ),
                ),
            )
        return Message(role="assistant", content="已定位 parser 在 row 42 的中段失败。")


class BaselineDemoProvider:
    """基线直接接收完整工具输出，并在第二轮扫描中段错误。"""

    def __init__(self) -> None:
        """初始化脚本步骤、任务成功标记和 prompt Token 估算记录。"""
        self.step = 0
        self.solved = False
        self.prompt_tokens: list[int] = []

    async def complete(self, messages: Sequence[Message]) -> Message:
        """首轮请求日志，第二轮从完整正文中核对固定错误。"""
        self.prompt_tokens.append(estimate_messages(messages))
        self.step += 1
        if self.step == 1:
            return Message(
                role="assistant",
                tool_calls=(ToolCall(call_id="baseline-logs", name="collect_logs", arguments={}),),
            )
        result = messages[-1].tool_result
        self.solved = (
            result is not None and "MIDDLE_ERROR: parser failed at row 42" in result.content
        )
        return Message(
            role="assistant",
            content="已定位 parser 在 row 42 的中段失败。" if self.solved else "未定位失败。",
        )


class DemoSummarizer:
    """离线返回结构化摘要，用来演示压缩契约和成本记录。"""

    async def summarize(self, messages: Sequence[Message], target_tokens: int) -> SummaryResult:
        """核对输入仍是完整调用结果组，再返回固定摘要。"""
        if len(messages) % 2 or any(
            messages[index].role != "assistant" or messages[index + 1].role != "tool"
            for index in range(0, len(messages), 2)
        ):
            raise ValueError("压缩输入拆散了工具回合")
        return SummaryResult(
            content=(
                "用户目标与约束：保持接口兼容。\n"
                "待办：检查解析边界。\n"
                "已修改内容：parser.py。\n"
                "测试与故障证据：两轮检查已完成。\n"
                "未解决风险：跨平台未验证。"
            ),
            usage=Usage(input_tokens=96, output_tokens=38, source="estimate"),
        )


def _round(index: int) -> tuple[Message, Message]:
    """构造供手动压缩演示使用的完整工具回合。"""
    call_id = f"compact-{index}"
    return (
        Message(
            role="assistant",
            content=f"检查第 {index} 项",
            tool_calls=(ToolCall(call_id=call_id, name="collect_logs", arguments={}),),
        ),
        Message(
            role="tool",
            tool_result=ToolResult(call_id=call_id, content="evidence:" + "x" * 320),
        ),
    )


async def run_demo() -> dict[str, object]:
    """运行大输出闭环和手动压缩，并返回不含原始大正文的结果摘要。"""
    with tempfile.TemporaryDirectory(prefix="codepilot-m10-") as raw_directory:
        root = Path(raw_directory)
        executor = DemoExecutor()
        baseline_provider = BaselineDemoProvider()
        baseline_report = await Runner(baseline_provider, executor).run(
            (Message(role="user", content="定位日志中的解析失败"),)
        )
        store = ContextArtifactStore(root / "artifacts")
        manager = ContextManager(
            executor,
            store,
            limits=ContextLimits(
                max_prompt_tokens=8_000,
                response_reserve_tokens=1_000,
                compact_trigger_tokens=6_000,
                compact_target_tokens=4_000,
                recent_rounds=1,
                large_output_bytes=1_024,
                artifact_preview_chars=500,
                artifact_read_chars=512,
            ),
            summarizer=DemoSummarizer(),
        )
        provider = AdaptiveDemoProvider(executor.raw.index("MIDDLE_ERROR"))
        report = await Runner(provider, manager, context=manager).run(
            (Message(role="user", content="定位日志中的解析失败"),)
        )
        rounds = [_round(index) for index in range(4)]
        compacted = await manager.compact(
            (
                Message(role="system", content="必须保持公开接口兼容"),
                Message(role="user", content="修复解析器并保留测试证据"),
                *(message for group in rounds for message in group),
            )
        )
        artifact = next((root / "artifacts").glob("*.txt"))
        return {
            "run_reason": report.reason,
            "baseline_run_reason": baseline_report.reason,
            "baseline_solved": baseline_provider.solved,
            "governed_solved": report.messages[-1].content.startswith("已定位"),
            "tool_calls": report.tool_calls,
            "raw_bytes": len(executor.raw.encode("utf-8")),
            "artifact_bytes": artifact.stat().st_size,
            "baseline_total_estimated_prompt_tokens": sum(baseline_provider.prompt_tokens),
            "governed_total_estimated_prompt_tokens": sum(provider.prompt_tokens),
            "governed_largest_prompt_tokens": max(provider.prompt_tokens),
            "middle_error_read": any(
                message.role == "tool"
                and message.tool_result is not None
                and "MIDDLE_ERROR" in message.tool_result.content
                for message in report.messages
            ),
            "compacted": compacted.compacted,
            "compacted_roles": [message.role for message in compacted.messages],
            "context_records": [record.kind for record in manager.records],
            "summary_usage": manager.records[-1].usage.model_dump()
            if manager.records[-1].usage is not None
            else None,
        }


def main() -> None:
    """以格式化 JSON 输出稳定演示证据。"""
    print(json.dumps(asyncio.run(run_demo()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
