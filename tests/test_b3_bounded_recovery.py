"""验证恢复只保存已知产物，归属、阶段、会话与故障不能被模型重放绕过。"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from codepilot.app.operations import recover
from codepilot.config import ProductConfig
from codepilot.contracts import Message, ToolCall
from codepilot.sessions.store import SessionStore
from codepilot.workspace.bounded import BoundedWorkspace, seed_archive
from codepilot.workspace.recovery import recover_workspace


class Engine:
    """保留真实归档字节的确定性 daemon 通道，仅模拟 Docker 生命周期。"""

    def __init__(self, record: dict[str, Any], payload: bytes) -> None:
        """构造与绑定一致的 inspect，故障开关只影响归档/删除/连接。"""
        self.record = record
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []
        self.remove_fail = False
        self.export_fail = False
        self.unavailable = False
        self.data: dict[str, Any] | None = {
            "Id": record["container_id"],
            "Name": "/" + record["name"],
            "Config": {
                "Image": record["image_id"],
                "User": "0:0",
                "Labels": {
                    "codepilot.managed": "true",
                    "codepilot.owner": record["owner"],
                    "codepilot.workspace_mode": "bounded-copy",
                    "codepilot.backend_id": record["backend_id"],
                },
            },
            "HostConfig": {
                "Memory": record["memory_bytes"],
                "Init": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "Tmpfs": {"/workspace": f"size={record['workspace_bytes']}"},
            },
            "State": {"Running": True},
        }

    async def __call__(
        self, argv: tuple[str, ...], stdin: bytes | None, limit: int
    ) -> tuple[int, bytes, bytes]:
        """记录精确操作，任何工具执行或重新创建都会失败。"""
        self.calls.append(argv)
        if self.unavailable:
            return 1, b"", b"Cannot connect"
        if argv[0] == "info":
            return 0, b'["name=rootless"]', b""
        if argv[:2] == ("container", "inspect"):
            return (
                (0, json.dumps([self.data]).encode(), b"")
                if self.data
                else (1, b"", b"No such container")
            )
        if argv[0] == "exec" and "export" in argv:
            return (1, b"", b"failure") if self.export_fail else (0, self.payload, b"")
        if argv[0] == "rm":
            if self.remove_fail:
                return 1, b"", b"failure"
            self.data = None
            return 0, b"", b""
        raise AssertionError(f"unexpected action {argv[0]}")


def fixture(root: Path) -> tuple[ProductConfig, BoundedWorkspace, Engine]:
    """创建实际源与私有副本、未配对调用及持久预算，避免恢复测试跳过会话链。"""
    source = root / "source"
    source.mkdir()
    (source / "a").write_text("old")
    state = root / "state"
    with SessionStore(state, "session-test") as s:
        s.check(source, "docker:False:bounded-copy")
        s.begin(
            "run-test",
            (
                Message(role="user", content="keep"),
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(call_id="write", name="shell", arguments={"command": "unknown"}),
                    ),
                ),
            ),
            source,
        )
        s.commit(
            "budget",
            {},
            request_attempts=3,
            provider_budget={"pending": {"attempt-unknown": {"input": 11, "output": 13}}},
        )
        directory = s.directory / "task-test/run-test"
        owned = BoundedWorkspace(source, directory, "run-test", 1024**2)
        owned.prepare()
        info = owned.workspace.stat()
        record: dict[str, Any] = {
            "version": 1,
            "backend_id": "docker-" + "a" * 32,
            "name": "codepilot-" + "b" * 32,
            "owner": f"codepilot-uid-{os.getuid()}",
            "identity": [info.st_dev, info.st_ino],
            "workspace_bytes": 1024**2,
            "memory_bytes": 256 * 1024**2,
            "image_id": "sha256:" + "c" * 64,
            "phase": "active",
            "container_id": "d" * 64,
            "main_pid": 7,
            "seeded": True,
        }
        initial = seed_archive(owned.workspace, owned.limit)
        (directory / "bounded-latest.tar").write_bytes(initial)
        record["checkpoint_sha256"] = hashlib.sha256(initial).hexdigest()
        blobs = directory / "bounded-checkpoints"
        blobs.mkdir()
        (blobs / (record["checkpoint_sha256"] + ".tar")).write_bytes(initial)
        path = directory / "backend/bounded-container.json"
        path.parent.mkdir()
        path.write_text(json.dumps(record))
        s.resource("container", {"id": record["container_id"], "run_id": "run-test"})
    live = root / "live"
    live.mkdir()
    (live / "a").write_text("partial")
    return (
        ProductConfig(state_directory=state),
        owned,
        Engine(record, seed_archive(live, owned.limit)),
    )


def test_preview_then_recover_reconcile_deliver_and_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未知调用和预算保留；恢复只保存，显式协调/交付才写源；重复恢复不逆转 applied。"""
    import codepilot.workspace.recovery as module
    from codepilot.app.operations import changes

    config, owned, engine = fixture(tmp_path)
    assert config.state_directory is not None
    monkeypatch.setattr(module, "_run_docker", engine)
    preview = asyncio.run(recover(config, owned.source, "session-test", "run-test"))
    assert preview["preview"] and len(engine.calls) == 2
    with SessionStore(config.state_directory, "session-test", create=False) as s:
        with pytest.raises(ValueError, match="recover"):
            s.reconcile(owned.source)
    result = asyncio.run(recover(config, owned.source, "session-test", "run-test", apply=True))
    assert result["recovered"] and result["tools_replayed"] == 0 and not result["source_modified"]
    assert (owned.source / "a").read_text() == "old"
    with SessionStore(config.state_directory, "session-test", create=False) as s:
        assert s.get("request_attempts") == 3 and s.get("provider_budget")["pending"]
        assert s.get("status") == "needs_reconciliation" and not s.get("resources")
        s.reconcile(owned.source)
        final = s.history()[-1].tool_result
        assert final is not None and final.is_error
        with pytest.raises(ValueError, match="retains bounded"):
            s.ensure_deletable()
    assert changes(config, owned.source, "session-test", "run-test", apply=True, deliver=True)[
        "applied"
    ]
    repeat = asyncio.run(recover(config, owned.source, "session-test", "run-test", apply=True))
    assert repeat["already_closed"] and repeat["artifact_state"] == "applied"
    assert (owned.source / "a").read_text() == "partial"


