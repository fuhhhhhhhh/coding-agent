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

    def __init__(
        self,
        registry: CatalogExecutor,
        root: Path,
        directory: Path,
        run_id: str,
        *,
        require_read: bool = False,
    ) -> None:
        """绑定统一执行链、真实目录和私有 run 日志；不创建第二套工具。"""
        self.require_read = require_read
        self.read_versions: dict[str, str] = {}
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
        """读取建立版本；单文件和批次写前记意图，部分成功按独立版本和清单认领。"""
        arguments: dict[str, Any]
        if call.name == "read_file" and self.require_read:
            arguments = {"mode": "page", **call.arguments, "include_sha256": True}
            result = await self.registry.execute(call.model_copy(update={"arguments": arguments}))
            # 只有真实读取成功的控制器输出能建立版本，不从模型自述提取哈希。
            if not result.is_error and isinstance(arguments.get("path"), str):
                try:
                    sha = (
                        json.loads(result.content)["sha256"]
                        if result.content.startswith("{")
                        else result.content.splitlines()[0].removeprefix("sha256=")
                    )
                    if (
                        isinstance(sha, str)
                        and len(sha) == 64
                        and all(c in "0123456789abcdef" for c in sha)
                    ):
                        self.read_versions[arguments["path"]] = sha
                except (ValueError, KeyError, TypeError):
                    pass
            return result
        if call.name not in {"write_file", "edit_file"}:
            return await self.registry.execute(call)
        arguments = dict(call.arguments)
        operations = [arguments]
        extra = arguments.get("additional_edits", []) if call.name == "edit_file" else []
        if not isinstance(extra, (list, tuple)) or len(extra) > 15:
            return ToolResult(call_id=call.call_id, content="invalid_batch", is_error=True)
        # 批次输入要复制，不能改变审批保存的原始参数对象。
        if extra:
            operations.extend(dict(item) for item in extra if isinstance(item, dict))
            if len(operations) != len(extra) + 1:
                return ToolResult(call_id=call.call_id, content="invalid_batch", is_error=True)
            arguments["additional_edits"] = operations[1:]
        entries: list[dict[str, Any]] = []
        predictions: list[str | None] = []
        after_reserved = 0
        try:
            for operation in operations:
                relative = operation.get("path")
                if not isinstance(relative, str):
                    raise ToolFailure("invalid_path: missing file target")
                before = snapshot(self.root, relative)
                desired = operation.get("content")
                if call.name == "edit_file" and before is not None:
                    text = base64.b64decode(before["data"]).decode("utf-8")
                    old, new = operation.get("old_text"), operation.get("new_text")
                    if (
                        isinstance(old, str)
                        and old
                        and isinstance(new, str)
                        and text.count(old) == 1
                    ):
                        desired = text.replace(old, new, 1)
                if before is not None:
                    expected = operation.get("expected_sha256") or self.read_versions.get(relative)
                    # 生产链不再在写前自动挑选新版本，防止覆盖读取后的用户修改。
                    if self.require_read and expected is None:
                        raise ToolFailure(f"read_required: {relative}")
                    if expected is not None and (
                        not isinstance(expected, str) or expected.lower() != before["sha256"]
                    ):
                        raise ToolFailure(f"conflict: read version changed: {relative}")
                    operation["expected_sha256"] = expected or before["sha256"]
                elif call.name == "write_file":
                    operation["expected_absent"] = True
                # 已知目标可精确预留 base64 正文及元数据，避免十六个小文件被最坏单文件额度误拒。
                after_reserved += (
                    max(
                        ((len(desired.encode()) + 2) // 3) * 4,
                        len(before["data"]) if before is not None else 0,
                    )
                    + 1024
                    if isinstance(desired, str)
                    else 2 * MAX_FILE_BYTES
                )
                entries.append(
                    {
                        "call_id": call.call_id,
                        "path": relative,
                        "before": before,
                        "after": None,
                        "state": "intent",
                    }
                )
                predictions.append(
                    hashlib.sha256(desired.encode()).hexdigest()
                    if isinstance(desired, str)
                    else None
                )
            # 一次写入全部 intent，任何预检失败都没有目标文件副作用。
            old_count = len(self.record["entries"])
            self.record["entries"].extend(entries)
            if len(json.dumps(self.record).encode()) + after_reserved > 16 * 1024**2:
                del self.record["entries"][old_count:]
                raise ValueError("journal quota exceeded")
            save(self.path, self.record)
        except ToolFailure as error:
            return ToolResult(call_id=call.call_id, content=str(error), is_error=True)
        except (OSError, ValueError):
            return ToolResult(
                call_id=call.call_id, content="change_journal_unavailable", is_error=True
            )
        result = await self.registry.execute(call.model_copy(update={"arguments": arguments}))
        applied: list[str] = []
        if extra:
            try:
                report = json.loads(result.content)
                if report.get("kind") == "edit_batch" and isinstance(report.get("applied"), list):
                    applied = report["applied"]
            except (ValueError, AttributeError):
                pass
        for entry, predicted in zip(entries, predictions, strict=True):
            entry["after"] = snapshot(self.root, entry["path"])
            entry["state"] = "failed" if result.is_error else "confirmed"
            confirmed = not result.is_error or entry["path"] in applied
            # 批次整体失败不否定明确完成的文件，但还必须核验真实目标版本。
            if confirmed:
                image = entry["after"]
                mode = entry["before"]["mode"] if entry["before"] else 0o600
                valid = image is not None and image["sha256"] == predicted and image["mode"] == mode
                entry["state"] = "confirmed" if valid else "unknown"
                if valid:
                    self.read_versions[entry["path"]] = image["sha256"]
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
