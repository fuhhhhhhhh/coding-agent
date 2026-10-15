"""验收截断前输出捕获、跨进程索引、配额与真实子进程失败清理。"""

import asyncio
import hashlib
import json
import shlex
import sys
from pathlib import Path

import pytest

from codepilot.context.artifacts import ContextArtifactStore
from codepilot.contracts import ToolCall
from codepilot.sessions.outputs import CURRENT_OUTPUT, OutputStore
from codepilot.tools import create_basic_registry
from codepilot.workspace import BackendLimits, LocalExecutionBackend


def test_large_interleaved_output_preserves_middle_and_hashes(tmp_path: Path) -> None:
    """两路合计超过 100 MiB，预览有界而中段原文和逐路哈希可独立核验。"""

    async def scenario() -> None:
        """用真实 argv 产生二进制分块输出并检查持久化字节。"""
        output = OutputStore(tmp_path / "outputs")
        token = CURRENT_OUTPUT.set(output)
        script = (
            "import os; b=b'x'*65536; "
            "[(os.write(1,b),os.write(2,b)) for _ in range(420)]; "
            "os.write(1,b'MIDDLE_MARKER'); "
            "[(os.write(1,b),os.write(2,b)) for _ in range(420)]"
        )
        try:
            async with LocalExecutionBackend(
                tmp_path, limits=BackendLimits(output_bytes=1024)
            ) as backend:
                result = await backend.run_argv((sys.executable, "-c", script), timeout_seconds=20)
        finally:
            CURRENT_OUTPUT.reset(token)
        assert result.exit_code == 0 and result.stdout_truncated and result.stderr_truncated
        assert len(result.stdout) == 1024
        index = next((tmp_path / "outputs").glob("*.json"))
        metadata = json.loads(index.read_bytes())
        assert metadata["state"] == "complete"
        for stream in ("stdout", "stderr"):
            digest = hashlib.sha256()
            with (tmp_path / "outputs" / f"{metadata['output_id']}.{stream}.bin").open(
                "rb"
            ) as source:
                while chunk := source.read(65536):
                    digest.update(chunk)
            assert digest.hexdigest() == metadata["sha256"][stream]
        reopened = OutputStore(tmp_path / "outputs")
        assert str(420 * 65536) in reopened.search(metadata["output_id"], "stdout", "MIDDLE_MARKER")
        assert "MIDDLE_MARKER" in reopened.read(metadata["output_id"], "stdout", 420 * 65536, 20)
        assert metadata["saved"]["stdout"] + metadata["saved"]["stderr"] > 100 * 1024**2

    asyncio.run(scenario())


def test_quota_stops_process_and_keeps_partial_index(tmp_path: Path) -> None:
    """配额耗尽立即终止持续输出命令，保存量不越界且不可报告完整。"""

    async def scenario() -> None:
        """通过 Shell 正式注册入口测试额度和进程收敛。"""
        store = OutputStore(tmp_path / "outputs", command_bytes=16384)
        token = CURRENT_OUTPUT.set(store)
        registry = create_basic_registry(tmp_path)
        command = (
            shlex.quote(sys.executable)
            + " -c "
            + shlex.quote("import os;\nwhile True: os.write(1,b'x'*8192)")
        )
        try:
            async with asyncio.timeout(5):
                with pytest.raises(OSError):
                    await registry.execute(
                        ToolCall(call_id="quota", name="shell", arguments={"command": command})
                    )
        finally:
            CURRENT_OUTPUT.reset(token)
        record = json.loads(next((tmp_path / "outputs").glob("*.json")).read_bytes())
        assert record["state"] == "quota_exceeded"
        assert sum(record["saved"].values()) == 16384
        assert sum(record["observed"].values()) > 16384

    asyncio.run(scenario())


