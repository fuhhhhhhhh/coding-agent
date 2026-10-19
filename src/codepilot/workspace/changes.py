"""控制器拥有的文件修改意图、版本证据及保守撤销；不推断 Shell 归属。"""

import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, cast

from codepilot.contracts import ToolCall, ToolResult
from codepilot.permissions.executor import CatalogExecutor
from codepilot.tools.base import ToolFailure, ToolSpec
from codepilot.tools.file_common import MAX_FILE_BYTES, _atomic_write
from codepilot.tools.workspace import Workspace


def snapshot(root: Path, relative: str) -> dict[str, Any] | None:
    """有界读取普通单链接文件，保存原始字节与普通模式；拒绝链接绕过。"""
    path = Workspace(root).resolve(relative, must_exist=False)
    # 元数据不属于文件工具的可撤销工作成果，避免恢复时修改 Git 管理状态。
    if ".git" in Path(relative).parts:
        raise ValueError("Git metadata cannot be journaled")
    # 不存在是新建文件的有效前版本，不应误报成读取失败。
    if not path.exists():
        return None
    info = path.lstat()
    # 多链接和特殊权限存在额外归属，不能承诺按普通文件安全恢复。
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o7000:
        raise ValueError("journal requires an ordinary single-link file")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    # 快照必须有硬上限，不能为撤销读入任意大的工作区文件。
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("journal file exceeds 1MiB")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": stat.S_IMODE(info.st_mode),
        "data": base64.b64encode(data).decode(),
    }


