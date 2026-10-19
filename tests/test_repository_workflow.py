"""真实仓库分页、版本前置条件、批次失败、限容副本交付和归档边界。"""

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from codepilot.contracts import ToolCall
from codepilot.tools import create_basic_registry
from codepilot.workspace.archive import pack, unpack
from codepilot.workspace.bounded import BoundedWorkspace
from codepilot.workspace.changes import ChangeJournal, review


def call(name: str, **arguments: object) -> ToolCall:
    """构造独立调用，避免测试把状态直接写成工具完成。"""
    return ToolCall.model_validate({"call_id": name, "name": name, "arguments": arguments})


def test_pagination_huge_line_utf8_and_version_bound_cursor(tmp_path: Path) -> None:
    """巨大单行分页不能丢字节，UTF-8 边界游标与完整哈希在每页保持一致。"""
    text = "你好" * 20000 + "\r\nlast\n"
    (tmp_path / "large").write_bytes(text.encode())
    registry = create_basic_registry(tmp_path)
    parts = []
    args: dict[str, object] = {"path": "large", "mode": "page", "max_bytes": 65535}
    while True:
        result = asyncio.run(registry.execute(call("read_file", **args)))
        assert not result.is_error, result.content
        page = json.loads(result.content)
        assert page["total_lines"] == 2
        assert page["sha256"] == hashlib.sha256(text.encode()).hexdigest()
        parts.append(page["content"])
        if page["next_cursor"] is None:
            break
        args.update(page["next_cursor"])
    assert "".join(parts) == text
    (tmp_path / "large").write_text("user changed")
    conflict = asyncio.run(registry.execute(call("read_file", **args)))
    assert conflict.is_error and "conflict" in conflict.content


def test_page_line_window_binary_and_invalid_cursor(tmp_path: Path) -> None:
    """行窗口、文件尾、二进制、游标越界与字符中间位置都明确处理。"""
    registry = create_basic_registry(tmp_path)
    (tmp_path / "a").write_text("one\ntwo\nthree")
    result = asyncio.run(
        registry.execute(call("read_file", path="a", mode="page", start_line=2, line_count=1))
    )
    page = json.loads(result.content)
    assert (
        page["content"] == "two\n"
        and page["total_lines"] == 3
        and page["next_cursor"]["offset"] == 8
    )
    (tmp_path / "a").write_bytes("中\n".encode())
    bad = asyncio.run(registry.execute(call("read_file", path="a", mode="page", offset=1)))
    assert bad.is_error and "invalid_cursor" in bad.content
    (tmp_path / "a").write_bytes(b"a\0b")
    assert asyncio.run(registry.execute(call("read_file", path="a", mode="page"))).is_error


def test_production_requires_read_and_refuses_stale_read(tmp_path: Path) -> None:
    """生产写入只能使用真实读取版本或明确前置哈希，不能临时取最新值覆盖用户。"""
    (tmp_path / "a").write_text("old")
    journal = ChangeJournal(
        create_basic_registry(tmp_path),
        tmp_path,
        tmp_path.parent / "state",
        "run-test",
        require_read=True,
    )
    denied = asyncio.run(journal.execute(call("write_file", path="a", content="agent")))
    assert denied.is_error and "read_required" in denied.content
    read = asyncio.run(journal.execute(call("read_file", path="a")))
    assert json.loads(read.content)["content"] == "old"
    (tmp_path / "a").write_text("user")
    conflict = asyncio.run(journal.execute(call("write_file", path="a", content="agent")))
    assert conflict.is_error and "conflict" in conflict.content
    assert (tmp_path / "a").read_text() == "user"


