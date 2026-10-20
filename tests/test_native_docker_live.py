"""真实 Rootless Docker 中验收同一 Shell 的代理、原生 Git 与失败关闭。"""

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from codepilot.app import CodePilotApplication
from codepilot.app.operations import test_command as execute_command
from codepilot.config import BackendConfig, ProductConfig
from codepilot.contracts import ToolCall
from codepilot.network.models import NativeNetworkConfig, NetworkConfig
from codepilot.permissions import ScriptedApproval

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="显式真实 Rootless Docker"
)


def config_for(tmp_path: Path) -> ProductConfig:
    """只允许公开 Git 与固定包文件源，不读取模型凭据。"""
    return ProductConfig(
        state_directory=tmp_path / "state",
        permission="controlled-edit",
        backend=BackendConfig(kind="docker", docker_image="codepilot-sandbox:0.1.0"),
        network=NetworkConfig(
            enabled=True,
            native=NativeNetworkConfig(
                enabled=True, hosts=("gitlab.com", "files.pythonhosted.org"), lease_seconds=120
            ),
        ),
    )


def test_actual_public_git_and_normal_shell_isolation(tmp_path: Path) -> None:
    """正式产品工具以原生 Git 访问公开仓库；结束后无临时容器或 socket。"""
    root = tmp_path / "repo"
    root.mkdir()
    config = config_for(tmp_path)
    approval = ScriptedApproval([True])
    result = asyncio.run(
        CodePilotApplication(config, root, approval=approval).run(
            "查询公开固定仓库版本",
            direct_tool=ToolCall(
                call_id="native-git",
                name="shell",
                arguments={
                    "command": (
                        "git ls-remote https://gitlab.com/gitlab-org/gitlab-test.git "
                        "refs/heads/master > refs.txt"
                    ),
                    "network": True,
                    "timeout_seconds": 45.0,
                },
            ),
        )
    )
    tool = next(o.result for o in result.report.observations if o.result)
    assert not tool.is_error, tool.content
    assert result.resources.backend_closed and not result.needs_reconciliation
    parts = (root / "refs.txt").read_text().split()
    assert len(parts[0]) == 40 and parts[1] == "refs/heads/master"
    assert approval.requests and approval.requests[0].action.network
    assert config.state_directory is not None
    directory = config.state_directory / result.identity.session_id
    manifests = list(directory.rglob("lease-*.json"))
    assert len(manifests) == 1
    record = json.loads(manifests[0].read_bytes())
    assert record["phase"] == "closed" and record["connections"] >= 1 and record["bytes"] > 0
    assert not Path(record["socket_directory"]).exists()
    inspected = subprocess.run(
        ["docker", "inspect", record["container_id"]], capture_output=True, timeout=10
    )
    assert inspected.returncode != 0 and b"no such" in inspected.stderr.lower()
    plain = asyncio.run(
        execute_command(
            config,
            root,
            [
                "python",
                "-c",
                "import os,pathlib; assert 'HTTPS_PROXY' not in os.environ; "
                "assert not pathlib.Path('/run/codepilot-egress.sock').exists()",
            ],
            10,
            approval=ScriptedApproval([True]),
        )
    )
    assert plain["command_succeeded"] and plain["backend_closed"]


def test_native_denied_domain_and_approval_rejection(tmp_path: Path) -> None:
    """拒绝审批零租约；原生客户端访问未授权域名失败且容器清理。"""
    root = tmp_path / "repo"
    root.mkdir()
    config = config_for(tmp_path)
    call = ToolCall(
        call_id="denied",
        name="shell",
        arguments={"command": "git ls-remote https://example.com/repo.git", "network": True},
    )
    rejected = asyncio.run(
        CodePilotApplication(config, root, approval=ScriptedApproval([False])).run(
            "拒绝联网", direct_tool=call
        )
    )
    assert rejected.resources.backend_closed
    assert config.state_directory is not None
    assert not list(config.state_directory.rglob("lease-*.json"))
    denied = asyncio.run(
        CodePilotApplication(config, root, approval=ScriptedApproval([True])).run(
            "未授权目的地", direct_tool=call
        )
    )
    tool = next(o.result for o in denied.report.observations if o.result)
    assert tool.is_error and "native_destination_denied" in tool.content
    assert denied.resources.backend_closed and not denied.needs_reconciliation


