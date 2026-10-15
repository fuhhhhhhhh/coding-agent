"""用事务事件日志与独占写锁保存真实会话事实，保守处理未知副作用。"""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from codepilot.contracts import Message, ToolResult
from codepilot.contracts.run import ObservationRejected, RunObservation, RunReport
from codepilot.sessions.outputs import private_directory

SCHEMA_VERSION = 1


class ReconciliationRequired(ValueError):
    """表示必须先核验工作区或未知调用，不能自动重放。"""


def safe_id(value: str) -> str:
    """只接受适合单个目录名的身份，阻止 CLI ID 穿越状态目录。"""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise ValueError("invalid session/task/run id")
    return value


def workspace_stamp(workspace: Path) -> str:
    """流式核对用户可见文件，忽略构建缓存而不读取或打印文件正文。"""
    digest = hashlib.sha256()
    count = 0
    ignored = {
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    for directory, names, files in os.walk(workspace, followlinks=False):
        names[:] = sorted(name for name in names if name not in ignored and name != ".git")
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                digest.update(path.relative_to(workspace).as_posix().encode())
                digest.update(os.readlink(path).encode())
        # Git 身份只纳入 HEAD/ref 指针，不扫描对象库或凭据配置。
        if (
            Path(directory) == workspace
            and (workspace / ".git").is_dir()
            and not (workspace / ".git").is_symlink()
        ):
            git = workspace / ".git"
            for entry in (git / "HEAD", git / "packed-refs"):
                if entry.is_file() and not entry.is_symlink():
                    digest.update(entry.read_bytes())
            for entry in sorted((git / "refs").rglob("*")):
                if entry.is_file() and not entry.is_symlink():
                    digest.update(entry.relative_to(git).as_posix().encode())
                    digest.update(entry.read_bytes())
        for name in sorted(files):
            path = Path(directory) / name
            count += 1
            if count > 100000:
                raise ValueError("workspace manifest exceeds 100000 files")
            digest.update(path.relative_to(workspace).as_posix().encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
                continue
            # FIFO/设备不应在恢复前核验阶段阻塞控制器。
            import stat

            metadata = path.stat()
            digest.update(str(metadata.st_mode).encode())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("workspace contains unsupported special file")
            with path.open("rb") as stream:
                while data := stream.read(65536):
                    digest.update(data)
    return digest.hexdigest()


def process_identity(pid: int) -> str:
    """读取 Linux 进程启动时钟，PID 复用时不能把新进程当作旧资源。"""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


class SessionStore:
    """事务事件是唯一事实日志；SQLite 索引和消息视图可从事件重建。"""

    def __init__(self, root: Path, session_id: str, *, create: bool = True) -> None:
        """打开私有会话并核验 Schema，读取不存在会话不创建空目录。"""
        self.directory = root / safe_id(session_id)
        self.session_id = session_id
        # 查看或恢复拼错的 ID 不应生成一个看似成功的新会话。
        if not create and not (self.directory / "session.db").is_file():
            raise ValueError("session not found")
        private_directory(self.directory)
        db_path = self.directory / "session.db"
        if db_path.is_symlink():
            raise ValueError("session database cannot be symlink")
        descriptor = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        db_path.chmod(0o600)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        # 未来版本不可被旧程序降级写入，保留原数据供明确迁移。
        if version not in {0, SCHEMA_VERSION}:
            self.db.close()
            raise ValueError("unsupported session schema version")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, "
            "version INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()
        self.lock_fd: int | None = None
        self._messages: list[Message] | None = None
        self.request_limit = 1000
        self.history_limit = 16 * 1024**2
        self.secrets: tuple[str, ...] = ()

    def __enter__(self) -> Self:
        """独占会话写锁，活跃控制器存在时立即拒绝第二个恢复进程。"""
        descriptor = os.open(
            self.directory / "write.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            self.db.close()
            raise ValueError("session is already in use") from error
        self.lock_fd = descriptor
        try:
            # 追加事件是唯一事实源；索引丢失或落后不能把未知 run 当作新会话。
            self.rebuild()
        except BaseException:
            self.db.close()
            os.close(descriptor)
            self.lock_fd = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """关闭事务连接并释放内核锁，进程崩溃时锁由操作系统回收。"""
        self.db.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def get(self, key: str, default: Any = None) -> Any:
        """读取可重建视图，不从未知字段猜测运行成功。"""
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def commit(self, kind: str, payload: dict[str, Any], **updates: Any) -> None:
        """在同一 SQLite 事务追加事实并更新视图，失败时整个事务回滚。"""
        envelope = {"time": time.time(), **payload, "updates": updates}
        # 先在结构化值中脱敏，再做 JSON 编码，含引号或反斜杠的密钥也不会漏出。
        envelope = self._redact(envelope)
        updates = envelope["updates"]
        with self.db:
            self.db.execute(
                "INSERT INTO events(version,kind,payload) VALUES(?,?,?)",
                (SCHEMA_VERSION, kind, json.dumps(envelope, ensure_ascii=False)),
            )
            for key, value in updates.items():
                self.db.execute(
                    "INSERT OR REPLACE INTO state VALUES(?,?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )

    def _redact(self, value: Any) -> Any:
        """递归过滤已知秘密值，不改变事件结构或依赖编码后的字符串匹配。"""
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "<redacted>")
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        return value

    def history(self) -> tuple[Message, ...]:
        """还原严格的内部消息；数据库损坏不降级为空会话。"""
        if self._messages is None:
            messages: list[Message] = []
            for kind, raw in self.db.execute("SELECT kind,payload FROM events ORDER BY seq"):
                event = json.loads(raw)
                snapshot = event["updates"].get("messages")
                if snapshot is not None:
                    messages = [
                        Message.model_validate_json(json.dumps(value)) for value in snapshot
                    ]
                elif kind == "model_replied" and event.get("message") is not None:
                    messages.append(Message.model_validate_json(json.dumps(event["message"])))
                elif kind == "tool_result" and event.get("result") is not None:
                    messages.append(
                        Message(
                            role="tool",
                            tool_result=ToolResult.model_validate_json(json.dumps(event["result"])),
                        )
                    )
            self._messages = messages
        return tuple(self._messages)

    def check(self, workspace: Path, mode: str) -> None:
        """启动前核验工作区身份、未完成 run 和末次文件版本。"""
        # 到期不自动销毁可能重要的材料，但不继续使用过期上下文。
        if self.get("expires_at", float("inf")) < time.time():
            raise ValueError("session expired; inspect or delete it instead of resuming")
        identity = [str(workspace), workspace.stat().st_dev, workspace.stat().st_ino, mode]
        previous = self.get("workspace")
        # 同一路径被替换或执行模式变化，都不能静默套用旧授权和历史。
        if previous is not None and previous != identity:
            raise ReconciliationRequired("session workspace/backend mode mismatch")
        # 没有可靠收尾事实的运行可能已经产生副作用，只允许显式协调。
        if self.get("status") in {"running", "needs_reconciliation"}:
            raise ReconciliationRequired(
                "needs_reconciliation: previous run may have side effects; "
                "use sessions show/reconcile"
            )
        # 历史视图必须完整配对，不能以 ready 索引掩盖失踪工具结果。
        pending: list[str] = []
        for message in self.history():
            if message.role == "tool":
                if (
                    not pending
                    or message.tool_result is None
                    or message.tool_result.call_id != pending[0]
                ):
                    raise ReconciliationRequired("needs_reconciliation: history pairing invalid")
                pending.pop(0)
            else:
                if pending:
                    raise ReconciliationRequired("needs_reconciliation: history pairing incomplete")
                pending.extend(call.call_id for call in message.tool_calls)
        if pending:
            raise ReconciliationRequired("needs_reconciliation: history pairing incomplete")
        stamp = self.get("stamp")
        if stamp is not None and stamp != workspace_stamp(workspace):
            raise ReconciliationRequired(
                "needs_reconciliation: workspace changed; use sessions reconcile --accept-workspace"
            )
        # 只在所有检查通过后建立新会话归属。
        if previous is None:
            self.commit("session_created", {}, workspace=identity, status="ready")

    def begin(self, run_id: str, messages: tuple[Message, ...]) -> None:
        """副作用前保存新输入与运行意图，崩溃后保持 running 供协调。"""
        self._messages = list(messages)
        self.commit(
            "run_started",
            {"run_id": run_id},
            status="running",
            run_id=run_id,
            messages=[message.model_dump(mode="json") for message in messages],
        )

    def observe(self, observation: RunObservation) -> None:
        """同步保存模型回复、工具意图与结果；持久化失败阻止后续执行。"""
        # 在任何 Provider 请求前预扣共享次数，不能用换 run 绕过上限。
        if observation.kind == "model_requested":
            count = self.get("request_attempts", 0)
            if count >= self.request_limit:
                raise ObservationRejected("session_limit", "request_budget_exhausted")
            self.commit("request_reserved", {}, request_attempts=count + 1)
        if self._messages is None:
            self.history()
        assert self._messages is not None
        if observation.kind == "model_replied" and observation.message is not None:
            self._messages.append(observation.message)
        elif observation.kind == "tool_result" and observation.result is not None:
            self._messages.append(Message(role="tool", tool_result=observation.result))
        if (
            sum(len(message.model_dump_json().encode()) for message in self._messages)
            > self.history_limit
        ):
            raise ObservationRejected("session_limit", "history_quota_exceeded")
        calls = self.get("calls", {})
        if observation.message is not None:
            for call in observation.message.tool_calls:
                calls[call.call_id] = "planned"
        if observation.call is not None:
            calls[observation.call.call_id] = "running"
        if observation.result is not None:
            calls[observation.result.call_id] = (
                "failed" if observation.result.is_error else "succeeded"
            )
        self.commit(observation.kind, observation.model_dump(mode="json"), calls=calls)

    def resource(self, kind: str, identity: dict[str, Any]) -> None:
        """副作用资源创建后立即记录身份，后续检查不得只依赖模型描述。"""
        resources = self.get("resources", [])
        resources.append({"kind": kind, "uid": os.getuid(), **identity})
        self.commit("resource_owned", identity, resources=resources)

    def finish(self, report: RunReport | None, workspace: Path, *, clean: bool) -> None:
        """终态与快照原子提交，未配对调用或清理失败保持需要协调。"""
        messages = report.messages if report is not None else self.history()
        pending: set[str] = set()
        for message in messages:
            pending.update(call.call_id for call in message.tool_calls)
            if message.tool_result is not None:
                pending.discard(message.tool_result.call_id)
        reliable = report is None or report.reason != "persistence_error"
        status = "ready" if clean and not pending and reliable else "needs_reconciliation"
        calls = self.get("calls", {})
        for call_id in pending:
            calls[call_id] = "unknown"
        self.commit(
            "run_settled",
            {"reason": report.reason if report else "unknown"},
            status=status,
            calls=calls,
            stamp=workspace_stamp(workspace),
            resources=[] if clean else self.get("resources", []),
            messages=[message.model_dump(mode="json") for message in messages],
        )

    def reconcile(self, workspace: Path) -> None:
        """用户确认实际工作区后结束未知调用，保持旧事实且禁止重放。"""
        if self.get("workspace", [None])[0] != str(workspace):
            raise ValueError("workspace mismatch")
        # 活跃资源先独立检查，不能仅凭用户确认误杀或忽略仍运行的进程。
        for resource in self.get("resources", []):
            # 外来所有者的资源不能由当前会话确认或清理。
            if resource.get("uid", os.getuid()) != os.getuid():
                raise ValueError("resource owner mismatch")
            if resource["kind"] == "process":
                current_identity = process_identity(resource["pid"])
                # 进程不存在时空身份不表示活跃；仍存在但起点未知时保守阻断。
                if current_identity and (
                    not resource["start"] or current_identity == resource["start"]
                ):
                    raise ValueError("owned process still active; stop it before reconciliation")
            if resource["kind"] == "container":
                import subprocess

                result = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", resource["id"]],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip() == b"true":
                    raise ValueError("owned container still active; stop it before reconciliation")
                if result.returncode != 0 and b"No such" not in result.stderr:
                    raise ValueError("container state unknown")
        messages = list(self.history())
        pending: dict[str, None] = {}
        for message in messages:
            pending.update(dict.fromkeys(call.call_id for call in message.tool_calls))
            if message.tool_result is not None:
                pending.pop(message.tool_result.call_id, None)
        for call_id in pending:
            messages.append(
                Message(
                    role="tool",
                    tool_result=ToolResult(
                        call_id=call_id,
                        is_error=True,
                        content=(
                            "Execution outcome unknown; user reconciled workspace. "
                            "Do not replay this call."
                        ),
                    ),
                )
            )
        self._messages = messages
        calls = self.get("calls", {})
        calls.update(dict.fromkeys(pending, "unknown"))
        self.commit(
            "user_reconciled",
            {},
            calls=calls,
            status="ready",
            resources=[],
            stamp=workspace_stamp(workspace),
            messages=[message.model_dump(mode="json") for message in messages],
        )

    @staticmethod
    def inspect(root: Path, session_id: str, *, messages: bool = False) -> dict[str, Any]:
        """以只读事务查看活跃会话，不竞争执行锁，也不触发索引重建。"""
        directory = root / safe_id(session_id)
        if any(part.is_symlink() for part in (directory, *directory.parents)):
            raise ValueError("unsafe session path")
        if not directory.is_dir() or directory.stat().st_uid != os.getuid():
            raise ValueError("session not found or owner mismatch")
        path = directory / "session.db"
        if path.is_symlink():
            raise ValueError("unsafe session database")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("unsupported session schema version")
            snapshot = {
                key: json.loads(value) for key, value in db.execute("SELECT key,value FROM state")
            }
            result = {
                key: snapshot.get(key)
                for key in (
                    "status",
                    "run_id",
                    "request_attempts",
                    "resources",
                    "calls",
                    "configuration",
                    "expires_at",
                )
            }
            result["session_id"] = session_id
            if messages:
                history: list[dict[str, Any]] = []
                for kind, raw in db.execute("SELECT kind,payload FROM events ORDER BY seq"):
                    event = json.loads(raw)
                    if "messages" in event["updates"]:
                        history = event["updates"]["messages"]
                    elif kind == "model_replied" and event.get("message") is not None:
                        history.append(event["message"])
                    elif kind == "tool_result" and event.get("result") is not None:
                        history.append({"role": "tool", "tool_result": event["result"]})
                result["messages"] = history
            return result
        finally:
            db.close()

    def ensure_deletable(self) -> None:
        """私有记录可删除，但仍保留的代码工作树与补丁不能随会话销毁。"""
        if self.get("status") in {"running", "needs_reconciliation"}:
            raise ValueError("reconcile session before deletion")
        # 只要存在未完成交付清单就保守保留，不能用 TTL 代替代码归属核验。
        for path in self.directory.rglob("workspace.json"):
            if path.is_symlink():
                raise ValueError("unsafe worktree manifest")
            record = json.loads(path.read_bytes())
            if record.get("state") != "cleaned":
                raise ValueError("session retains worktree or undelivered patch; refusing deletion")

    def rebuild(self) -> None:
        """从事务事件重建视图；不截断或猜测损坏的已提交事件。"""
        updates: dict[str, Any] = {}
        expected_sequence = 1
        for sequence, version, payload in self.db.execute(
            "SELECT seq,version,payload FROM events ORDER BY seq"
        ):
            # 缺失或损坏的是已提交事实时不能擅自删尾并继续副作用。
            if version != SCHEMA_VERSION or sequence != expected_sequence:
                raise ValueError("invalid event version or sequence")
            expected_sequence += 1
            updates.update(json.loads(payload)["updates"])
        self._messages = None
        with self.db:
            self.db.execute("DELETE FROM state")
            self.db.executemany(
                "INSERT INTO state VALUES(?,?)",
                [(key, json.dumps(value)) for key, value in updates.items()],
            )
