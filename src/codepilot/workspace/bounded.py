"""显式限容任务副本、源版本前置条件、产物保留和可审查的交付/撤销。"""

import hashlib
import io
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from codepilot.tools.discovery_common import DEFAULT_EXCLUDES
from codepilot.tools.file_common import _atomic_write
from codepilot.tools.workspace import Workspace
from codepilot.workspace.archive import MAX_ENTRIES, pack


def selected_files(root: Path) -> list[Path]:
    """遍历普通源文件，剪枝 Git/缓存/环境秘密文件，不跟随链接。"""
    paths: list[Path] = []
    entries = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in DEFAULT_EXCLUDES)
        entries += len(dirs) + len(files)
        # 空目录和深层目录同样会消耗遍历与 inode，不能只按有正文的文件计数。
        if entries > MAX_ENTRIES:
            raise ValueError("bounded workspace has too many entries")
        for name in [*dirs, *files]:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("bounded workspace does not accept symbolic links")
        for name in sorted(files):
            # 凭据文件不默认复制；不声称文件名规则能识别任意秘密。
            if name in DEFAULT_EXCLUDES or name.startswith(".env"):
                continue
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError("bounded workspace requires regular files")
            paths.append(path)
            if len(paths) > MAX_ENTRIES:
                raise ValueError("bounded workspace has too many files")
    return paths


