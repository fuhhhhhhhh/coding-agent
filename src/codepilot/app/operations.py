"""无模型的诊断、审查和精确资源维护，测试复用正式产品执行链。"""

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import signal
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.app.service import CodePilotApplication, _permission_policy, _state_root
from codepilot.config import ProductConfig, ProviderConfig
from codepilot.contracts import ToolCall
from codepilot.permissions import ApprovalProvider
from codepilot.sessions.migrations import DATABASE_VERSION
from codepilot.sessions.store import SessionStore, process_identity
from codepilot.tools.process import collect, settle, spawn, stop_process


async def command(argv: tuple[str, ...], root: Path, timeout: float = 15) -> dict[str, Any]:
    """执行控制器固定的只读诊断命令，限制输出与时间且回收后代。"""
    if argv[0] == "git":
        # 候选能修改仓库配置，控制器查询必须禁用可执行的 fsmonitor/Hook。
        argv = (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            *argv[1:],
        )
    process = await spawn(argv, cwd=str(root))
    try:
        async with asyncio.timeout(timeout):
            stdout, cut, stderr, err_cut, _ = await collect(process, 65536, "maintenance")
        return {
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="replace"),
            "truncated": cut or err_cut,
        }
    finally:
        # stderr 可能含秘密或服务器配置，维护接口不回显原始错误正文。
        await settle(stop_process(process))


async def doctor(config: ProductConfig, root: Path) -> dict[str, Any]:
    """只检查本机实际依赖与后端，不连接模型、不创建容器或修服务器配置。"""
    checks: dict[str, Any] = {}
    for name in ("git", "rg", "docker"):
        checks[name] = {"available": shutil.which(name) is not None}
    checks["workspace"] = {
        "readable": os.access(root, os.R_OK),
        "writable": os.access(root, os.W_OK),
    }
    state = _state_root(config)
    ancestor = state
    while not ancestor.exists():
        ancestor = ancestor.parent
    checks["storage"] = {
        "path": str(state),
        "writable_parent": os.access(ancestor, os.W_OK),
        "free_bytes": shutil.disk_usage(ancestor).free,
    }
    if config.backend.kind == "docker" and checks["docker"]["available"]:
        try:
            info = await command(("docker", "info", "--format", "{{json .}}"), root)
            data = json.loads(info["stdout"]) if info["exit_code"] == 0 else {}
            checks["daemon"] = {
                "available": bool(data),
                "version": data.get("ServerVersion"),
                "storage_driver": data.get("Driver"),
                "rootless": any("rootless" in x for x in data.get("SecurityOptions", [])),
            }
            image = await command(
                (
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    config.backend.docker_image or "",
                ),
                root,
            )
            checks["image"] = {"available": image["exit_code"] == 0, "id": image["stdout"].strip()}
        except (OSError, ValueError, TimeoutError):
            # 不可达 daemon 是诊断结果，不能尝试启动或重启共享服务。
            checks["daemon"] = {"available": False}
    required = (
        checks["git"]["available"]
        and checks["rg"]["available"]
        and checks["workspace"]["readable"]
        and checks["storage"]["writable_parent"]
    )
    if config.backend.kind == "docker":
        required = (
            required
            and checks.get("daemon", {}).get("available", False)
            and checks.get("image", {}).get("available", False)
        )
    return {
        "schema_version": 1,
        "ready": bool(required),
        "python": platform.python_version(),
        "platform": sys.platform,
        "package_version": version("codepilot"),
        "database_version": DATABASE_VERSION,
        "backend": config.backend.kind,
        "model": config.provider.model,
        "checks": checks,
        "limits": [
            "Docker network=none",
            "bind mount has no hard disk quota",
            "worktree+Docker Git unsupported",
            "automatic safe undo unavailable",
        ],
    }


async def diff(root: Path) -> dict[str, Any]:
    """原生 Git 审查含暂存/未暂存及未跟踪索引；非 Git 明确返回边界。"""
    inside = await command(
        ("git", "--no-optional-locks", "rev-parse", "--is-inside-work-tree"), root
    )
    if inside["exit_code"] != 0:
        return {
            "schema_version": 1,
            "git": False,
            "detail": "非 Git 目录；可查看 sessions facts 的版本记录",
        }
    status = await command(("git", "--no-optional-locks", "status", "--porcelain=v1", "-z"), root)
    unstaged = await command(
        ("git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--binary"), root
    )
    staged = await command(
        ("git", "--no-pager", "diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary"),
        root,
    )
    return {
        "schema_version": 1,
        "git": True,
        "status": status["stdout"],
        "unstaged": unstaged["stdout"],
        "staged": staged["stdout"],
        "truncated": any(x["truncated"] for x in (status, unstaged, staged)),
        "scope": "whole current workspace; not attribution to one agent run",
    }


