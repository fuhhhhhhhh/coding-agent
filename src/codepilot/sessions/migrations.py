"""在独占会话锁下备份和迁移数据库，原始事件保持唯一事实来源。"""

import fcntl
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from codepilot.sessions.outputs import private_directory

DATABASE_VERSION = 2
EVENT_VERSION = 1
SNAPSHOT_SQL = (
    "CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY CHECK(id=1), "
    "sequence INTEGER NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL)"
)


def backup_database(db: sqlite3.Connection, directory: Path) -> Path:
    """使用 SQLite 一致性备份生成私有文件，失败不保留半份备份。"""
    private_directory(directory)
    target = directory / f"backup-{uuid4().hex}.db"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(target)) as destination:
            db.backup(destination)
            # 备份必须可独立读取，不能以创建了文件作为成功标准。
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup integrity check failed")
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
    except BaseException:
        # 迁移只能依赖完整备份，写入失败或中断时清除未完成文件。
        target.unlink(missing_ok=True)
        raise
    return target


def event_state(db: sqlite3.Connection) -> dict[str, Any]:
    """迁移前核对已提交事实，不能把损坏库仅改版本号后报告成功。"""
    expected = 1
    state: dict[str, Any] = {}
    for sequence, version, payload in db.execute(
        "SELECT seq,version,payload FROM events ORDER BY seq"
    ):
        # 已提交事实缺失或版本未知时，不能用可能落后的索引代替。
        if sequence != expected or version != EVENT_VERSION:
            raise ValueError("invalid event version or sequence")
        event = json.loads(payload)
        if not isinstance(event, dict) or not isinstance(event.get("updates"), dict):
            raise ValueError("invalid session event")
        expected += 1
        state.update(event["updates"])
    return state


def preserved_budget(current: sqlite3.Connection, saved: sqlite3.Connection) -> dict[str, object]:
    """恢复历史不能恢复免费额度，合并两边较大的计量和未知预留。"""
    now = event_state(current)
    old = event_state(saved)
    current_budget = now.get("provider_budget", {})
    saved_budget = old.get("provider_budget", {})
    merged = {**saved_budget, **current_budget}
    for key in ("input", "output", "legacy_requests_without_usage", "unpriced_attempts"):
        merged[key] = max(current_budget.get(key, 0), saved_budget.get(key, 0))
    merged["pending"] = {**saved_budget.get("pending", {}), **current_budget.get("pending", {})}
    merged["attempt_prices"] = {
        **saved_budget.get("attempt_prices", {}),
        **current_budget.get("attempt_prices", {}),
    }
    merged["known_cost_usd"] = str(
        max(
            Decimal(current_budget.get("known_cost_usd", "0")),
            Decimal(saved_budget.get("known_cost_usd", "0")),
        )
    )
    return {
        "request_attempts": max(now.get("request_attempts", 0), old.get("request_attempts", 0)),
        "provider_budget": merged,
        "status": "needs_reconciliation",
    }


def maintain_database(
    directory: Path, *, migrate: bool = False, restore_name: str | None = None
) -> dict[str, object]:
    """维护现有会话；先备份，再在单事务中升级，失败自动回滚原库。"""
    if not directory.is_dir():
        raise ValueError("session not found")
    private_directory(directory)
    path = directory / "session.db"
    # 不存在、符号链接或其他用户文件均不进入 SQLite，避免无意创建新会话。
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError("unsafe or missing session database")
    lock = os.open(directory / "write.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    db = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("session is already in use") from error
        db = sqlite3.connect(path)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        # 未来版本不能猜测升级或降级；不改变主库和事件。
        if version not in {1, DATABASE_VERSION}:
            raise ValueError("unsupported session schema version")
        if restore_name is not None:
            # 恢复是显式数据操作，但仍不能丢掉活跃运行的资源所有权。
            status = event_state(db).get("status")
            if status in {"running", "needs_reconciliation"}:
                raise ValueError("reconcile session before restoring database")
        backup = backup_database(db, directory / "backups")
        if restore_name is not None:
            # 仅接受本会话命名空间内的完整备份，绝对路径和链接不能穿越私有目录。
            if re.fullmatch(r"backup-[0-9a-f]{32}\.db", restore_name) is None:
                raise ValueError("invalid backup name")
            source = directory / "backups" / restore_name
            if source.is_symlink() or not source.is_file():
                raise ValueError("unsafe or missing backup")
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as saved:
                saved_version = saved.execute("PRAGMA user_version").fetchone()[0]
                if saved_version not in {1, DATABASE_VERSION}:
                    raise ValueError("unsupported backup schema")
                event_state(saved)
                retained = preserved_budget(db, saved)
                replacement = backup_database(saved, directory / "backups")
            try:
                with closing(sqlite3.connect(replacement)) as restored:
                    with restored:
                        # 恢复历史不能宣称当前文件也已回滚，强制重新协调工作区。
                        payload = json.dumps({"time": time.time(), "updates": retained})
                        restored.execute(
                            "INSERT INTO events(version,kind,payload) VALUES(?,?,?)",
                            (EVENT_VERSION, "database_restored", payload),
                        )
                        for key, value in retained.items():
                            restored.execute(
                                "INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value))
                            )
                        if saved_version == DATABASE_VERSION:
                            restored.execute("DELETE FROM snapshots")
                db.close()
                db = None
                os.replace(replacement, path)
            finally:
                # 原库替换失败时仍保留原数据和升级前备份，半成品不当作当前库。
                replacement.unlink(missing_ok=True)
            return {
                "backup": str(backup),
                "restored": restore_name,
                "schema_after": saved_version,
                "status": "needs_reconciliation",
            }
        if migrate:
            event_state(db)
        if migrate and version == 1:
            # 显式 BEGIN 使 DDL 与版本号一同提交，升级故障时两者都回滚。
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(SNAPSHOT_SQL)
                db.execute(f"PRAGMA user_version={DATABASE_VERSION}")
        return {
            "schema_before": version,
            "schema_after": db.execute("PRAGMA user_version").fetchone()[0],
            "backup": str(backup),
            "scope": "database_only; output/artifact files remain in session directory",
        }
    finally:
        # 维护失败也必须释放写锁，让后续诊断能够访问旧库或备份。
        if db is not None:
            db.close()
        os.close(lock)
