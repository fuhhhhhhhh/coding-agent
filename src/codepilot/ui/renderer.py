"""把 Runtime 事件和最终产品结果渲染为人类文本或脱敏 JSONL。"""

import json
from typing import TextIO

from codepilot.app.models import ProductRunResult, RunIdentity
from codepilot.contracts.stream import RuntimeEvent
from codepilot.observability import EventRecorder


class EventRenderer:
    """只观察运行事件；界面不持有 Provider、工具或执行后端。"""

    def __init__(self, stream: TextIO, *, jsonl: bool = False) -> None:
        """绑定输出流和稳定格式，并跟踪是否已经展示流式正文。"""
        self.stream = stream
        self.jsonl = jsonl
        self._streamed_text = False

    def started(self, identity: RunIdentity, backend: str, provider: str) -> None:
        """在副作用开始前展示本次运行身份和所选执行模式。"""
        if self.jsonl:
            self._json(
                {
                    "kind": "product_started",
                    **identity.model_dump(mode="json"),
                    "backend": backend,
                    "provider": provider,
                }
            )
            return
        self._line(f"会话 {identity.session_id}｜任务 {identity.task_id}｜运行 {identity.run_id}")
        self._line(f"后端 {backend}｜Provider {provider}")

    def __call__(self, event: RuntimeEvent) -> None:
        """按事件类型展示进度；JSONL 使用 Trace 相同的默认脱敏规则。"""
        if self.jsonl:
            self._json(EventRecorder.redacted(event))
            return
        if event.kind == "text_delta":
            self.stream.write(event.text)
            self.stream.flush()
            self._streamed_text = True
        elif event.kind == "tool_call_ready" and event.call is not None:
            self._line(f"\n[工具请求] {event.call.name}")
        elif event.kind == "tool_started":
            self._line(f"[工具开始] {event.call_id}")
        elif event.kind == "tool_finished":
            self._line(f"[工具结束] {event.call_id}：{event.outcome}")
        elif event.kind == "approval_requested":
            self._line(f"[等待审批] {event.call_id}")
        elif event.kind == "context_compacted":
            self._line(f"[上下文压缩] {event.prompt_tokens_before} → {event.prompt_tokens_after}")

    def finished(self, result: ProductRunResult) -> None:
        """展示最终回答、停止原因、计数和保留的 Trace 位置。"""
        answer = next(
            (
                message.content
                for message in reversed(result.report.messages)
                if message.role == "assistant" and message.content.strip()
            ),
            "",
        )
        if self.jsonl:
            self._json(
                {
                    "kind": "product_result",
                    **result.identity.model_dump(mode="json"),
                    "state": result.report.state.value,
                    "reason": result.report.reason,
                    "steps": result.report.steps,
                    "tool_calls": result.report.tool_calls,
                    "answer": answer,
                    "trace_path": str(result.resources.trace_path)
                    if result.resources.trace_path is not None
                    else None,
                    "backend_closed": result.resources.backend_closed,
                    "context_artifacts_cleaned": result.resources.context_artifacts_cleaned,
                }
            )
            return
        # 非流式 Provider 没有 text_delta，需要从 RunReport 补上正式回答。
        if answer and not self._streamed_text:
            self._line(f"\n{answer}")
        elif self._streamed_text:
            self._line("")
        self._line(
            f"[运行结束] {result.report.reason}｜步骤 {result.report.steps}｜"
            f"工具 {result.report.tool_calls}"
        )
        if result.report.reason == "output_limit":
            # 说明安全停止的直接修复方向，同时强调半截工具参数没有被执行。
            self._line("[失败详情] 模型输出达到 max_tokens；不完整工具调用未执行。")
        if result.resources.trace_path is not None:
            self._line(f"[Trace] {result.resources.trace_path}")

    def _line(self, value: str) -> None:
        """写入一行并立即刷新，使长任务进度不会滞留在缓冲区。"""
        self.stream.write(value + "\n")
        self.stream.flush()

    def _json(self, value: dict[str, object]) -> None:
        """输出一条 UTF-8 JSONL，便于脚本消费而不混入人类前缀。"""
        self._line(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