def image(root: Path, relative: str, limit: int) -> dict[str, Any] | None:
    """有界计算文件内容版本及普通权限，拒绝目录、链接和特殊模式。"""
    path = Workspace(root).resolve(relative, must_exist=False)
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_mode & 0o7000:
        raise ValueError("unsafe bounded file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    after = path.stat()
    if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("file changed while snapshotting")
    return {"sha256": digest.hexdigest(), "mode": stat.S_IMODE(info.st_mode), "bytes": info.st_size}


def tree(root: Path, limit: int) -> dict[str, Any]:
    """记录有界完整选中文件集合，产物元数据不能替代实际字节核验。"""
    result: dict[str, Any] = {}
    size = 0
    for path in selected_files(root):
        relative = path.relative_to(root).as_posix()
        version = image(root, relative, limit)
        if version is None:
            raise ValueError("file disappeared while snapshotting")
        size += version["bytes"]
        if size > limit:
            raise ValueError("bounded workspace payload exceeds limit")
        result[relative] = version
    return result


class BoundedWorkspace:
    """拥有私有初始副本和最终副本，源目录只在明确版本检查后接收交付。"""

    def __init__(self, source: Path, directory: Path, run_id: str, limit: int) -> None:
        """保存固定源目录和本 run 状态；对象构造不触碰源文件。"""
        self.source = source
        self.directory = directory
        self.workspace = directory / "bounded-workspace"
        self.baseline = directory / "bounded-baseline"
        self.path = directory / "bounded-workspace.json"
        self.limit = limit
        info = source.stat()
        self.record: dict[str, Any] = {
            "version": 1,
            "run_id": run_id,
            "root": str(source),
            "identity": [info.st_dev, info.st_ino],
            "limit": limit,
            "state": "preparing",
            "before": {},
            "after": {},
            "applied_paths": [],
            "changed_files": [],
        }

    def save(self) -> None:
        """私有原子保存进度，失败不能宣称批次交付完成。"""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write(
            self.path, json.dumps(self.record, ensure_ascii=False, sort_keys=True).encode()
        )

    def prepare(self) -> Path:
        """复制真实脏内容而非 HEAD，初始版本与副本字节必须完全一致。"""
        before = tree(self.source, self.limit)
        self.baseline.mkdir(parents=True, exist_ok=False, mode=0o700)
        for relative, version in before.items():
            target = self.baseline / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Workspace(self.source).resolve(relative, must_exist=True), target)
            target.chmod(version["mode"])
        if tree(self.baseline, self.limit) != before or tree(self.source, self.limit) != before:
            raise ValueError("source changed during bounded workspace preparation")
        shutil.copytree(self.baseline, self.workspace)
        self.record.update(before=before, state="active")
        self.save()
        return self.workspace

    def finish(self, *, apply: bool) -> dict[str, Any]:
        """保留最终副本，只有运行正常结束才尝试有条件交付。"""
        after = tree(self.workspace, self.limit)
        changed = sorted(
            name
            for name in self.record["before"].keys() | after.keys()
            if self.record["before"].get(name) != after.get(name)
        )
        self.record.update(
            after=after, changed_files=changed, state="retained" if changed else "empty"
        )
        self.save()
        return self.transfer(undo=False) if apply and changed else self.review()

    def review(self) -> dict[str, Any]:
        """返回不含正文的交付/撤销预览，所有版本均由控制器实际读取核验。"""
        info = self.source.stat()
        if self.record["root"] != str(self.source) or self.record["identity"] != [
            info.st_dev,
            info.st_ino,
        ]:
            raise ValueError("bounded source workspace identity changed")
        undo = self.record["state"] == "applied"
        expected = self.record["after"] if undo else self.record["before"]
        conflicts = []
        for name in self.record["changed_files"]:
            if image(self.source, name, self.limit) != expected.get(name):
                conflicts.append(name)
        return {
            "run_id": self.record["run_id"],
            "state": self.record["state"],
            "applied": False,
            "conflicts": conflicts,
            "files": [
                {
                    "path": name,
                    "before": self.record["before"].get(name),
                    "after": self.record["after"].get(name),
                }
                for name in self.record["changed_files"]
            ],
            "artifact_root": str(self.workspace),
            "scope": "isolated bounded workspace; source versions checked",
        }

    def transfer(self, *, undo: bool) -> dict[str, Any]:
        """整批核验后逐文件交付或撤销；中断保留 applying/undoing，禁止盲目重放。"""
        expected_state = "applied" if undo else "retained"
        if self.record["state"] != expected_state:
            raise ValueError("bounded delivery is not in a transferable state")
        result = self.review()
        if result["conflicts"]:
            return result
        expected = self.record["after"] if undo else self.record["before"]
        desired = self.record["before"] if undo else self.record["after"]
        content_root = self.baseline if undo else self.workspace
        # 私有副本也可能损坏，开始写源目录前验证全部目标正文。
        for name in self.record["changed_files"]:
            if (
                desired.get(name) is not None
                and image(content_root, name, self.limit) != desired[name]
            ):
                raise ValueError("bounded delivery artifact was corrupted")
        self.record["state"] = "undoing" if undo else "applying"
        self.record["applied_paths"] = []
        self.save()
        for name in self.record["changed_files"]:
            if image(self.source, name, self.limit) != expected.get(name):
                raise ValueError(
                    "source changed during bounded transfer; partial progress retained"
                )
            target = Workspace(self.source).resolve(name, must_exist=False)
            if desired.get(name) is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(
                    target,
                    (content_root / name).read_bytes(),
                    expected_absent=expected.get(name) is None,
                )
                target.chmod(desired[name]["mode"])
            self.record["applied_paths"].append(name)
            self.save()
        self.record["state"] = "undone" if undo else "applied"
        self.save()
        result.update(state=self.record["state"], applied=True)
        return result

    @classmethod
    def open(cls, path: Path, source: Path) -> "BoundedWorkspace":
        """只从当前会话的私有清单重开维护，不使用清单提供的任意文件目标。"""
        if path.is_symlink() or path.stat().st_size > 8 * 1024**2:
            raise ValueError("unsafe bounded workspace record")
        record = json.loads(path.read_bytes())
        # 私有清单损坏也必须可诊断地拒绝，不能泄漏类型错误或据未知进度重放。
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or type(record.get("limit")) is not int
            or not 1024**2 <= record["limit"] <= 64 * 1024**2
            or record.get("state")
            not in ("active", "retained", "empty", "applying", "undoing", "applied", "undone")
            or not isinstance(record.get("run_id"), str)
            or not isinstance(record.get("root"), str)
            or not isinstance(record.get("identity"), list)
            or len(record["identity"]) != 2
            or not all(type(value) is int for value in record["identity"])
            or not all(isinstance(record.get(key), dict) for key in ("before", "after"))
            or not isinstance(record.get("changed_files"), list)
            or not all(isinstance(name, str) for name in record["changed_files"])
        ):
            raise ValueError("invalid bounded workspace record")
        owned = cls(source, path.parent, record["run_id"], record["limit"])
        owned.record = record
        owned.review()
        return owned


def seed_archive(root: Path, limit: int) -> bytes:
    """从控制器私有副本生成有界原始 tar，正文不进入 Trace。"""
    output = io.BytesIO()
    pack(root, output, limit)
    return output.getvalue()
