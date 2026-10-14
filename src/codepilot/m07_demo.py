"""演示离线分片、审批和 Trace；显式 --live 才进行有额度的 DeepSeek 验证。"""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from openai import AsyncOpenAI

from codepilot.contracts import Message
from codepilot.contracts.run import RunLimits
from codepilot.contracts.stream import StreamChunk
from codepilot.observability.trace import EventRecorder
from codepilot.permissions import (
    PermissionedExecutor,
    PermissionEngine,
    ScriptedApproval,
    ask_edits_policy,
    read_only_policy,
)
from codepilot.providers.deepseek import DeepSeekProvider, RequestBudget
from codepilot.providers.fake_stream import FakeStreamProvider
from codepilot.runtime import Runner
from codepilot.tools import create_basic_registry


async def offline(directory: Path) -> None:
    """运行成功、断流和审批取消场景，保存不含正文的 Trace。"""
    call = StreamChunk(
        kind="tool_call_delta",
        call_id="write-1",
        name="write_file",
        arguments_delta='{"path":"value.txt","content":"ok"}',
    )
    end = StreamChunk(kind="turn_finished", finish_reason="tool_calls")
    final = (
        StreamChunk(kind="text_delta", text="done"),
        StreamChunk(kind="turn_finished", finish_reason="stop"),
    )
    normal: list[tuple[dict[str, object], ...]] = []
    for scenario in ("approved", "replay", "interrupted", "cancelled"):
        with tempfile.TemporaryDirectory(prefix="codepilot-m07-") as temporary:
            root = Path(temporary)
            approval = ScriptedApproval((scenario != "cancelled",))
            executor = PermissionedExecutor(
                create_basic_registry(root), PermissionEngine(ask_edits_policy()), approval
            )
            first = (
                (call, OSError("controlled disconnect"))
                if scenario == "interrupted"
                else (call, end)
            )
            recorder = EventRecorder(directory / scenario)
            runner = Runner(FakeStreamProvider((first, final)), executor, recorder=recorder)
            report = await runner.run((Message(role="user", content="write the fixture"),))
            if scenario in {"approved", "replay"}:
                normal.append(recorder.normalized())
            assert (root / "value.txt").exists() == (scenario in {"approved", "replay"})
            assert report.reason == ("provider_error" if scenario == "interrupted" else "completed")
            print(
                json.dumps(
                    {
                        "scenario": scenario,
                        "reason": report.reason,
                        "tool_calls": report.tool_calls,
                        "events": len(recorder.events),
                    }
                )
            )
    assert normal[0] == normal[1]
    print(json.dumps({"replay_equal": True, "note": "behavior replay, not model task accuracy"}))


async def live(directory: Path) -> None:
    """仅向官方地址发送最多三次短请求，不加载或发送用户仓库内容。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is not configured; no request sent")
    budget = RequestBudget(3)
    with tempfile.TemporaryDirectory(prefix="codepilot-m07-live-") as temporary:
        root = Path(temporary)
        (root / "number.txt").write_text("17\n", encoding="utf-8")
        registry = create_basic_registry(root)
        executor = PermissionedExecutor(registry, PermissionEngine(read_only_policy()))
        read_tools = tuple(spec for spec in registry.specs if spec.name == "read_file")
        async with AsyncOpenAI(
            api_key=key, base_url="https://api.deepseek.com", max_retries=0, timeout=20.0
        ) as client:
            provider = DeepSeekProvider(client, read_tools, budget=budget, max_tokens=256)
            recorder = EventRecorder(directory / "live")
            runner = Runner(
                provider,
                executor,
                RunLimits(max_steps=3, max_tool_calls=2, timeout_seconds=60.0),
                recorder=recorder,
            )
            report = await runner.run(
                (
                    Message(
                        role="user",
                        content=(
                            "Use read_file to read number.txt, "
                            "then reply only with the number in that file."
                        ),
                    ),
                )
            )
            passed = (
                report.reason == "completed"
                and report.tool_calls >= 1
                and report.messages[-1].content.strip() == "17"
            )
            print(
                json.dumps(
                    {
                        "mode": "live",
                        "model": provider.model,
                        "requests": budget.used,
                        "max_requests": budget.max_requests,
                        "max_output_tokens_per_request": provider.max_tokens,
                        "reason": report.reason,
                        "tool_calls": report.tool_calls,
                        "passed": passed,
                        "usage": [
                            event.usage.model_dump()
                            for event in recorder.events
                            if event.kind == "usage_updated" and event.usage is not None
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            if not passed:
                raise SystemExit(1)


def main() -> None:
    """解析显式联网选项，默认仅离线验证并落盘元数据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--trace-dir", type=Path, default=Path(".codepilot-traces"))
    arguments = parser.parse_args()
    asyncio.run(live(arguments.trace_dir) if arguments.live else offline(arguments.trace_dir))


if __name__ == "__main__":
    main()
