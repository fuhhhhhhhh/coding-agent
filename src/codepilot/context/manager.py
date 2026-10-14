"""组合大输出落盘、按需读取、Token 估算和完整回合压缩。"""

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Annotated, Protocol

from pydantic import Field

from codepilot.context.artifacts import ArtifactNotFoundError, ContextArtifactStore
from codepilot.context.models import (
    ArtifactReference,
    ContextLimits,
    ContextRecord,
    ContextView,
)
from codepilot.context.summarizer import Summarizer
from codepilot.contracts import Message, ToolCall, ToolResult
from codepilot.contracts.models import Contract
from codepilot.contracts.stream import Usage
from codepilot.observability.trace import emit
from codepilot.tools.base import Tool, ToolFailure, ToolOutput, ToolSpec
from codepilot.tools.registry import ToolRegistry


class ContextPreparationError(RuntimeError):
    """表示 prompt 在安全压缩后仍无法满足预算或摘要过程失败。"""


class ContextExecutor(Protocol):
    """约束 ContextManager 包装的下游工具目录和执行入口。"""

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """返回下游真实工具声明。"""
        ...

    async def execute(self, call: ToolCall) -> ToolResult:
        """执行普通工作区工具并返回完整结果。"""
        ...


class ReadContextArtifactArguments(Contract):
    """定义按 opaque ID 和字符范围读取原始工具输出的参数。"""

    artifact_id: Annotated[str, Field(pattern=r"^artifact-[0-9a-f]{32}$")]
    start: Annotated[int, Field(ge=0)] = 0
    max_chars: Annotated[int, Field(ge=1, le=65_536)] = 8_000


class ReadContextArtifactTool(Tool[ReadContextArtifactArguments]):
    """只读访问当前 ContextArtifactStore 已登记的输出片段。"""

    name = "read_context_artifact"
    description = "按 artifact_id 和字符范围读取此前落盘的完整工具输出"
    effect = "read"
    resources = ("context:read",)
    default_timeout = 5.0
    arguments_model = ReadContextArtifactArguments

    def __init__(self, store: ContextArtifactStore, default_max_chars: int) -> None:
        """绑定受控 store，并保存策略允许的默认最大读取量。"""
        self.store = store
        self.default_max_chars = default_max_chars

    async def run(self, arguments: ReadContextArtifactArguments) -> str | ToolOutput:
        """读取一个有界片段，并返回可继续分页的范围头。"""
        max_chars = min(arguments.max_chars, self.default_max_chars)
        try:
            fragment = self.store.read(arguments.artifact_id, arguments.start, max_chars)
        except ArtifactNotFoundError as error:
            raise ToolFailure("artifact_not_found") from error
        except ValueError as error:
            raise ToolFailure("artifact_range_invalid") from error
        return (
            f"artifact_id={fragment.artifact_id}\n"
            f"range={fragment.start}:{fragment.end}/{fragment.total_characters}\n"
            f"has_more={str(fragment.has_more).lower()}\n"
            f"content:\n{fragment.content}"
        )


