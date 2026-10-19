"""真实 Rootless Docker 强制 tmpfs 配额、源版本交付、取消产物与文件工具批次。"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from codepilot.app import CodePilotApplication
from codepilot.app.operations import changes
from codepilot.config import BackendConfig, ProductConfig
from codepilot.contracts import ToolCall
from codepilot.permissions import ScriptedApproval
from codepilot.workspace import BackendLimits, DockerExecutionBackend
from codepilot.workspace.bounded import BoundedWorkspace

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="需要显式 Rootless Docker"
)
IMAGE = os.environ.get("CODEPILOT_TEST_IMAGE", "codepilot-sandbox:0.1.0")


def config(root: Path) -> ProductConfig:
    """提供真实 8MiB 限容副本，控制器私有目录位于源目录之外。"""
    return ProductConfig(
        state_directory=root / "state",
        permission="controlled-edit",
        backend=BackendConfig(
            kind="docker",
            docker_image=IMAGE,
            workspace_mode="bounded-copy",
            limits=BackendLimits(workspace_bytes=8 * 1024**2),
        ),
    )


def test_product_hard_quota_delivery_and_source_undo(tmp_path: Path) -> None:
    """实际写盘触发 ENOSPC，移除填充物后交付两文件，再有条件恢复原始脏内容。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"user\r\n")
    (source / "notes").write_text("keep")
    (source / ".env").write_text("synthetic-credentials-stay-on-host")
    (source / ".git").write_text("gitdir: ../synthetic-outside-metadata\n")
    script = (
        "from pathlib import Path; import errno,os; "
        "assert not Path('.env').exists() and not Path('.git').exists(); "
        "s=os.statvfs('.'); assert s.f_blocks*s.f_frsize==8388608; "
        "f=open('fill','wb');\n"
        "try:\n for i in range(20): f.write(b'x'*1048576); f.flush()\n"
        "except OSError as e:\n assert e.errno==errno.ENOSPC; print('kernel_ENOSPC_confirmed')\n"
        "else: raise AssertionError('quota not enforced')\n"
        "finally: f.close(); Path('fill').unlink()\n"
        "Path('a').write_text('agent');Path('new').write_text('new')"
    )
    import shlex

    result = asyncio.run(
        CodePilotApplication(config(tmp_path), source, approval=ScriptedApproval([True])).run(
            "bounded command",
            direct_tool=ToolCall(
                call_id="quota",
                name="shell",
                arguments={"command": "python3 -c " + shlex.quote(script), "timeout_seconds": 30.0},
            ),
        )
    )
    assert result.resources.backend_closed and result.bounded_delivery
    assert result.bounded_delivery["state"] == "applied", result.bounded_delivery
    outputs = [m.tool_result for m in result.report.messages if m.tool_result]
    assert "kernel_ENOSPC_confirmed" in outputs[-1].content and not outputs[-1].is_error
    assert (source / "a").read_text() == "agent" and (source / "new").read_text() == "new"
    assert (source / ".git").read_text() == "gitdir: ../synthetic-outside-metadata\n"
    (source / "a").write_text("user later")
    identity = result.identity
    assert changes(config(tmp_path), source, identity.session_id, identity.run_id, apply=True)[
        "conflicts"
    ] == ["a"]
    (source / "a").write_text("agent")
    assert changes(config(tmp_path), source, identity.session_id, identity.run_id, apply=True)[
        "applied"
    ]
    assert (source / "a").read_bytes() == b"user\r\n" and not (source / "new").exists()
    assert (source / "notes").read_text() == "keep" and (
        source / ".env"
    ).read_text() == "synthetic-credentials-stay-on-host"