def save(path: Path, record: dict[str, Any]) -> None:
    """私有、原子并同步保存日志，写失败保留旧版本。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(record, ensure_ascii=False, sort_keys=True).encode())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # 文件同步后还要同步 rename 所在目录；意图落盘失败时不能启动写工具。
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ChangeJournal:
    """在调度锁内包围明确文件工具，意图先落盘，确认后才提供撤销资格。"""

    def __init__(self, registry: CatalogExecutor, root: Path, directory: Path, run_id: str) -> None:
        """绑定统一执行链、真实目录和私有 run 日志；不创建第二套工具。"""
        self.registry = registry
        self.root = root
        self.path = directory / "changes.json"
        info = root.stat()
        self.record: dict[str, Any] = {
            "version": 1,
            "run_id": run_id,
            "root": str(root),
            "identity": [info.st_dev, info.st_ino],
            "state": "recording",
            "entries": [],
        }

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """转发原有声明，模型与容器无需增加日志工具。"""
        return self.registry.specs

    async def execute(self, call: ToolCall) -> ToolResult:
        """仅记录文件写调用，任何不能证实完成的调用保持未知且禁止撤销。"""
        # 其他工具仍走原链；工作区变化本身不能证明是本次文件工具所有。
        if call.name not in {"write_file", "edit_file"}:
            return await self.registry.execute(call)
        relative = call.arguments.get("path")
        # 缺少参数继续交给原 Schema 校验，不创建无路径的修改意图。
        if not isinstance(relative, str):
            return await self.registry.execute(call)
        try:
            before = snapshot(self.root, relative)
            # 预先计算唯一目标内容，不能把执行后用户恰好写入的内容认成任务成果。
            desired = call.arguments.get("content")
            if call.name == "edit_file" and before is not None:
                text = base64.b64decode(before["data"]).decode("utf-8")
                old = call.arguments.get("old_text")
                new = call.arguments.get("new_text")
                if isinstance(old, str) and old and isinstance(new, str) and text.count(old) == 1:
                    desired = text.replace(old, new, 1)
            predicted = (
                hashlib.sha256(desired.encode()).hexdigest() if isinstance(desired, str) else None
            )
            # 覆盖已有文件强制使用当前版本，模型显式给出的旧版本仍由工具拒绝。
            arguments = dict(call.arguments)
            if before is not None:
                arguments.setdefault("expected_sha256", before["sha256"])
            elif call.name == "write_file":
                # 新建版本同样必须受条件约束，Docker 传输等待不能变成覆盖窗口。
                arguments["expected_absent"] = True
            entry: dict[str, Any] = {
                "call_id": call.call_id,
                "path": relative,
                "before": before,
                "after": None,
                "state": "intent",
            }
            self.record["entries"].append(entry)
            # 写入前为后版本编码预留空间，单 run 日志保持在 16MiB 以内。
            encoded = json.dumps(self.record).encode()
            if len(encoded) + 2 * MAX_FILE_BYTES > 16 * 1024**2:
                self.record["entries"].pop()
                raise ValueError("change journal quota exceeded")
            save(self.path, self.record)
        except (OSError, ValueError, ToolFailure):
            # 日志不可用时停止本次修改，不能先写文件再承诺可恢复。
            return ToolResult(
                call_id=call.call_id, content="change_journal_unavailable", is_error=True
            )
        result = await self.registry.execute(call.model_copy(update={"arguments": arguments}))
        # 即使工具错误也核对版本；写入后报错不能误标成没有副作用。
        entry["after"] = snapshot(self.root, relative)
        entry["state"] = "failed" if result.is_error else "confirmed"
        # 工具成功不足以确认归属，还要与事先预计的字节及普通权限一致。
        if not result.is_error and (
            entry["after"] is None
            or entry["after"]["sha256"] != predicted
            or entry["after"]["mode"] != (before["mode"] if before else 0o600)
        ):
            entry["state"] = "unknown"
        save(self.path, self.record)
        return result

    def finish(self) -> None:
        """仅正常退出才能封闭日志；未知意图保留供人工协调。"""
        self.record["state"] = "sealed"
        # 没有明确文件修改的 run 不产生空日志，避免给审查造成虚假归属。
        if self.record["entries"]:
            save(self.path, self.record)


def load(path: Path, root: Path) -> dict[str, Any]:
    """核对日志版本、根目录身份、正文哈希与有界长度，拒绝损坏或替换。"""
    # 先检查大小和链接，再解析正文，避免无界读取或跟随替换后的文件。
    if path.is_symlink() or path.stat().st_size > 16 * 1024**2:
        raise ValueError("unsafe change journal")
    record = json.loads(path.read_bytes())
    # 损坏日志必须以可诊断错误结束，不能依靠缺省值推断可撤销。
    if not isinstance(record, dict) or not isinstance(record.get("entries"), list):
        raise ValueError("invalid change journal schema")
    info = root.stat()
    # 路径相同也可能目录被替换，设备与 inode 必须共同匹配。
    if (
        record.get("version") != 1
        or record.get("root") != str(root)
        or record.get("identity") != [info.st_dev, info.st_ino]
    ):
        raise ValueError("change journal workspace identity mismatch")
    for entry in record["entries"]:
        # 每条记录先核对结构，再使用其中的路径和正文。
        if not isinstance(entry, dict) or not {
            "path",
            "before",
            "after",
            "state",
            "call_id",
        }.issubset(entry):
            raise ValueError("invalid change journal entry")
        # JSON 类型错误不能被隐式转换成文件路径。
        if not isinstance(entry["path"], str) or not isinstance(entry["call_id"], str):
            raise ValueError("invalid change journal path")
        Workspace(root).resolve(entry["path"], must_exist=False)
        for side in ("before", "after"):
            image = entry[side]
            # None 表示文件不存在，其他值必须是完整且自洽的快照。
            if image is not None:
                if (
                    not isinstance(image, dict)
                    or set(image) != {"data", "sha256", "mode"}
                    or not isinstance(image["data"], str)
                    or not isinstance(image["sha256"], str)
                    or not isinstance(image["mode"], int)
                ):
                    raise ValueError("invalid change journal snapshot")
                data = base64.b64decode(image["data"], validate=True)
                if (
                    len(data) > MAX_FILE_BYTES
                    or hashlib.sha256(data).hexdigest() != image["sha256"]
                    or not 0 <= image["mode"] <= 0o777
                ):
                    raise ValueError("corrupt change journal snapshot")
    return cast(dict[str, Any], record)


def review(path: Path, root: Path, *, apply: bool = False) -> dict[str, Any]:
    """反向预检全部文件版本；显式应用逐条撤销，冲突不开始，故障保留进度。"""
    record = load(path, root)
    entries = record["entries"]
    # 未封闭、半批撤销或未知调用都缺少安全重放前置条件。
    if record["state"] not in {"sealed", "undone"} or any(
        e["state"] not in {"confirmed", "failed", "undone"} for e in entries
    ):
        raise ValueError("unknown change ownership; reconciliation required")
    simulated: dict[str, Any] = {}
    conflicts: list[str] = []
    for entry in reversed(entries):
        # 已撤销记录不参与新的回放推断，重复维护不会再次写入。
        if entry["state"] == "undone":
            continue
        relative = entry["path"]
        current = simulated.get(relative, snapshot(root, relative))
        # 失败且前后不同的工具不能认领其副作用；不猜测失败发生在哪一步。
        if current != entry["after"] or (
            entry["state"] == "failed" and entry["before"] != entry["after"]
        ):
            conflicts.append(relative)
        simulated[relative] = entry["before"]
    result = {
        "run_id": record["run_id"],
        "state": record["state"],
        "conflicts": sorted(set(conflicts)),
        "files": [
            {
                "path": e["path"],
                "call_id": e["call_id"],
                "state": e["state"],
                "before": e["before"]["sha256"] if e["before"] else None,
                "after": e["after"]["sha256"] if e["after"] else None,
            }
            for e in entries
        ],
        "applied": False,
        "scope": "file tools only; Shell/extension changes are not owned",
    }
    # 整批先预检；预览、冲突或重复调用都没有工作区副作用。
    if not apply or conflicts or record["state"] == "undone":
        return result
    record["state"] = "undoing"
    save(path, record)
    for entry in reversed(entries):
        # 整批预检后仍逐文件复核，外部编辑不能被静默覆盖；不承诺跨文件原子事务。
        if snapshot(root, entry["path"]) != entry["after"]:
            raise ValueError("workspace changed during undo; inspect partial progress")
        target = Workspace(root).resolve(entry["path"], must_exist=False)
        # 无变化记录只更新进度，避免无意义地替换用户文件。
        if entry["before"] != entry["after"]:
            # 任务新建文件通过删除恢复不存在状态，已有文件恢复原字节和模式。
            if entry["before"] is None:
                target.unlink()
            else:
                _atomic_write(target, base64.b64decode(entry["before"]["data"]))
                os.chmod(target, entry["before"]["mode"])
        entry["state"] = "undone"
        save(path, record)
    record["state"] = "undone"
    save(path, record)
    result.update(state="undone", applied=True)
    return result
