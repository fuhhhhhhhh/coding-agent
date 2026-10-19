"""从控制器私有绑定恢复限容产物，禁止模型重放与源目录自动交付。"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.workspace.archive import unpack
from codepilot.workspace.bounded import BoundedWorkspace
from codepilot.workspace.common import BackendJournal
from codepilot.workspace.docker import (
    CONTROL_TIMEOUT_SECONDS,
    DockerExecutionBackend,
    DockerRunner,
    _run_docker,
)
from codepilot.workspace.models import BackendLimits


def load_handoff(path: Path, root: Path) -> dict[str, Any]:
    """校验有限私有记录与原目录身份，旧任务不能借文件名猜测容器归属。"""
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("unsafe recovery binding")
    record = json.loads(path.read_bytes())
    info = root.stat()
    if (
        not isinstance(record, dict)
        or record.get("version") != 1
        or record.get("identity") != [info.st_dev, info.st_ino]
        or record.get("owner") != f"codepilot-uid-{os.getuid()}"
        or not re.fullmatch(r"docker-[a-f0-9]{32}", str(record.get("backend_id", "")))
        or not re.fullmatch(r"codepilot-[a-f0-9]{32}", str(record.get("name", "")))
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(record.get("image_id", "")))
        or not re.fullmatch(r"(?:[a-f0-9]{64})?", str(record.get("container_id", "")))
        or record.get("phase")
        not in ("creating", "created", "seeding", "active", "exported", "closed")
        or type(record.get("seeded")) is not bool
        or type(record.get("main_pid")) is not int
        or type(record.get("workspace_bytes")) is not int
        or type(record.get("memory_bytes")) is not int
        or (
            record.get("seeded")
            and not re.fullmatch(r"[a-f0-9]{64}", str(record.get("checkpoint_sha256", "")))
        )
    ):
        raise ValueError("invalid recovery binding or workspace identity")
    return record


def restore_checkpoint(owned: BoundedWorkspace, record: dict[str, Any]) -> None:
    """先核验完整归档及哈希，再刷新私有镜像，不写源目录。"""
    directory = owned.directory / "bounded-checkpoints"
    if directory.is_symlink():
        raise ValueError("unsafe checkpoint directory")
    path = directory / f"{record['checkpoint_sha256']}.tar"
    cap = owned.limit * 2 + 32 * 1024**2
    if path.is_symlink() or path.stat().st_size > cap:
        raise ValueError("unsafe recovery checkpoint")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != record.get("checkpoint_sha256"):
        raise ValueError("recovery checkpoint hash mismatch")
    with tempfile.TemporaryDirectory(prefix="restore-", dir=owned.directory) as temp:
        unpack(data, Path(temp), owned.limit)
        for item in owned.workspace.iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        shutil.copytree(temp, owned.workspace, dirs_exist_ok=True)


def verify_container(data: dict[str, Any], record: dict[str, Any]) -> None:
    """核对不可由模型工具更新的容器创建身份与实际约束，错配拒绝任何信号。"""
    config, host = data.get("Config", {}), data.get("HostConfig", {})
    labels = config.get("Labels", {}) or {}
    expected = {
        "codepilot.managed": "true",
        "codepilot.owner": record["owner"],
        "codepilot.workspace_mode": "bounded-copy",
        "codepilot.backend_id": record["backend_id"],
    }
    if (
        any(labels.get(k) != v for k, v in expected.items())
        or data.get("Name") != "/" + record["name"]
        or config.get("Image") != record["image_id"]
        or (record["container_id"] and data.get("Id") != record["container_id"])
        or not re.fullmatch(r"[a-f0-9]{64}", str(data.get("Id", "")))
        or host.get("NetworkMode") != "none"
        or host.get("PidMode", "") != ""
        or host.get("Privileged", False)
        or "ALL" not in (host.get("CapDrop") or [])
        or "no-new-privileges" not in (host.get("SecurityOpt") or [])
        or not host.get("Init")
        or config.get("User") != "0:0"
        or not host.get("ReadonlyRootfs")
        or host.get("Memory") != record["memory_bytes"]
        or host.get("Binds")
        or host.get("Mounts")
        or not any(
            str(option) == f"size={record['workspace_bytes']}"
            for option in str(host.get("Tmpfs", {}).get("/workspace", "")).split(",")
        )
    ):
        raise ValueError("container recovery identity or boundary mismatch")


async def recover_workspace(
    owned: BoundedWorkspace, *, apply: bool = False, runner: DockerRunner | None = None
) -> dict[str, Any]:
    """预览或恢复一个 run；活盘先导出，丢失活盘则只用可信旧检查点并报告退化。"""
    if owned.record["state"] in {"applying", "undoing"}:
        raise ValueError("source transfer outcome unknown; recovery cannot replay source writes")
    path = owned.directory / "backend" / "bounded-container.json"
    record = load_handoff(path, owned.workspace)
    if record["workspace_bytes"] != owned.limit:
        raise ValueError("recovery quota mismatch")
    raw_transport = runner or _run_docker

    async def transport(
        argv: tuple[str, ...], data: bytes | None, limit: int
    ) -> tuple[int, bytes, bytes]:
        """每个恢复操作有时限，超时保留未知绑定，不重发写请求。"""
        async with asyncio.timeout(CONTROL_TIMEOUT_SECONDS):
            return await raw_transport(argv, data, limit)

    code, output, _ = await transport(
        ("info", "--format", "{{json .SecurityOptions}}"), None, 65536
    )
    if code != 0 or not any("rootless" in str(x) for x in json.loads(output)):
        raise ValueError("recovery requires available Rootless Docker")
    identity = record["container_id"] or record["name"]
    code, output, error = await transport(("container", "inspect", identity), None, 65536)
    missing = code != 0 and b"No such" in error
    if code != 0 and not missing:
        raise ValueError("container state unavailable; recovery retained")
    container = None if missing else json.loads(output)[0]
    if container is not None:
        verify_container(container, record)
    running = bool(container and container.get("State", {}).get("Running"))
    result: dict[str, Any] = {
        "schema_version": 1,
        "run_id": owned.record["run_id"],
        "preview": not apply,
        "phase": record["phase"],
        "container_present": container is not None,
        "container_id": container["Id"] if container else record["container_id"],
        "container_running": running,
        "source_modified": False,
        "tools_replayed": 0,
        "data_loss_possible": bool(
            record["seeded"] and not running and record["phase"] not in {"exported", "closed"}
        ),
        "recovered": False,
        "automatic_reconcile": False,
    }
    # 预览只有状态读取；不暂停进程、不触碰正文、不减少未知预算。
    if not apply:
        return result
    # create 的客户端可能先退出而 daemon 稍后创建；一次查无不能证明从未接受请求。
    if missing and record["phase"] == "creating" and not record["container_id"]:
        raise ValueError(
            "container creation outcome unknown; retry inspection after daemon settles"
        )
    # 已完成的恢复/交付不能被重复恢复改回 retained，尤其不能重复交付已有修改。
    if not container and record["phase"] == "closed" and owned.record["state"] != "active":
        result.update(recovered=True, already_closed=True, artifact_state=owned.record["state"])
        return result
    backend = DockerExecutionBackend(
        owned.workspace,
        record["image_id"],
        workspace_mode="bounded-copy",
        limits=BackendLimits(workspace_bytes=owned.limit, memory_bytes=record["memory_bytes"]),
        audit_directory=path.parent,
        runner=transport,
    )
    backend.backend_id = record["backend_id"]
    backend.container_name = record["name"]
    backend._handoff = record
    backend._container_id = container["Id"] if container else ""
    backend._main_pid = record["main_pid"]
    backend._bounded_seeded = record["seeded"]
    backend._journal = BackendJournal(
        path.parent / ("recovery-" + uuid4().hex), owned.workspace, backend.backend_id
    )
    # 只有已发布的 seeded 阶段才可能接收工具；引导中断时本地初始副本仍是权威基线。
    if record["seeded"]:
        if running:
            await backend._checkpoint_bounded(final=True)
        else:
            restore_checkpoint(owned, record)
            backend._bounded_final = True
    else:
        backend._bounded_final = True
    # 成功导出后先保存可审查产物，保存失败不能提前删除唯一活盘。
    artifacts = owned.finish(apply=False)
    await backend.close()
    result.update(
        recovered=True,
        preview=False,
        phase="closed",
        artifact_state=artifacts["state"],
        conflicts=artifacts["conflicts"],
        files=artifacts["files"],
        checkpoint="latest_export" if running and record["seeded"] else "last_known",
    )
    return result
