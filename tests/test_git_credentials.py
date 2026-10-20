"""真实 RSA/TLS/Git 协议及持久凭据故障验收；测试发行方不冒充 GitHub 官方验收。"""

import asyncio
import base64
import json
import os
import ssl
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h11
import jwt
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import codepilot.network.transport as transport
from codepilot.app import CodePilotApplication
from codepilot.app.operations import prune
from codepilot.config import BackendConfig, ProductConfig
from codepilot.contracts import ToolCall
from codepilot.network.broker import contains_secret
from codepilot.network.credentials import CredentialVault, GitHubLease, file_identity, recover
from codepilot.network.egress import ConnectProxy, EgressBudget
from codepilot.network.git_gateway import GitGateway
from codepilot.network.models import GitHubRepository, NativeNetworkConfig, NetworkConfig
from codepilot.network.native import NativeShellTool, NetworkShellArguments
from codepilot.permissions import ScriptedApproval
from codepilot.permissions.action import build_action
from codepilot.sessions.store import SessionStore
from codepilot.tools.base import ToolFailure
from codepilot.workspace.docker import DockerExecutionBackend

TOKEN = b"synthetic-github-installation-token-0123456789"


@pytest.fixture
def repository(tmp_path: Path) -> GitHubRepository:
    """为测试生成一次性 RSA 私钥，真实用户凭据从不参与。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return GitHubRepository(
        name="fixture",
        client_id="test-app",
        installation_id=7,
        repository_id=9,
        owner="fixture",
        repository="repo",
        private_key_file=path,
        contents="write",
    )


class Issuer:
    """独立合成发行方事实和实际 Git CGI 后端，不代替生产判断。"""

    def __init__(self, root: Path, repository: GitHubRepository, mode: str) -> None:
        """冻结测试范围、故障方式和服务端计数。"""
        self.root, self.repository, self.mode = root, repository, mode
        self.issued = self.revoked = self.git_calls = 0
        self.valid = False
        self.port = 0
        self.seen = asyncio.Event()
        self.errors: list[str] = []

    async def response(self, request: h11.Request, body: bytes) -> tuple[int, bytes, bytes]:
        """服务端独立验证 JWT、单仓库请求以及实际 Git Basic 认证。"""
        headers = dict(request.headers)
        if request.target == b"/app/installations/7/access_tokens":
            key = serialization.load_pem_private_key(
                self.repository.private_key_file.read_bytes(), None
            )
            assert isinstance(key, rsa.RSAPrivateKey)
            claims = jwt.decode(
                headers[b"authorization"].removeprefix(b"Bearer "),
                key.public_key(),
                algorithms=["RS256"],
                issuer="test-app",
            )
            assert 0 < claims["exp"] - claims["iat"] <= 600
            assert json.loads(body) == {
                "repository_ids": [9],
                "permissions": {"contents": self.repository.contents},
            }
            assert headers[b"x-github-api-version"] == b"2026-03-10"
            self.issued += 1
            self.valid = True
            self.seen.set()
            if self.mode == "issue_hang":
                await asyncio.Event().wait()
            if self.mode == "issue_truncate":
                return 201, b"application/json", b"{"
            permissions = {"contents": self.repository.contents, "metadata": "read"}
            if self.mode == "scope":
                permissions["issues"] = "write"
            seconds = -1 if self.mode == "expired" else 7200 if self.mode == "long" else 3600
            value = {
                "token": TOKEN.decode(),
                "permissions": permissions,
                "repository_selection": "selected",
                "repositories": [
                    {"id": 9 if self.mode != "wrong_repo" else 10, "full_name": "fixture/repo"}
                ],
                "expires_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
            }
            return 201, b"application/json", json.dumps(value).encode()
        if request.target == b"/installation/token":
            assert request.method == b"DELETE" and headers[b"authorization"] == b"Bearer " + TOKEN
            self.revoked += 1
            if self.mode == "revoke":
                return 500, b"application/json", b"private server failure"
            self.valid = False
            return 204, b"application/json", b""
        assert headers[b"host"] == b"github.com" and self.valid
        assert headers[b"authorization"] == b"Basic " + base64.b64encode(b"x-access-token:" + TOKEN)
        assert request.target.startswith(b"/fixture/repo.git/")
        self.git_calls += 1
        if self.mode == "echo":
            return 200, b"application/x-git-upload-pack-advertisement", TOKEN
        path, _, query = request.target.decode().partition("?")
        process = await asyncio.create_subprocess_exec(
            "git",
            "http-backend",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_PROJECT_ROOT": str(self.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "REQUEST_METHOD": request.method.decode(),
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": headers.get(b"content-type", b"").decode(),
                "REMOTE_USER": "app",
                "HTTP_GIT_PROTOCOL": headers.get(b"git-protocol", b"").decode(),
            },
        )
        try:
            output, error = await process.communicate(body)
            assert process.returncode == 0, error.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        head, payload = output.split(b"\r\n\r\n", 1)
        fields = dict(line.split(b": ", 1) for line in head.split(b"\r\n"))
        return (
            int(fields.get(b"Status", b"200").split()[0]),
            fields.get(b"Content-Type", b""),
            payload,
        )


@asynccontextmanager
async def issuer_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository, mode: str = "ok"
) -> AsyncIterator[Issuer]:
    """真实 TLS 连接仅替换测试路由和根证书，生产配置没有私网或跳过验证选项。"""
    key, cert = tmp_path / "tls-key.pem", tmp_path / "tls-cert.pem"
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
            "/CN=api.github.com",
            "-addext",
            "subjectAltName=DNS:api.github.com,DNS:github.com",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.create_default_context(cafile=str(cert))
    issuer = Issuer(tmp_path / "remote", repository, mode)
    bare = issuer.root / "fixture/repo.git"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True, timeout=10)
    subprocess.run(
        ["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True, timeout=10
    )
    tasks: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """使用 h11 重组请求，包括二进制 pack；服务端断言错误必须使测试失败。"""
        task = asyncio.current_task()
        assert task
        tasks.add(task)
        connection = h11.Connection(h11.SERVER)
        request: h11.Request | None = None
        body = bytearray()
        try:
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    connection.receive_data(await reader.read(16384))
                elif isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    assert request
                    status, content_type, payload = await issuer.response(request, bytes(body))
                    writer.write(
                        connection.send(
                            h11.Response(
                                status_code=status,
                                headers=[
                                    (b"content-type", content_type),
                                    (b"content-length", str(len(payload)).encode()),
                                    (b"connection", b"close"),
                                ],
                                reason=b"Test",
                            )
                        )
                    )
                    if payload:
                        writer.write(connection.send(h11.Data(data=payload)))
                    writer.write(connection.send(h11.EndOfMessage()))
                    await writer.drain()
                    break
                else:
                    break
        except Exception as error:
            issuer.errors.append(type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    issuer.port = port
    original = asyncio.open_connection

    async def resolve(host: str) -> str:
        """服务仅允许两个固定官方名字，不放宽生产域名匹配。"""
        assert host in {"api.github.com", "github.com"}
        return "93.184.216.34"

    async def connect(
        host: str, requested: int, **kwargs: Any
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """断言数字 IP、443、SNI 和实际证书校验，测试地址才转换到回环。"""
        assert host == "93.184.216.34" and requested == 443
        assert kwargs["server_hostname"] in {"api.github.com", "github.com"}
        return await original("127.0.0.1", port, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(transport, "resolve_public", resolve)
        patch.setattr(transport, "system_tls_context", lambda: client_context)
        patch.setattr(asyncio, "open_connection", connect)
        try:
            yield issuer
        finally:
            server.close()
            await server.wait_closed()
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert not issuer.errors, issuer.errors


@pytest.mark.parametrize(
    "mode", ["ok", "scope", "expired", "long", "wrong_repo", "issue_truncate", "revoke"]
)
def test_issuer_scope_encryption_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository, mode: str
) -> None:
    """真实签名和 TLS 覆盖最小权限、到期、无自动重放与撤销失败后的恢复。"""

    async def run() -> None:
        """独立读取密文与发行方事实，任何失败不得泄漏合成 token。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with SessionStore(tmp_path / "state", "fixture") as session:
            session.commit("test_workspace", {}, workspace=[str(workspace)])
            lease = GitHubLease(
                repository, file_identity(repository.private_key_file), session, "run"
            )
            async with issuer_service(tmp_path, monkeypatch, repository, mode) as issuer:
                if mode in {"ok", "revoke"}:
                    assert await lease.acquire() == TOKEN
                    assert await lease.acquire() == TOKEN and issuer.issued == 1
                    if mode == "ok":
                        lease.expires = time.time() - 1
                        with pytest.raises(ToolFailure, match="expired"):
                            await lease.acquire()
                        assert issuer.issued == 1
                else:
                    with pytest.raises(ToolFailure):
                        await lease.acquire()
                    with pytest.raises(ToolFailure):
                        await lease.acquire()
                    assert issuer.issued == 1
                assert lease.record is not None
                vault = lease.vault
                assert not contains_secret(vault.path(lease.record["id"]).read_bytes(), TOKEN)
                if mode in {"revoke", "issue_truncate"}:
                    with pytest.raises(ToolFailure):
                        await lease.close()
                    session.finish(None, workspace, clean=True)
                    assert session.get("status") == "needs_reconciliation"
                    with pytest.raises(ValueError, match="credential"):
                        session.reconcile(workspace, accept_network_unknown=mode == "revoke")
                    lease.record["controller_pid"] = 999999999
                    vault.save(lease.record)
                    issuer.mode = "ok"
                    result = await recover(
                        session,
                        lease.record["id"],
                        apply=True,
                        accept_unknown=mode == "issue_truncate",
                    )
                    assert result["removed"]
                    assert issuer.revoked == (2 if mode == "revoke" else 0)
                else:
                    await lease.close()
                    assert issuer.revoked == 1 and not issuer.valid
                assert session.credential_leases()[0]["phase"] == "closed"
                for path in session.directory.rglob("*"):
                    if path.is_file():
                        assert not contains_secret(path.read_bytes(), TOKEN), path.name

    asyncio.run(run())


