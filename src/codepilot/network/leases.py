"""核验并回收原生命令私有代理租约，不把清理等同于接受未知外部副作用。"""

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from codepilot.sessions.store import process_identity
from codepilot.tools.file_common import _atomic_write
from codepilot.workspace.docker import DockerRunner


def lease_path(directory: Path, reference: str) -> Path:
    """将会话中的相对资源引用限制在其私有目录，拒绝绝对或穿越路径。"""
    relative = Path(reference)
    path = directory / relative
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.resolve().is_relative_to(directory.resolve())
    ):
        raise ValueError("unsafe native lease reference")
    return path


def save_lease(path: Path, record: dict[str, Any]) -> None:
    """在控制器私有目录原子保存单个租约，正文不包含命令或业务凭据。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write(path, json.dumps(record, ensure_ascii=False).encode())


def load_lease(path: Path) -> dict[str, Any]:
    """拒绝链接与异主清单，不能从工作区脚本接受资源归属信息。"""
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_size > 65536:
        raise ValueError("unsafe native lease manifest")
    # 私有清单也可能因损坏而变成数组或标量，不能把解析成功当作合法对象。
    record: dict[str, Any] = json.loads(path.read_bytes())
    if not isinstance(record, dict):
        raise ValueError("native lease requires an object")
    if record.get("version") != 1 or record.get("uid") != os.getuid():
        raise ValueError("native lease owner/version mismatch")
    if re.fullmatch(r"codepilot-[a-f0-9]{32}", record.get("name", "")) is None:
        raise ValueError("native lease container name invalid")
    return record


def cleanup_socket(record: dict[str, Any]) -> None:
    """只删除确切身份的 socket 与空私有目录，禁止递归删除任意路径。"""
    directory = Path(record["socket_directory"])
    path = directory / "proxy.sock"
    # 已不存在可以幂等结束；仍存在时必须核对 inode，不能凭路径名删除。
    if not directory.exists():
        return
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or [info.st_dev, info.st_ino] != record["directory_identity"]
        or not directory.name.startswith(f"codepilot-egress-{os.getuid()}-")
    ):
        raise ValueError("native socket directory identity mismatch")
    if path.exists() or path.is_symlink():
        socket_info = path.lstat()
        if (
            not stat.S_ISSOCK(socket_info.st_mode)
            or socket_info.st_uid != os.getuid()
            or [socket_info.st_dev, socket_info.st_ino] != record["socket_identity"]
        ):
            raise ValueError("native socket identity mismatch")
        path.unlink()
    # 额外文件代表目录内容已漂移，宁可留存也不能泛化为递归清理。
    directory.rmdir()


async def recover_lease(path: Path, runner: DockerRunner, *, apply: bool) -> dict[str, Any]:
    """独立核验已结束控制器的精确容器和 socket，默认预览且不重放旧命令。"""
    record = load_lease(path)
    result: dict[str, Any] = {"eligible": False, "removed": False, "phase": record["phase"]}
    current = process_identity(record["controller_pid"])
    if (
        current
        and current == record["controller_start"]
        and record["phase"] not in {"closed", "cleanup_failed"}
    ):
        result["reason"] = "native_controller_still_active"
        return result
    code, stdout, _ = await runner(("inspect", record["name"]), None, 65536)
    identity = ""
    # 随机名只帮助定位，真正删除资格还需独立核验镜像、标签与隔离属性。
    if code == 0:
        data = json.loads(stdout)[0]
        labels = data.get("Config", {}).get("Labels", {}) or {}
        if (
            labels.get("codepilot.managed") != "true"
            or labels.get("codepilot.owner") != f"codepilot-uid-{os.getuid()}"
            or labels.get("codepilot.backend_id") != record["backend_id"]
            or data.get("Image") != record["image_id"]
            or data.get("HostConfig", {}).get("NetworkMode") != "none"
            or not data.get("HostConfig", {}).get("ReadonlyRootfs")
            or data.get("HostConfig", {}).get("Privileged", False)
            or "ALL" not in data.get("HostConfig", {}).get("CapDrop", [])
            or not any(
                m.get("Source") == str(Path(record["socket_directory"]) / "proxy.sock")
                and m.get("Destination") == "/run/codepilot-egress.sock"
                and m.get("RW") is False
                for m in data.get("Mounts", [])
            )
            or data.get("Name") != "/" + record["name"]
        ):
            raise ValueError("native container identity mismatch")
        identity = data["Id"]
        if record.get("container_id") and identity != record["container_id"]:
            raise ValueError("native container ID mismatch")
    elif (record["phase"] == "creating" or record.get("creation_unknown")) and not record.get(
        "container_id"
    ):
        # daemon 可能仍在接受创建，单次未找到不能证明此容器永远不会出现。
        result["reason"] = "native_creation_outcome_unknown"
        return result
    elif record.get("container_id") and record["phase"] not in {"closed", "preparing"}:
        # 不能把 daemon 不可用误当容器已删除；再按确切 ID 检查可诊断的不存在结果。
        _, _, error = await runner(("inspect", record["container_id"]), None, 65536)
        if b"no such" not in error.lower():
            result["reason"] = "native_container_state_unknown"
            return result
    result["eligible"] = True
    # 默认只预览；明确 apply 也只删除当前核验到的确切 ID，不重放业务命令。
    if apply:
        if identity:
            removed, _, _ = await runner(("rm", "-f", "-v", identity), None, 65536)
            if removed != 0:
                raise ValueError("native container cleanup failed")
        cleanup_socket(record)
        record.update(phase="closed", container_id=identity or record.get("container_id", ""))
        save_lease(path, record)
        result.update(removed=True, phase="closed")
    return result
