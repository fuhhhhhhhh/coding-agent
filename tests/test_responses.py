"""使用真实 SDK SSE 解析验证第二协议的工具配对、断流和终态边界。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from codepilot.contracts import Message
from codepilot.providers.openai_compatible import RequestBudget
from codepilot.providers.responses import ResponsesProvider
from codepilot.runtime import Runner
from codepilot.tools import create_basic_registry


def response_events(*, tool: bool = False, terminal: str = "response.completed") -> bytes:
    """构造带单调序号、最终函数清单和 usage 的 Responses 语义事件。"""
    item = {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "write_file",
        "arguments": '{"path":"answer.txt","content":"42"}',
        "status": "completed",
    }
    if tool:
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "arguments": "", "status": "in_progress"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc-1",
                "delta": item["arguments"],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
    else:
        events = [
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "item_id": "msg-1",
                "content_index": 0,
                "delta": "完成",
                "logprobs": [],
            }
        ]
    if terminal != "missing":
        events.append(
            {
                "type": "response.completed" if terminal == "conflict" else terminal,
                "response": {
                    "id": "resp-1",
                    "status": "completed"
                    if terminal in {"response.completed", "conflict"}
                    else "incomplete",
                    "output": [{**item, "name": "shell"}]
                    if terminal == "conflict"
                    else ([item] if tool else []),
                    "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
                },
            }
        )
    return "".join(
        "event: " + str(e["type"]) + "\ndata: " + json.dumps({**e, "sequence_number": n}) + "\n\n"
        for n, e in enumerate(events)
    ).encode()


def test_responses_tool_roundtrip_uses_same_runtime(tmp_path: Path) -> None:
    """函数调用经统一注册表写文件，结果以 call_id 回传第二协议。"""

    async def scenario() -> None:
        """独立核对真实文件、HTTP 消息和请求账本。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """返回工具流后核验 function_call_output 历史，再结束任务。"""
            nonlocal count
            count += 1
            assert request.url.path == "/responses"
            body = json.loads(request.content)
            assert body["store"] is False
            if count == 2:
                assert body["input"][-1] == {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "written: path=answer.txt bytes=2",
                }
            return httpx.Response(
                200,
                content=response_events(tool=count == 1),
                headers={"content-type": "text/event-stream"},
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            budget = RequestBudget(2)
            provider = ResponsesProvider(
                client, create_basic_registry(tmp_path).specs, model="mock", budget=budget
            )
            report = await Runner(provider, create_basic_registry(tmp_path)).run(
                (Message(role="user", content="写文件"),)
            )
            assert report.reason == "completed" and report.tool_calls == 1
            assert (tmp_path / "answer.txt").read_text() == "42"
            assert budget.ledger.snapshot.requests == 2
            assert budget.ledger.snapshot.input_tokens == 100

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "terminal,reason",
    [
        ("missing", "contract_error"),
        ("response.incomplete", "output_limit"),
        ("response.failed", "contract_error"),
        ("conflict", "contract_error"),
    ],
)
def test_responses_incomplete_never_executes_tools(
    tmp_path: Path, terminal: str, reason: str
) -> None:
    """已接收完整工具参数但未确认成功终态时仍零执行。"""

    async def scenario() -> None:
        """用真实 SSE 协议边界核验截断、失败和缺失终态。"""

        def handle(request: httpx.Request) -> httpx.Response:
            """返回指定故障流供 SDK 解码。"""
            return httpx.Response(
                200,
                content=response_events(tool=True, terminal=terminal),
                headers={"content-type": "text/event-stream"},
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            report = await Runner(
                ResponsesProvider(client, model="mock"), create_basic_registry(tmp_path)
            ).run((Message(role="user", content="写文件"),))
            assert report.reason == reason and report.tool_calls == 0
            assert not (tmp_path / "answer.txt").exists()

    asyncio.run(scenario())
