"""用 HTTP 模拟流验证真实 SDK 边界，测试期间不访问付费接口。"""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from codepilot.contracts import Message
from codepilot.observability.trace import EventRecorder
from codepilot.providers.deepseek import DeepSeekProvider, RequestBudget
from codepilot.providers.openai_compatible import OpenAICompatibleProvider
from codepilot.runtime import Runner
from codepilot.tools import create_basic_registry


def sse(parts: Sequence[dict[str, object]]) -> bytes:
    """将受控片段编码成 SDK 真正读取的 SSE 格式。"""
    return (
        "".join("data: " + json.dumps(part) + "\n\n" for part in parts) + "data: [DONE]\n\n"
    ).encode()


def chunk(delta: dict[str, object], finish: str | None = None) -> dict[str, object]:
    """构造最小 Chat Completions 协议片段。"""
    return {
        "id": "server-id",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mock",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def test_sdk_tool_roundtrip_and_budget(tmp_path: Path) -> None:
    """SDK 工具分片、结果回传、用量与跨轮额度全部经过真实编码路径。"""

    async def scenario() -> None:
        """只用 MockTransport 执行两轮，第三次在网络前拒绝。"""
        requests: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            """记录请求体并返回工具回合或文本回合。"""
            body = json.loads(request.content)
            requests.append(body)
            assert body["thinking"] == {"type": "disabled"}
            assert body["max_tokens"] == 256
            if len(requests) == 1:
                parts = [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path":"out.txt",',
                                    },
                                }
                            ]
                        }
                    ),
                    chunk(
                        {"tool_calls": [{"index": 0, "function": {"arguments": '"content":"ok"}'}}]}
                    ),
                    chunk({}, "tool_calls"),
                ]
                parts[-1]["usage"] = {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                }
            else:
                assert body["messages"][-1]["role"] == "tool"
                assert body["messages"][-1]["tool_call_id"] == "call-1"
                parts = [
                    chunk({"content": "done"}),
                    chunk({}, "stop"),
                    {
                        "id": "server-id",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "mock",
                        "choices": [],
                        "usage": {"prompt_tokens": 30, "completion_tokens": 2, "total_tokens": 32},
                    },
                ]
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(parts)
            )

        async with AsyncOpenAI(
            api_key="fake-test-key",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            registry = create_basic_registry(tmp_path)
            provider = DeepSeekProvider(client, registry.specs, budget=RequestBudget(2))
            runner = Runner(provider, registry)
            report = await runner.run((Message(role="user", content="write a file"),))
            assert report.reason == "completed"
            assert (tmp_path / "out.txt").read_text() == "ok"
            usages = [e.usage for e in runner.recorder.events if e.kind == "usage_updated"]
            assert usages[0] is not None and usages[0].output_tokens == 20
            assert usages[1] is not None and usages[1].input_tokens == 30
            exhausted = await Runner(provider, registry).run(
                (Message(role="user", content="again"),)
            )
            assert exhausted.reason == "provider_error" and len(requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["length", "content_filter", None])
def test_sdk_incomplete_finish_never_executes(tmp_path: Path, finish: str | None) -> None:
    """供应商长度截断、过滤和缺失结束原因都不能执行完整外观的调用。"""

    async def scenario() -> None:
        """给出合法参数但非法结束状态，确认执行次数为零。"""

        def handle(request: httpx.Request) -> httpx.Response:
            """输出看似完整但实际未正常结束的工具调用。"""
            part = chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "a",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path":"x","content":"oops"}',
                            },
                        }
                    ]
                },
                finish,
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse([part])
            )

        async with AsyncOpenAI(
            api_key="fake",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            runner = Runner(DeepSeekProvider(client), create_basic_registry(tmp_path))
            report = await runner.run((Message(role="user", content="test"),))
            assert report.reason == "contract_error" and report.tool_calls == 0
            assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


def test_sdk_errors_do_not_retry_or_persist_credentials(tmp_path: Path) -> None:
    """鉴权失败不会自动重试，供应商错误正文不会进入 Trace。"""

    async def scenario() -> None:
        """通过真实 SDK 异常处理路径检查请求上限与脱敏。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """返回携带假凭据的错误，用于验证正文不泄漏。"""
            nonlocal count
            count += 1
            return httpx.Response(500, json={"error": {"message": "SECRET_CREDENTIAL"}})

        async with AsyncOpenAI(
            api_key="fake",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            recorder = EventRecorder(tmp_path / "trace")
            runner = Runner(
                DeepSeekProvider(client), create_basic_registry(tmp_path), recorder=recorder
            )
            report = await runner.run((Message(role="user", content="SECRET_PROMPT"),))
            assert report.reason == "provider_error" and count == 1
            assert recorder.events[-1].error_type == "InternalServerError"
            assert all("SECRET" not in path.read_text() for path in (tmp_path / "trace").iterdir())

    asyncio.run(scenario())


def test_compatible_gateway_ignores_empty_tool_placeholders(tmp_path: Path) -> None:
    """兼容网关只有 index 的工具占位项不能破坏已经组装的合法调用。"""

    async def scenario() -> None:
        """模拟 Qwen 网关的空占位分片，再完成一次只读工具闭环。"""
        request_count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """首轮返回省略 index 的工具片段和空占位，次轮返回最终文本。"""
            nonlocal request_count
            request_count += 1
            body = json.loads(request.content)
            assert "thinking" not in body
            if request_count == 1:
                parts = [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": None,
                                    "id": "call-compatible",
                                    "type": "function",
                                    "function": {"name": "glob", "arguments": ""},
                                }
                            ]
                        }
                    ),
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": None,
                                    "function": {"arguments": '{"pattern":"*"}'},
                                }
                            ]
                        }
                    ),
                    chunk(
                        {
                            "tool_calls": [
                                {"index": None, "function": {"name": None, "arguments": None}}
                            ]
                        }
                    ),
                    chunk({}, "tool_calls"),
                ]
            else:
                parts = [chunk({"content": "完成"}), chunk({}, "stop")]
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(parts)
            )

        async with AsyncOpenAI(
            api_key="fake",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            registry = create_basic_registry(tmp_path)
            provider = OpenAICompatibleProvider(client, registry.specs, model="qwen-test")
            report = await Runner(provider, registry).run(
                (Message(role="user", content="检查目录"),)
            )

        assert report.reason == "completed"
        assert report.tool_calls == 1
        assert request_count == 2

    asyncio.run(scenario())
