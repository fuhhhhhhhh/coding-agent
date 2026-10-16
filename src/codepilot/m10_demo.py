"""演示大输出与上下文压缩；只有显式 --live 才调用一次真实摘要模型。"""

import argparse
import asyncio
import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

from openai import AsyncOpenAI

from codepilot.context import (
    ContextArtifactStore,
    ContextLimits,
    ContextManager,
    ContextPreparationError,
    ModelSummarizer,
    SummaryResult,
    estimate_messages,
)
from codepilot.contracts import Message, ToolCall, ToolResult
from codepilot.contracts.stream import Usage
from codepilot.providers.deepseek import DeepSeekProvider, RequestBudget
from codepilot.runtime import Runner
from codepilot.sessions.checkpoints import CheckpointStore
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


def _live_history() -> tuple[Message, ...]:
    """构造不含真实仓库内容的合成历史，用于核对摘要是否保留关键信息。"""
    messages = [
        Message(role="system", content="仓库规则：公开函数签名不可改变，新增注释使用中文。"),
        Message(role="user", content="修复 parser.py 的空输入崩溃，并保留已有行为。"),
    ]
    evidence = (
        (
            "inspect",
            "约束：公开函数签名不可改变。待办：检查空输入与 Windows 换行。",
            "发现 ValueError: empty token；风险为 Windows 尚未验证。",
        ),
        (
            "edit",
            "已修改 parser.py，仅为空输入增加提前返回。",
            "test_parser_empty 已通过，完整回归 18 passed。",
        ),
        (
            "review",
            "复核公开 API 与类型。",
            "parser.py 签名保持不变；仍需真实 Windows 环境验证。",
        ),
    )
    for call_id, assistant_text, result_text in evidence:
        messages.extend(
            (
                Message(
                    role="assistant",
                    content=assistant_text,
                    tool_calls=(ToolCall(call_id=call_id, name="collect_logs", arguments={}),),
                ),
                Message(
                    role="tool",
                    tool_result=ToolResult(call_id=call_id, content=result_text),
                ),
            )
        )
    return tuple(messages)


async def run_live(
    timeout_seconds: float = 45.0, model: str = "deepseek-flash"
) -> dict[str, object]:
    """用一次指定 DeepSeek 模型验证真实摘要，并允许覆盖排队总时限。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is not configured; no request sent")
    budget = RequestBudget(1)
    async with AsyncOpenAI(
        api_key=key,
        base_url="https://api.deepseek.com",
        max_retries=0,
        timeout=20.0,
    ) as client:
        provider = DeepSeekProvider(client, model=model, budget=budget, max_tokens=1024)
        summarizer = ModelSummarizer(provider, timeout_seconds=timeout_seconds)
        with tempfile.TemporaryDirectory(prefix="codepilot-m10-live-") as temporary:
            manager = ContextManager(
                DemoExecutor(),
                ContextArtifactStore(Path(temporary) / "artifacts"),
                limits=ContextLimits(
                    max_prompt_tokens=3_000,
                    response_reserve_tokens=400,
                    compact_trigger_tokens=1_800,
                    compact_target_tokens=1_200,
                    recent_rounds=1,
                ),
                summarizer=summarizer,
                checkpoint_store=CheckpointStore(Path(temporary) / "checkpoints"),
            )
            source = _live_history()
            try:
                view = await manager.compact(source)
            except ContextPreparationError as error:
                # Live 演示只输出稳定错误类型，避免 SDK 堆栈或服务端正文污染证据。
                return {
                    "mode": "live_context_compaction",
                    "model": provider.model,
                    "requests": budget.used,
                    "max_requests": budget.max_requests,
                    "max_output_tokens": provider.max_tokens,
                    "summary_timeout_seconds": timeout_seconds,
                    "passed": False,
                    "reason": str(error),
                    "usage": None,
                }
            summary = view.messages[2].content
            checks = {
                "prefix_preserved": view.messages[:2] == source[:2],
                "recent_pair_preserved": view.messages[-2:] == source[-2:],
                "mentions_parser": "parser.py" in summary,
                "mentions_test": "test_parser_empty" in summary and "18 passed" in summary,
                "mentions_windows_risk": "Windows" in summary,
                "mentions_constraint": "签名" in summary,
            }
            usage = manager.records[-1].usage
            return {
                "mode": "live_context_compaction",
                "model": provider.model,
                "requests": budget.used,
                "max_requests": budget.max_requests,
                "max_output_tokens": provider.max_tokens,
                "summary_timeout_seconds": timeout_seconds,
                "compacted": view.compacted,
                "estimated_tokens_after": view.estimated_tokens,
                "roles": [message.role for message in view.messages],
                "checks": checks,
                "passed": all(checks.values()),
                "usage": usage.model_dump() if usage is not None else None,
                # 输入完全是合成材料，保留摘要可让真实质量验收可复核。
                "summary": summary,
            }


def main() -> None:
    """默认运行离线演示；显式 --live 时只执行一次有预算的真实摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--live-timeout",
        type=float,
        default=45.0,
        help="真实摘要的墙钟总时限；DeepSeek 高峰排队可能需要显式增大",
    )
    parser.add_argument(
        "--live-model",
        choices=("deepseek-flash", "deepseek-v4-pro"),
        default="deepseek-flash",
        help="真实摘要使用的 DeepSeek 官方模型",
    )
    arguments = parser.parse_args()
    result = asyncio.run(
        run_live(arguments.live_timeout, arguments.live_model) if arguments.live else run_demo()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if arguments.live and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
