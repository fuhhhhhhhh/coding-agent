"""控制器私有 CONNECT 代理，强制执行精确 TCP:443 目的地及运行累计额度。"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import h11

from codepilot.network.broker import resolve_public
from codepilot.network.models import NativeNetworkConfig, checked_host
from codepilot.tools.base import ToolFailure

Progress = Callable[[str, int, int], None]


class EgressBudget:
    """多个原生命令共享运行额度，临时容器不能重置已消耗流量或租期。"""

    def __init__(self, config: NativeNetworkConfig, progress: Progress | None = None) -> None:
        """冻结可信策略及到期时间，不加载业务凭据。"""
        self.config = config
        self.progress = progress
        self.deadline = float("inf")
        self.connections = 0
        self.bytes = 0
        self.active = 0
        self.uncertain = False

    def reserve(self) -> int:
        """任何解析或连接前扣尝试，未知远端结果阻止同 run 再申请。"""
        self.check()
        # 并发与总次数独立约束，不能靠并发预留竞态超发。
        if (
            self.connections >= self.config.max_connections
            or self.active >= self.config.max_parallel
        ):
            raise ToolFailure("native_connection_budget_exhausted")
        self.connections += 1
        self.active += 1
        try:
            self.record("native_reserved", self.connections)
        except Exception:
            # 持久化预留失败时绝不联网，同时释放尚未取得的活跃槽。
            self.active -= 1
            self.uncertain = True
            raise
        return self.connections

    def check(self) -> None:
        """租期与双向流量耗尽后失败关闭，不返回可直连地址。"""
        # 首次申请才启动租期，不能让用户思考或模型首响应提前耗尽代理寿命。
        if self.deadline == float("inf"):
            self.deadline = time.monotonic() + self.config.lease_seconds
        if self.uncertain:
            raise ToolFailure("native_needs_reconciliation; do not replay")
        if time.monotonic() >= self.deadline:
            raise ToolFailure("native_lease_expired")
        if self.bytes >= self.config.total_bytes:
            raise ToolFailure("native_byte_budget_exhausted")

    def account(self, size: int) -> None:
        """转发前累计实际观察字节，超限块不发送给另一端。"""
        self.bytes += size
        if self.bytes > self.config.total_bytes:
            raise ToolFailure("native_byte_budget_exhausted")

    def record(self, state: str, connection: int) -> None:
        """仅发布阶段、计数与字节，不记录目标请求、TLS 明文或凭据。"""
        if self.progress is not None:
            self.progress(state, connection, self.bytes)


class ConnectProxy:
    """每次批准的 Shell 拥有一个私有 socket，只有专有临时容器挂载它。"""

    def __init__(
        self,
        budget: EgressBudget,
        path: Path,
        gateway: Callable[
            [
                h11.Request,
                h11.Connection,
                asyncio.StreamReader,
                asyncio.StreamWriter,
                Callable[[], None],
            ],
            Awaitable[None],
        ]
        | None = None,
    ) -> None:
        """保存共享账本和固定 socket 路径，构造阶段不监听或联网。"""
        self.budget, self.path = budget, path
        self.gateway = gateway
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.opened: set[int] = set()
        self.failed = False
        self.errors: set[str] = set()
        self.idle = asyncio.Event()
        self.idle.set()

    async def start(self) -> None:
        """只在 owner-only 目录创建 Unix 端点，不对宿主 TCP 网络开放代理。"""
        self.budget.check()
        self.server = await asyncio.start_unix_server(
            self.handle, str(self.path), limit=32768, backlog=16
        )
        os.chmod(self.path, 0o600)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """核验一个 CONNECT 后固定 IP 转发；错误不回显客户端输入。"""
        task = asyncio.current_task()
        assert task is not None
        # 未授权/未完成头也受并发上限，避免在解析前累积无界连接。
        if len(self.tasks) >= self.budget.config.max_parallel:
            self.failed = True
            writer.close()
            return
        self.tasks.add(task)
        self.idle.clear()
        upstream: asyncio.StreamWriter | None = None
        pipes: list[asyncio.Task[None]] = []
        reserved = 0
        try:
            duration = self.budget.deadline - time.monotonic()
            async with asyncio.timeout(max(0.001, duration)):
                parsed = await self._request(reader)
                # 桥的就绪探测只建立空连接，不计成联网或违规请求。
                if parsed is None:
                    return
                request, connection = parsed
                if request.method != b"CONNECT" and self.gateway is not None:
                    reserved = self.budget.reserve()

                    def opened() -> None:
                        """网关完成本地校验后才登记外连，拒绝请求不能伪装成远端未知。"""
                        self.opened.add(reserved)
                        self.budget.record("native_opened", reserved)

                    await self.gateway(request, connection, reader, writer, opened)
                    return
                host = self._host(request)
                reserved = self.budget.reserve()
                address = await resolve_public(host)
                # 外连前先持久标记，覆盖 TCP 已发包而控制器尚未记录就崩溃的窗口。
                self.opened.add(reserved)
                self.budget.record("native_opened", reserved)
                async with asyncio.timeout(self.budget.config.idle_seconds):
                    remote, upstream = await asyncio.open_connection(address, 443, limit=32768)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                activity = [time.monotonic()]
                pipes = [
                    asyncio.create_task(self._pipe(reader, upstream, activity)),
                    asyncio.create_task(self._pipe(remote, writer, activity)),
                ]
                await asyncio.gather(*pipes)
        except Exception as error:
            # 部分连接可能已发送字节，最终结算由命令结果及连接清理共同决定。
            self.failed = True
            self.errors.add(str(error) if isinstance(error, ToolFailure) else type(error).__name__)
            try:
                # 建立隧道后只能断开，不能向 TLS 字节中插入明文 HTTP 错误。
                if upstream is None:
                    writer.write(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    async with asyncio.timeout(0.5):
                        await writer.drain()
            except (OSError, TimeoutError):
                pass
        finally:
            # 异常、EOF 或取消都停止另一方向，不能留下仍向外发送的泵。
            for pipe in pipes:
                pipe.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    try:
                        async with asyncio.timeout(0.5):
                            await stream.wait_closed()
                    except (OSError, TimeoutError):
                        stream.transport.abort()
            try:
                if reserved:
                    self.budget.active -= 1
                    try:
                        self.budget.record("native_closed", reserved)
                    except Exception as error:
                        # 结算存储失败不得让命令返回成功或产生未回收的服务任务异常。
                        self.failed = self.budget.uncertain = True
                        self.errors.add(type(error).__name__)
            finally:
                # 存储事件失败也要释放任务标记，否则退出会永远等待自己。
                self.tasks.discard(task)
                if not self.tasks:
                    self.idle.set()

    async def _request(
        self, reader: asyncio.StreamReader
    ) -> tuple[h11.Request, h11.Connection] | None:
        """有限时间和容量解析请求头，不接受无限头、正文或流水线升级。"""
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=16384)
        size = 0
        async with asyncio.timeout(self.budget.config.idle_seconds):
            while True:
                data = await reader.read(4096)
                # 回环桥只建立空连接做就绪探测，不代表业务请求或网络尝试。
                if not data and size == 0:
                    return None
                size += len(data)
                if size > 16384 or not data:
                    raise ToolFailure("native_proxy_header_invalid")
                connection.receive_data(data)
                event = connection.next_event()
                if isinstance(event, h11.Request):
                    # 客户端需等 200 再发送 TLS，提前流水线字节拒绝以免静默丢失。
                    if event.method == b"CONNECT" and connection.trailing_data[0]:
                        raise ToolFailure("native_proxy_pipeline_denied")
                    return event, connection
                if event is not h11.NEED_DATA:
                    raise ToolFailure("native_proxy_request_invalid")

    def _host(self, request: h11.Request) -> str:
        """授权仅对应精确域名 TCP:443，不解释加密后的 HTTP 路径或方法。"""
        # 不转发普通 HTTP，避免绕过统一目标解析或混用请求级授权。
        if request.method != b"CONNECT":
            raise ToolFailure("native_proxy_connect_only")
        target = request.target.decode("ascii")
        host, separator, port = target.rpartition(":")
        checked_host(host)
        # 仅接受唯一规范域名和固定端口，不能从请求头推导更宽目标。
        if not separator or port != "443" or host not in self.budget.config.hosts:
            raise ToolFailure("native_destination_denied")
        # CONNECT 不能带消息正文，防止不同 HTTP 解释方式造成请求走私。
        for key, value in request.headers:
            if key == b"transfer-encoding" or key == b"content-length" and value != b"0":
                raise ToolFailure("native_proxy_body_denied")
        return host

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, activity: list[float]
    ) -> None:
        """原生客户端保留 TLS 与应用语义，代理只执行网络流量边界。"""
        while True:
            self.budget.check()
            try:
                async with asyncio.timeout(self.budget.config.idle_seconds):
                    data = await reader.read(16384)
            except TimeoutError:
                # 下载时上传方向可以持续静默；只有双向都无进展才是隧道空闲。
                if time.monotonic() - activity[0] >= self.budget.config.idle_seconds:
                    raise
                continue
            if not data:
                if writer.can_write_eof():
                    writer.write_eof()
                    await writer.drain()
                return
            self.budget.account(len(data))
            activity[0] = time.monotonic()
            writer.write(data)
            async with asyncio.timeout(self.budget.config.idle_seconds):
                await writer.drain()

    async def close(self) -> None:
        """先停止接入再关闭全部转发，最后移除 socket；容器清理归调用者所有。"""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.path.unlink(missing_ok=True)