def test_batch_preflight_and_partial_failure_remain_reviewable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二个歧义先拒绝整批；实际第二个写入故障则只认领第一个可确认结果。"""
    import codepilot.tools.edit_file as module

    for name in ("a", "b"):
        (tmp_path / name).write_text("old")
    registry = create_basic_registry(tmp_path)
    bad = call(
        "edit_file",
        path="a",
        old_text="old",
        new_text="new",
        additional_edits=[{"path": "b", "old_text": "missing", "new_text": "new"}],
    )
    assert asyncio.run(registry.execute(bad)).is_error
    assert (tmp_path / "a").read_text() == "old"
    from codepilot.tools.file_common import _atomic_write as original

    def fail_second(path: Path, data: bytes) -> None:
        """首文件使用真实原子写入，第二个模拟磁盘故障。"""
        if path.name == "b":
            raise OSError("disk full")
        original(path, data)

    monkeypatch.setattr(module, "_atomic_write", fail_second)
    journal = ChangeJournal(registry, tmp_path, tmp_path.parent / "partial", "run-partial")
    result = asyncio.run(
        journal.execute(
            call(
                "edit_file",
                path="a",
                old_text="old",
                new_text="new",
                additional_edits=[{"path": "b", "old_text": "old", "new_text": "new"}],
            )
        )
    )
    assert result.is_error and json.loads(result.content)["applied"] == ["a"]
    assert (tmp_path / "a").read_text() == "new" and (tmp_path / "b").read_text() == "old"
    journal.finish()
    assert review(journal.path, tmp_path, apply=True)["applied"]
    assert (tmp_path / "a").read_text() == "old"


def test_search_respects_ignore_and_reports_coverage(tmp_path: Path) -> None:
    """忽略文件、隐藏选择、上下文与跳过范围不被搜索结果掩盖。"""
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("needle")
    (tmp_path / ".hidden").write_text("needle")
    (tmp_path / "normal.txt").write_text("before\nneedle\nafter\n")
    registry = create_basic_registry(tmp_path)
    result = asyncio.run(registry.execute(call("search", query="needle", context_lines=1)))
    assert not result.is_error
    assert "ignored.txt:" not in result.content and ".hidden:" not in result.content
    assert "normal.txt-1-before" in result.content and "normal.txt-3-after" in result.content
    all_files = asyncio.run(
        registry.execute(call("search", query="needle", include_hidden=True, include_ignored=True))
    )
    assert "ignored.txt:1:needle" in all_files.content and ".hidden:1:needle" in all_files.content
    limited = asyncio.run(registry.execute(call("search", query="needle", max_scan_bytes=1)))
    assert '"truncated": true' in limited.content and "scan_budget" in limited.content


def test_archive_rejects_links_traversal_and_oversized_payload(tmp_path: Path) -> None:
    """不可信容器归档不能用链接、父路径或假大小逃出产物目录。"""
    for name, kind in [
        ("../outside", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("a", tarfile.LNKTYPE),
    ]:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.linkname = "/tmp/outside"
            archive.addfile(item)
        with pytest.raises(ValueError):
            unpack(buffer.getvalue(), tmp_path, 1024)
    (tmp_path / "source").mkdir()
    (tmp_path / "source/data").write_bytes(b"x" * 100)
    buffer = io.BytesIO()
    pack(tmp_path / "source", buffer, 100)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="limit"):
        unpack(buffer.getvalue(), target, 99)


def test_bounded_source_conflict_then_binary_delivery_and_undo(tmp_path: Path) -> None:
    """限容副本保存原始脏字节，源变化拒绝整批，交付与撤销都核对实际版本。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"original\r\n")
    (source / "note").write_text("keep")
    (source / ".env").write_text("synthetic-private")
    owned = BoundedWorkspace(source, tmp_path / "state", "run-copy", 1024**2)
    workspace = owned.prepare()
    assert not (workspace / ".env").exists()
    (workspace / "a").write_bytes(b"\0binary\xff")
    (workspace / "new").write_text("new")
    (source / "a").write_text("user later")
    result = owned.finish(apply=True)
    assert result["conflicts"] == ["a"] and not (source / "new").exists()
    (source / "a").write_bytes(b"original\r\n")
    assert owned.transfer(undo=False)["applied"]
    assert (source / "a").read_bytes() == b"\0binary\xff"
    assert owned.transfer(undo=True)["applied"]
    assert (source / "a").read_bytes() == b"original\r\n" and not (source / "new").exists()
    assert (source / "note").read_text() == "keep" and (
        source / ".env"
    ).read_text() == "synthetic-private"


