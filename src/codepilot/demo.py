"""M01 验收入口：发起两次独立请求，不运行 Agent 或工具循环。"""

import asyncio
import sys
from pathlib import Path

from codepilot.config import load_config
from codepilot.contracts import AgentEvent, Message
from codepilot.providers.base import Provider
from codepilot.providers.fake import FakeProvider


async def collect_demo_events(provider: Provider) -> tuple[AgentEvent, AgentEvent]:
    """分别请求文本回复和工具请求，并将二者包装为 M01 事件。"""
    text = await provider.complete((Message(role="user", content="Say you are ready."),))
    tool_request = await provider.complete((Message(role="user", content="Request README.md."),))
    return (
        AgentEvent(
            session_id="demo-session",
            run_id="demo-run",
            turn_id="turn-1",
            sequence=1,
            message=text,
        ),
        AgentEvent(
            session_id="demo-session",
            run_id="demo-run",
            turn_id="turn-2",
            sequence=2,
            message=tool_request,
        ),
    )


def main() -> None:
    """校验可选配置路径，并打印确定性的 M01 JSONL 演示。"""
    # 尽早拒绝多余参数，避免输入错误悄悄改变实际验证的配置。
    if len(sys.argv) > 2:
        raise SystemExit("usage: python -m codepilot.demo [config.toml]")
    load_config(Path(sys.argv[1]) if len(sys.argv) == 2 else None)
    for event in asyncio.run(collect_demo_events(FakeProvider())):
        print(event.model_dump_json())


if __name__ == "__main__":
    main()