def permissions(config: ProductConfig) -> dict[str, Any]:
    """展示实际权限预设和扩展开关，不读取凭据或执行工具。"""
    return {
        "schema_version": 1,
        "policy": _permission_policy(config.permission).model_dump(mode="json"),
        "extensions_enabled": config.extensions.enabled,
        "backend": config.backend.kind,
        "local_is_trusted_host_execution": config.backend.kind == "local",
    }


async def test_command(
    config: ProductConfig,
    root: Path,
    argv: list[str],
    timeout: float,
    approval: ApprovalProvider | None = None,
) -> dict[str, Any]:
    """用户 argv 转为单个审批绑定命令，零模型调用且保留正式会话/输出事实。"""
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("test requires command argv after --")
    if not 0 < timeout <= 900:
        raise ValueError("test timeout must be between 0 and 900 seconds")
    config = config.model_copy(
        update={
            "provider": ProviderConfig(),
            "runtime": config.runtime.model_copy(update={"timeout_seconds": timeout + 30}),
            "git_workspace": config.git_workspace.model_copy(update={"enabled": False}),
        }
    )
    result = await CodePilotApplication(config, root, approval=approval).run(
        "用户请求运行测试命令；成功只表示命令退出 0。",
        direct_tool=ToolCall(
            call_id=f"command-{uuid4().hex}",
            name="shell",
            arguments={"command": shlex.join(argv), "timeout_seconds": timeout},
        ),
    )
    tool = next(item.result for item in result.report.observations if item.result)
    return {
        "schema_version": 1,
        "session_id": result.identity.session_id,
        "source": result.execution_source,
        "model_requests": 0,
        "command_succeeded": not tool.is_error,
        "independent_verdict": "not_run",
        "result": tool.model_dump(mode="json"),
        "backend_closed": result.resources.backend_closed,
        "trace_path": str(result.resources.trace_path),
    }


async def prune(config: ProductConfig, session_id: str, *, apply: bool = False) -> dict[str, Any]:
    """持会话锁预览或回收精确资源，保留代码、工作树、历史与协调要求。"""
    root = _state_root(config)
    results: list[dict[str, Any]] = []
    with SessionStore(root, session_id, create=False) as stored:
        for record in stored.get("resources", []):
            entry: dict[str, Any] = {
                "kind": record.get("kind"),
                "eligible": False,
                "removed": False,
            }
            results.append(entry)
            if record.get("uid") != os.getuid():
                entry["reason"] = "owner_mismatch"
                continue
            if record["kind"] == "process":
                pid = int(record["pid"])
                current = process_identity(pid)
                entry["pid"] = pid
                if not current:
                    entry["reason"] = "already_gone"
                    continue
                if (
                    not record.get("start")
                    or current != record["start"]
                    or os.getpgid(pid) != pid
                    or pid == os.getpid()
                ):
                    entry["reason"] = "identity_mismatch"
                    continue
                entry["eligible"] = True
                if apply:
                    # 只处理记录中的独立进程组，起点/组身份在发送信号前重新核验。
                    if process_identity(pid) != current or os.getpgid(pid) != pid:
                        raise ValueError("process identity changed")
                    os.killpg(pid, signal.SIGTERM)
                    await asyncio.sleep(0.1)
                    if process_identity(pid) == current:
                        os.killpg(pid, signal.SIGKILL)
                    for _ in range(20):
                        if process_identity(pid) != current:
                            break
                        await asyncio.sleep(0.01)
                    entry["removed"] = process_identity(pid) != current
            elif record["kind"] == "container":
                identity = record["id"]
                entry["id"] = identity
                if re.fullmatch(r"[a-f0-9]{12,64}", identity) is None:
                    entry["reason"] = "invalid_container_id"
                    continue
                check = await command(("docker", "inspect", identity), Path.cwd())
                if check["exit_code"] != 0:
                    entry["reason"] = "unavailable_or_gone"
                    continue
                data = json.loads(check["stdout"])[0]
                labels = data.get("Config", {}).get("Labels", {}) or {}
                entry["eligible"] = (
                    labels.get("codepilot.managed") == "true"
                    and labels.get("codepilot.owner") == f"codepilot-uid-{os.getuid()}"
                    and data["Id"].startswith(identity)
                )
                if apply and entry["eligible"]:
                    removed = await command(("docker", "rm", "--force", data["Id"]), Path.cwd())
                    entry["removed"] = removed["exit_code"] == 0
        if apply:
            # 不把物理清理等同于接受工作区；未知调用仍需显式 reconcile。
            stored.commit("resources_pruned", {"results": results})
    return {
        "schema_version": 1,
        "preview": not apply,
        "resources": results,
        "workspace_files_deleted": False,
    }