@pytest.mark.parametrize("field,value", [("owner", "foreign"), ("backend_id", "wrong")])
def test_wrong_container_binding_never_signals(tmp_path: Path, field: str, value: str) -> None:
    """相似容器不是归属证明，错误标签在任何导出或删除之前拒绝。"""
    _, owned, engine = fixture(tmp_path)
    assert engine.data is not None
    engine.data["Config"]["Labels"]["codepilot." + field] = value
    with pytest.raises(ValueError, match="identity"):
        asyncio.run(recover_workspace(owned, apply=True, runner=engine))
    assert len(engine.calls) == 2


@pytest.mark.parametrize("failure", ["export", "remove", "daemon"])
def test_failures_keep_recoverable_state_and_source(tmp_path: Path, failure: str) -> None:
    """导出/删除/连接故障保留容器和源版本，恢复通道后可安全继续而非重放工具。"""
    _, owned, engine = fixture(tmp_path)
    engine.export_fail = failure == "export"
    engine.remove_fail = failure == "remove"
    engine.unavailable = failure == "daemon"
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(recover_workspace(owned, apply=True, runner=engine))
    assert engine.data and (owned.source / "a").read_text() == "old"
    engine.export_fail = engine.remove_fail = engine.unavailable = False
    assert asyncio.run(recover_workspace(owned, apply=True, runner=engine))["recovered"]


@pytest.mark.parametrize("missing", [False, True])
def test_stopped_or_missing_uses_known_checkpoint_and_reports_loss(
    tmp_path: Path, missing: bool
) -> None:
    """活盘丢失只用已核验旧检查点，不能把旧数据称为最新成功产物。"""
    _, owned, engine = fixture(tmp_path)
    if missing:
        engine.data = None
    else:
        assert engine.data is not None
        engine.data["State"]["Running"] = False
    result = asyncio.run(recover_workspace(owned, apply=True, runner=engine))
    assert result["data_loss_possible"] and result["checkpoint"] == "last_known"
    assert (owned.workspace / "a").read_text() == "old"
    assert not any(c[0] == "exec" for c in engine.calls)