def test_corrupt_bounded_artifact_never_touches_source(tmp_path: Path) -> None:
    """产物在交付前被修改或批次未收尾时，不能自动重放原来的授权。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_text("old")
    owned = BoundedWorkspace(source, tmp_path / "state", "run-copy", 1024**2)
    root = owned.prepare()
    (root / "a").write_text("new")
    owned.finish(apply=False)
    (root / "a").write_text("corrupt")
    with pytest.raises(ValueError, match="corrupted"):
        owned.transfer(undo=False)
    assert (source / "a").read_text() == "old"
    owned.record["state"] = "applying"
    owned.save()
    reopened = BoundedWorkspace.open(owned.path, source)
    with pytest.raises(ValueError, match="transferable"):
        reopened.transfer(undo=False)


def test_long_search_match_has_explicit_excerpt(tmp_path: Path) -> None:
    """命中超长行尾部时提供命中片段，并标明未返回整行。"""
    (tmp_path / "a").write_text("x" * 2000 + "needle" + "z" * 1000)
    result = asyncio.run(create_basic_registry(tmp_path).execute(call("search", query="needle")))
    assert not result.is_error and "needle" in result.content
    assert "[line truncated]" in result.content
    assert json.loads(result.content.splitlines()[-1])["shortened_lines"] == 1


def test_bounded_partial_delivery_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二个源写入失败保留第一文件的真实进度，重开不得自动继续交付。"""
    import codepilot.workspace.bounded as module
    from codepilot.tools.file_common import _atomic_write as original

    source = tmp_path / "source"
    source.mkdir()
    for name in ("a", "b"):
        (source / name).write_text("old")
    owned = BoundedWorkspace(source, tmp_path / "run", "partial", 1024**2)
    root = owned.prepare()
    for name in ("a", "b"):
        (root / name).write_text("new")
    owned.finish(apply=False)

    def fail_second(path: Path, data: bytes, *, expected_absent: bool = False) -> None:
        """只在实际第二个源文件写入注入故障，清单保存仍使用真实原子写。"""
        if path == source / "b":
            raise OSError("synthetic disk full")
        original(path, data, expected_absent=expected_absent)

    monkeypatch.setattr(module, "_atomic_write", fail_second)
    with pytest.raises(OSError):
        owned.transfer(undo=False)
    assert (source / "a").read_text() == "new" and (source / "b").read_text() == "old"
    reopened = BoundedWorkspace.open(owned.path, source)
    assert reopened.record["state"] == "applying" and reopened.record["applied_paths"] == ["a"]
    with pytest.raises(ValueError, match="transferable"):
        reopened.transfer(undo=False)


def test_bounded_budget_rejected_before_start(tmp_path: Path) -> None:
    """私有产物预算不足时在任何模型请求和容器创建之前拒绝。"""
    from codepilot.app import CodePilotApplication
    from codepilot.config import BackendConfig, ProductConfig, SessionConfig
    from codepilot.workspace import BackendLimits

    source = tmp_path / "source"
    source.mkdir()
    config = ProductConfig(
        state_directory=tmp_path / "state",
        sessions=SessionConfig(workspace_artifact_bytes=1024**2),
        backend=BackendConfig(
            kind="docker",
            docker_image="unused",
            workspace_mode="bounded-copy",
            limits=BackendLimits(workspace_bytes=1024**2),
        ),
    )
    with pytest.raises(ValueError, match="artifact budget"):
        asyncio.run(CodePilotApplication(config, source).run("no resources may start"))


