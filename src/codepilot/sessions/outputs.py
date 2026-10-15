"""保存截断前原始字节，提供持久引用、配额和有界读搜。"""

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol


class OutputLimitError(OSError):
    """表示输出额度耗尽，调用方应停止命令且保留不完整证据。"""


class Capture(Protocol):
    """约束本地落盘与容器分块传输共用的最小捕获接口。"""

    def write(self, stream: str, data: bytes) -> None:
        """追加一路原始字节，失败必须向执行端传播。"""
        ...

    def finish(self, state: str) -> None:
        """关闭捕获并记录完整或部分状态。"""
        ...

    def description(self) -> str:
        """返回给模型的引用提示，不包含私有宿主路径。"""
        ...


class CaptureFactory(Protocol):
    """让执行端创建捕获而不依赖具体数据库或传输。"""

    def begin(self, call_id: str) -> Capture:
        """为一次命令创建独立 stdout/stderr 捕获。"""
        ...


CURRENT_OUTPUT: ContextVar[CaptureFactory | None] = ContextVar("command_output", default=None)
CURRENT_CALL: ContextVar[str] = ContextVar("command_call", default="")


def private_directory(path: Path) -> None:
    """拒绝符号链接与其他用户目录，创建仅当前用户访问的存储目录。"""
    # 检查整个路径，避免私有子目录被父级链接重定向。
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("private storage cannot contain symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("private storage owner mismatch")
    path.chmod(0o700)


def atomic_json(path: Path, value: object) -> None:
    """以私有临时文件提交完整索引，失败不覆盖上一份有效文件。"""
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class OutputStore:
    """在会话私有目录中保留每条命令的原文，统计跨 run 与当前 task 用量。"""

    def __init__(
        self,
        directory: Path,
        *,
        command_bytes: int = 128 * 1024**2,
        task_bytes: int = 256 * 1024**2,
        session_bytes: int = 1024**3,
    ) -> None:
        """扫描已保存字节，孤立文件也计入额度，避免崩溃后绕过上限。"""
        private_directory(directory)
        self.directory = directory
        self.command_bytes = command_bytes
        self.task_limit = task_bytes
        self.session_limit = session_bytes
        self.on_process: Callable[[int], None] | None = None
        self.task_used = 0
        self.total = sum(path.stat().st_size for path in directory.glob("*.bin"))

    def begin(self, call_id: str) -> "FileCapture":
        """创建可在取消或失败时仍可诊断的初始捕获索引。"""
        return FileCapture(self, call_id)

    def metadata(self, output_id: str) -> dict[str, object]:
        """仅允许本会话登记的随机 ID，禁止利用参数拼接任意宿主路径。"""
        # 模型提供的 ID 不是路径，只能匹配本协议随机名称。
        if re.fullmatch(r"output-[a-f0-9]{32}", output_id) is None:
            raise ValueError("invalid output id")
        path = self.directory / f"{output_id}.json"
        if path.is_symlink():
            raise ValueError("invalid output index")
        record: dict[str, object] = json.loads(path.read_bytes())
        # 完整引用是证据承诺，使用前必须核对真实文件而非盲信索引。
        if record.get("state") == "complete":
            hashes = record.get("sha256")
            if not isinstance(hashes, dict):
                raise ValueError("output index missing hashes")
            for name in ("stdout", "stderr"):
                descriptor = os.open(
                    self.directory / f"{output_id}.{name}.bin", os.O_RDONLY | os.O_NOFOLLOW
                )
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "rb") as source:
                    while chunk := source.read(65536):
                        digest.update(chunk)
                if digest.hexdigest() != hashes.get(name):
                    raise ValueError("output integrity mismatch")
        return record

    def for_call(self, call_id: str) -> list[dict[str, object]]:
        """按调用关联失败或取消前已捕获的输出，不依赖模型自述。"""
        records = []
        for path in self.directory.glob("output-*.json"):
            if path.is_symlink():
                raise ValueError("invalid output index")
            record = json.loads(path.read_bytes())
            if record.get("call_id") == call_id:
                records.append(self.metadata(path.stem))
        return records

    def read(self, output_id: str, stream: str, offset: int, limit: int) -> str:
        """以字节偏移读取有限片段，解码仅影响视图不改变原始字节。"""
        record = self.metadata(output_id)
        if stream not in {"stdout", "stderr"} or offset < 0 or not 1 <= limit <= 65536:
            raise ValueError("invalid output range")
        path = self.directory / f"{output_id}.{stream}.bin"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            source.seek(offset)
            data = source.read(limit)
        return (
            f"output_state={record['state']}\nbyte_range={offset}:{offset + len(data)}\n"
            f"{data.decode('utf-8', errors='replace')}"
        )

    def search(self, output_id: str, stream: str, query: str) -> str:
        """分块搜索字面 UTF-8 字节并返回至多二十个位置，内存不随文件增长。"""
        self.metadata(output_id)
        needle = query.encode()
        if stream not in {"stdout", "stderr"} or not 1 <= len(needle) <= 1024:
            raise ValueError("invalid output search")
        descriptor = os.open(
            self.directory / f"{output_id}.{stream}.bin", os.O_RDONLY | os.O_NOFOLLOW
        )
        hits: list[int] = []
        position = 0
        carry = b""
        with os.fdopen(descriptor, "rb") as source:
            while chunk := source.read(65536):
                data = carry + chunk
                start = 0
                while (index := data.find(needle, start)) >= 0:
                    hits.append(position - len(carry) + index)
                    if len(hits) >= 20:
                        return f"byte_offsets={hits}; truncated=true"
                    start = index + len(needle)
                position += len(chunk)
                carry = data[-(len(needle) - 1) :] if len(needle) > 1 else b""
        return f"byte_offsets={hits}; truncated=false"