class ContextManager:
    """包装工具执行器，并为每次模型请求构造有预算的 prompt 视图。

    原始工具输出写入 controller-owned ArtifactStore；Runner 历史只得到短引用。
    历史压缩同样只改变发给 Provider 的视图，不修改 Runner 保存的规范消息历史。
    因此调试证据、后续压缩和模型当前看到的内容各有清楚的数据所有权。
    """

    def __init__(
        self,
        registry: ContextExecutor,
        store: ContextArtifactStore,
        *,
        limits: ContextLimits | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        """绑定下游执行器、外部 artifact store、预算策略和可选摘要器。"""
        self.registry = registry
        self.store = store
        self.limits = limits or ContextLimits()
        self.summarizer = summarizer
        self._artifact_registry = ToolRegistry(
            (ReadContextArtifactTool(store, self.limits.artifact_read_chars),)
        )
        self.records: list[ContextRecord] = []
        self._summary_cache: dict[str, Message] = {}

    @property
    def supports_parallel(self) -> bool:
        """沿用下游调度器并发能力；artifact 读取本身不降低该能力。"""
        return bool(getattr(self.registry, "supports_parallel", False))

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """在下游工具之后追加只读 artifact 工具声明。"""
        names = {spec.name for spec in self.registry.specs}
        if ReadContextArtifactTool.name in names:
            raise ValueError("downstream executor already defines read_context_artifact")
        return (*self.registry.specs, *self._artifact_registry.specs)

    async def execute(self, call: ToolCall) -> ToolResult:
        """执行工具；大结果落盘后只把可定位的短引用交给 Runtime。"""
        if call.name == ReadContextArtifactTool.name:
            result = await self._artifact_registry.execute(call)
            self._record(
                "artifact_read",
                artifact_id=str(call.arguments.get("artifact_id", "")),
                call_id=call.call_id,
                outcome="error" if result.is_error else "success",
            )
            return result
        result = await self.registry.execute(call)
        encoded = result.content.encode("utf-8")
        if len(encoded) <= self.limits.large_output_bytes:
            return result
        try:
            reference = self.store.write(call.call_id, result.content)
        except OSError as error:
            # 落盘失败时保留原始结果，绝不能用一个不存在的引用替换唯一证据。
            self._record(
                "artifact_write_failed",
                call_id=call.call_id,
                outcome=type(error).__name__,
                original_bytes=len(encoded),
            )
            emit("context_degraded", call_id=call.call_id, error_type=type(error).__name__)
            return result
        self._record(
            "artifact_written",
            artifact_id=reference.artifact_id,
            call_id=call.call_id,
            outcome="stored",
            original_bytes=reference.bytes,
        )
        emit(
            "context_artifact_written",
            call_id=call.call_id,
            artifact_id=reference.artifact_id,
            original_bytes=reference.bytes,
        )
        return result.model_copy(
            update={"content": self._artifact_prompt(reference, result.content)}
        )

    async def prepare(self, messages: Sequence[Message], *, force: bool = False) -> ContextView:
        """构造下一次 Provider 请求视图，必要时按完整回合压缩旧历史。"""
        snapshot = tuple(message.model_copy(deep=True) for message in messages)
        before = estimate_messages(snapshot)
        # 单条消息超过整个可用预算时，摘要无法安全保留其中唯一的目标或约束。
        if any(
            estimate_message(message) > self.limits.usable_prompt_tokens for message in snapshot
        ):
            self._compaction_failed("single_message_too_large", before)
        if not force and before <= self.limits.compact_trigger_tokens:
            return ContextView(messages=snapshot, estimated_tokens=before)
        try:
            prefix, rounds = _split_atomic_rounds(snapshot)
        except ContextPreparationError as error:
            # 配对损坏时不能继续摘要，否则可能把一次工具调用和错误结果混成可信事实。
            self._compaction_failed(str(error), before)
        keep_count = min(self.limits.recent_rounds, len(rounds))
        old_rounds = rounds[:-keep_count] if keep_count else rounds
        recent_rounds = rounds[-keep_count:] if keep_count else ()
        if not old_rounds:
            # 没有可压缩的旧回合时，不能删除初始约束或拆开最近工具调用组。
            if before > self.limits.compact_target_tokens:
                self._compaction_failed("no_compactable_history", before)
            return ContextView(messages=snapshot, estimated_tokens=before)
        old_messages = tuple(message for group in old_rounds for message in group)
        try:
            summary, usage, cache_hit = await self._summary(old_messages)
        except Exception as error:
            # 摘要失败不能退回一个已经超过触发阈值的 prompt 并继续请求模型。
            self._compaction_failed(f"summary_failed:{type(error).__name__}", before)
        view = (*prefix, summary, *(message for group in recent_rounds for message in group))
        after = estimate_messages(view)
        if after > self.limits.compact_target_tokens:
            self._compaction_failed("summary_above_target", before, after)
        self._record(
            "compacted",
            outcome="cache" if cache_hit else "model",
            tokens_before=before,
            tokens_after=after,
            usage=usage,
        )
        emit(
            "context_compacted",
            outcome="cache" if cache_hit else "model",
            prompt_tokens_before=before,
            prompt_tokens_after=after,
            usage=usage,
        )
        return ContextView(messages=view, estimated_tokens=after, compacted=True)

    async def compact(self, messages: Sequence[Message]) -> ContextView:
        """手动触发与自动路径相同的原子回合压缩和预算验证。"""
        return await self.prepare(messages, force=True)

    async def _summary(self, messages: tuple[Message, ...]) -> tuple[Message, Usage, bool]:
        """复用相同旧历史的摘要，避免每轮重复支付压缩成本。"""
        cache_key = hashlib.sha256(
            json.dumps(
                [message.model_dump(mode="json") for message in messages],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cached = self._summary_cache.get(cache_key)
        if cached is not None:
            return cached, Usage(input_tokens=0, output_tokens=0, source="estimate"), True
        if self.summarizer is None:
            raise RuntimeError("summarizer is required after compact trigger")
        result = await self.summarizer.summarize(messages, self.limits.compact_target_tokens)
        summary = Message(role="system", content=f"[历史压缩摘要]\n{result.content}")
        self._summary_cache[cache_key] = summary
        return summary, result.usage, False

    def _artifact_prompt(self, reference: ArtifactReference, content: str) -> str:
        """生成含头尾、错误线索、范围和读取入口的稳定短引用。"""
        preview = self.limits.artifact_preview_chars
        head_size = preview // 2
        tail_size = preview - head_size
        head = content[:head_size]
        tail = content[-tail_size:] if len(content) > head_size else ""
        lines = content.splitlines()
        # 扫描完整输出中的错误行，使只出现在中段的关键失败仍能进入 prompt。
        matches = [
            line.strip()[:240]
            for line in lines
            if re.search(r"error|failed|failure|exception|traceback|fatal|assert", line, re.I)
        ][:8]
        clues = "\n".join(matches) or "<未发现常见错误关键词>"
        first_nonempty = next((line.strip()[:160] for line in lines if line.strip()), "<空输出>")
        # 摘要只使用行数、错误计数和有界首行，保证它不会再次随原始输出无限增长。
        summary = (
            f"共 {len(lines)} 行，检测到 {len(matches)} 条有界错误线索；"
            f"首个非空行：{first_nonempty}"
        )
        return (
            "[工具输出已落盘]\n"
            f"artifact_id={reference.artifact_id}\n"
            f"source_call_id={reference.call_id}\n"
            f"characters={reference.characters}\n"
            f"bytes={reference.bytes}\n"
            f"sha256={reference.sha256}\n"
            f"summary={summary}\n"
            f"head_range=0:{len(head)}\n{head}\n"
            f"middle_error_clues:\n{clues}\n"
            f"tail_range={reference.characters - len(tail)}:{reference.characters}\n{tail}\n"
            "读取完整范围：调用 read_context_artifact，传入 artifact_id、start、max_chars。"
        )

    def _compaction_failed(self, outcome: str, before: int, after: int | None = None) -> None:
        """记录不含历史正文的失败事实，并抛出供 Runtime 转终态的稳定错误。"""
        self._record(
            "compaction_failed",
            outcome=outcome,
            tokens_before=before,
            tokens_after=after,
        )
        emit(
            "context_degraded",
            outcome=outcome,
            prompt_tokens_before=before,
            prompt_tokens_after=after,
        )
        raise ContextPreparationError(outcome)

    def _record(self, kind: str, **payload: object) -> None:
        """追加一条不含 prompt、摘要或工具正文的 ContextRecord。"""
        record = ContextRecord.model_validate(
            {"sequence": len(self.records) + 1, "kind": kind, **payload}
        )
        self.records.append(record)


def estimate_message(message: Message) -> int:
    """按 ASCII 约四字符一 Token、非 ASCII 约一字符一 Token 估算消息。"""
    serialized = message.model_dump_json()
    ascii_characters = sum(character.isascii() for character in serialized)
    non_ascii_characters = len(serialized) - ascii_characters
    # 这是跨模型的保守近似，不冒充 provider tokenizer 的精确计数。
    return (ascii_characters + 3) // 4 + non_ascii_characters + 4


def estimate_messages(messages: Sequence[Message]) -> int:
    """累加消息估算，并为请求级包装保留固定协议开销。"""
    return 8 + sum(estimate_message(message) for message in messages)


def _split_atomic_rounds(
    messages: tuple[Message, ...],
) -> tuple[tuple[Message, ...], tuple[tuple[Message, ...], ...]]:
    """把初始约束和 assistant+tool 配对组分开，禁止压缩拆散调用结果。"""
    prefix: list[Message] = []
    rounds: list[tuple[Message, ...]] = []
    index = 0
    while index < len(messages) and messages[index].role in {"system", "user"}:
        prefix.append(messages[index])
        index += 1
    while index < len(messages):
        assistant = messages[index]
        if assistant.role != "assistant":
            raise ContextPreparationError("history_pairing_invalid")
        group = [assistant]
        index += 1
        expected_ids = [call.call_id for call in assistant.tool_calls]
        for expected_id in expected_ids:
            if index >= len(messages):
                raise ContextPreparationError("history_pairing_incomplete")
            result_message = messages[index]
            if (
                result_message.role != "tool"
                or result_message.tool_result is None
                or result_message.tool_result.call_id != expected_id
            ):
                raise ContextPreparationError("history_pairing_invalid")
            group.append(result_message)
            index += 1
        rounds.append(tuple(group))
    return tuple(prefix), tuple(rounds)