def test_cancel_exports_partial_tmpfs_before_removing_container(tmp_path: Path) -> None:
    """取消正在写入的真实容器，保留开始后的文件且不修改源目录。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_text("old")
    owned = BoundedWorkspace(source, tmp_path / "run", "run-cancel", 8 * 1024**2)
    root = owned.prepare()

    async def scenario() -> None:
        """以独立容器命令观察文件屏障后取消，避免依赖主机进程启动耗时。"""
        backend = DockerExecutionBackend(
            root,
            IMAGE,
            workspace_mode="bounded-copy",
            limits=BackendLimits(workspace_bytes=8 * 1024**2),
            audit_directory=tmp_path / "run/backend",
        )
        await backend.start()
        try:
            task = asyncio.create_task(
                backend.execute(
                    ToolCall(
                        call_id="writer",
                        name="shell",
                        arguments={
                            "command": "printf partial > started; sleep 60",
                            "timeout_seconds": 90.0,
                        },
                    )
                )
            )
            async with asyncio.timeout(20):
                while True:
                    code, _, _ = await backend._docker(
                        ("exec", backend._container_id, "test", "-f", "/workspace/started")
                    )
                    if code == 0:
                        break
                    await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await backend.close()
        assert (root / "started").read_text() == "partial"
        assert any(e.kind == "backend_closed" for e in backend.events)

    asyncio.run(scenario())
    report = owned.finish(apply=False)
    assert report["state"] == "retained" and not (source / "started").exists()
    assert (source / "a").read_text() == "old" and (tmp_path / "run/bounded-latest.tar").is_file()


def test_real_container_read_versions_and_multifile_tool(tmp_path: Path) -> None:
    """真实 worker 用分页建立十六个版本，再用原 edit_file 的批次接口修改并撤销。"""
    from codepilot.workspace.changes import ChangeJournal, review

    root = tmp_path / "root"
    root.mkdir()
    names = ["a", "b", *(f"file-{i}" for i in range(14))]
    for name in names:
        (root / name).write_text("old\n")

    async def scenario() -> None:
        """实际 Docker 后端与生产版本检查组合，不用 fake 执行替代 worker。"""
        async with DockerExecutionBackend(root, IMAGE) as backend:
            journal = ChangeJournal(
                backend, root, tmp_path / "state", "run-batch", require_read=True
            )
            for name in names:
                result = await journal.execute(
                    ToolCall(call_id="read-" + name, name="read_file", arguments={"path": name})
                )
                assert not result.is_error and json.loads(result.content)["sha256"]
            result = await journal.execute(
                ToolCall(
                    call_id="batch",
                    name="edit_file",
                    arguments={
                        "path": "a",
                        "old_text": "old",
                        "new_text": "first",
                        "additional_edits": [
                            {"path": name, "old_text": "old", "new_text": "second"}
                            for name in names[1:]
                        ],
                    },
                )
            )
            assert not result.is_error and json.loads(result.content)["applied"] == names
            journal.finish()
        assert review(journal.path, root, apply=True)["applied"]

    asyncio.run(scenario())
    assert all((root / name).read_text() == "old\n" for name in names)


def test_export_failure_retains_container_until_recovery(tmp_path: Path) -> None:
    """导出通道故障时保留真实 tmpfs 容器，恢复通道后才能导出并删除。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_text("old")
    owned = BoundedWorkspace(source, tmp_path / "run", "run-export-fail", 8 * 1024**2)
    root = owned.prepare()

    async def scenario() -> None:
        """仅拦截自有后端导出响应，独立 inspect 证明失败没有删除容器。"""
        backend = DockerExecutionBackend(
            root,
            IMAGE,
            workspace_mode="bounded-copy",
            limits=BackendLimits(workspace_bytes=8 * 1024**2),
        )
        await backend.start()
        original = backend._runner
        container = backend._container_id
        try:
            result = await backend.execute(
                ToolCall(call_id="write", name="shell", arguments={"command": "printf new > a"})
            )
            assert not result.is_error

            async def broken(
                argv: tuple[str, ...], stdin: bytes | None, limit: int
            ) -> tuple[int, bytes, bytes]:
                """模拟最后一次归档网络通道失效，其他真实 Docker 调用照常执行。"""
                if "export" in argv:
                    return 1, b"", b"synthetic transport failure"
                return await original(argv, stdin, limit)

            backend._runner = broken
            with pytest.raises(RuntimeError, match="export failed"):
                await backend.close()
            code, _, _ = await original(("inspect", container), None, 65536)
            assert code == 0 and not any(e.kind == "backend_closed" for e in backend.events)
            assert any(e.kind == "backend_close_failed" for e in backend.events)
            assert (source / "a").read_text() == "old"
        finally:
            # 只恢复本测试拥有的通道和容器，绝不操作其他用户的 Docker 资源。
            backend._runner = original
            await backend.close()
        assert (root / "a").read_text() == "new"
        code, _, _ = await original(("inspect", container), None, 65536)
        assert code != 0

    asyncio.run(scenario())
