"""把显式联网 Shell 接入原权限/调度链，专有容器与代理租约共同控制生命周期。"""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from pydantic import field_validator, model_validator

from codepilot.contracts import ToolCall
from codepilot.network.broker import check_resolver
from codepilot.network.credentials import GitHubLease, file_identity
from codepilot.network.egress import ConnectProxy, EgressBudget
from codepilot.network.git_gateway import GitGateway, git_environment
from codepilot.network.leases import cleanup_socket, save_lease
from codepilot.network.models import NativeNetworkConfig
from codepilot.observability.trace import emit
from codepilot.sessions.outputs import CURRENT_CALL
from codepilot.sessions.store import SessionStore, process_identity
from codepilot.tools.base import Tool, ToolFailure, ToolOutput, ToolSpec
from codepilot.tools.process import settle
from codepilot.tools.shell import ShellArguments
from codepilot.workspace.docker import DockerExecutionBackend

# 可信控制器代码在模型运行前冻结，工作区随后改同名文件不能改变已批准的桥实现。
_BRIDGE_SOURCE = Path(__file__).with_name("bridge.py").read_text(encoding="utf-8")


def credential_identities(
    config: NativeNetworkConfig, workspace: Path, provider_file: Path | None = None
) -> dict[str, tuple[int, ...]]:
    """共享启动与 doctor 的文件身份检查，不读取私钥或探测实际发行方。"""
    identities: dict[str, tuple[int, ...]] = {}
    for ref in config.github_repositories:
        path = ref.private_key_file
        if path.resolve().is_relative_to(workspace.resolve()) or (
            provider_file is not None and path.resolve() == provider_file.resolve()
        ):
            raise ValueError("GitHub key must be outside workspace and distinct from provider")
        identities[ref.name] = file_identity(path)
    return identities


class NetworkShellArguments(ShellArguments):
    """复用 Shell 命令契约，只增加由控制器消费的显式联网能力请求。"""

    network: bool = False
    git_credentials: tuple[str, ...] = ()

    @field_validator("git_credentials", mode="before")
    @classmethod
    def parse_references(cls, value: object) -> object:
        """JSON 数组转换为不可变引用，禁止运行中扩展授权。"""
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def check_references(self) -> Self:
        """凭据必须明确申请联网，且每次至多四个不重复名字。"""
        if self.git_credentials and not self.network:
            raise ValueError("git_credentials requires network=true")
        if len(self.git_credentials) > 4 or len(set(self.git_credentials)) != len(
            self.git_credentials
        ):
            raise ValueError("git_credentials must be unique and bounded")
        return self