def test_cancel_output_is_partial_and_old_context_artifacts_reopen(tmp_path: Path) -> None:
    """取消保持 partial；Context 引用跨 store 重开并支持有界字符读取。"""

    async def scenario() -> None:
        """启动真实写输出进程，等待索引建立后取消并验证关闭。"""
        store = OutputStore(tmp_path / "outputs")
        token = CURRENT_OUTPUT.set(store)
        try:
            async with LocalExecutionBackend(tmp_path) as backend:
                task = asyncio.create_task(
                    backend.run_argv(
                        (
                            sys.executable,
                            "-c",
                            "import time; print('ready',flush=True); time.sleep(60)",
                        )
                    )
                )
                while not list(store.directory.glob("*.bin")):
                    await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            CURRENT_OUTPUT.reset(token)
        record = json.loads(next(store.directory.glob("*.json")).read_bytes())
        assert record["state"] == "partial"

    asyncio.run(scenario())
    artifacts = ContextArtifactStore(tmp_path / "artifacts", persistent=True)
    reference = artifacts.write("call", "中文前缀" * 10000 + "middle")
    reopened = ContextArtifactStore(tmp_path / "artifacts", persistent=True)
    assert reopened.read(reference.artifact_id, 40000, 6).content == "middle"
    with pytest.raises(ValueError):
        OutputStore(tmp_path / "outputs").read("../../escape", "stdout", 0, 10)


def test_disk_failure_stops_command_and_preserves_failure_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """捕获中途磁盘故障不能令持续输出命令遗留，也不能发布 complete。"""
    from codepilot.sessions.outputs import FileCapture

    original = FileCapture.write

    def fail_write(self: FileCapture, stream: str, data: bytes) -> None:
        """先保存一块再模拟 ENOSPC，验证已经生成的部分证据仍可读取。"""
        if sum(self.saved.values()) >= 8192:
            raise OSError(28, "synthetic disk full")
        original(self, stream, data)

    monkeypatch.setattr(FileCapture, "write", fail_write)

    async def scenario() -> None:
        """真实命令持续输出，在存储失败后五秒内收敛。"""
        output = OutputStore(tmp_path / "outputs")
        token = CURRENT_OUTPUT.set(output)
        try:
            async with LocalExecutionBackend(tmp_path) as backend:
                async with asyncio.timeout(5):
                    with pytest.raises(OSError):
                        await backend.run_argv(
                            (sys.executable, "-c", "import os;\nwhile True: os.write(1,b'x'*8192)")
                        )
        finally:
            CURRENT_OUTPUT.reset(token)
        record = json.loads(next(output.directory.glob("*.json")).read_bytes())
        assert record["state"] == "partial" and record["saved"]["stdout"] == 8192

    asyncio.run(scenario())


def test_corrupted_complete_output_is_not_returned_as_trusted_evidence(tmp_path: Path) -> None:
    """完成后的字节被外部篡改时拒绝读取，不能继续沿用旧哈希作证。"""
    output = OutputStore(tmp_path / "outputs")
    capture = output.begin("integrity")
    capture.write("stdout", b"original")
    capture.finish("complete")
    path = output.directory / f"{capture.output_id}.stdout.bin"
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="integrity"):
        output.read(capture.output_id, "stdout", 0, 100)


def test_capture_creation_failure_prevents_process_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """输出捕获无法建立时命令不能先启动，避免写入后才发现没有日志归属。"""
    from codepilot.sessions.outputs import FileCapture

    def broken_begin(self: OutputStore, call_id: str) -> FileCapture:
        """在进程创建前模拟索引写盘失败。"""
        raise OSError(28, "synthetic disk full")

    monkeypatch.setattr(OutputStore, "begin", broken_begin)

    async def scenario() -> None:
        """被拒绝的命令不创建标记文件。"""
        token = CURRENT_OUTPUT.set(OutputStore(tmp_path / "outputs"))
        try:
            async with LocalExecutionBackend(tmp_path) as backend:
                with pytest.raises(OSError):
                    await backend.run_argv(
                        (sys.executable, "-c", "open('must-not-exist','w').write('bad')")
                    )
        finally:
            CURRENT_OUTPUT.reset(token)
        assert not (tmp_path / "must-not-exist").exists()

    asyncio.run(scenario())
