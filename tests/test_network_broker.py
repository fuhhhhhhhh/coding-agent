"""受控联网的确定性边界、真实本地 TLS 协议与正式产品链测试。"""

import asyncio
import base64
import json
import os
import ssl
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import codepilot.app.service as service
import codepilot.network.broker as broker
from codepilot.app import CodePilotApplication
from codepilot.config import BackendConfig, ProductConfig, load_product_config
from codepilot.contracts import ToolCall
from codepilot.network.broker import HttpArguments, HttpRequestTool, public_address, resolve_public
from codepilot.network.models import Destination, NetworkConfig, SecretReference, parse_url
from codepilot.permissions import ScriptedApproval
from codepilot.tools.base import ToolFailure
from codepilot.workspace import LocalExecutionBackend

HOST = "broker.example"


@pytest.fixture
def certificates(tmp_path: Path) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """临时生成测试专用自签 TLS 根，不在生产配置中提供私网或跳过验证选项。"""
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={HOST}",
            "-addext",
            f"subjectAltName=DNS:{HOST}",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    return server, client


@asynccontextmanager
async def tls_service(
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
    response: bytes | Callable[[bytes], Awaitable[bytes]],
) -> AsyncIterator[list[bytes]]:
    """真实 socket/TLS/HTTP 服务，仅测试替换 DNS 路由和信任根，收集独立服务端事实。"""
    requests: list[bytes] = []
    tasks: set[asyncio.Task[None]] = set()
    original_connect = asyncio.open_connection

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """记录实际请求并发送可控分块，退出关闭 socket 且不记录秘密到磁盘。"""
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            payload = await response(request) if callable(response) else response
            # 分成小块，强制测试 HTTP 消息重组与跨块秘密拦截。
            for offset in range(0, len(payload), 11):
                writer.write(payload[offset : offset + 11])
                await writer.drain()
                await asyncio.sleep(0)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=certificates[0])
    port = server.sockets[0].getsockname()[1]

    async def resolve(host: str) -> str:
        """测试域名返回固定公开数字地址，生产解析过滤另有独立断言。"""
        assert host == HOST
        return "93.184.216.34"

    async def connect(
        host: str, requested_port: int, **kwargs: Any
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """证明代理连接核验后的数字 IP，同时保持原域名证书验证。"""
        assert host == "93.184.216.34" and requested_port == 443
        assert kwargs["server_hostname"] == HOST
        kwargs["ssl"] = certificates[1]
        return await original_connect("127.0.0.1", port, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(broker, "resolve_public", resolve)
        patch.setattr(asyncio, "open_connection", connect)
        try:
            yield requests
        finally:
            # 先停止接入再结束测试服务所属任务，不遗留后台 socket。
            server.close()
            await server.wait_closed()
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def response(body: bytes, status: int = 200, headers: bytes = b"") -> bytes:
    """生成标准 HTTP fixture，真实解析仍由生产 h11 负责。"""
    return (
        f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n".encode()
        + headers
        + b"Connection: close\r\n\r\n"
        + body
    )


def network(**changes: Any) -> NetworkConfig:
    """只允许测试域名的运行级配置。"""
    return NetworkConfig(enabled=True, destinations=(Destination(host=HOST),), **changes)


@pytest.mark.parametrize(
    "url",
    [
        "http://broker.example/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://user:pass@broker.example/",
        "https://broker.example:444/",
        "https://broker.example./",
        "https://BROKER.example/",
        "https://broker.example/a/../b",
        "https://broker.example/%2e%2e/x",
        "https://broker.example/a\\b",
        "https://broker.example/#x",
        "https://broker.example/\r\nx",
        "https://broker.example//x",
    ],
)
def test_url_rejects_ambiguous_destination(url: str) -> None:
    """拒绝 URL 模糊形式，不能交给服务器再决定凭据路径作用域。"""
    with pytest.raises(ValueError):
        parse_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff0e::1",
        "::ffff:8.8.8.8",
        "2002:0808:0808::1",
    ],
)
def test_private_and_special_addresses_are_denied(address: str) -> None:
    """公开可达不等于任意全局地址，映射/隧道/组播均不授权。"""
    assert not public_address(address)
    assert public_address("8.8.8.8")
    assert public_address("2606:4700:4700::1111")


