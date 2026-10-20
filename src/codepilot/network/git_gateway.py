"""指定仓库的 Git smart HTTP 网关，令牌只存在于控制器至 GitHub 的 TLS 内。"""

import asyncio
import base64
import time
from collections.abc import Callable

import h11

from codepilot.network.broker import contains_secret
from codepilot.network.credentials import GitHubLease
from codepilot.network.egress import EgressBudget
from codepilot.network.transport import exchange
from codepilot.tools.base import ToolFailure
from codepilot.tools.process import workspace_environment


class GitGateway:
    """只转发显式选择的仓库协议，拒绝通用 HTTP、重定向及客户端认证头。"""

    def __init__(self, leases: dict[str, GitHubLease], budget: EgressBudget) -> None:
        """绑定同一次已审批 Shell 的引用，不根据进程输入增加授权。"""
        self.leases, self.budget = leases, budget
        self.locks = {name: asyncio.Lock() for name in leases}

    def route(self, request: h11.Request) -> tuple[GitHubLease, str, str]:
        """只接受唯一规范本地路径，并在签发 token 前完成范围和方法核验。"""
        target = request.target.decode("ascii")
        parts = target.split("/")
        # 精确分段而不做 URL 解码，避免点段、编码和仓库前缀变体借用引用。
        if len(parts) != 4 or parts[0] or parts[1] != "git" or parts[2] not in self.leases:
            raise ToolFailure("git_gateway_route_denied")
        lease = self.leases[parts[2]]
        service = parts[3]
        method = request.method.decode("ascii")
        allowed = {"git-upload-pack"}
        if lease.repository.contents == "write":
            allowed.add("git-receive-pack")
        if method != "POST" or service not in allowed:
            raise ToolFailure("git_gateway_operation_denied")
        return lease, service, "application/x-" + service + "-result"

    def resolve(self, request: h11.Request) -> tuple[GitHubLease, str, str]:
        """GET 广告和 POST RPC 分开比较，不解码路径、不接受附加查询参数。"""
        # 广告只有两个标准服务，不能把普通 GET 变成任意认证下载。
        if request.method == b"GET":
            for name, lease in self.leases.items():
                services = ["git-upload-pack"]
                if lease.repository.contents == "write":
                    services.append("git-receive-pack")
                for service in services:
                    if request.target == f"/git/{name}/info/refs?service={service}".encode():
                        return (
                            lease,
                            "info/refs?service=" + service,
                            "application/x-" + service + "-advertisement",
                        )
            raise ToolFailure("git_gateway_operation_denied")
        return self.route(request)

    async def handle(
        self,
        request: h11.Request,
        connection: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        opened: Callable[[], None],
    ) -> None:
        """有界读取 Git 正文，完整校验认证响应后才交付进程，始终关闭单次连接。"""
        lease, suffix, response_type = self.resolve(request)
        headers = dict(request.headers)
        # 不允许客户端选择认证、压缩或第三方目的地，也不透传任意头。
        if any(
            k in headers
            for k in (b"authorization", b"cookie", b"proxy-authorization", b"content-encoding")
        ):
            raise ToolFailure("git_gateway_client_auth_denied")
        # 本地 Host 不参与选择上游，出现不一致时直接拒绝，不推导其他目的地。
        if headers.get(b"host") != b"127.0.0.1:3129":
            raise ToolFailure("git_gateway_host_denied")
        forwarded: list[tuple[bytes, bytes]] = []
        if request.method == b"POST":
            wanted = ("application/x-" + suffix + "-request").encode()
            if headers.get(b"content-type") != wanted:
                raise ToolFailure("git_gateway_content_type_denied")
            forwarded.append((b"content-type", wanted))
        protocol = headers.get(b"git-protocol")
        if protocol is not None:
            if protocol not in {b"version=1", b"version=2"}:
                raise ToolFailure("git_gateway_protocol_denied")
            forwarded.append((b"git-protocol", protocol))
        # Git 推送可能等到 100 才发送 pack，必须在有界正文读取前解除这次等待。
        if headers.get(b"expect") == b"100-continue":
            writer.write(connection.send(h11.InformationalResponse(status_code=100, headers=[])))
            await writer.drain()
        body = bytearray()
        async with asyncio.timeout(self.budget.config.idle_seconds):
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    connection.receive_data(await reader.read(16384))
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                    # 请求和响应均有限；绝不把 pack 全量无限缓存在控制器。
                    if len(body) > self.budget.config.total_bytes or request.method == b"GET":
                        raise ToolFailure("git_gateway_request_limit")
                elif isinstance(event, h11.EndOfMessage):
                    if event.headers or connection.trailing_data[0]:
                        raise ToolFailure("git_gateway_pipeline_denied")
                    break
                else:
                    raise ToolFailure("git_gateway_body_incomplete")
        opened()
        async with self.locks[lease.repository.name]:
            token = await lease.acquire()
        self.budget.check()
        authorization = "Basic " + base64.b64encode(b"x-access-token:" + token).decode()
        response = await exchange(
            "github.com",
            f"/{lease.repository.owner}/{lease.repository.repository}.git/{suffix}",
            request.method.decode(),
            bytes(body),
            authorization=lambda: authorization,
            sending=lambda: self.budget.check(),
            headers=tuple(forwarded),
            limit=self.budget.config.total_bytes,
            timeout=min(self.budget.config.idle_seconds, self.budget.deadline - time.monotonic()),
            account=self.budget.account,
        )
        if (
            response.status != 200
            or response.headers.get(b"content-type") != response_type.encode()
            or b"content-encoding" in response.headers
            or contains_secret(response.body, token)
            or contains_secret(response.body, b"x-access-token:" + token)
        ):
            raise ToolFailure("git_gateway_response_rejected")
        writer.write(
            connection.send(
                h11.Response(
                    status_code=200,
                    headers=[
                        (b"content-type", response_type.encode()),
                        (b"content-length", str(len(response.body)).encode()),
                        (b"connection", b"close"),
                    ],
                )
            )
        )
        writer.write(connection.send(h11.Data(data=response.body)))
        writer.write(connection.send(h11.EndOfMessage()))
        await writer.drain()

    async def close(self) -> None:
        """各仓库独立撤销，一个失败也继续清理其他令牌；最终集中报告未知。"""
        failed = False
        for lease in self.leases.values():
            try:
                await lease.close()
            except Exception:
                failed = True
        if failed:
            raise ToolFailure("git_credentials_cleanup_incomplete; resources prune required")


def git_environment(leases: dict[str, GitHubLease]) -> tuple[str, ...]:
    """通过进程级 Git 配置精确重写 URL，无密钥 argv，也不写用户 .git/config。"""
    # 匿名命令不应改变既有 Git 进程配置，尤其不能清除禁用 Hook 的保护。
    if not leases:
        return ()
    # 追加到已禁用 hooks/fsmonitor 的可信配置，不能覆盖已有两项进程安全设置。
    offset = int(workspace_environment()["GIT_CONFIG_COUNT"])
    values: list[str] = [f"GIT_CONFIG_COUNT={offset + len(leases)}"]
    for index, (name, lease) in enumerate(leases.items(), start=offset):
        repo = lease.repository
        values.extend(
            (
                f"GIT_CONFIG_KEY_{index}=url.http://127.0.0.1:3129/git/{name}.insteadOf",
                f"GIT_CONFIG_VALUE_{index}=https://github.com/{repo.owner}/{repo.repository}.git",
            )
        )
    return tuple(values)