def test_key_rotation_action_and_reference_boundary(
    tmp_path: Path, repository: GitHubRepository
) -> None:
    """审批包含精确范围和文件版本；轮换、工作区内密钥和无联网引用均拒绝。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert GitHubRepository.model_validate_json(repository.model_dump_json()) == repository
    canonical = GitHubRepository.model_validate({**repository.model_dump(), "owner": "Fixture-Org"})
    assert "/Fixture-Org/repo.git:" in canonical.scope
    linked = tmp_path / "linked.pem"
    os.link(repository.private_key_file, linked)
    with pytest.raises(ValueError, match="private, regular"):
        file_identity(linked)
    linked.unlink()
    config = NativeNetworkConfig(enabled=True, github_repositories=(repository,))
    with SessionStore(tmp_path / "state", "fixture") as session:
        backend = DockerExecutionBackend(workspace, "codepilot-sandbox:0.1.0")
        tool = NativeShellTool(backend, config, session, "run", session.directory / "leases")
        call = ToolCall(
            call_id="a",
            name="shell",
            arguments={"command": "git fetch", "network": True, "git_credentials": ["fixture"]},
        )
        before = build_action(tool.spec, call, backend)
        assert before.secrets == ("fixture",) and repository.scope in before.network_scope
        assert "GIT_CONFIG_KEY_2" in before.environment_names
        repository.private_key_file.write_bytes(repository.private_key_file.read_bytes() + b"\n")
        assert build_action(tool.spec, call, backend).fingerprint != before.fingerprint
        with pytest.raises(ValueError, match="requires network"):
            NetworkShellArguments(command="pwd", git_credentials=("fixture",))
        with pytest.raises(ValueError, match="outside workspace"):
            NativeShellTool(
                DockerExecutionBackend(tmp_path, "fixture"), config, session, "run", tmp_path
            )
        assert not session.get("resources", [])


@pytest.mark.parametrize(
    "target,method,extra",
    [
        ("/git/fixture/info/refs?service=git-receive-pack", "GET", ""),
        ("/git/fixture/../repo/info/refs?service=git-upload-pack", "GET", ""),
        ("/git/fixture/info/refs?service=git-upload-pack&other=x", "GET", ""),
        ("/git/other/info/refs?service=git-upload-pack", "GET", ""),
        ("/git/fixture/info/refs?service=git-upload-pack", "GET", "Authorization: Basic nope\r\n"),
        ("/git/fixture/git-upload-pack", "POST", "Content-Type: text/plain\r\n"),
    ],
)
def test_gateway_rejects_before_issuance(
    tmp_path: Path, repository: GitHubRepository, target: str, method: str, extra: str
) -> None:
    """真实 Unix HTTP 字节触发拒绝，服务端签发函数不应运行。"""

    async def run() -> None:
        """不用 fake 工具结果，独立核验持久资源仍为空。"""
        with SessionStore(tmp_path / "state", "fixture") as session:
            ref = repository.model_copy(update={"contents": "read"})
            lease = GitHubLease(ref, file_identity(ref.private_key_file), session, "run")
            budget = EgressBudget(NativeNetworkConfig(enabled=True, github_repositories=(ref,)))
            gateway = GitGateway({ref.name: lease}, budget)
            proxy = ConnectProxy(budget, tmp_path / "proxy.sock", gateway.handle)
            await proxy.start()
            try:
                reader, writer = await asyncio.open_unix_connection(proxy.path)
                writer.write(
                    f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1:3129\r\n{extra}\r\n".encode()
                )
                await writer.drain()
                assert b"403" in await reader.read()
                writer.close()
                await writer.wait_closed()
                await proxy.idle.wait()
                assert proxy.failed and lease.record is None and not session.get("resources", [])
            finally:
                await proxy.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["cancel", "key_loss", "tamper", "tls", "rotation"])
def test_secret_cleanup_failure_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository, mode: str
) -> None:
    """取消、缺钥和密文移接均保留资源；不可信 TLS 与私钥轮换时零签发。"""

    async def run() -> None:
        """实际服务端计数独立于工具返回和会话阶段。"""
        with SessionStore(tmp_path / "state", "fixture") as session:
            lease = GitHubLease(
                repository, file_identity(repository.private_key_file), session, "run"
            )
            service_mode = "issue_hang" if mode == "cancel" else "ok"
            async with issuer_service(tmp_path, monkeypatch, repository, service_mode) as issuer:
                if mode == "tls":
                    monkeypatch.setattr(transport, "system_tls_context", ssl.create_default_context)
                    with pytest.raises(ToolFailure, match="SSLCertVerificationError"):
                        await lease.acquire()
                    await lease.close()
                    assert issuer.issued == 0
                elif mode == "rotation":
                    repository.private_key_file.write_bytes(
                        repository.private_key_file.read_bytes() + b"\n"
                    )
                    with pytest.raises(ToolFailure, match="ValueError"):
                        await lease.acquire()
                    await lease.close()
                    assert issuer.issued == 0
                elif mode == "cancel":
                    task = asyncio.create_task(lease.acquire())
                    await asyncio.wait_for(issuer.seen.wait(), 10)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    with pytest.raises(ToolFailure, match="issuance_unknown"):
                        await lease.close()
                    assert issuer.issued == 1 and issuer.revoked == 0
                    assert session.credential_leases()[0]["phase"] == "issuing"
                else:
                    await lease.acquire()
                    assert lease.record is not None
                    if mode == "key_loss":
                        lease.vault.key_path.unlink()
                        with pytest.raises(ValueError, match="missing"):
                            lease.vault.key(create=True)
                        with pytest.raises(FileNotFoundError):
                            lease.vault.decrypt(lease.record)
                    else:
                        moved = {**lease.record, "run_id": "another-run"}
                        with pytest.raises(InvalidTag):
                            lease.vault.decrypt(moved)
                    assert lease.vault.load(lease.record["id"], "fixture")["phase"] == "issued"
                    await lease.close()
                    assert issuer.revoked == 1

    asyncio.run(run())


@pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="显式真实 Rootless Docker"
)
@pytest.mark.parametrize(
    "mode", ["ok", "read", "revoke", "echo", "cancel", "denied", "omitted", "quota"]
)
def test_rootless_authenticated_git_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository, mode: str
) -> None:
    """正式产品链实际 Git push/fetch；独立远端 ref、文件与撤销事实决定验收。"""

    async def run() -> None:
        """合成发行方真实 TLS；容器和 Git 没有模拟，秘密从不进入镜像或环境。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "protected.txt").write_bytes(b"user-owned\r\n")
        ref = repository.model_copy(update={"contents": "read"}) if mode == "read" else repository
        config = ProductConfig(
            state_directory=tmp_path / "state",
            permission="controlled-edit",
            backend=BackendConfig(kind="docker", docker_image="codepilot-sandbox:0.1.0"),
            network=NetworkConfig(
                enabled=True,
                native=NativeNetworkConfig(
                    enabled=True,
                    github_repositories=(ref,),
                    lease_seconds=120,
                    total_bytes=4 * 1024**2,
                    max_connections=1 if mode == "quota" else 32,
                ),
            ),
        )
        command = (
            "git init -q -b main && "
            "printf '#!/bin/sh\\ntouch hook-ran\\n' > .git/hooks/pre-commit && "
            "chmod +x .git/hooks/pre-commit && printf 'verified payload\\n' > payload.txt && "
            "git add payload.txt && git -c user.name=Fixture -c user.email=fixture@example.invalid "
            "commit -qm fixture && "
            "git push https://github.com/fixture/repo.git main && "
            "git fetch https://github.com/fixture/repo.git main && "
            "git show FETCH_HEAD:payload.txt && "
            "env > environment.txt && git config --local --list > local-config.txt"
        )
        if mode in {"read", "echo"}:
            command = "git ls-remote https://github.com/fixture/repo.git"
            if mode == "read":
                command = (
                    "git init -q -b main && printf fixture > file.txt && git add file.txt && "
                    "git -c user.name=Fixture -c user.email=f@example.invalid "
                    "commit -qm fixture && "
                    "git ls-remote https://github.com/fixture/repo.git && "
                    "git push https://github.com/fixture/repo.git HEAD:main"
                )
        if mode == "omitted":
            command = "env > environment.txt"
        if mode == "cancel":
            command = "git ls-remote https://github.com/fixture/repo.git && touch ready && sleep 90"
        async with issuer_service(
            tmp_path,
            monkeypatch,
            ref,
            "ok" if mode in {"read", "cancel", "denied", "omitted", "quota"} else mode,
        ) as issuer:
            application = CodePilotApplication(
                config, workspace, approval=ScriptedApproval([mode != "denied"])
            )
            task = asyncio.create_task(
                application.run(
                    "测试专用仓库读写",
                    session_id="fixture",
                    direct_tool=ToolCall(
                        call_id="git",
                        name="shell",
                        arguments={
                            "command": command,
                            "network": True,
                            "git_credentials": [] if mode == "omitted" else [ref.name],
                            "timeout_seconds": 100.0,
                        },
                    ),
                )
            )
            if mode == "cancel":
                async with asyncio.timeout(60):
                    while not (workspace / "ready").exists() and not task.done():
                        await asyncio.sleep(0.05)
                assert (workspace / "ready").exists()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                result = None
            else:
                result = await task
                assert result.resources.backend_closed
            tools = [o.result for o in result.report.observations if o.result] if result else []
            assert (workspace / "protected.txt").read_bytes() == b"user-owned\r\n"
            assert not (workspace / "hook-ran").exists()
            if mode == "ok":
                assert result is not None
                assert tools and not tools[0].is_error, tools
                assert not result.needs_reconciliation
                bare = issuer.root / "fixture/repo.git"
                remote = subprocess.check_output(
                    ["git", "-C", str(bare), "rev-parse", "refs/heads/main"], timeout=10
                ).strip()
                local = subprocess.check_output(
                    ["git", "-C", str(workspace), "rev-parse", "HEAD"], timeout=10
                ).strip()
                assert remote == local
                assert (
                    subprocess.check_output(
                        ["git", "-C", str(bare), "show", "main:payload.txt"], timeout=10
                    )
                    == b"verified payload\n"
                )
                assert issuer.issued == issuer.revoked == 1 and issuer.git_calls >= 4
            elif mode in {"denied", "omitted"}:
                assert issuer.issued == issuer.git_calls == issuer.revoked == 0
            elif mode == "revoke":
                assert result is not None
                assert result.needs_reconciliation and issuer.issued == issuer.revoked == 1
                issuer.mode = "ok"
                recovery = await prune(config, "fixture", apply=True)
                credential = next(
                    r for r in recovery["resources"] if r["kind"] == "credential_lease"
                )
                # 旧控制器与测试同进程，但已结束时仅凭 PID 活着不能拒绝明确失败的撤销。
                assert credential["removed"], recovery
                assert issuer.revoked == 2
            elif mode == "cancel":
                assert issuer.issued == issuer.revoked == 1
            else:
                assert result is not None and result.needs_reconciliation
                assert issuer.issued == issuer.revoked == 1
                assert tools and tools[0].is_error
            for directory in (workspace, config.state_directory):
                assert directory
                for path in directory.rglob("*"):
                    if path.is_file():
                        assert not contains_secret(path.read_bytes(), TOKEN), path.name
            with SessionStore(tmp_path / "state", "fixture", create=False) as session:
                if mode not in {"denied", "omitted"}:
                    assert all(r["phase"] == "closed" for r in session.credential_leases())
                    for lease in session.native_leases():
                        assert lease["phase"] == "closed"

    asyncio.run(run())


@pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="显式真实 Rootless Docker"
)
def test_rootless_sigkill_then_formal_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository
) -> None:
    """实际 SIGKILL 整个控制器后，仅从密文恢复服务端撤销，并清理精确容器/socket。"""

    async def run() -> None:
        """父进程没有子控制器的内存 token；正式维护链决定恢复结果。"""
        import sys

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "reference.json").write_text(repository.model_dump_json())
        async with issuer_service(tmp_path, monkeypatch, repository) as issuer:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).parent / "fixtures/credential_controller.py"),
                str(tmp_path),
                str(issuer.port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(120):
                    while not (workspace / "ready").exists() and process.returncode is None:
                        await asyncio.sleep(0.05)
                assert (workspace / "ready").exists(), (
                    await process.communicate()
                    if process.returncode is not None
                    else "barrier missing"
                )
                assert issuer.valid and issuer.issued == 1 and issuer.revoked == 0
                process.kill()
                await process.wait()
                assert process.returncode == -9
                config = ProductConfig(state_directory=tmp_path / "state")
                preview = await prune(config, "killed")
                assert any(
                    r["kind"] == "credential_lease" and r["eligible"] for r in preview["resources"]
                )
                assert issuer.revoked == 0
                result = await prune(config, "killed", apply=True)
                assert all(
                    r["removed"]
                    for r in result["resources"]
                    if r["kind"] in {"credential_lease", "native_lease"}
                ), result
                assert issuer.revoked == 1 and not issuer.valid
                with SessionStore(tmp_path / "state", "killed", create=False) as session:
                    assert session.get("status") == "running"
                    assert session.get("network_pending")
                    session.reconcile(workspace, accept_network_unknown=True)
                    assert session.get("status") == "ready"
                    assert not session.get("network_pending")
                assert not contains_secret((tmp_path / "reference.json").read_bytes(), TOKEN)
            finally:
                # 测试失败也只终止自己创建的控制器；确切 Docker 资源仍由正式恢复链处理。
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["ok", "quota", "echo"])
def test_native_git_through_tls_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository, mode: str
) -> None:
    """不依赖 Docker 的真实原生 Git push/fetch，CI 也核验完整凭据和网关协议。"""

    async def run() -> None:
        """真实 Git 使用生产 URL 环境；远端对象、hook 禁用和服务端撤销独立核对。"""
        from codepilot.network.git_gateway import git_environment
        from codepilot.tools.process import workspace_environment

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with SessionStore(tmp_path / "state", "fixture") as session:
            lease = GitHubLease(
                repository, file_identity(repository.private_key_file), session, "run"
            )
            budget = EgressBudget(
                NativeNetworkConfig(
                    enabled=True,
                    github_repositories=(repository,),
                    max_connections=1 if mode == "quota" else 32,
                )
            )
            gateway = GitGateway({repository.name: lease}, budget)
            proxy = ConnectProxy(budget, tmp_path / "proxy.sock", gateway.handle)
            async with issuer_service(
                tmp_path, monkeypatch, repository, "echo" if mode == "echo" else "ok"
            ) as issuer:
                # 固定回环端口与容器桥相同；测试不开放宿主外部接口。
                proxy.server = await asyncio.start_server(proxy.handle, "127.0.0.1", 3129)
                environment = workspace_environment()
                environment.update(
                    dict(item.split("=", 1) for item in git_environment(gateway.leases))
                )
                process: asyncio.subprocess.Process | None = None
                try:
                    process = await asyncio.create_subprocess_exec(
                        "/bin/sh",
                        "-c",
                        "git init -q -b main && "
                        "printf '#!/bin/sh\\ntouch hook-ran\\n' > .git/hooks/pre-commit && "
                        "chmod +x .git/hooks/pre-commit && printf 'payload\\n' > file.txt && "
                        "git add file.txt && git -c user.name=Fixture "
                        "-c user.email=f@example.invalid "
                        "commit -qm fixture && "
                        "git push https://github.com/fixture/repo.git main && "
                        "git fetch https://github.com/fixture/repo.git main",
                        cwd=workspace,
                        env=environment,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    async with asyncio.timeout(20):
                        stdout, stderr = await process.communicate()
                    assert not (workspace / "hook-ran").exists()
                    assert issuer.issued == 1
                    if mode == "ok":
                        assert process.returncode == 0, (stdout, stderr)
                        assert issuer.git_calls >= 4
                        bare = issuer.root / "fixture/repo.git"
                        assert (
                            subprocess.check_output(
                                ["git", "-C", str(bare), "show", "main:file.txt"], timeout=10
                            )
                            == b"payload\n"
                        )
                    else:
                        # 业务失败不能跳过后面的撤销，且远端写 pack 未通过完整协议授权。
                        assert process.returncode != 0 and issuer.git_calls == 1
                finally:
                    if process is not None and process.returncode is None:
                        process.kill()
                        await process.wait()
                    await proxy.close()
                    await gateway.close()
                assert issuer.revoked == 1 and not issuer.valid
                assert not contains_secret(stdout + stderr, TOKEN)
                assert session.credential_leases()[0]["phase"] == "closed"

    asyncio.run(run())


def test_zero_model_cli_reference_contract(tmp_path: Path) -> None:
    """CLI 显式引用与原生联网绑定；无权限配置时在状态创建前拒绝。"""
    from codepilot.app.operations import test_command as execute_command
    from codepilot.ui.cli import build_parser

    options = build_parser().parse_args(
        ["test", "--network", "--git-credential", "fixture", "--", "git", "fetch"]
    )
    assert options.git_credential == ["fixture"] and options.network
    config = ProductConfig(state_directory=tmp_path / "state")
    with pytest.raises(ValueError, match="require enabled native network"):
        asyncio.run(
            execute_command(
                config, tmp_path, ["git", "fetch"], 10, network=True, git_credentials=("fixture",)
            )
        )
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("lose_key", [False, True])
def test_sigkill_vault_recovery_in_new_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository, lose_key: bool
) -> None:
    """普通 CI 真实杀控制器，正式 prune 从密文撤销；缺钥保留事实后恢复同一钥匙。"""

    async def run() -> None:
        """发行方独立保存有效性，父控制器从未接收子进程的明文 token。"""
        import sys

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "reference.json").write_text(repository.model_dump_json())
        async with issuer_service(tmp_path, monkeypatch, repository) as issuer:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).parent / "fixtures/credential_controller.py"),
                str(tmp_path),
                str(issuer.port),
                "lease-only",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(15):
                    while not (workspace / "ready").exists() and process.returncode is None:
                        await asyncio.sleep(0.02)
                assert (workspace / "ready").exists()
                process.kill()
                await process.wait()
                assert process.returncode == -9 and issuer.valid and issuer.revoked == 0
                config = ProductConfig(state_directory=tmp_path / "state")
                vault = CredentialVault(tmp_path / "state")
                if lose_key:
                    original_key = vault.key_path.read_bytes()
                    vault.key_path.unlink()
                    failed = await prune(config, "killed", apply=True)
                    assert not failed["resources"][0]["removed"]
                    assert issuer.valid and issuer.revoked == 0
                    with SessionStore(tmp_path / "state", "killed", create=False) as session:
                        assert session.credential_leases()[0]["phase"] == "issued"
                        with pytest.raises(ValueError, match="credential"):
                            session.reconcile(workspace, accept_network_unknown=True)
                    # 只恢复此测试保存的合成主密钥，不生成替代钥匙掩盖旧凭据。
                    vault.key_path.write_bytes(original_key)
                    vault.key_path.chmod(0o600)
                recovered = await prune(config, "killed", apply=True)
                assert recovered["resources"][0]["removed"], recovered
                assert issuer.revoked == 1 and not issuer.valid and issuer.issued == 1
                with SessionStore(tmp_path / "state", "killed", create=False) as session:
                    assert session.get("status") == "running"
                    session.reconcile(workspace)
                    assert session.get("status") == "ready"
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    asyncio.run(run())