def test_create_registration_failure_still_removes_owned_container(tmp_path: Path) -> None:
    """所有权持久化失败仍删除刚创建的容器，删除失败则保留身份供复核。"""
    from test_workspace_backends import FakeDockerRunner

    from codepilot.workspace import DockerExecutionBackend

    def unavailable(container: str) -> None:
        """模拟控制器数据库写失败，不能把容器启动继续当作安全成功。"""
        raise OSError("synthetic ledger failure")

    async def scenario() -> None:
        """使用无真实资源的确定性 Docker 故障响应验证创建后清理出口。"""
        runner = FakeDockerRunner()
        backend = DockerExecutionBackend(
            tmp_path, "unused", runner=runner, on_container=unavailable
        )
        with pytest.raises(OSError):
            await backend.start()
        assert any(argv[:3] == ("rm", "-f", "-v") for argv, _ in runner.calls)
        assert not backend._container_id and not any(
            e.kind == "backend_started" for e in backend.events
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("malformed", [[], {"version": 1, "limit": 1048576, "state": []}])
def test_bounded_corrupt_record_is_diagnostic(tmp_path: Path, malformed: object) -> None:
    """损坏或类型漂移的清单必须稳定拒绝，不能产生未处理类型错误。"""
    path = tmp_path / "bounded-workspace.json"
    path.write_text(json.dumps(malformed))
    with pytest.raises(ValueError, match="invalid bounded"):
        BoundedWorkspace.open(path, tmp_path)


def test_stale_cleanup_preserves_bounded_tmpfs(tmp_path: Path) -> None:
    """即使超过 TTL 也不能静默删除尚无导出证明的限容任务盘。"""
    from codepilot.workspace import DockerExecutionBackend

    removed = []

    async def runner(
        argv: tuple[str, ...], stdin: bytes | None, limit: int
    ) -> tuple[int, bytes, bytes]:
        """给出旧直写与限容副本两条过期记录，只允许删除直写容器。"""
        if argv[0] == "ps":
            return 0, b"legacy 0\nbind-id 0 bind\nbounded-id 0 bounded-copy\n", b""
        removed.append(argv[-1])
        return 0, b"", b""

    assert asyncio.run(DockerExecutionBackend.cleanup_stale(runner, "owned")) == (
        "legacy",
        "bind-id",
    )
    assert removed == ["legacy", "bind-id"]


def test_resources_prune_cannot_discard_unsaved_tmpfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式资源回收也不能静默删除可能包含唯一产物的限容容器。"""
    from typing import Any

    import codepilot.app.operations as operations
    from codepilot.config import ProductConfig
    from codepilot.sessions.store import SessionStore

    state = tmp_path / "state"
    container = "a" * 64
    with SessionStore(state, "owned") as stored:
        stored.resource("container", {"id": container})
    calls = []

    async def command(argv: tuple[str, ...], root: Path, timeout: float = 30) -> dict[str, Any]:
        """只提供真实 inspect 结构，任何删除命令都会使断言失败。"""
        calls.append(argv)
        return {
            "exit_code": 0,
            "stdout": json.dumps(
                [
                    {
                        "Id": container,
                        "Config": {"Labels": {"codepilot.workspace_mode": "bounded-copy"}},
                    }
                ]
            ),
        }

    monkeypatch.setattr(operations, "command", command)
    result = asyncio.run(
        operations.prune(ProductConfig(state_directory=state), "owned", apply=True)
    )
    entry = result["resources"][0]
    assert entry["reason"] == "bounded_workspace_requires_export" and not entry["removed"]
    assert len(calls) == 1 and calls[0][1] == "inspect"


def test_sixteen_small_edits_fit_real_journal_quota(tmp_path: Path) -> None:
    """十六个小文件的实际编码可容纳时必须成功，不能按每文件最坏上限误拒。"""
    root = tmp_path / "workspace"
    root.mkdir()
    journal = ChangeJournal(
        create_basic_registry(root), root, tmp_path / "state", "run-sixteen", require_read=True
    )
    edits = []
    for index in range(16):
        name = f"{index}.py"
        (root / name).write_text("old")
        assert not asyncio.run(journal.execute(call("read_file", path=name))).is_error
        edits.append({"path": name, "old_text": "old", "new_text": "new"})
    result = asyncio.run(journal.execute(call("edit_file", **edits[0], additional_edits=edits[1:])))
    assert not result.is_error, result.content
    assert len(json.loads(result.content)["applied"]) == 16
    assert all(entry["state"] == "confirmed" for entry in journal.record["entries"])
    journal.finish()
    assert review(journal.path, root, apply=True)["applied"]
    assert all((root / f"{i}.py").read_text() == "old" for i in range(16))
