"""显式限容工作区的有界归档协议；不使用会遗漏 tmpfs 的 docker cp 导出。"""

import argparse
import io
import os
import signal
import stat
import sys
import tarfile
import time
from pathlib import Path
from typing import BinaryIO

MAX_ENTRIES = 20000


def pack(root: Path, output: BinaryIO, limit: int) -> None:
    """流式输出普通文件和目录，拒绝链接、设备、过多条目及超额正文。"""
    total = 0
    count = 0
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in sorted([*dirs, *files]):
                path = Path(directory) / name
                info = path.lstat()
                count += 1
                if (
                    count > MAX_ENTRIES
                    or stat.S_ISLNK(info.st_mode)
                    or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                ):
                    raise ValueError("unsupported archive entry")
                member = tarfile.TarInfo(path.relative_to(root).as_posix())
                member.mode = stat.S_IMODE(info.st_mode) & 0o777
                if stat.S_ISDIR(info.st_mode):
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                else:
                    total += info.st_size
                    if total > limit or info.st_nlink != 1:
                        raise ValueError("archive exceeds workspace limit or contains hard links")
                    member.size = info.st_size
                    with path.open("rb") as stream:
                        archive.addfile(member, stream)
                    after = path.stat()
                    if (info.st_ino, info.st_mtime_ns, info.st_size) != (
                        after.st_ino,
                        after.st_mtime_ns,
                        after.st_size,
                    ):
                        raise ValueError("workspace changed while exporting")


def unpack(data: bytes, root: Path, limit: int) -> None:
    """先验证完整条目和大小再手工展开；不信任 tar 的路径、链接和所有者。"""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = []
        names: set[str] = set()
        total = 0
        for item in archive:
            name = Path(item.name)
            # 解包只接收相对普通条目，硬链接/符号链接都不能变成写入跳板。
            if (
                name.is_absolute()
                or ".." in name.parts
                or not name.parts
                or not (item.isfile() or item.isdir())
            ):
                raise ValueError("unsafe archive path or type")
            normalized = name.as_posix()
            if normalized in names or len(names) >= MAX_ENTRIES or item.mode & 0o7000:
                raise ValueError("duplicate or unsafe archive member")
            names.add(normalized)
            total += item.size
            if item.size < 0 or total > limit:
                raise ValueError("archive payload exceeds limit")
            members.append(item)
        for item in members:
            target = root / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.isdir():
                target.mkdir(exist_ok=True)
            else:
                stream = archive.extractfile(item)
                if stream is None:
                    raise ValueError("missing archive file body")
                with stream, target.open("xb") as output:
                    while chunk := stream.read(65536):
                        output.write(chunk)
                target.chmod(item.mode & 0o777)


def quiesce(main_pid: int) -> None:
    """停止当前 PID 命名空间里的其他工作进程，保留 init、已知主进程及导出器。"""
    # 该入口只服务 --init 容器；误在宿主调用时必须在发送任何信号前拒绝。
    if not Path("/.dockerenv").is_file() or Path("/proc/1/comm").read_text().strip() not in {
        "docker-init",
        "tini",
    }:
        raise ValueError("workspace quiesce requires the owned init container")
    if main_pid <= 1 or not Path(f"/proc/{main_pid}").exists():
        raise ValueError("container main identity unavailable")
    keep = {1, main_pid, os.getpid()}
    for _ in range(8):
        running = False
        for path in Path("/proc").iterdir():
            if not path.name.isdigit() or int(path.name) in keep:
                continue
            try:
                state = (path / "stat").read_text().rpartition(")")[2].split()[0]
                if state not in {"T", "t", "Z", "X"}:
                    running = True
                    os.kill(int(path.name), signal.SIGSTOP)
            except (FileNotFoundError, ProcessLookupError):
                # 扫描过程中自然退出的进程已经不再写工作区。
                continue
        if not running:
            return
        time.sleep(0.02)
    raise RuntimeError("workspace processes did not quiesce")


def main() -> int:
    """固定 /workspace 导入/导出，异常只输出类型，不泄露归档正文。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("import", "export"))
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--main-pid", type=int)
    args = parser.parse_args()
    try:
        if not 1024**2 <= args.limit <= 64 * 1024**2:
            raise ValueError("unsupported workspace size")
        root = Path("/workspace")
        if args.action == "import":
            data = sys.stdin.buffer.read(args.limit * 2 + 32 * 1024**2 + 1)
            if len(data) > args.limit * 2 + 32 * 1024**2:
                raise ValueError("archive stream exceeds limit")
            unpack(data, root, args.limit)
        else:
            if args.main_pid is not None:
                quiesce(args.main_pid)
            pack(root, sys.stdout.buffer, args.limit)
        return 0
    except Exception as error:
        print(f"workspace_archive_error: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
