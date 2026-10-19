"""验证真实 SDK 编码链的重试、尝试预算、取消与断流不重放。"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from codepilot.contracts import Message
from codepilot.providers.openai_compatible import OpenAICompatibleProvider, RequestBudget
from codepilot.providers.reliability import RetryPolicy
from codepilot.runtime import Runner
from codepilot.tools import create_basic_registry


def reply() -> bytes:
    """构造可验证结束标记和 usage 的标准流。"""
    parts = [
        {
            "id": "id",
            "object": "chat.completion.chunk",
            "created": 1,
            "choices": [{"index": 0, "delta": {"content": "完成"}, "finish_reason": "stop"}],
        },
        {
            "id": "id",
            "object": "chat.completion.chunk",
            "created": 1,
            "choices": [],
            "usage": {"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43},
        },
    ]
    return ("".join("data: " + json.dumps(p) + "\n\n" for p in parts) + "data: [DONE]\n\n").encode()


@pytest.mark.parametrize("status,expected", [(429, 2), (401, 1), (500, 1), (402, 1)])
def test_only_explicit_rejection_is_retried(tmp_path: Path, status: int, expected: int) -> None:
    """只有可重试明确拒绝会消耗第二次尝试，认证和未知服务器失败不重放。"""

    async def scenario() -> None:
        """通过真实 SDK MockTransport 检查 HTTP 请求数和 Runtime 终态。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """第一次返回受控错误，后续返回正常完整流。"""
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(
                    status,
                    headers={"Retry-After": "0"},
                    json={"error": {"message": "secret-must-not-leak"}},
                )
            return httpx.Response(
                200, content=reply(), headers={"content-type": "text/event-stream"}
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            budget = RequestBudget(3)
            provider = OpenAICompatibleProvider(client, model="mock", budget=budget)
            runner = Runner(provider, create_basic_registry(tmp_path))
            report = await runner.run((Message(role="user", content="检查"),))
            assert count == expected == budget.used
            assert report.reason == ("completed" if status == 429 else "provider_error")
            assert "secret-must-not-leak" not in str(runner.recorder.normalized())
            if status == 429:
                assert budget.ledger.snapshot.input_tokens == 40
                assert budget.ledger.snapshot.unknown_requests == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("header", ["999999", "NaN", "-1", "invalid"])
def test_invalid_or_long_retry_after_stops(tmp_path: Path, header: str) -> None:
    """超长或非法 Retry-After 不无限等待或提前重发。"""

    async def scenario() -> None:
        """每种拒绝都只允许一个真实 SDK 请求。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """返回指定等待值，不给后续成功机会。"""
            nonlocal count
            count += 1
            return httpx.Response(
                429, headers={"Retry-After": header}, json={"error": {"message": "rate"}}
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            report = await Runner(
                OpenAICompatibleProvider(client, model="mock"), create_basic_registry(tmp_path)
            ).run((Message(role="user", content="检查"),))
            assert report.reason == "provider_error" and count == 1

    asyncio.run(scenario())


def test_retry_wait_can_be_cancelled(tmp_path: Path) -> None:
    """退避中的取消立即结束，后续网络与工具均零启动。"""

    async def scenario() -> None:
        """在退避事件屏障后取消，不靠随机等待推测时序。"""
        waiting = asyncio.Event()
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """要求长等待供取消屏障测试。"""
            nonlocal count
            count += 1
            return httpx.Response(
                429, headers={"Retry-After": "20"}, json={"error": {"message": "rate"}}
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            runner = Runner(
                OpenAICompatibleProvider(client, model="mock"), create_basic_registry(tmp_path)
            )
            runner.recorder.on_event = lambda event: (
                waiting.set() if event.kind == "provider_retry" else None
            )
            task = asyncio.create_task(runner.run((Message(role="user", content="检查"),)))
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert count == 1 and runner.report is not None and runner.report.reason == "cancelled"
            assert runner.report.tool_calls == 0

    asyncio.run(scenario())


class BrokenStream(httpx.AsyncByteStream):
    """先返回半截工具参数再断流，检验绝不自动重放。"""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """返回有效工具首片段后注入读取故障。"""
        value = {
            "id": "id",
            "object": "chat.completion.chunk",
            "created": 1,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": '{"path":"bad"'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        yield ("data: " + json.dumps(value) + "\n\n").encode()
        raise httpx.ReadError("secret stream body")


def test_partial_tool_stream_is_never_replayed(tmp_path: Path) -> None:
    """已经消费的工具参数流失败后零写入，并保留未知预算占用。"""

    async def scenario() -> None:
        """让 SDK 真正消费损坏流，独立检查文件及网络次数。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """每次请求提供半截流，重试将被独立计数发现。"""
            nonlocal count
            count += 1
            return httpx.Response(
                200, stream=BrokenStream(), headers={"content-type": "text/event-stream"}
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            budget = RequestBudget(3)
            provider = OpenAICompatibleProvider(
                client, model="mock", budget=budget, retry=RetryPolicy(base_delay_seconds=0.0)
            )
            report = await Runner(provider, create_basic_registry(tmp_path)).run(
                (Message(role="user", content="检查"),)
            )
            assert count == 1 and report.reason == "provider_error" and report.tool_calls == 0
            assert not (tmp_path / "bad").exists()
            assert budget.ledger.snapshot.reserved_output_tokens == 256

    asyncio.run(scenario())


def test_production_summary_and_main_share_session_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正式装配中摘要不暴露工具，摘要与主调用逐次计入 SQLite 额度。"""
    import codepilot.app.service as service
    from codepilot.config import ProductConfig, ProviderConfig
    from codepilot.context import ContextLimits
    from codepilot.sessions.store import SessionStore

    async def scenario() -> None:
        """首轮长回复、次轮摘要再主回复，核对三次真实 SDK 尝试。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """第二次请求必须是无工具压缩，三次返回值均包含计量。"""
            nonlocal count
            count += 1
            body = json.loads(request.content)
            if count in {2, 4}:
                assert not body.get("tools")
                assert "上下文压缩器" in body["messages"][0]["content"]
            content = "L" * 8000 if count == 1 else "目标：检查。待办：继续。"
            parts = [
                {
                    "id": "id",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "choices": [
                        {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
                    ],
                },
                {
                    "id": "id",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "choices": [],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45},
                },
            ]
            data = (
                "".join("data: " + json.dumps(p) + "\n\n" for p in parts) + "data: [DONE]\n\n"
            ).encode()
            return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"})

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:

            def factory(*args: object, **kwargs: object) -> service._ProviderHandle:
                """注入真实 SDK 的受控传输，不替换正式 Runtime 或会话链。"""
                return service._ProviderHandle(
                    OpenAICompatibleProvider(client, model="mock", budget=RequestBudget(5))
                )

            monkeypatch.setattr(service, "_create_provider", factory)
            monkeypatch.setenv("CODEPILOT_TEST_KEY", "synthetic-key-with-no-path-overlap")
            config = ProductConfig(
                provider=ProviderConfig(
                    kind="openai-compatible",
                    model="mock",
                    base_url="https://mock.invalid",
                    api_key_env="CODEPILOT_TEST_KEY",
                ),
                state_directory=tmp_path / "state",
                permission="read-only",
                context=ContextLimits(
                    max_prompt_tokens=8000,
                    response_reserve_tokens=1000,
                    compact_trigger_tokens=1500,
                    compact_target_tokens=1000,
                    recent_rounds=1,
                ),
            )
            first = await service.CodePilotApplication(config, workspace).run(
                "先检查", session_id="summary"
            )
            second = await service.CodePilotApplication(config, workspace).run(
                "继续检查", session_id="summary"
            )
            assert first.report.reason == second.report.reason == "completed"
            assert count == 3
            with SessionStore(tmp_path / "state", "summary", create=False) as stored:
                assert stored.get("request_attempts") == 3
                assert stored.get("provider_budget")["input"] == 120
                assert not stored.get("provider_budget")["pending"]
            assert (tmp_path / "state/summary/checkpoints/latest.json").exists()
            compacted = await service.CodePilotApplication(config, workspace).run(
                "/compact", session_id="summary", compact_only=True
            )
            assert compacted.report.reason == "completed"
            assert compacted.report.detail == "context_compacted"
            assert compacted.report.steps == compacted.report.tool_calls == 0
            assert count == 4
            with SessionStore(tmp_path / "state", "summary", create=False) as stored:
                assert stored.get("request_attempts") == 4
                assert stored.get("provider_budget")["input"] == 160
                assert not stored.get("provider_budget")["pending"]

    asyncio.run(scenario())


def test_retry_attempts_are_stopped_by_request_budget(tmp_path: Path) -> None:
    """退避重试同样受请求预算约束，额度耗尽不能发送第二次 HTTP。"""

    async def scenario() -> None:
        """策略允许五次但预算只有一次，独立计数实际发送次数。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """总是返回可重试限流拒绝。"""
            nonlocal count
            count += 1
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"error": {"message": "rate"}}
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            provider = OpenAICompatibleProvider(
                client, model="mock", budget=RequestBudget(1), retry=RetryPolicy(max_attempts=5)
            )
            report = await Runner(provider, create_basic_registry(tmp_path)).run(
                (Message(role="user", content="检查"),)
            )
            assert count == 1 and report.detail == "BudgetExceededError"

    asyncio.run(scenario())


def test_schema_size_is_checked_before_http(tmp_path: Path) -> None:
    """消息很短但工具目录巨大时也必须在网络前拒绝上下文超限。"""
    from codepilot.tools.base import ToolSpec

    async def scenario() -> None:
        """把巨型描述放入真实请求 Schema，网络 handler 永远不应被调用。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """若发生 HTTP 就计数，使断言揭示预检绕过。"""
            nonlocal count
            count += 1
            return httpx.Response(
                200, content=reply(), headers={"content-type": "text/event-stream"}
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            spec = ToolSpec(
                name="huge",
                description="D" * 10000,
                input_schema={"type": "object"},
                effect="read",
                resources=(),
                default_timeout=1.0,
                supports_cancel=True,
            )
            provider = OpenAICompatibleProvider(client, (spec,), model="mock", context_tokens=2000)
            report = await Runner(provider, create_basic_registry(tmp_path)).run(
                (Message(role="user", content="hi"),)
            )
            assert count == 0 and report.reason == "provider_error"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault,expected",
    [
        (httpx.ConnectError, 2),
        (httpx.ConnectTimeout, 2),
        (httpx.ReadTimeout, 1),
        (httpx.WriteError, 1),
    ],
)
def test_sdk_connection_failure_distinguishes_unknown_acceptance(
    tmp_path: Path, fault: type[httpx.TransportError], expected: int
) -> None:
    """真实 SDK 包装连接超时与读取超时不同，只有未发送 HTTP 的情况可重试。"""

    async def scenario() -> None:
        """首次传输失败后提供成功流，独立核对是否发生第二次尝试。"""
        count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """模拟传输层故障，保留真实 SDK 的异常因果链。"""
            nonlocal count
            count += 1
            if count == 1:
                raise fault("synthetic transport failure", request=request)
            return httpx.Response(
                200, content=reply(), headers={"content-type": "text/event-stream"}
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="https://mock.invalid",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            provider = OpenAICompatibleProvider(
                client, model="mock", retry=RetryPolicy(base_delay_seconds=0)
            )
            report = await Runner(provider, create_basic_registry(tmp_path)).run(
                (Message(role="user", content="检查"),)
            )
            assert count == expected
            assert report.reason == ("completed" if expected == 2 else "provider_error")
            assert report.tool_calls == 0

    asyncio.run(scenario())