class NativeShellTool(Tool[NetworkShellArguments]):
    """保持唯一 shell 名称；普通调用走原后端，联网调用使用短生命周期同工作区容器。"""

    name = "shell"
    effect = "process"
    resources = ("workspace:process",)
    arguments_model = NetworkShellArguments
    default_timeout = 30.0

    def __init__(
        self,
        backend: DockerExecutionBackend,
        config: NativeNetworkConfig,
        session: SessionStore,
        run_id: str,
        directory: Path,
        *,
        provider_file: Path | None = None,
    ) -> None:
        """绑定原后端、共享额度及会话，不在构造阶段创建 socket 或容器。"""
        check_resolver(backend.root)
        self.backend, self.config, self.session = backend, config, session
        self.run_id, self.directory = run_id, directory
        self.identities = credential_identities(config, backend.root, provider_file)
        if config.github_repositories and session.directory.parent.resolve().is_relative_to(
            backend.root.resolve()
        ):
            raise ValueError("credential vault must be outside workspace")
        self.budget = EgressBudget(config, self.progress)
        self.description = (
            "在当前工作区执行原生 Shell/Git/包管理器。默认 network=false 仍断网；"
            "network=true 申请专有临时容器及精确 TCP:443 CONNECT 代理，必须 process 批准。"
            f"允许匿名域名：{','.join(config.hosts)}。不注入业务密钥，TLS 由原生客户端校验。"
            "git_credentials 显式选择下列仓库引用，通过控制器网关认证，token 不进入进程："
            + ",".join(r.name + "=" + r.scope for r in config.github_repositories)
        )

    @property
    def spec(self) -> ToolSpec:
        """策略与固定桥源摘要进入审批指纹，工具声明不向模型公开宿主 socket 路径。"""
        identities: list[tuple[str, object]] = []
        for ref in self.config.github_repositories:
            try:
                identities.append((ref.name, file_identity(ref.private_key_file)))
            except (OSError, ValueError):
                identities.append((ref.name, "unavailable"))
        digest = hashlib.sha256(
            (self.config.model_dump_json() + _BRIDGE_SOURCE + json.dumps(identities)).encode()
        ).hexdigest()
        return super().spec.model_copy(
            update={
                "provenance": "native-egress:" + digest,
                "network_scope": tuple("tcp://" + host + ":443" for host in self.config.hosts),
                "credential_scopes": {r.name: r.scope for r in self.config.github_repositories},
            }
        )

    def timeout(self, arguments: NetworkShellArguments) -> float:
        """给容器创建及清理保留有限控制面时间，实际命令仍受原 Shell 时限约束。"""
        command = (
            arguments.timeout_seconds
            if "timeout_seconds" in arguments.model_fields_set
            else self.backend.limits.command_timeout_seconds
        )
        return command + 130.0 if arguments.network else command

    def progress(self, state: str, connection: int, size: int) -> None:
        """共享会话锁内提交连接预留与未知状态，不保存 TLS 或命令正文。"""
        pending = self.session.get("network_pending", {})
        key = f"native:{self.run_id}:{connection}"
        if state == "native_opened":
            pending[key] = {"run_id": self.run_id, "state": "native_tunnel", "bytes": size}
        self.session.commit(
            "network_attempt",
            {
                "run_id": self.run_id,
                "state": state,
                "connections": self.budget.connections,
                "bytes": size,
            },
            network_pending=pending,
        )
        emit(
            "network_attempt",
            call_id=CURRENT_CALL.get(),
            outcome=state,
            call_index=connection,
            original_bytes=size,
        )

    async def run(self, arguments: NetworkShellArguments) -> ToolOutput:
        """控制参数不进入 worker；正常、失败和取消都先撤销代理再清理专有容器。"""
        # 默认调用严格复用原后端，启用策略本身不能给普通 Shell 授予联网。
        if not arguments.network:
            normal = await self.backend.execute(
                ToolCall(
                    call_id=CURRENT_CALL.get() or "shell",
                    name="shell",
                    arguments=arguments.model_dump(
                        exclude={"network", "git_credentials"}, exclude_unset=True
                    ),
                )
            )
            return ToolOutput(content=normal.content, is_error=normal.is_error)
        # 固定 argv worker 的预览协议最少 1KiB，不能悄悄扩大用户申请的更小上限。
        if arguments.max_output_bytes < 1024:
            raise ToolFailure("native output preview requires at least 1024 bytes")
        references = {r.name: r for r in self.config.github_repositories}
        if any(name not in references for name in arguments.git_credentials):
            raise ToolFailure("git_credential_reference_denied")
        leases = {
            name: GitHubLease(references[name], self.identities[name], self.session, self.run_id)
            for name in arguments.git_credentials
        }
        gateway = GitGateway(leases, self.budget) if leases else None
        self.budget.check()
        # 优先用户运行目录；缺失时只能选工作区外的私有临时目录。
        runtime = Path(f"/run/user/{os.getuid()}")
        if not runtime.is_dir() or runtime.is_relative_to(self.backend.root):
            runtime = Path("/tmp")
        if runtime.is_relative_to(self.backend.root):
            raise ToolFailure("native socket must be outside workspace")
        socket_directory = Path(
            tempfile.mkdtemp(prefix=f"codepilot-egress-{os.getuid()}-", dir=runtime)
        )
        proxy = ConnectProxy(
            self.budget, socket_directory / "proxy.sock", gateway.handle if gateway else None
        )
        manifest = self.directory / f"lease-{uuid4().hex}.json"
        info = socket_directory.stat()
        record: dict[str, Any] = {
            "version": 1,
            "uid": os.getuid(),
            "run_id": self.run_id,
            "controller_pid": os.getpid(),
            "controller_start": process_identity(os.getpid()),
            "socket_directory": str(socket_directory),
            "directory_identity": [info.st_dev, info.st_ino],
            "socket_identity": [],
            "phase": "preparing",
            "container_id": "",
        }
        child: DockerExecutionBackend | None = None
        success = False
        try:

            def creating() -> None:
                """在 daemon 接受创建前持久绑定随机名称，覆盖返回 ID 前崩溃窗口。"""
                record["phase"] = "creating"
                save_lease(manifest, record)

            def created(cid: str) -> None:
                """返回确切 ID 后立即提交，不等 worker 握手成功才登记资源。"""
                record.update(phase="created", container_id=cid)
                save_lease(manifest, record)

            child = DockerExecutionBackend(
                self.backend.root,
                self.backend.image_id or self.backend.image,
                limits=self.backend.limits.model_copy(
                    update={
                        "output_bytes": min(
                            self.backend.limits.output_bytes, arguments.max_output_bytes
                        )
                    }
                ),
                proxy_socket=proxy.path,
                audit_directory=manifest.parent / manifest.stem,
                on_creating=creating,
                on_container=created,
                lifetime_seconds=max(60, int(self.config.lease_seconds) + 60),
            )
            record.update(
                name=child.container_name,
                backend_id=child.backend_id,
                image_id=self.backend.image_id,
            )
            save_lease(manifest, record)
            self.session.resource(
                "native_lease", {"manifest": str(manifest.relative_to(self.session.directory))}
            )
            # 先登记私有清单再监听，避免 socket 已开放但资源清单尚不存在。
            await proxy.start()
            socket_info = proxy.path.stat()
            record["socket_identity"] = [socket_info.st_dev, socket_info.st_ino]
            save_lease(manifest, record)
            await child.start()
            await child.start_native_bridge(_BRIDGE_SOURCE)
            record.update(phase="active", image_id=child.image_id)
            save_lease(manifest, record)
            command_timeout = (
                arguments.timeout_seconds
                if "timeout_seconds" in arguments.model_fields_set
                else self.backend.limits.command_timeout_seconds
            )
            remaining = min(command_timeout, self.budget.deadline - time.monotonic())
            # 容器启动也消耗同一租期，耗尽后不得再发送原生命令。
            if remaining <= 0:
                raise ToolFailure("native_lease_expired")
            result = await child.run_argv(
                (
                    "/usr/bin/env",
                    "HTTP_PROXY=http://127.0.0.1:3129",
                    "HTTPS_PROXY=http://127.0.0.1:3129",
                    "ALL_PROXY=http://127.0.0.1:3129",
                    "http_proxy=http://127.0.0.1:3129",
                    "https_proxy=http://127.0.0.1:3129",
                    "all_proxy=http://127.0.0.1:3129",
                    "NO_PROXY=" + ("127.0.0.1" if leases else ""),
                    "no_proxy=" + ("127.0.0.1" if leases else ""),
                    "GIT_TERMINAL_PROMPT=0",
                    *git_environment(leases),
                    "/bin/sh",
                    "-c",
                    arguments.command,
                ),
                timeout_seconds=remaining,
            )
            try:
                async with asyncio.timeout(2):
                    await proxy.idle.wait()
            except TimeoutError:
                proxy.failed = True
            success = result.exit_code == 0 and not result.timed_out and not proxy.failed
            return ToolOutput(
                is_error=not success,
                content=json.dumps(
                    {
                        **result.model_dump(),
                        "native_egress": {
                            "connections": self.budget.connections,
                            "bytes": self.budget.bytes,
                            "policy_failed": proxy.failed,
                            "errors": sorted(proxy.errors),
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        except (OSError, ValueError, RuntimeError) as error:
            # Docker/桥故障属于工具可诊断失败，不把宿主异常正文或路径复制到模型。
            raise ToolFailure(f"native_execution_failed: {type(error).__name__}") from None
        finally:

            async def cleanup() -> None:
                """清理失败不能跳过其他资源，二次取消也要等共同清理出口结束。"""
                failed = False
                try:
                    await proxy.close()
                except BaseException:
                    failed = True
                if gateway is not None:
                    try:
                        await gateway.close()
                    except BaseException:
                        failed = True
                if child is not None:
                    if record["phase"] == "creating" and not record["container_id"]:
                        failed = True
                        record["creation_unknown"] = True
                    else:
                        try:
                            await child.close()
                        except BaseException:
                            failed = True
                try:
                    cleanup_socket(record)
                except (OSError, ValueError):
                    failed = True
                # 只有执行和全部清理都确定成功才结算远端未知，不能只看退出码。
                if success and not failed:
                    pending = self.session.get("network_pending", {})
                    for connection in proxy.opened:
                        pending.pop(f"native:{self.run_id}:{connection}", None)
                    self.session.commit(
                        "native_command_settled", {"run_id": self.run_id}, network_pending=pending
                    )
                elif failed or proxy.opened:
                    # 未知结论不能被后续调用或恢复清空，也不能因命令退出零就忽略清理失败。
                    self.budget.uncertain = True
                record.update(connections=self.budget.connections, bytes=self.budget.bytes)
                record["phase"] = "cleanup_failed" if failed else "closed"
                if child is not None:
                    save_lease(manifest, record)
                # 清单保留失败阶段，让会话阻断续跑并提供精确维护入口。
                if failed:
                    raise ToolFailure("native_cleanup_incomplete; inspect native lease")

            # 代理先撤销，整容器随后销毁；调用者取消不会打断这组共同清理。
            await settle(cleanup())