class FileCapture:
    """实时写入两路文件并维护完整性，先落盘后发布可用引用。"""

    def __init__(self, store: OutputStore, call_id: str) -> None:
        """创建私有文件及初始 partial 元数据，启动失败不冒充完整。"""
        self.store = store
        self.output_id = f"output-{uuid.uuid4().hex}"
        self.call_id = call_id
        self.saved = dict.fromkeys(("stdout", "stderr"), 0)
        self.observed = dict.fromkeys(("stdout", "stderr"), 0)
        self.hashes = {name: hashlib.sha256() for name in self.saved}
        self.files = {}
        self.state = "partial"
        self.metrics: dict[str, int] = {}
        self.closed = False
        try:
            for name in self.saved:
                descriptor = os.open(
                    store.directory / f"{self.output_id}.{name}.bin",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                self.files[name] = os.fdopen(descriptor, "wb", buffering=0)
            self._save()
        except BaseException:
            for stream in self.files.values():
                stream.close()
            raise

    def write(self, stream: str, data: bytes) -> None:
        """原始输出写入额度以内部分，超限立刻报告并停止命令。"""
        self.observed[stream] += len(data)
        room = min(
            self.store.command_bytes - sum(self.saved.values()),
            self.store.task_limit - self.store.task_used,
            self.store.session_limit - self.store.total,
        )
        chunk = data[: max(0, room)]
        # 只保存已预留额度以内字节；超出部分仅计入 observed，不能静默宣称完整。
        if chunk:
            written = self.files[stream].write(chunk)
            self.saved[stream] += written
            self.store.task_used += written
            self.store.total += written
            self.hashes[stream].update(chunk[:written])
            if written != len(chunk):
                raise OSError("output short write")
        # 超限必须把失败反馈到生产端，继续排空会允许命令无限占用资源。
        if len(chunk) != len(data):
            self.state = "quota_exceeded"
            raise OutputLimitError("output quota exceeded")

    def finish(self, state: str) -> None:
        """关闭全部文件；索引写入失败不掩饰成完整且保证句柄释放。"""
        # finish 可以由多个异常出口调用，但句柄和索引只提交一次。
        if self.closed:
            return
        self.closed = True
        try:
            for stream in self.files.values():
                os.fsync(stream.fileno())
        finally:
            for stream in self.files.values():
                stream.close()
        self.state = self.state if self.state == "quota_exceeded" else state
        self._save()

    def _save(self) -> None:
        """原子保存字节、哈希与状态，不含命令正文或凭据。"""
        atomic_json(
            self.store.directory / f"{self.output_id}.json",
            {
                "schema_version": 1,
                "output_id": self.output_id,
                "call_id": self.call_id,
                "state": self.state,
                "saved": self.saved,
                "observed": self.observed,
                "sha256": {key: value.hexdigest() for key, value in self.hashes.items()},
                "updated_at": time.time(),
                "metrics": self.metrics,
            },
        )

    def description(self) -> str:
        """提供完整性和后续读搜入口；模型看不到控制器路径。"""
        return (
            f"output_id={self.output_id}\noutput_state={self.state}\n"
            f"saved_bytes={self.saved}\n原始输出可用 read_output/search_output 按字节读取或搜索。"
        )
