"""原生 CONNECT 的真实流边界、累计额度、DNS 子进程与租约归属测试。"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import codepilot.network.broker as broker
import codepilot.network.egress as egress
from codepilot.config import BackendConfig, ProductConfig
from codepilot.contracts import ToolCall
from codepilot.network.egress import ConnectProxy, EgressBudget
from codepilot.network.leases import cleanup_socket, recover_lease, save_lease
from codepilot.network.models import NativeNetworkConfig, NetworkConfig
from codepilot.permissions.actions import is_safe_process_read
from codepilot.tools.base import ToolFailure

HOST = "native.example"


def policy(**kwargs: Any) -> NativeNetworkConfig:
    """生成有限且明确的可信策略，测试不提供私网开关。"""
    return NativeNetworkConfig(enabled=True, hosts=(HOST,), **kwargs)


def test_native_configuration_and_safe_read_boundary() -> None:
    """原生范围独立且默认关闭，联网请求不能借只读命令跳过审批。"""
    assert not NetworkConfig().native.enabled
    for hosts in [(), ("*.example",), ("127.0.0.1",), (HOST, HOST)]:
        with pytest.raises(ValueError):
            NativeNetworkConfig(enabled=True, hosts=hosts)
    config = NetworkConfig(enabled=True, native=policy())
    with pytest.raises(ValueError, match="requires Docker"):
        ProductConfig(network=config)
    with pytest.raises(ValueError, match="bind"):
        ProductConfig(
            network=config,
            backend=BackendConfig(
                kind="docker", docker_image="fixture", workspace_mode="bounded-copy"
            ),
        )
    assert not is_safe_process_read(
        ToolCall(call_id="read", name="shell", arguments={"command": "pwd", "network": True})
    )


@pytest.mark.parametrize(
    "target,method,extra",
    [
        ("other.example:443", "CONNECT", ""),
        (HOST + ":80", "CONNECT", ""),
        ("127.0.0.1:443", "CONNECT", ""),
        (HOST.upper() + ":443", "CONNECT", ""),
        (HOST + ":443", "GET", ""),
        (HOST + ":443", "CONNECT", "Content-Length: 1\r\n"),
        (HOST + ":443", "CONNECT", "Transfer-Encoding: chunked\r\n"),
    ],
)
def test_proxy_denies_before_dns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, method: str, extra: str
) -> None:
    """真实 Unix 请求拒绝域名/端口/协议歧义，并证明没有调用解析器。"""

    async def forbidden(host: str) -> str:
        """拒绝路径不允许触达任何 DNS。"""
        raise AssertionError("unexpected DNS")

    async def run() -> None:
        """发送真实协议字节并独立核验连接账本。"""
        monkeypatch.setattr(egress, "resolve_public", forbidden)
        budget = EgressBudget(policy())
        proxy = ConnectProxy(budget, tmp_path / "proxy.sock")
        await proxy.start()
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.path)
            writer.write(f"{method} {target} HTTP/1.1\r\nHost: {target}\r\n{extra}\r\n".encode())
            await writer.drain()
            assert b"403" in await reader.read()
            writer.close()
            await writer.wait_closed()
            await proxy.idle.wait()
            assert proxy.failed and budget.connections == 0 and not proxy.opened
        finally:
            await proxy.close()
        assert not proxy.path.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["success", "bytes", "idle", "cancel", "parallel", "attempts", "download"]
)
def test_real_proxy_stream_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """实际流验证转发、双向限额、空闲、取消与并发/尝试配额，没有模拟读写结果。"""

    async def run() -> None:
        """只替换目的地址路由，其他代理实现和实际 socket 都参与测试。"""
        connected = asyncio.Event()
        remote_closed = asyncio.Event()
        remote_tasks: set[asyncio.Task[None]] = set()

        async def server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            """远端独立记录真正收到的字节与断连。"""
            task = asyncio.current_task()
            assert task
            remote_tasks.add(task)
            connected.set()
            try:
                if mode == "download":
                    # 总下载时长超过空闲阈值，持续单向进展不能被另一方向误判。
                    for _ in range(20):
                        writer.write(b"d")
                        await writer.drain()
                        await asyncio.sleep(0.06)
                while data := await reader.read(16384):
                    if mode != "bytes":
                        writer.write(data)
                        await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                remote_closed.set()
                remote_tasks.discard(task)

        listener = await asyncio.start_server(server, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        original = asyncio.open_connection

        async def resolve(host: str) -> str:
            """保留生产域名核验，只在测试把公开地址映射至本地服务。"""
            assert host == HOST
            return "93.184.216.34"

        async def connect(
            host: str, requested: int, **kwargs: Any
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            """断言代理使用已核验数字地址和固定端口，再连接测试服务。"""
            assert host == "93.184.216.34" and requested == 443
            return await original("127.0.0.1", port, **kwargs)

        monkeypatch.setattr(egress, "resolve_public", resolve)
        monkeypatch.setattr(asyncio, "open_connection", connect)
        budget = EgressBudget(
            policy(
                max_parallel=1,
                max_connections=1,
                total_bytes=1024,
                idle_seconds=0.5 if mode == "download" else 1 if mode == "idle" else 2,
            )
        )
        proxy = ConnectProxy(budget, tmp_path / "proxy.sock")
        await proxy.start()

        async def client() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            """通过真实 Unix socket 申请一个隧道。"""
            reader, writer = await asyncio.open_unix_connection(proxy.path)
            writer.write(f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
            await writer.drain()
            return reader, writer

        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await client()
            assert b"200" in await reader.readuntil(b"\r\n\r\n")
            await connected.wait()
            if mode == "parallel":
                other, output = await client()
                try:
                    assert b"200" not in await other.read()
                except ConnectionResetError:
                    pass
                output.close()
                try:
                    await output.wait_closed()
                except ConnectionResetError:
                    pass
            elif mode == "download":
                assert await reader.readexactly(20) == b"d" * 20
                assert not proxy.failed and budget.bytes == 20
            elif mode == "bytes":
                writer.write(b"x" * 2048)
                await writer.drain()
                assert await reader.read() == b""
                assert budget.bytes > 1024 and proxy.failed
            elif mode == "idle":
                assert await reader.read() == b""
                assert proxy.failed
            elif mode == "cancel":
                await proxy.close()
                assert await reader.read() == b""
            else:
                writer.write(b"actual-payload")
                await writer.drain()
                assert await reader.readexactly(14) == b"actual-payload"
                assert budget.bytes == 28
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(proxy.idle.wait(), 3)
            await asyncio.wait_for(remote_closed.wait(), 3)
            assert budget.active == 0
            if mode == "attempts":
                other, output = await client()
                assert b"403" in await other.read()
                output.close()
                await output.wait_closed()
                assert budget.connections == 1
        finally:
            if writer:
                writer.close()
            await proxy.close()
            listener.close()
            await listener.wait_closed()
            for task in tuple(remote_tasks):
                task.cancel()
            await asyncio.gather(*remote_tasks, return_exceptions=True)
        assert not proxy.tasks and not proxy.path.exists()

    asyncio.run(run())


def test_lease_and_uncertainty_are_shared() -> None:
    """后续命令不能重置累计配额、租期或未知结果。"""
    budget = EgressBudget(policy())
    assert budget.deadline == float("inf")
    budget.check()
    deadline = budget.deadline
    budget.check()
    assert budget.deadline == deadline
    budget.deadline = time.monotonic() - 1
    with pytest.raises(ToolFailure, match="expired"):
        budget.reserve()
    budget.deadline = time.monotonic() + 10
    budget.uncertain = True
    with pytest.raises(ToolFailure, match="reconciliation"):
        budget.reserve()


def test_dns_cancel_kills_real_resolver_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """系统解析替身在独立进程阻塞；取消必须真实杀进程且不能留下线程等待。"""
    pidfile = tmp_path / "pid"
    monkeypatch.setattr(
        broker,
        "_DNS_PROGRAM",
        f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)",
    )

    async def run() -> None:
        """等待独立 PID 出现后取消，操作系统独立确认其已消失。"""
        task = asyncio.create_task(broker.resolve_public(HOST))
        async with asyncio.timeout(3):
            while not pidfile.exists():
                await asyncio.sleep(0.01)
        pid = int(pidfile.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(run())


def test_socket_cleanup_rejects_replacement(tmp_path: Path) -> None:
    """记录的空目录被替换后不删除新内容，也不递归清理其他文件。"""
    directory = tmp_path / f"codepilot-egress-{os.getuid()}-fixture"
    directory.mkdir(mode=0o700)
    info = directory.stat()
    record = {
        "socket_directory": str(directory),
        "directory_identity": [info.st_dev, info.st_ino],
        "socket_identity": [],
    }
    (directory / "keep").write_text("user")
    with pytest.raises(OSError):
        cleanup_socket(record)
    assert (directory / "keep").read_text() == "user"
    record["directory_identity"] = [0, 0]
    with pytest.raises(ValueError, match="identity"):
        cleanup_socket(record)


def test_lease_unknown_creation_refuses_absence(tmp_path: Path) -> None:
    """创建结果不明不能凭一次 daemon 未找到就称清理完成。"""
    path = tmp_path / "lease.json"
    save_lease(
        path,
        {
            "version": 1,
            "uid": os.getuid(),
            "phase": "creating",
            "controller_pid": 999999999,
            "controller_start": "",
            "name": "codepilot-" + "a" * 32,
            "container_id": "",
        },
    )

    async def absent(
        argv: tuple[str, ...], stdin: bytes | None, limit: int
    ) -> tuple[int, bytes, bytes]:
        """模拟 daemon 的未找到响应，不能消除创建歧义。"""
        return 1, b"", b"No such container"

    result = asyncio.run(recover_lease(path, absent, apply=True))
    assert not result["eligible"] and not result["removed"]
    assert json.loads(path.read_bytes())["phase"] == "creating"


def test_unclosed_native_resource_prevents_ready_even_without_network(tmp_path: Path) -> None:
    """主后端关闭不能清空未关闭子容器；损坏清单同样需要协调并保留引用。"""
    from codepilot.sessions.store import SessionStore

    workspace = tmp_path / "source"
    workspace.mkdir()
    with SessionStore(tmp_path / "state", "native") as session:
        session.check(workspace, "docker")
        session.begin("run-native", (), workspace)
        session.resource("native_lease", {"manifest": "missing.json"})
        session.finish(None, workspace, clean=True)
        assert session.get("status") == "needs_reconciliation"
        assert session.get("resources")
        assert session.diagnose()["native_leases"][0]["phase"] == "unreadable"
        with pytest.raises(OSError):
            session.reconcile(workspace, accept_network_unknown=True)


@pytest.mark.parametrize(
    "payload",
    [
        b"CONNECT native.example:443 HTTP/1.1\r\nHost: native.example\r\n\r\nearly-data",
        b"CONNECT native.example:443 HTTP/1.1\r\nHost: native.example\r\nX: "
        + b"x" * 17000
        + b"\r\n\r\n",
    ],
)
def test_header_pipeline_and_size_fail_closed(tmp_path: Path, payload: bytes) -> None:
    """提前流水线或超大头不会被转发或静默丢弃后继续执行。"""

    async def run() -> None:
        """仅本地 Unix 协议输入，无外部 DNS 和联网。"""
        budget = EgressBudget(policy())
        proxy = ConnectProxy(budget, tmp_path / "proxy.sock")
        await proxy.start()
        try:
            reader, writer = await asyncio.open_unix_connection(proxy.path)
            writer.write(payload)
            await writer.drain()
            assert b"403" in await reader.read()
            writer.close()
            await writer.wait_closed()
            await proxy.idle.wait()
            assert budget.connections == 0 and proxy.failed
        finally:
            await proxy.close()

    asyncio.run(run())


def test_project_can_disable_inherited_native_network(tmp_path: Path) -> None:
    """不可信项目只能关闭联网，但关闭总开关必须真正压住继承的原生能力。"""
    from codepilot.config import load_product_config

    root = tmp_path / "source"
    root.mkdir()
    user = tmp_path / "user.yaml"
    user.write_text(
        "backend:\n  kind: docker\n  docker_image: fixture\n"
        "network:\n  enabled: true\n  native:\n    enabled: true\n    hosts: [native.example]\n"
    )
    (root / ".codepilot.yaml").write_text("network:\n  enabled: false\n")
    config = load_product_config(workspace=root, user_path=user)
    assert not config.network.enabled and not config.network.native.enabled
    (root / ".codepilot.yaml").write_text(
        "network:\n  enabled: true\n  native:\n    enabled: true\n    hosts: [other.example]\n"
    )
    with pytest.raises(ValueError, match="只能禁用 network"):
        load_product_config(workspace=root, user_path=user)


def test_dns_uses_frozen_real_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启动后替换解释器别名不会执行工作区提供的宿主程序。"""
    fake = tmp_path / "python"
    marker = tmp_path / "escaped"
    fake.write_text("#!/bin/sh\ntouch " + str(marker) + "\nprintf '[\"8.8.8.8\"]'\n")
    fake.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(fake))
    monkeypatch.setattr(broker, "_DNS_PROGRAM", "print('[\"8.8.8.8\"]')")
    assert asyncio.run(broker.resolve_public(HOST)) == "8.8.8.8"
    assert not marker.exists()


def test_network_preflight_rejects_interpreter_in_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原生和 HTTP 都在凭据/状态/容器创建前拒绝可被工作区替换的解释器。"""
    from codepilot.app import CodePilotApplication
    from codepilot.network.broker import HttpRequestTool
    from codepilot.network.models import Destination

    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(broker, "_DNS_EXECUTABLE", root / "bin" / "python")
    for network in [
        NetworkConfig(enabled=True, native=policy()),
        NetworkConfig(enabled=True, destinations=(Destination(host=HOST),)),
    ]:
        config = ProductConfig(
            state_directory=tmp_path / "state",
            network=network,
            backend=BackendConfig(kind="docker", docker_image="fixture"),
        )
        with pytest.raises(ValueError, match="outside workspace"):
            CodePilotApplication(config, root)
        with pytest.raises(ValueError, match="outside workspace"):
            HttpRequestTool(network, root)
    assert not (tmp_path / "state").exists()