def test_unreceived_token_requires_explicit_network_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: GitHubRepository
) -> None:
    """正式协调可接受未收到 token 的发行未知，但不得把它写成已服务端撤销。"""

    async def run() -> None:
        """合成发行方保持 token 有效，证明接受未知与撤销是不同事实。"""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with SessionStore(tmp_path / "state", "fixture") as session:
            session.commit("test_workspace", {}, workspace=[str(workspace)])
            lease = GitHubLease(
                repository, file_identity(repository.private_key_file), session, "run"
            )
            async with issuer_service(
                tmp_path, monkeypatch, repository, "issue_truncate"
            ) as issuer:
                with pytest.raises(ToolFailure):
                    await lease.acquire()
                with pytest.raises(ToolFailure):
                    await lease.close()
                session.finish(None, workspace, clean=True)
                with pytest.raises(ValueError, match="credential"):
                    session.reconcile(workspace)
                session.reconcile(workspace, accept_network_unknown=True)
                assert lease.record is not None
                saved = lease.vault.load(lease.record["id"], session.session_id)
                assert saved["phase"] == "closed" and saved["issuance_unknown_accepted"]
                assert issuer.valid and issuer.issued == 1 and issuer.revoked == 0
                assert session.get("status") == "ready"

    asyncio.run(run())