def test_fixed_hashed_wheel_install_and_offline_import(tmp_path: Path) -> None:
    """按预先固定 SHA256 安装轻量 wheel，后续独立断网容器校验实际导入版本。"""
    root = tmp_path / "repo"
    root.mkdir()
    config = config_for(tmp_path)
    # 版本与 SHA 取自项目 uv.lock，不让模型现场选择依赖版本或内容。
    import tomllib

    lock = tomllib.loads((Path(__file__).parents[1] / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == "idna")
    wheel = package["wheels"][0]
    url = wheel["url"]
    assert url.startswith("https://files.pythonhosted.org/")
    requirement = url + " --hash=" + wheel["hash"]
    (root / "requirements.txt").write_text(requirement + "\n")
    data = asyncio.run(
        execute_command(
            config,
            root,
            [
                "python",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--require-hashes",
                "--target",
                "/workspace/deps",
                "-r",
                "requirements.txt",
            ],
            60,
            approval=ScriptedApproval([True]),
            network=True,
        )
    )
    assert data["command_succeeded"], data["result"]
    assert not data["needs_reconciliation"]
    probe = (
        "import sys;sys.path.insert(0,'deps');import idna;"
        f"assert idna.__version__=={package['version']!r}"
    )
    offline = asyncio.run(
        execute_command(
            config, root, ["python", "-c", probe], 15, approval=ScriptedApproval([True])
        )
    )
    assert offline["command_succeeded"] and offline["backend_closed"]
    # 验收读取真实产物，不能仅相信 pip 自述安装成功。
    assert (root / "deps" / "idna" / "__init__.py").is_file()
    assert len(hashlib.sha256((root / "requirements.txt").read_bytes()).hexdigest()) == 64


def test_native_bypass_and_deadline_cleanup(tmp_path: Path) -> None:
    """即使主动忽略代理，专有容器的 IPv4/IPv6 外连仍被 none 网络阻断。"""
    root = tmp_path / "repo"
    root.mkdir()
    script = (
        "import socket\n"
        "for family,target in [(socket.AF_INET,('1.1.1.1',443)),"
        "(socket.AF_INET6,('2606:4700:4700::1111',443))]:\n"
        " s=socket.socket(family);s.settimeout(.5)\n"
        " try:s.connect(target)\n"
        " except OSError:print('denied')\n"
        " else:raise AssertionError('bypass')\n"
        " finally:s.close()\n"
    )
    result = asyncio.run(
        execute_command(
            config_for(tmp_path),
            root,
            ["python", "-c", script],
            10,
            approval=ScriptedApproval([True]),
            network=True,
        )
    )
    assert result["command_succeeded"] and not result["needs_reconciliation"]
    assert result["result"]["content"].count("denied") == 2


def test_local_tls_git_fetch_push_independent_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 Git HTTP 后端与真实 Docker 完成 fetch/push，宿主独立核验远端提交。"""
    import ssl
    from typing import Any
    from urllib.parse import urlsplit

    from test_git_workspace import _git, _repository

    import codepilot.network.egress as egress

    root = tmp_path / "workspace"
    root.mkdir()
    seed = _repository(tmp_path / "seed")
    bare = tmp_path / "fixture.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(bare)], check=True, capture_output=True
    )
    _git(bare, "config", "http.receivepack", "true")
    before = _git(bare, "rev-parse", "HEAD")
    key, cert = tmp_path / "tls.key", root / "ca.pem"
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
            "/CN=git.example",
            "-addext",
            "subjectAltName=DNS:git.example",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    seen: list[str] = []

    async def run() -> None:
        """测试只替换 DNS/端口路由；客户端保留实际证书校验与原生 Git 协议。"""
        tasks: set[asyncio.Task[None]] = set()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            """真实 git-http-backend 处理 CGI 请求；远端记录路径并核对 push。"""
            task = asyncio.current_task()
            assert task
            tasks.add(task)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode().split("\r\n")
                method, target, _ = lines[0].split(" ")
                headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
                length = int(headers.get("Content-Length", "0"))
                body = await reader.readexactly(length) if length else b""
                url = urlsplit(target)
                seen.append(url.path)
                env = {
                    **os.environ,
                    "GIT_PROJECT_ROOT": str(tmp_path),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": url.path,
                    "QUERY_STRING": url.query,
                    "REQUEST_METHOD": method,
                    "CONTENT_TYPE": headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "REMOTE_USER": "fixture",
                }
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "http-backend",
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await process.communicate(body)
                assert process.returncode == 0, error.decode()
                response_headers, response = output.split(b"\r\n\r\n", 1)
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    + response_headers
                    + b"\r\nContent-Length: "
                    + str(len(response)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + response
                )
                await writer.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                tasks.discard(task)

        listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
        port = listener.sockets[0].getsockname()[1]
        original = asyncio.open_connection

        async def resolve(host: str) -> str:
            """测试专用 DNS 映射，不改变生产公开地址分类器。"""
            assert host == "git.example"
            return "93.184.216.34"

        async def connect(
            host: str, requested: int, **kwargs: Any
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            """确保控制器仍先固定公开数字地址与 443，再映射 TLS 测试端口。"""
            assert host == "93.184.216.34" and requested == 443
            return await original("127.0.0.1", port, **kwargs)

        monkeypatch.setattr(egress, "resolve_public", resolve)
        monkeypatch.setattr(asyncio, "open_connection", connect)
        config = config_for(tmp_path).model_copy(
            update={
                "network": NetworkConfig(
                    enabled=True, native=NativeNetworkConfig(enabled=True, hosts=("git.example",))
                )
            }
        )
        command = (
            "git init -q && git config user.name Fixture && "
            "git config user.email f@example.invalid && "
            "git config http.sslCAInfo /workspace/ca.pem && "
            "git fetch -q https://git.example/fixture.git HEAD && "
            "git checkout -qb task FETCH_HEAD && "
            "printf 'native push verified\\n' > native.txt && git add native.txt && "
            "git commit -qm native && "
            "git push -q https://git.example/fixture.git HEAD:refs/heads/native"
        )
        try:
            result = await CodePilotApplication(
                config, root, approval=ScriptedApproval([True])
            ).run(
                "受控服务原生 fetch/push",
                direct_tool=ToolCall(
                    call_id="git-roundtrip",
                    name="shell",
                    arguments={"command": command, "network": True, "timeout_seconds": 45.0},
                ),
            )
            tool = next(o.result for o in result.report.observations if o.result)
            assert not tool.is_error, tool.content
            assert not result.needs_reconciliation and result.resources.backend_closed
            assert _git(bare, "rev-parse", "HEAD") == before
            assert _git(bare, "rev-parse", "refs/heads/native") == _git(root, "rev-parse", "HEAD")
            assert _git(bare, "show", "refs/heads/native:native.txt") == "native push verified\n"
            assert any("git-receive-pack" in p for p in seen)
        finally:
            listener.close()
            await listener.wait_closed()
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_controller_kill_requires_exact_resource_prune(tmp_path: Path) -> None:
    """真实 SIGKILL 控制器后从正式维护入口清理租约，不能靠会话确认掩盖活容器。"""
    import signal
    import sys
    import time

    from codepilot.app.operations import prune
    from codepilot.sessions.store import SessionStore

    root = tmp_path / "workspace"
    root.mkdir()
    config = config_for(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    program = (
        "import asyncio,sys;from pathlib import Path;"
        "from codepilot.config import ProductConfig;"
        "from codepilot.app import CodePilotApplication;"
        "from codepilot.contracts import ToolCall;"
        "from codepilot.permissions import ScriptedApproval;"
        "c=ProductConfig.model_validate(__import__('json').loads(Path(sys.argv[1]).read_text()));"
        "asyncio.run(CodePilotApplication(c,Path(sys.argv[2]),approval=ScriptedApproval([True])).run("
        "'lease crash',session_id='native-crash',direct_tool=ToolCall(call_id='sleep',name='shell',"
        "arguments={'command':'sleep 50','network':True,'timeout_seconds':60.0})))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(config_path), str(root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    manifest: Path | None = None
    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            assert process.poll() is None, "controller exited before crash barrier"
            candidates = list((tmp_path / "state").rglob("lease-*.json"))
            if candidates and json.loads(candidates[0].read_bytes())["phase"] == "active":
                manifest = candidates[0]
                break
            time.sleep(0.1)
        assert manifest is not None
        record = json.loads(manifest.read_bytes())
        process.kill()
        process.wait(timeout=5)
        preview = asyncio.run(prune(config, "native-crash"))
        native = next(r for r in preview["resources"] if r["kind"] == "native_lease")
        assert native["eligible"] and not native["removed"]
        applied = asyncio.run(prune(config, "native-crash", apply=True))
        native = next(r for r in applied["resources"] if r["kind"] == "native_lease")
        assert native["removed"] and not Path(record["socket_directory"]).exists()
        assert json.loads(manifest.read_bytes())["phase"] == "closed"
        with SessionStore(tmp_path / "state", "native-crash", create=False) as session:
            assert session.get("status") == "running"
            with pytest.raises(ValueError, match="needs_reconciliation"):
                session.check(root, session.get("workspace")[3])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        # 回收测试控制器留下的宿主子进程组，生产维护同样只处理精确归属资源。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("failure", ["bridge", "cleanup"])
def test_child_lifecycle_failures_keep_real_resource_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """真实容器桥启动和删除故障分别验证独立清理、保留清单与正式恢复。"""
    from codepilot.app.operations import prune
    from codepilot.sessions.store import SessionStore
    from codepilot.workspace.docker import DockerExecutionBackend

    root = tmp_path / "workspace"
    root.mkdir()
    config = config_for(tmp_path)
    original_bridge = DockerExecutionBackend.start_native_bridge
    original_close = DockerExecutionBackend.close
    failed = False

    async def bridge(backend: DockerExecutionBackend, source: str) -> None:
        """在真实 child 创建后注入桥故障，主容器不受测试替身影响。"""
        if failure == "bridge" and backend.proxy_socket is not None:
            raise RuntimeError("synthetic bridge failure")
        await original_bridge(backend, source)

    async def close(backend: DockerExecutionBackend) -> None:
        """只拒绝首次 child 删除，保留真实容器供后续精确 prune。"""
        nonlocal failed
        if failure == "cleanup" and backend.proxy_socket is not None and not failed:
            failed = True
            raise RuntimeError("synthetic child cleanup failure")
        await original_close(backend)

    monkeypatch.setattr(DockerExecutionBackend, "start_native_bridge", bridge)
    monkeypatch.setattr(DockerExecutionBackend, "close", close)
    result = asyncio.run(
        CodePilotApplication(config, root, approval=ScriptedApproval([True])).run(
            "故障清理",
            session_id="native-fault",
            direct_tool=ToolCall(
                call_id="fault", name="shell", arguments={"command": "true", "network": True}
            ),
        )
    )
    assert result.resources.backend_closed
    tool = next(o.result for o in result.report.observations if o.result)
    assert tool.is_error
    manifests = list((tmp_path / "state").rglob("lease-*.json"))
    assert len(manifests) == 1
    record = json.loads(manifests[0].read_bytes())
    assert not Path(record["socket_directory"]).exists()
    if failure == "bridge":
        assert record["phase"] == "closed" and not result.needs_reconciliation
    else:
        assert record["phase"] == "cleanup_failed" and result.needs_reconciliation
        with SessionStore(tmp_path / "state", "native-fault", create=False) as session:
            assert (
                session.get("resources") and session.native_leases()[0]["phase"] == "cleanup_failed"
            )
        recovered = asyncio.run(prune(config, "native-fault", apply=True))
        item = next(r for r in recovered["resources"] if r["kind"] == "native_lease")
        assert item["removed"]
        assert json.loads(manifests[0].read_bytes())["phase"] == "closed"


@pytest.mark.parametrize("failure", ["quota", "timeout", "cancel"])
def test_native_remote_unknown_requires_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """实际容器发送后超限/超时/取消都关闭远端与容器，并保留网络未知阻断。"""
    from typing import Any

    import codepilot.network.egress as egress
    from codepilot.sessions.store import SessionStore

    root = tmp_path / "workspace"
    root.mkdir()

    async def run() -> None:
        """控制服务端收到实际数据的屏障，故障后独立检查持久事实与资源。"""
        received = asyncio.Event()
        closed = asyncio.Event()
        handlers: set[asyncio.Task[None]] = set()

        async def server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            """接收真实外发后制造超限正文或等待调用者撤销；不模拟工具结果。"""
            task = asyncio.current_task()
            assert task
            handlers.add(task)
            try:
                assert await reader.readexactly(4) == b"ping"
                received.set()
                if failure == "quota":
                    writer.write(b"x" * 2048)
                    await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()
                handlers.discard(task)

        listener = await asyncio.start_server(server, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        original = asyncio.open_connection

        async def resolve(host: str) -> str:
            """只在测试将固定已授权域名映射到独立服务。"""
            assert host == "failure.example"
            return "93.184.216.34"

        async def connect(
            host: str, requested: int, **kwargs: Any
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            """核对固定公开 IP 和 443 之后转向本地故障服务。"""
            assert host == "93.184.216.34" and requested == 443
            return await original("127.0.0.1", port, **kwargs)

        monkeypatch.setattr(egress, "resolve_public", resolve)
        monkeypatch.setattr(asyncio, "open_connection", connect)
        config = config_for(tmp_path).model_copy(
            update={
                "network": NetworkConfig(
                    enabled=True,
                    native=NativeNetworkConfig(
                        enabled=True, hosts=("failure.example",), total_bytes=1024, idle_seconds=5
                    ),
                )
            }
        )
        script = (
            "import socket; s=socket.create_connection(('127.0.0.1',3129));"
            "s.sendall(b'CONNECT failure.example:443 HTTP/1.1\\r\\n"
            "Host: failure.example\\r\\n\\r\\n');"
            "data=b''\n"
            "while not data.endswith(b'\\r\\n\\r\\n'):data+=s.recv(1)\n"
            "assert b'200' in data;s.sendall(b'ping');"
            "assert len(s.recv(4096))==2048"
        )
        # 字符串生成代码需真正保留 Python 字节转义，由原 Shell 按 argv 传给 worker。
        script = script.replace("\\\\r", "\\r").replace("\\\\n", "\\n")
        task = asyncio.create_task(
            CodePilotApplication(config, root, approval=ScriptedApproval([True])).run(
                "真实外发故障",
                session_id="native-unknown",
                direct_tool=ToolCall(
                    call_id="unknown",
                    name="shell",
                    arguments={
                        "command": "python -c " + shlex.quote(script),
                        "network": True,
                        "timeout_seconds": 2.0 if failure == "timeout" else 15.0,
                    },
                ),
            )
        )
        try:
            async with asyncio.timeout(25):
                await received.wait()
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                tool = next(o.result for o in result.report.observations if o.result)
                assert (
                    tool.is_error
                    and result.needs_reconciliation
                    and result.resources.backend_closed
                )
            await asyncio.wait_for(closed.wait(), 5)
            manifests = list((tmp_path / "state").rglob("lease-*.json"))
            record = json.loads(manifests[0].read_bytes())
            assert record["phase"] == "closed" and not Path(record["socket_directory"]).exists()
            with SessionStore(tmp_path / "state", "native-unknown", create=False) as session:
                assert (
                    session.get("network_pending")
                    and session.get("status") == "needs_reconciliation"
                )
                with pytest.raises(ValueError, match="accept-network-unknown"):
                    session.reconcile(root)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            listener.close()
            await listener.wait_closed()
            for handler in tuple(handlers):
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    asyncio.run(run())