def test_corrupt_checkpoint_keeps_stopped_resource(tmp_path: Path) -> None:
    """旧归档损坏不能靠回退到基线伪造恢复成功，仍保留资源和 unknown。"""
    _, owned, engine = fixture(tmp_path)
    assert engine.data is not None
    engine.data["State"]["Running"] = False
    (
        owned.directory / "bounded-checkpoints" / (engine.record["checkpoint_sha256"] + ".tar")
    ).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        asyncio.run(recover_workspace(owned, apply=True, runner=engine))
    assert engine.data and not any(c[0] == "rm" for c in engine.calls)


def test_lock_and_unknown_source_transfer_refuse_recovery(tmp_path: Path) -> None:
    """不能与活跃控制器抢会话，也不能借恢复重放半完成的源目录交付。"""
    config, owned, engine = fixture(tmp_path)
    assert config.state_directory is not None
    with SessionStore(config.state_directory, "session-test", create=False):
        with pytest.raises(ValueError, match="in use"):
            asyncio.run(recover(config, owned.source, "session-test", "run-test", apply=True))
    owned.record["state"] = "applying"
    owned.save()
    with pytest.raises(ValueError, match="transfer outcome unknown"):
        asyncio.run(recover_workspace(owned, apply=True, runner=engine))
    assert not engine.calls


def test_uncommitted_latest_archive_does_not_destroy_old_checkpoint(tmp_path: Path) -> None:
    """新归档写完但指针未提交时崩溃，恢复仍可使用旧哈希指向的正文。"""
    _, owned, engine = fixture(tmp_path)
    assert engine.data is not None
    engine.data["State"]["Running"] = False
    (owned.directory / "bounded-latest.tar").write_bytes(b"new incomplete archive")
    result = asyncio.run(recover_workspace(owned, apply=True, runner=engine))
    assert result["recovered"] and result["data_loss_possible"]
    assert (owned.workspace / "a").read_text() == "old"


def test_unknown_pending_create_is_not_marked_closed(tmp_path: Path) -> None:
    """daemon 可能稍后接受创建，一次查无不能授权清掉创建绑定。"""
    _, owned, engine = fixture(tmp_path)
    path = owned.directory / "backend/bounded-container.json"
    engine.record.update(phase="creating", container_id="", seeded=False, main_pid=0)
    path.write_text(json.dumps(engine.record))
    engine.data = None
    with pytest.raises(ValueError, match="creation outcome unknown"):
        asyncio.run(recover_workspace(owned, apply=True, runner=engine))
    assert json.loads(path.read_text())["phase"] == "creating"
    assert not any(call[0] == "rm" for call in engine.calls)


def test_create_timeout_keeps_binding_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建超时取消客户端后保留未知绑定，关闭不能伪称没产生资源，更不能重发。"""
    import codepilot.workspace.docker as module
    from codepilot.workspace import BackendLimits, DockerExecutionBackend

    root = tmp_path / "root"
    root.mkdir()
    calls = []

    async def runner(
        argv: tuple[str, ...], data: bytes | None, limit: int
    ) -> tuple[int, bytes, bytes]:
        """只让 create 卡住，其他元数据响应确定，精确定位控制面期限。"""
        calls.append(argv[0])
        if argv[0] == "ps":
            return 0, b"", b""
        if argv[0] == "image":
            return 0, ("sha256:" + "a" * 64).encode(), b""
        if argv[0] == "info":
            return 0, b'["name=rootless"]', b""
        if argv[0] == "create":
            await asyncio.Event().wait()
        raise AssertionError("unexpected lifecycle operation")

    async def scenario() -> None:
        """缩短常量而不模拟超时异常，真实验证 asyncio 取消及清单状态。"""
        monkeypatch.setattr(module, "CONTROL_TIMEOUT_SECONDS", 0.01)
        backend = DockerExecutionBackend(
            root,
            "image",
            workspace_mode="bounded-copy",
            limits=BackendLimits(workspace_bytes=1048576),
            audit_directory=tmp_path / "backend",
            runner=runner,
        )
        with pytest.raises(TimeoutError):
            await backend.start()
        with pytest.raises(RuntimeError, match="creation outcome unknown"):
            await backend.close()
        record = json.loads((tmp_path / "backend/bounded-container.json").read_text())
        assert record["phase"] == "creating" and record["container_id"] == ""
        assert calls.count("create") == 1 and "rm" not in calls

    asyncio.run(scenario())
