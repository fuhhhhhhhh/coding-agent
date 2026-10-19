"""使用官方 MCP ClientSession，传输单消息有界且进程由控制器清理。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from jsonschema import Draft202012Validator
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage

from codepilot.tools.base import ToolFailure
from codepilot.tools.process import settle, spawn, stop_process


@asynccontextmanager
async def bounded_transport(argv: tuple[str, ...], cwd: str) -> AsyncIterator[tuple[Any, Any]]:
    """只转发有界 JSON-RPC 消息；不继承 Provider 凭据环境，不回显服务端原始错误。"""
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "DOCKER_HOST", "DOCKER_CONTEXT"}
    }
    # spawn 的进程归属回调先于任何 RPC，取消窗口仍使用现有不可遗弃清理。
    process = await spawn(argv, cwd=cwd, stdin=True, stream_limit=65536, env=allowed)
    incoming, reader = anyio.create_memory_object_stream[SessionMessage | Exception](8)
    writer, outgoing = anyio.create_memory_object_stream[SessionMessage](8)

    async def read_messages() -> None:
        """逐行解析时限制消息长度，EOF 或非法 JSON 使 SDK 明确断开。"""
        assert process.stdout is not None
        try:
            async with incoming:
                while line := await process.stdout.readline():
                    if len(line) > 65536:
                        raise ValueError("MCP message too large")
                    await incoming.send(
                        SessionMessage(types.JSONRPCMessage.model_validate_json(line))
                    )
        except Exception:
            # 原始解析错误可能携带服务器正文，仅向 SDK 发送稳定内部类别。
            await incoming.aclose()

    async def write_messages() -> None:
        """有限队列提供背压，发送失败由连接关闭转成未知结果而非自动重试。"""
        assert process.stdin is not None
        async with outgoing:
            async for message in outgoing:
                data = message.message.model_dump_json(by_alias=True, exclude_none=True).encode()
                if len(data) > 65536:
                    raise ValueError("MCP request too large")
                process.stdin.write(data + b"\n")
                await process.stdin.drain()

    async def discard_errors() -> None:
        """排空原始 stderr 防止死锁，不把可能含秘密的正文写入终端或 Trace。"""
        assert process.stderr is not None
        while await process.stderr.read(8192):
            pass

    tasks = [
        asyncio.create_task(operation())
        for operation in (read_messages, write_messages, discard_errors)
    ]
    try:
        yield reader, writer
    finally:
        # 读写任务、进程组和内存通道都由同一调用清理，不留下服务端后台进程。
        for task in tasks:
            task.cancel()
        await settle(asyncio.gather(*tasks, return_exceptions=True))
        await settle(stop_process(process))
        await reader.aclose()
        await writer.aclose()


def check_schema(schema: dict[str, Any]) -> None:
    """拒绝远端引用与异常 Schema，验证参数不能暗中发起网络请求。"""
    raw = json.dumps(schema)
    if len(raw) > 8192:
        raise ToolFailure("MCP schema too large")

    def visit(value: Any) -> None:
        """递归检查引用，模型或服务器不能把 Schema 当作网络能力。"""
        if isinstance(value, dict):
            if any(
                key in value and not str(value[key]).startswith("#")
                for key in ("$ref", "$dynamicRef", "$recursiveRef")
            ):
                raise ToolFailure("MCP external schema reference refused")
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    Draft202012Validator.check_schema(schema)


async def request_mcp(
    argv: tuple[str, ...],
    cwd: str,
    expected: dict[str, Any] | None,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, bool]:
    """每次完整初始化/列举/调用/关闭，避免跨工具任务共享 SDK 取消作用域。"""
    async with bounded_transport(argv, cwd) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            response = await session.list_tools()
            if response.nextCursor or len(response.tools) > 32:
                raise ToolFailure("MCP catalog exceeds supported bound")
            catalog: dict[str, Any] = {}
            for item in response.tools:
                if item.name in catalog:
                    raise ToolFailure("duplicate MCP tool")
                check_schema(item.inputSchema)
                catalog[item.name] = {
                    "description": (item.description or "")[:500],
                    "schema": item.inputSchema,
                }
            if len(json.dumps(catalog).encode()) > 49152:
                raise ToolFailure("MCP catalog too large")
            if tool is None:
                return catalog, json.dumps(catalog, ensure_ascii=False), False
            if expected is None or catalog != expected:
                raise ToolFailure("MCP catalog changed; list tools again before calling")
            if tool not in catalog:
                raise ToolFailure("unknown MCP tool")
            Draft202012Validator(catalog[tool]["schema"]).validate(arguments or {})
            result = await session.call_tool(tool, arguments or {})
            if any(item.type != "text" for item in result.content):
                raise ToolFailure("unsupported MCP content; outcome unknown")
            text = "\n".join(item.text for item in result.content if item.type == "text")
            if len(text.encode()) > 49152:
                raise ToolFailure("MCP result exceeds bound; outcome must be reconciled")
            return catalog, text, bool(result.isError)
