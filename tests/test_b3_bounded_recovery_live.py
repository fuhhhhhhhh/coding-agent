"""真实杀死控制器后以新 CLI 恢复限容产物，独立核验源、归属、预算和容器。"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEPILOT_DOCKER_LIVE") != "1", reason="需要显式 Rootless Docker"
)
IMAGE = os.environ.get("CODEPILOT_TEST_IMAGE", "codepilot-sandbox:0.1.0")


def cli(root: Path, *args: str) -> dict[str, object]:
    """新进程正式 CLI，隔离用户配置以保证恢复不借真实密钥或模型完成。"""
    env = {**os.environ, "XDG_CONFIG_HOME": str(root / "config")}
    p = subprocess.run(
        [
            sys.executable,
            "-c",
            "from codepilot.ui.cli import main;raise SystemExit(main())",
            "--workspace",
            str(root / "source"),
            "--state-directory",
            str(root / "state"),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert p.returncode == 0, p.stderr
    result: dict[str, object] = json.loads(p.stdout)
    return result


def test_real_controller_kill_then_cli_recovery_and_guarded_delivery(tmp_path: Path) -> None:
    """Shell 实际写入后杀控制器，独立预览不暂停，恢复保存并删除后再显式协调交付。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_text("original")
    script = """import asyncio,sys
from pathlib import Path
from codepilot.app import CodePilotApplication
from codepilot.config import ProductConfig,BackendConfig
from codepilot.contracts import ToolCall
from codepilot.contracts.run import RunLimits
from codepilot.permissions import ScriptedApproval
from codepilot.workspace import BackendLimits
c=ProductConfig(
 state_directory=Path(sys.argv[2]),permission='controlled-edit',
 runtime=RunLimits(timeout_seconds=120.0),
 backend=BackendConfig(kind='docker',docker_image=sys.argv[3],
 workspace_mode='bounded-copy',limits=BackendLimits(workspace_bytes=8388608)))
asyncio.run(CodePilotApplication(
 c,Path(sys.argv[1]),approval=ScriptedApproval([True])).run(
 'crash test',session_id='session-crash',task_id='task-crash',run_id='run-crash',
 direct_tool=ToolCall(call_id='write',name='shell',arguments={
 'command':'printf partial > marker; sleep 90','timeout_seconds':100.0})))
"""
    binding = tmp_path / "state/session-crash/task-crash/run-crash/backend/bounded-container.json"
    container = ""
    with (tmp_path / "controller.log").open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(source), str(tmp_path / "state"), IMAGE],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 45
            # 以真实容器内文件屏障证明发生了副作用，而非依赖模型或固定等待。
            while time.monotonic() < deadline:
                assert child.poll() is None, (tmp_path / "controller.log").read_text()
                if binding.exists():
                    record = json.loads(binding.read_text())
                    container = record["container_id"]
                    if container and record["phase"] == "active":
                        p = subprocess.run(
                            ["docker", "exec", container, "test", "-f", "/workspace/marker"],
                            capture_output=True,
                            timeout=5,
                        )
                        if p.returncode == 0:
                            break
                time.sleep(0.1)
            else:
                raise AssertionError("owned writer did not reach file barrier")
            child.kill()
            child.wait(timeout=10)
            preview = cli(tmp_path, "recover", "session-crash", "run-crash")
            assert preview["preview"] and preview["container_running"] and not preview["recovered"]
            assert (
                subprocess.run(["docker", "inspect", container], capture_output=True).returncode
                == 0
            )
            result = cli(tmp_path, "recover", "session-crash", "run-crash", "--apply")
            assert (
                result["recovered"]
                and result["tools_replayed"] == 0
                and not result["source_modified"]
            )
            assert not result["data_loss_possible"]
            assert not (source / "marker").exists()
            assert (
                subprocess.run(["docker", "inspect", container], capture_output=True).returncode
                != 0
            )
            diagnose = cli(tmp_path, "sessions", "diagnose", "session-crash")
            assert diagnose["status"] == "needs_reconciliation" and not diagnose["automatic_replay"]
            p = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from codepilot.ui.cli import main;raise SystemExit(main())",
                    "--workspace",
                    str(source),
                    "--state-directory",
                    str(tmp_path / "state"),
                    "sessions",
                    "reconcile",
                    "session-crash",
                    "--accept-workspace",
                ],
                env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config")},
                capture_output=True,
                timeout=30,
            )
            assert p.returncode == 0, p.stderr
            delivery = cli(tmp_path, "delivery", "session-crash", "run-crash", "--apply")
            assert delivery["applied"] and (source / "marker").read_text() == "partial"
            assert (source / "a").read_text() == "original"
            repeat = cli(tmp_path, "recover", "session-crash", "run-crash", "--apply")
            assert repeat["already_closed"] and repeat["artifact_state"] == "applied"
        finally:
            # 只清理本测试持有的精确进程/容器；异常也不能留下睡眠 writer。
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            if container:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=15)


def test_real_stopped_tmpfs_reports_checkpoint_loss(tmp_path: Path) -> None:
    """实际停止活盘后只恢复已提交检查点，最后未捕获的写入不能被冒充保留。"""
    import asyncio

    from codepilot.contracts import Message, ToolCall
    from codepilot.sessions.store import SessionStore
    from codepilot.workspace import BackendLimits, DockerExecutionBackend
    from codepilot.workspace.bounded import BoundedWorkspace

    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_text("original")
    state = tmp_path / "state"
    container = ""

    async def scenario() -> None:
        """用正式后端完成检查点，再独立写入并停止唯一自有容器。"""
        nonlocal container
        with SessionStore(state, "session-stopped") as stored:
            stored.check(source, "docker:False:bounded-copy")
            stored.begin("run-stopped", (Message(role="user", content="checkpoint"),), source)
            owned = BoundedWorkspace(
                source, stored.directory / "task-stopped/run-stopped", "run-stopped", 8388608
            )
            root = owned.prepare()
            backend = DockerExecutionBackend(
                root,
                IMAGE,
                workspace_mode="bounded-copy",
                limits=BackendLimits(workspace_bytes=8388608),
                audit_directory=owned.directory / "backend",
                on_container=lambda cid: stored.resource("container", {"id": cid}),
            )
            await backend.start()
            container = backend._container_id
            result = await backend.execute(
                ToolCall(
                    call_id="checkpoint",
                    name="shell",
                    arguments={"command": "printf checkpoint > a"},
                )
            )
            assert not result.is_error
            code, _, _ = await backend._docker(("exec", container, "sh", "-c", "printf lost > a"))
            assert code == 0
            code, _, _ = await backend._docker(("kill", container))
            assert code == 0

    try:
        asyncio.run(scenario())
        result = cli(tmp_path, "recover", "session-stopped", "run-stopped", "--apply")
        assert (
            result["recovered"]
            and result["data_loss_possible"]
            and result["checkpoint"] == "last_known"
        )
        assert (source / "a").read_text() == "original"
        artifact = state / "session-stopped/task-stopped/run-stopped/bounded-workspace/a"
        assert artifact.read_text() == "checkpoint"
    finally:
        # tmpfs 已失活也只回收此测试的确切容器，不扫描或修改其他任务。
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=15)
