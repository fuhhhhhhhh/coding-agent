"""只供控制器使用的有限 HTTPS 交换；TLS 成功后才创建认证头，绝不自动重试。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import h11

from codepilot.network.broker import resolve_public, system_tls_context
from codepilot.tools.base import ToolFailure


@dataclass(frozen=True)
class Response:
    """完整响应的有限字节与状态；原始头和异常正文不得进入事件。"""

    status: int
    headers: dict[bytes, bytes]
    body: bytes


async def exchange(
    host: str,
    target: str,
    method: str,
    body: bytes = b"",
    *,
    authorization: Callable[[], str],
    sending: Callable[[], None],
    headers: tuple[tuple[bytes, bytes], ...] = (),
    limit: int = 65536,
    timeout: float = 15,
    account: Callable[[int], None] | None = None,
) -> Response:
    """固定公开 IP、SNI 和系统信任后发送一次；发送意图持久化失败则零业务字节。"""
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout):
            address = await resolve_public(host)
            reader, writer = await asyncio.open_connection(
                address, 443, ssl=system_tls_context(), server_hostname=host, limit=32768
            )
            connection = h11.Connection(h11.CLIENT, max_incomplete_event_size=16384)
            request = h11.Request(
                method=method.encode(),
                target=target.encode(),
                headers=[
                    (b"host", host.encode()),
                    (b"user-agent", b"CodePilot-credential-gateway"),
                    (b"authorization", authorization().encode()),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                    *headers,
                ],
            )
            data = connection.send(request)
            if body:
                data += connection.send(h11.Data(data=body))
            data += connection.send(h11.EndOfMessage())
            if account is not None:
                account(len(data))
            # 发行/撤销结果可能在下一条指令前变为未知，必须先提交发送意图。
            sending()
            writer.write(data)
            await writer.drain()
            response: h11.Response | None = None
            chunks = bytearray()
            wire_size = 0
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    incoming = await reader.read(16384)
                    wire_size += len(incoming)
                    if account is not None:
                        account(len(incoming))
                    # 头和分块框架也有限额，不能靠大量空分块耗尽内存或时间。
                    if wire_size > limit + 32768:
                        raise ToolFailure("credential_response_limit")
                    connection.receive_data(incoming)
                elif isinstance(event, h11.Response):
                    response = event
                    if sum(len(k) + len(v) for k, v in event.headers) > 16384:
                        raise ToolFailure("credential_headers_limit")
                elif isinstance(event, h11.Data):
                    chunks.extend(event.data)
                    if len(chunks) > limit:
                        raise ToolFailure("credential_response_limit")
                elif isinstance(event, h11.EndOfMessage) and response is not None:
                    # 不解释重定向，不向新目标复用认证，业务层只接受各自明确的状态码。
                    return Response(response.status_code, dict(response.headers), bytes(chunks))
                else:
                    raise ToolFailure("credential_response_incomplete")
    except (OSError, ValueError, h11.ProtocolError, TimeoutError) as error:
        # SDK/网络错误可能含认证正文，反馈只能保留异常类别。
        raise ToolFailure(f"credential_transport_failed:{type(error).__name__}") from None
    finally:
        # TLS 关闭有独立时限，取消不能把资源清理无限拖延。
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                writer.transport.abort()