def test_mixed_dns_answer_fails_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 DNS 同时返回公开和私网时整批拒绝，不能只选看起来安全的第一个。"""

    async def run() -> None:
        """仅替换解析器答案，不替换生产地址分类逻辑。"""

        async def resolve(*args: Any, **kwargs: Any) -> list[Any]:
            """产生模拟重绑定答案，原生产连接代码不会被调用。"""
            return [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        with pytest.raises(ToolFailure, match="not_public"):
            await resolve_public(HOST)

    asyncio.run(run())


def test_config_cannot_expand_from_project_and_local(tmp_path: Path) -> None:
    """仓库只能关闭网络，Local 不能声称经代理限制任意 Shell。"""
    with pytest.raises(ValueError, match="requires Docker"):
        ProductConfig(network=network())
    with pytest.raises(ValueError, match="within network"):
        network(
            secrets=(SecretReference(name="test", host="other.example", file=tmp_path / "key"),)
        )
    project = tmp_path / ".codepilot.yaml"
    project.write_text("network:\n  enabled: true\n")
    with pytest.raises(ValueError, match="只能禁用 network"):
        load_product_config(workspace=tmp_path, user_path=tmp_path / "absent")


def test_path_scopes_use_segment_boundaries() -> None:
    """凭据路径不会把 api 前缀误用到 api-evil。"""
    scope = Destination(host=HOST, path_prefix="/api")
    assert scope.permits(HOST, "/api/x") and scope.permits(HOST, "/api")
    assert not scope.permits(HOST, "/api-evil")


def test_tls_get_and_bounded_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """真实 TLS 响应完整解析，正文作为不可信资料返回，不继承代理或 Cookie。"""

    async def run() -> None:
        """服务端独立观察 Host/GET，客户端校验无活动连接。"""
        tool = HttpRequestTool(network(), tmp_path)
        async with tls_service(monkeypatch, certificates, response(b"hello")) as requests:
            output = json.loads(await tool.run(HttpArguments(url=f"https://{HOST}/docs?a=1")))
        assert output["text"] == "hello" and output["complete"]
        assert output["source"] == "untrusted_https_response"
        assert requests[0].startswith(b"GET /docs?a=1 HTTP/1.1")
        assert b"Authorization:" not in requests[0] and b"Cookie:" not in requests[0]
        assert tool.active_connections == 0 and tool.requests == 1

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["large", "short", "gzip", "status", "headers"])
def test_response_failures_are_not_complete_success(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """超限/截断/压缩/认证错误/畸形头均关闭连接，不泄露服务端正文。"""
    payloads = {
        "large": response(b"x" * 2048),
        "short": b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\nshort",
        "gzip": response(b"compressed", headers=b"Content-Encoding: gzip\r\n"),
        "status": response(b"private error body", status=401),
        "headers": b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 17000,
    }

    async def run() -> None:
        """通过真实协议故障触发失败，不用模拟成功 ToolResult。"""
        tool = HttpRequestTool(network(max_response_bytes=1024), tmp_path)
        async with tls_service(monkeypatch, certificates, payloads[kind]):
            with pytest.raises(ToolFailure) as error:
                await tool.run(HttpArguments(url=f"https://{HOST}/"))
        assert "private error body" not in str(error.value)
        assert tool.active_connections == 0 and tool.requests == 1

    asyncio.run(run())


@pytest.mark.parametrize("encoding", ["plain", "base64", "hex"])
def test_secret_echo_never_published(
    encoding: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """跨 TLS 分块的原文及常见编码回显被拒绝，秘密仅在成功 TLS 后进入请求头。"""
    secret = b"synthetic-broker-secret-0123456789"
    path = tmp_path / "credential"
    path.write_bytes(secret)
    path.chmod(0o600)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ref = SecretReference(name="fixture", host=HOST, path_prefix="/api", file=path)
    values = {"plain": secret, "base64": base64.b64encode(secret), "hex": secret.hex().encode()}

    async def run() -> None:
        """服务端独立确认凭据，调用方只能看到固定拒绝类别。"""
        tool = HttpRequestTool(network(secrets=(ref,)), workspace)
        async with tls_service(monkeypatch, certificates, response(values[encoding])) as requests:
            with pytest.raises(ToolFailure, match="secret_echo_blocked"):
                await tool.run(HttpArguments(url=f"https://{HOST}/api/me", secret_ref="fixture"))
        assert b"Authorization: Bearer " + secret in requests[0]
        assert tool.active_connections == 0

    asyncio.run(run())


def test_secrets_require_private_files_and_scope(tmp_path: Path) -> None:
    """权限、工作区内路径、Provider 来源和引用路径均在发送前受限。"""
    secret = tmp_path / "key"
    secret.write_text("synthetic-secret-0123456789")
    ref = SecretReference(name="fixture", host=HOST, path_prefix="/api", file=secret)
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="owner-private"):
        HttpRequestTool(network(secrets=(ref,)), tmp_path / "source")
    secret.chmod(0o600)
    with pytest.raises(ValueError, match="outside workspace"):
        HttpRequestTool(network(secrets=(ref,)), tmp_path)
    with pytest.raises(ValueError, match="distinct from provider"):
        HttpRequestTool(network(secrets=(ref,)), tmp_path / "source", secret)
    tool = HttpRequestTool(network(secrets=(ref,)), tmp_path / "source")
    with pytest.raises(ToolFailure, match="scope_denied"):
        asyncio.run(tool.run(HttpArguments(url=f"https://{HOST}/api-evil", secret_ref="fixture")))
    assert tool.requests == 0
    before = tool.spec.provenance
    secret.unlink()
    assert tool.spec.provenance != before


@pytest.mark.parametrize("mode", ["redirect", "budget", "expiry", "cancel", "timeout", "bytes"])
def test_redirect_budget_expiry_and_cancellation(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """失败必须有限且停止网络；重定向逐跳扣额并重核目的地。"""

    async def run() -> None:
        """用服务端实际请求数证明没有越界发送或自动重试。"""
        entered = asyncio.Event()

        async def delayed(request: bytes) -> bytes:
            """网络已接受请求后等待，由取消而非成功返回结束。"""
            entered.set()
            await asyncio.sleep(10)
            return response(b"late")

        payload: bytes | Callable[[bytes], Awaitable[bytes]] = (
            response(b"", status=302, headers=b"Location: https://other.example/\r\n")
            if mode == "redirect"
            else delayed
            if mode in {"cancel", "timeout"}
            else response(b"x" * 1600)
        )
        config = network(
            max_requests=1,
            total_bytes=1024 if mode == "bytes" else 4096,
            timeout_seconds=0.15 if mode == "timeout" else 5.0,
        )
        tool = HttpRequestTool(config, tmp_path)
        async with tls_service(monkeypatch, certificates, payload) as requests:
            if mode == "expiry":
                tool.deadline = 0
            if mode == "cancel":
                task = asyncio.create_task(tool.run(HttpArguments(url=f"https://{HOST}/")))
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if mode == "budget":
                    await tool.run(HttpArguments(url=f"https://{HOST}/"))
                with pytest.raises(ToolFailure):
                    await tool.run(HttpArguments(url=f"https://{HOST}/"))
        assert len(requests) == (0 if mode == "expiry" else 1)
        assert tool.active_connections == 0

    asyncio.run(run())


def test_product_chain_approval_audit_and_secret_free_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """正式会话链负责审批/调度/Trace，模型无调用；本用例不声称 Docker 隔离验收。"""
    source = tmp_path / "source"
    source.mkdir()
    secret = b"synthetic-full-chain-secret-0123456789"
    path = tmp_path / "credential"
    path.write_bytes(secret)
    path.chmod(0o600)
    ref = SecretReference(name="fixture", host=HOST, file=path)
    config = ProductConfig(
        backend=BackendConfig(kind="docker", docker_image="fixture"),
        network=network(secrets=(ref,)),
        state_directory=tmp_path / "state",
    )

    def factory(
        config: ProductConfig, root: Path, directory: Path, **kwargs: Any
    ) -> LocalExecutionBackend:
        """确定性产品装配用真实 Local 文件后端，实际 Docker 边界另行真机验收。"""
        return LocalExecutionBackend(root, audit_directory=directory)

    monkeypatch.setattr(service, "_create_backend", factory)

    async def run() -> None:
        """分别验证拒绝零请求、批准后秘密只在服务端请求头中出现。"""
        async with tls_service(monkeypatch, certificates, response(b"safe document")) as requests:
            for approved in (False, True):
                approval = ScriptedApproval([approved])
                result = await CodePilotApplication(config, source, approval=approval).run(
                    "读取文档",
                    direct_tool=ToolCall(
                        call_id="network",
                        name="http_request",
                        arguments={"url": f"https://{HOST}/api", "secret_ref": "fixture"},
                    ),
                )
                tool_result = next(o.result for o in result.report.observations if o.result)
                assert tool_result.is_error != approved
                assert result.resources.backend_closed
                action = approval.requests[0].action
                assert action.network and action.secrets == ("fixture",)
                assert action.network_scope == (f"https://{HOST}:443",)
                assert result.report.reason == "completed"
            assert len(requests) == 1
        # 正文、数据库、Trace 和其他产物均独立扫描，不能仅检查模型自述。
        for file in (tmp_path / "state").rglob("*"):
            if file.is_file():
                assert secret not in file.read_bytes()

    asyncio.run(run())


def test_authenticated_redirect_and_replaced_secret_are_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """认证重定向不跟随，审批后替换凭据也不发送旧授权下的新值。"""
    secret = tmp_path / "secret"
    secret.write_text("synthetic-0123456789-token")
    secret.chmod(0o600)
    source = tmp_path / "source"
    source.mkdir()
    ref = SecretReference(name="fixture", host=HOST, file=secret)

    async def run() -> None:
        """服务端实际请求数区分已拒绝的重定向和发送前失效。"""
        tool = HttpRequestTool(network(secrets=(ref,)), source)
        payload = response(b"", 302, b"Location: /other\r\n")
        async with tls_service(monkeypatch, certificates, payload) as requests:
            with pytest.raises(ToolFailure, match="authenticated_redirect_denied"):
                await tool.run(HttpArguments(url=f"https://{HOST}/", secret_ref="fixture"))
            secret.write_text("replacement-0123456789-token")
            with pytest.raises(ToolFailure, match="secret_changed"):
                await tool.run(HttpArguments(url=f"https://{HOST}/", secret_ref="fixture"))
            assert len(requests) == 1
        assert tool.active_connections == 0

    asyncio.run(run())


def test_anonymous_redirects_share_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """合法重定向可到同授权路径，但每跳都计尝试，不无限循环。"""

    async def run() -> None:
        """从服务端观察两次 GET，重定向下一跳已耗尽不再发请求。"""

        async def redirect(request: bytes) -> bytes:
            """首路径跳转到终路径，终路径返回可独立验证的内容。"""
            return (
                response(b"", 302, b"Location: /final\r\n")
                if request.startswith(b"GET /start ")
                else response(b"final")
            )

        tool = HttpRequestTool(network(max_requests=2), tmp_path)
        async with tls_service(monkeypatch, certificates, redirect) as requests:
            data = json.loads(await tool.run(HttpArguments(url=f"https://{HOST}/start")))
            assert data["text"] == "final" and data["requests_used"] == 2
            with pytest.raises(ToolFailure, match="request_budget"):
                await tool.run(HttpArguments(url=f"https://{HOST}/final"))
            assert len(requests) == 2

    asyncio.run(run())


def test_tls_certificate_failure_never_reads_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """TLS 身份失败不得提前读取或发送凭据。"""
    path = tmp_path / "credential"
    path.write_text("synthetic-tls-secret-0123456789")
    path.chmod(0o600)
    ref = SecretReference(name="fixture", host=HOST, file=path)
    tool = HttpRequestTool(network(secrets=(ref,)), tmp_path / "source")

    def forbidden_read(*args: Any) -> bytes:
        """如 TLS 校验前读取秘密则立即让测试失败。"""
        raise AssertionError("secret read before verified TLS")

    monkeypatch.setattr(broker, "read_secret", forbidden_read)

    async def fail_connect(*args: Any, **kwargs: Any) -> Any:
        """注入证书错误而不伪造成功连接，正常 TLS 由其他测试实际覆盖。"""
        raise ssl.SSLCertVerificationError("synthetic-private-error")

    async def run() -> None:
        """固定解析后在连接边界失败，客户端只得到安全类别。"""
        async with tls_service(monkeypatch, certificates, response(b"safe")):
            # 内层替换先还原，不能在外层 fixture 退出后再次恢复已关闭的测试连接器。
            with monkeypatch.context() as patch:
                patch.setattr(asyncio, "open_connection", fail_connect)
                with pytest.raises(ToolFailure, match="SSLCertVerificationError") as error:
                    await tool.run(HttpArguments(url=f"https://{HOST}/", secret_ref="fixture"))
                assert "synthetic-private-error" not in str(error.value)
        assert tool.active_connections == 0

    asyncio.run(run())


@pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="显式真实 Rootless Docker"
)
def test_real_docker_no_direct_network_or_controller_secret(tmp_path: Path) -> None:
    """正式 Docker Shell 实际无 IPv4/IPv6 外连且看不到工作区外秘密，代理独立验收。"""
    info = subprocess.run(
        ["docker", "info", "--format", "{{json .SecurityOptions}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "rootless" in info.stdout
    source = tmp_path / "source"
    source.mkdir()
    path = tmp_path / "credential"
    path.write_text("synthetic-container-secret-0123456789")
    path.chmod(0o600)
    ref = SecretReference(name="fixture", host=HOST, file=path)
    config = ProductConfig(
        network=network(secrets=(ref,)),
        state_directory=tmp_path / "state",
        backend=BackendConfig(
            kind="docker",
            docker_image=os.environ.get("CODEPILOT_TEST_IMAGE", "codepilot-sandbox:0.1.0"),
        ),
    )
    script = (
        "import os,socket,pathlib; "
        f"assert not pathlib.Path({str(path)!r}).exists(); "
        "assert not any('synthetic-container-secret' in v for v in os.environ.values()); "
        "print('private-secret-absent')\n"
        "for family,target in [(socket.AF_INET,('1.1.1.1',443)),"
        "(socket.AF_INET6,('2606:4700:4700::1111',443))]:\n"
        " s=socket.socket(family);s.settimeout(1)\n"
        " try:s.connect(target)\n"
        " except OSError:print('direct-network-denied')\n"
        " else:raise AssertionError('direct-network-available')\n"
        " finally:s.close()\n"
    )
    import shlex

    result = asyncio.run(
        CodePilotApplication(config, source, approval=ScriptedApproval([True])).run(
            "检查网络边界",
            direct_tool=ToolCall(
                call_id="boundary",
                name="shell",
                arguments={"command": "python -c " + shlex.quote(script)},
            ),
        )
    )
    tool = next(o.result for o in result.report.observations if o.result)
    assert not tool.is_error and tool.content.count("direct-network-denied") == 2
    assert "private-secret-absent" in tool.content and result.resources.backend_closed


@pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="显式真实 Rootless Docker"
)
def test_real_docker_public_https_broker_and_rejection(tmp_path: Path) -> None:
    """真实 Docker 正式产品链读取公开 HTTPS，未授权域名不会请求且资源关闭。"""
    source = tmp_path / "source"
    source.mkdir()
    config = ProductConfig(
        network=NetworkConfig(
            enabled=True,
            destinations=(Destination(host="www.python.org", path_prefix="/robots.txt"),),
        ),
        state_directory=tmp_path / "state",
        backend=BackendConfig(
            kind="docker",
            docker_image=os.environ.get("CODEPILOT_TEST_IMAGE", "codepilot-sandbox:0.1.0"),
        ),
    )

    async def run() -> None:
        """分别核验公开正文、未知域名拒绝和后台清理，不调用模型。"""
        for url, success in [
            ("https://www.python.org/robots.txt", True),
            ("https://example.com/", False),
        ]:
            result = await CodePilotApplication(
                config, source, approval=ScriptedApproval([True])
            ).run(
                "读取公开规则",
                direct_tool=ToolCall(call_id="https", name="http_request", arguments={"url": url}),
            )
            tool = next(o.result for o in result.report.observations if o.result)
            assert tool.is_error != success, tool.content
            assert result.resources.backend_closed
            if success:
                data = json.loads(tool.content)
                assert data["complete"] and "User-agent" in data["text"]
            else:
                assert "destination_denied" in tool.content

    asyncio.run(run())


@pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="显式真实 Rootless Docker"
)
def test_real_docker_synthetic_tls_secret_is_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """真实 Docker/正式应用配合本机受控 TLS，合成秘密只进入获批连接且无持久回显。"""
    source = tmp_path / "source"
    source.mkdir()
    path = tmp_path / "credential"
    value = b"synthetic-live-credential-0123456789"
    path.write_bytes(value)
    path.chmod(0o600)
    config = ProductConfig(
        network=network(secrets=(SecretReference(name="fixture", host=HOST, file=path),)),
        state_directory=tmp_path / "state",
        backend=BackendConfig(
            kind="docker",
            docker_image=os.environ.get("CODEPILOT_TEST_IMAGE", "codepilot-sandbox:0.1.0"),
        ),
    )

    async def run() -> None:
        """第一次回显拒绝，第二次正确正文；服务端和私有存储分别独立检查。"""
        for body, success in [(b"echo:" + value, False), (b"private fixture document", True)]:
            async with tls_service(monkeypatch, certificates, response(body)) as requests:
                result = await CodePilotApplication(
                    config, source, approval=ScriptedApproval([True])
                ).run(
                    "读取受控资料",
                    direct_tool=ToolCall(
                        call_id="secret",
                        name="http_request",
                        arguments={"url": f"https://{HOST}/", "secret_ref": "fixture"},
                    ),
                )
                tool = next(o.result for o in result.report.observations if o.result)
                assert tool.is_error != success and result.resources.backend_closed
                assert len(requests) == 1 and b"Authorization: Bearer " + value in requests[0]
        for file in (tmp_path / "state").rglob("*"):
            if file.is_file():
                assert value not in file.read_bytes()

    asyncio.run(run())


def test_sent_but_incomplete_response_blocks_resume_until_explicit_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificates: tuple[ssl.SSLContext, ssl.SSLContext],
) -> None:
    """请求已发送但响应截断，错误配对/Runtime completed 也不能清掉远端未知状态。"""
    from codepilot.sessions.store import SessionStore

    source = tmp_path / "source"
    source.mkdir()
    config = ProductConfig(
        network=network(),
        state_directory=tmp_path / "state",
        backend=BackendConfig(
            kind="docker",
            docker_image=(
                os.environ.get("CODEPILOT_TEST_IMAGE", "codepilot-sandbox:0.1.0")
                if os.environ.get("CODEPILOT_DOCKER_LIVE") == "1"
                else "fixture"
            ),
        ),
    )

    def factory(
        config: ProductConfig, root: Path, directory: Path, **kwargs: Any
    ) -> LocalExecutionBackend:
        """确定性主链只替换工作区后端，网络仍是实际 TLS 协议。"""
        return LocalExecutionBackend(root, audit_directory=directory)

    # 普通离线使用真实 Local 文件后端，显式真机运行则保留正式 Docker 工厂。
    if os.environ.get("CODEPILOT_DOCKER_LIVE") != "1":
        monkeypatch.setattr(service, "_create_backend", factory)

    async def run() -> None:
        """独立核对服务端实际一次请求、持久阻断和明确接受未知而非伪造远端成功。"""
        async with tls_service(
            monkeypatch, certificates, b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\npartial"
        ) as requests:
            result = await CodePilotApplication(
                config, source, approval=ScriptedApproval([True])
            ).run(
                "读取资料",
                session_id="unknown-network",
                direct_tool=ToolCall(
                    call_id="unknown", name="http_request", arguments={"url": f"https://{HOST}/"}
                ),
            )
            assert len(requests) == 1 and result.resources.backend_closed
            assert result.report.reason == "completed" and result.needs_reconciliation
        with SessionStore(tmp_path / "state", "unknown-network", create=False) as session:
            assert session.get("status") == "needs_reconciliation"
            assert session.diagnose()["network_pending"]
            with pytest.raises(ValueError, match="needs_reconciliation"):
                session.check(source, config.execution_mode)
            with pytest.raises(ValueError, match="accept-network-unknown"):
                session.reconcile(source)
            session.reconcile(source, accept_network_unknown=True)
            assert session.get("status") == "ready" and not session.get("network_pending")
            assert any(m.tool_result and m.tool_result.is_error for m in session.history())

    asyncio.run(run())
