"""容器内固定回环桥：只连接控制器指定 Unix socket，不自行解析域名或访问外网。"""

import argparse
import asyncio


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """有界转发并传播半关闭，客户端退出不能留下无限缓存。"""
    while data := await reader.read(16384):
        writer.write(data)
        await writer.drain()
    # TCP 与 Unix 流都支持半关闭，保留另一方向尚未返回的数据。
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()


async def serve(path: str, port: int) -> None:
    """仅在容器回环监听，连接归属专有临时容器并随整容器销毁。"""

    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """每个客户端独占 Unix 流，代理失效时关闭连接，绝不回落直连。"""
        upstream: asyncio.StreamWriter | None = None
        tasks: list[asyncio.Task[None]] = []
        try:
            remote, upstream = await asyncio.open_unix_connection(path, limit=32768)
            tasks = [
                asyncio.create_task(pipe(reader, upstream)),
                asyncio.create_task(pipe(remote, writer)),
            ]
            await asyncio.gather(*tasks)
        except (OSError, ConnectionError):
            # Unix 端点消失意味着授权通道已关闭，容器没有其他外网接口。
            pass
        finally:
            # 任一方向结束或失败，都等待其余泵退出并回收两端 socket。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    try:
                        async with asyncio.timeout(1):
                            await stream.wait_closed()
                    except (OSError, TimeoutError):
                        stream.transport.abort()

    server = await asyncio.start_server(connected, "127.0.0.1", port, limit=32768, backlog=16)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    asyncio.run(serve(arguments.socket, arguments.port))
