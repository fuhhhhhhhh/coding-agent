"""把 Runtime 事件和最终产品结果渲染为人类文本或脱敏 JSONL。"""

import json
import os
import shutil
import unicodedata
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
        self._approvals: set[str] = set()
        self._deferred: list[RuntimeEvent] = []
        self._color = (
            bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"
        )

    def welcome(self, workspace: object, model: str, backend: str, session_id: str) -> None:
        """绘制类似现代 Coding Agent 的启动卡片，非 TTY 保持纯文本兼容。"""
        if self.jsonl:
            return
        title = self._style("CodePilot", "1;97")
        width = max(38, min(100, shutil.get_terminal_size((100, 24)).columns - 2))
        self._line("╭─ " + title + " " + "─" * (width - 12) + "╮")
        for row in (
            f"model     {model}  backend {backend}",
            f"directory {workspace}",
            f"session   {session_id}",
        ):
            visible = ""
            used = 0
            for char in row:
                # 配置/路径中的控制字符不能变成终端指令；按实际字符宽度对齐中文。
                char = char if char.isprintable() else "?"
                size = (
                    0
                    if unicodedata.combining(char)
                    else 2
                    if unicodedata.east_asian_width(char) in {"W", "F"}
                    else 1
                )
                if used + size > width - 4:
                    visible += "…"
                    used += 1
                    break
                visible += char
                used += size
            self._line("│ " + visible + " " * (width - 2 - used) + " │")
        self._line("╰" + "─" * width + "╯")
        self._line("输入任务开始工作；/help 查看命令，/exit 退出。")

    def started(self, identity: RunIdentity, backend: str, provider: str) -> None:
        """在副作用开始前展示本次运行身份和所选执行模式。"""
        if self.jsonl:
            self._json(
                {
                    "schema_version": 1,
                    "kind": "product_started",
                    **identity.model_dump(mode="json"),
                    "backend": backend,
                    "provider": provider,
                }
            )
            return
        self._line(self._style(f"▸ session {identity.session_id}  task {identity.task_id}", "90"))
        # 保留稳定的中文身份行，便于旧脚本和用户从终端日志搜索任务。
        self._line(f"会话 {identity.session_id}｜任务 {identity.task_id}")
        self._line(
            f"  backend {self._style(backend, '36')} · provider {self._style(provider, '36')}"
        )

    def __call__(self, event: RuntimeEvent) -> None:
        """按事件类型展示进度；JSONL 使用 Trace 相同的默认脱敏规则。"""
        if self.jsonl:
            self._json(EventRecorder.redacted(event))
            return
        if event.kind == "approval_resolved":
            self._approvals.discard(event.call_id or "")
            if not self._approvals:
                queued, self._deferred = self._deferred, []
                for pending in queued:
                    self(pending)
            return
        if event.kind == "approval_requested":
            self._approvals.add(event.call_id or "")
            return
        if self._approvals:
            # 审批期间暂停人类进度写入，避免覆盖输入提示；机器事件仍实时输出。
            if len(self._deferred) < 128:
                self._deferred.append(event)
            return
        if event.kind == "model_requested":
            # 请求已发起但首个分片未到时给出明确反馈，避免把远端等待误认为终端挂起。
            self._line("  · 正在等待模型响应…（Ctrl-C 可取消）")
        elif event.kind == "text_delta":
            self.stream.write(event.text)
            self.stream.flush()
            self._streamed_text = True
        elif event.kind == "tool_call_ready" and event.call is not None:
            self._line(f"\n  {self._style('◆', '33')} {event.call.name}")
        elif event.kind == "tool_started":
            self._line(f"  {self._style('·', '33')} running {event.call_id}")
        elif event.kind == "tool_finished":
            icon = "✓" if event.outcome == "success" else "!"
            color = "32" if event.outcome == "success" else "31"
            self._line(f"  {self._style(icon, color)} {event.call_id} · {event.outcome}")
        elif event.kind == "provider_retry":
            self._line(f"  ↻ {event.outcome} · {event.duration_seconds:g} 秒后重试；Ctrl-C 可取消")
        elif event.kind == "context_compacted":
            self._line(
                f"  {self._style('↻', '35')} context compacted "
                f"{event.prompt_tokens_before} → {event.prompt_tokens_after}"
            )

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
        if result.report.detail == "context_compacted":
            # 手动压缩不是新助手回答，不能重复显示历史最后一条回复冒充本次生成。
            answer = ""
        if self.jsonl:
            self._json(
                {
                    "schema_version": 1,
                    "kind": "product_result",
                    "execution_source": result.execution_source,
                    "session_budget": result.session_budget,
                    **result.identity.model_dump(mode="json"),
                    "state": result.report.state.value,
                    "reason": result.report.reason,
                    "error_type": result.report.detail or None,
                    "steps": result.report.steps,
                    "tool_calls": result.report.tool_calls,
                    "answer": answer,
                    "trace_path": str(result.resources.trace_path)
                    if result.resources.trace_path is not None
                    else None,
                    "backend_closed": result.resources.backend_closed,
                    "context_artifacts_cleaned": result.resources.context_artifacts_cleaned,
                    "delivery": result.delivery.model_dump(mode="json")
                    if result.delivery is not None
                    else None,
                }
            )
            return
        # 非流式 Provider 没有 text_delta，需要从 RunReport 补上正式回答。
        if result.report.detail == "context_compacted":
            self._line("[上下文] 已完成历史检查或压缩；未执行主模型请求和工作区工具。")
        if answer and not self._streamed_text:
            self._line(f"\n{answer}")
        elif self._streamed_text:
            self._line("")
        outcome_color = "32" if result.report.reason == "completed" else "31"
        status_line = (
            f"[运行结束] {result.report.reason} · "
            f"{result.report.steps} steps · {result.report.tool_calls} tools"
        )
        self._line(
            self._style(
                status_line,
                outcome_color,
            )
        )
        if result.report.reason == "output_limit":
            # 说明安全停止的直接修复方向，同时强调半截工具参数没有被执行。
            self._line("[失败详情] 模型输出达到 max_tokens；不完整工具调用未执行。")
        elif result.report.reason in {"session_limit", "persistence_error"}:
            self._line(f"[会话停止] {result.report.detail}；未启动后续工具。")
        elif result.report.reason == "provider_error":
            # 只展示异常类型；SDK 错误正文可能包含 URL、请求片段或认证信息。
            error_type = result.report.detail or "UnknownProviderError"
            self._line(f"[失败详情] Provider 请求失败：{error_type}。本次未执行后续工具。")
            if error_type in {"APIConnectionError", "APITimeoutError"}:
                # 连接和超时通常可以安全地由用户重新发起任务，但不能谎称一定未到达服务端。
                self._line("[建议] 检查网关网络状态后重试；详细类型已写入 Trace。")
            elif error_type in {"AuthenticationError", "PermissionDeniedError"}:
                self._line("[建议] 检查当前 Provider 对应的用户凭据和权限；认证失败不会自动重试。")
            elif error_type == "RateLimitError":
                self._line("[建议] 限流重试已停止；稍后继续，检查额度或 Retry-After。")
            elif error_type == "BudgetExceededError":
                self._line(
                    "[建议] 请求或 Token 预算已耗尽；查看 sessions show 的预算记录后调整配置。"
                )
        if result.resources.trace_path is not None:
            self._line(f"[Trace] {result.resources.trace_path}")
        if result.session_budget:
            budget = result.session_budget
            self._line(
                f"[会话累计] 请求 {budget['request_attempts']} · "
                f"输入 {budget['input_tokens']} / 输出 {budget['output_tokens']} Token · "
                f"未结算 {budget['pending_attempts']} · 费用 {budget['cost_status']}"
            )
        if result.bounded_delivery is not None:
            bounded = result.bounded_delivery
            self._line(f"[受限副本交付] {bounded['state']} · {len(bounded['files'])} 个文件")
            if bounded["conflicts"]:
                self._line("[交付冲突] 源文件已变化，产物保留；没有覆盖这些用户修改。")
            self._line(f"[保留副本] {bounded['artifact_root']}")
        if result.delivery is not None:
            delivery = result.delivery
            self._line(f"[Git 起点] {delivery.base_commit}")
            if delivery.state == "cleaned":
                self._line("[工作树] 无修改，任务 worktree 已安全清理。")
            else:
                self._line(f"[工作树] {delivery.workspace}（已保留）")
                self._line(f"[修改文件] {len(delivery.changed_files)}")
                if delivery.patch_path is not None:
                    self._line(f"[补丁] {delivery.patch_path}")
                self._line(f"[补丁重放] {delivery.replay_status}")
                if delivery.verification is not None:
                    self._line(f"[独立复验] {delivery.verification.status}")

    def _line(self, value: str) -> None:
        """写入一行并立即刷新，使长任务进度不会滞留在缓冲区。"""
        self.stream.write(value + "\n")
        self.stream.flush()

    def _style(self, value: str, code: str) -> str:
        """仅在真正终端启用 ANSI 色彩，测试与管道输出保持可比较纯文本。"""
        return f"\033[{code}m{value}\033[0m" if self._color else value

    def _json(self, value: dict[str, object]) -> None:
        """输出一条 UTF-8 JSONL，便于脚本消费而不混入人类前缀。"""
        self._line(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
