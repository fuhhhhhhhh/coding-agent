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
    checks["execution_boundary"] = {
        "network": "none" if config.backend.kind == "docker" else "host-unrestricted",
        "hard_workspace_quota": False,
        "workspace_mode": config.backend.workspace_mode,
        "requested_workspace_bytes": config.backend.limits.workspace_bytes,
        "secret_injection": False,
        "worktree_git": "unsupported-with-bounded-copy"
        if config.backend.workspace_mode == "bounded-copy"
        else "independent-clone"
        if config.backend.kind == "docker"
        else "native-worktree",
    }
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
        and (
            config.backend.limits.workspace_bytes is None
            or config.backend.workspace_mode == "bounded-copy"
        )
    )
    if config.backend.kind == "docker":
        required = (
            required
            and checks.get("daemon", {}).get("available", False)
            and checks.get("image", {}).get("available", False)
            and (
                config.backend.workspace_mode != "bounded-copy"
                or checks.get("daemon", {}).get("rootless", False)
            )
        )
    checks["execution_boundary"]["hard_workspace_quota"] = bool(
        config.backend.workspace_mode == "bounded-copy"
        and checks.get("daemon", {}).get("rootless", False)
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
            "bounded-copy tmpfs is volatile, Rootless-only, and excludes source .git/.env files"
            if config.backend.workspace_mode == "bounded-copy"
            else "bind mount has no hard disk quota",
            "Docker isolated Git requires repository root and an independent clone",
            "bounded-copy delivers and undoes verified source versions, including Shell edits"
            if config.backend.workspace_mode == "bounded-copy"
            else "undo owns confirmed file-tool writes only; arbitrary Shell changes are excluded",
            "scoped networking and secret injection unavailable",
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
                # tmpfs 容器可能仍持有最新产物；资源回收不能伪称没有删除工作文件。
                if labels.get("codepilot.workspace_mode") == "bounded-copy":
                    entry["reason"] = "bounded_workspace_requires_export"
                    continue
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


def changes(
    config: ProductConfig,
    root: Path,
    session_id: str,
    run_id: str,
    *,
    apply: bool = False,
    deliver: bool = False,
) -> dict[str, Any]:
    """在现有会话排他锁内审查/撤销文件工具成果，拒绝未知运行与工作区身份漂移。"""
    from codepilot.sessions.store import safe_id, workspace_stamp
    from codepilot.workspace.changes import review

    safe_id(run_id)
    with SessionStore(_state_root(config), session_id, create=False) as session:
        identity = session.get("workspace")
        # 当前目录撤销不能把隔离副本的 stamp 写回源工作区会话。
        if not isinstance(identity, list) or identity[0] != str(root):
            raise ValueError("undo workspace does not match session")
        # 运行中或未知副作用必须先协调；维护命令不能绕过正常恢复边界。
        if session.get("status") != "ready":
            raise ValueError("session needs reconciliation before reviewing undo")
        from codepilot.workspace.bounded import BoundedWorkspace

        copies = list(session.directory.glob(f"*/{run_id}/bounded-workspace.json"))
        if len(copies) > 1:
            raise ValueError("ambiguous bounded run")
        if copies:
            owned = BoundedWorkspace.open(copies[0], root)
            preview = owned.review()
            if not apply or preview["conflicts"] or owned.record["state"] == "undone":
                return preview
            required = "retained" if deliver else "applied"
            if owned.record["state"] != required:
                raise ValueError(
                    "bounded workspace state requires reconciliation or another action"
                )
            session.commit(
                "bounded_transfer_started",
                {"run_id": run_id, "undo": not deliver},
                status="needs_reconciliation",
            )
            result = owned.transfer(undo=not deliver)
            session.commit(
                "bounded_transfer_settled",
                {"run_id": run_id, "state": result["state"]},
                status="ready",
                stamp=workspace_stamp(root),
            )
            return result
        if deliver:
            raise ValueError("delivery requires a bounded workspace run")
        paths = list(session.directory.glob(f"*/{run_id}/changes.json"))
        if len(paths) != 1:
            raise ValueError("run change journal is missing or ambiguous")
        if apply:
            # 先落盘恢复阻断，再修改文件；崩溃或半批失败不能让会话错误地显示 ready。
            session.commit("undo_started", {"run_id": run_id}, status="needs_reconciliation")
        result = review(paths[0], root, apply=apply)
        if apply:
            session.commit(
                "undo_settled",
                {"run_id": run_id, "applied": result["applied"], "conflicts": result["conflicts"]},
                status="ready",
                stamp=workspace_stamp(root) if result["applied"] else session.get("stamp"),
            )
        return result


async def recover(
    config: ProductConfig, root: Path, session_id: str, run_id: str, *, apply: bool = False
) -> dict[str, Any]:
    """会话锁内恢复精确 run 的限容产物，不调用模型、不协调未知工具、不交付源文件。"""
    from codepilot.sessions.store import safe_id
    from codepilot.workspace.bounded import BoundedWorkspace
    from codepilot.workspace.recovery import load_handoff, recover_workspace

    safe_id(run_id)
    with SessionStore(_state_root(config), session_id, create=False) as session:
        info = root.stat()
        identity = session.get("workspace", [])
        if identity[:3] != [str(root), info.st_dev, info.st_ino]:
            raise ValueError("recovery source workspace identity mismatch")
        paths = list(session.directory.glob(f"*/{run_id}/bounded-workspace.json"))
        if len(paths) != 1:
            raise ValueError("bounded recovery run unavailable or ambiguous")
        owned = BoundedWorkspace.open(paths[0], root)
        handoff = load_handoff(owned.directory / "backend/bounded-container.json", owned.workspace)
        old_id = handoff["container_id"]
        previous = session.get("status")
        # 恢复开始先标协调；中途退出不能把部分资源处理当作正常可续问。
        if apply and not (handoff["phase"] == "closed" and owned.record["state"] != "active"):
            session.commit("recovery_started", {"run_id": run_id}, status="needs_reconciliation")
        result = await recover_workspace(owned, apply=apply)
        if result["recovered"]:
            resources = [
                item
                for item in session.get("resources", [])
                if not (
                    item.get("kind") == "container"
                    and item.get("uid") == os.getuid()
                    and item.get("id") in {old_id, result["container_id"]}
                )
            ]
            session.commit(
                "bounded_recovered",
                {"run_id": run_id, "data_loss_possible": result["data_loss_possible"]},
                resources=resources,
                status=previous
                if previous == "ready" and result.get("already_closed")
                else "needs_reconciliation",
            )
        result["session_status"] = session.get("status")
        prefix = (
            f"codepilot --workspace {shlex.quote(str(root))} "
            f"--state-directory {shlex.quote(str(_state_root(config)))}"
        )
        result["next_steps"] = (
            [
                f"{prefix} sessions reconcile {session_id} --accept-workspace",
                f"{prefix} delivery {session_id} {run_id}",
            ]
            if session.get("status") != "ready"
            else []
        )
        return result
