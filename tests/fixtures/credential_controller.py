"""只供 SIGKILL 测试的独立控制器，路由到本机合成发行方，绝不用于生产。"""

import asyncio
import ssl
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import codepilot.network.transport as transport
from codepilot.app import CodePilotApplication
from codepilot.config import BackendConfig, ProductConfig
from codepilot.contracts import ToolCall
from codepilot.network.credentials import GitHubLease, file_identity
from codepilot.network.models import GitHubRepository, NativeNetworkConfig, NetworkConfig
from codepilot.permissions import ScriptedApproval
from codepilot.sessions.store import SessionStore


async def main() -> None:
    """通过正式应用启动真实容器和凭据，屏障后由父测试直接杀控制器。"""
    root = Path(sys.argv[1])
    port = int(sys.argv[2])
    ref = GitHubRepository.model_validate_json((root / "reference.json").read_text())
    original = asyncio.open_connection

    async def resolve(host: str) -> str:
        """测试只映射固定两个 GitHub 名字，不改变生产公开地址验证器。"""
        assert host in {"api.github.com", "github.com"}
        return "93.184.216.34"

    async def connect(
        host: str, requested: int, **kwargs: Any
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """固定数字 IP 和端口通过后才改为本机 TLS 测试服务。"""
        assert host == "93.184.216.34" and requested == 443
        return await original("127.0.0.1", port, **kwargs)

    with (
        patch.object(transport, "resolve_public", resolve),
        patch.object(
            transport,
            "system_tls_context",
            lambda: ssl.create_default_context(cafile=str(root / "tls-cert.pem")),
        ),
        patch.object(asyncio, "open_connection", connect),
    ):
        if len(sys.argv) > 3 and sys.argv[3] == "lease-only":
            # CI 不依赖 Docker，也要真正杀控制器，证明恢复不依赖原进程内存。
            with SessionStore(root / "state", "killed") as session:
                session.commit(
                    "test_run", {}, status="running", workspace=[str(root / "workspace")]
                )
                lease = GitHubLease(ref, file_identity(ref.private_key_file), session, "run")
                await lease.acquire()
                (root / "workspace/ready").touch()
                await asyncio.Event().wait()
            return
        config = ProductConfig(
            state_directory=root / "state",
            permission="controlled-edit",
            backend=BackendConfig(kind="docker", docker_image="codepilot-sandbox:0.1.0"),
            network=NetworkConfig(
                enabled=True,
                native=NativeNetworkConfig(
                    enabled=True, github_repositories=(ref,), lease_seconds=180
                ),
            ),
        )
        await CodePilotApplication(
            config, root / "workspace", approval=ScriptedApproval([True])
        ).run(
            "SIGKILL 凭据恢复",
            session_id="killed",
            direct_tool=ToolCall(
                call_id="git",
                name="shell",
                arguments={
                    "command": (
                        "git ls-remote https://github.com/fixture/repo.git "
                        "&& touch ready && sleep 90"
                    ),
                    "network": True,
                    "git_credentials": ["fixture"],
                    "timeout_seconds": 120.0,
                },
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
