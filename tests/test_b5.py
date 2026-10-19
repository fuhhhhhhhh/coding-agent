"""验证正式评测、独立判据、无模型命令和资源维护的组合边界。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from codepilot.app import service
from codepilot.app.operations import diff, permissions, prune
from codepilot.app.operations import test_command as run_test_command
from codepilot.config import ProductConfig
from codepilot.contracts import Message, ToolCall
from codepilot.evals.product import ProductHarness, ProductTask, Seed, Suite
from codepilot.permissions import ScriptedApproval
from codepilot.providers.fake import FakeProvider
from codepilot.sessions.store import SessionStore, process_identity


def fixture(root: Path) -> tuple[ProductConfig, Suite]:
    """创建实际 Git commit 与工作区外隐藏材料，不用字符串代替 checkout。"""
    source = root / "source"
    source.mkdir()
    (source / "calc.py").write_text('def add(a, b):\n    """计算两数之和。"""\n    return a + b\n')
    (source / "public_test.py").write_text("from calc import add; assert add(2,3)==5\n")
    for argv in [
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
    ]:
        subprocess.run(argv, cwd=source, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    hidden = root / "hidden"
    hidden.mkdir()
    (hidden / "verify.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path.cwd()))\n"
        "from calc import add\nassert add(2,3)==5\nassert add(-3,4)==1\n"
    )
    task = ProductTask(
        task_id="repair",
        repository=str(source),
        commit=commit,
        description="修复加法",
        seeds=[Seed(path="calc.py", before="return a + b", after="return a - b")],
        dirty_files={"user-note": "keep"},
        allowed_paths=["calc.py"],
        validation_material=str(hidden),
        verification_command=[sys.executable, ".codepilot-validation/verify.py"],
        max_requests=4,
    )
    return ProductConfig(state_directory=root / "state"), Suite(
        name="test", repetitions=3, request_cap=12, required_successes=3, tasks=[task]
    )


def provider(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """只控制模型选出的候选，执行、权限、存储、验证均走正式实现。"""

    def factory(*args: Any, **kwargs: Any) -> service._ProviderHandle:
        """生成正常/越界/空候选，不能由模型直接控制独立 verdict。"""
        calls = []
        if mode != "empty":
            calls.append(
                ToolCall(
                    call_id="fix",
                    name="write_file",
                    arguments={
                        "path": "calc.py",
                        "content": 'def add(a,b):\n    """计算两数之和。"""\n    return a+b\n',
                    },
                )
            )
        if mode == "tamper":
            calls.append(
                ToolCall(
                    call_id="tamper",
                    name="write_file",
                    arguments={"path": "public_test.py", "content": "pass\n"},
                )
            )
        script = [Message(role="assistant", tool_calls=tuple(calls))] if calls else []
        script.append(Message(role="assistant", content="模型声称已完成"))
        return service._ProviderHandle(FakeProvider(tuple(script)))

    monkeypatch.setattr(service, "_create_provider", factory)


@pytest.mark.parametrize("mode,successes", [("repair", 3), ("empty", 0), ("tamper", 0)])
def test_product_trials_checkout_repeat_and_independent_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, successes: int
) -> None:
    """真实 checkout 和三次独立正式运行，空补丁/改测试不能被完成自述蒙混。"""
    config, suite = fixture(tmp_path)
    provider(monkeypatch, mode)
    output = tmp_path / "evaluation"
    result = asyncio.run(ProductHarness(config, output, approve_tools=True).run(suite))
    assert result["counts"]["success"] == successes
    assert result["completed_trials"] == 3
    assert result["release_ready"] is False
    assert all(r["runtime_outcome"] == "completed" for r in result["results"])
    assert all(r["backend_closed"] for r in result["results"])
    for index in range(1, 4):
        assert (output / f"repair-{index}/workspace/user-note").read_text() == "keep"
        assert not (output / f"repair-{index}/workspace/.codepilot-validation").exists()
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(ProductHarness(config, output).run(suite))


def test_version_seed_and_budget_mismatch_stop_before_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """固定版本/预算错误不能继续请求模型或混合计算试验。"""
    config, suite = fixture(tmp_path)
    with pytest.raises(ValueError, match="request cap"):
        Suite.model_validate({**suite.model_dump(), "request_cap": 1})
    bad = suite.tasks[0].model_copy(update={"commit": "0" * 40})
    result = asyncio.run(
        ProductHarness(config, tmp_path / "bad").run(suite.model_copy(update={"tasks": [bad]}))
    )
    assert result["counts"]["infrastructure_error"] == 3
    assert result["request_attempts"] == 0


@pytest.mark.parametrize(
    "command,status",
    [
        (["missing-b5-command"], "error"),
        ([sys.executable, "-c", "import time;time.sleep(60)"], "timeout"),
    ],
)
def test_verifier_failure_is_not_code_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str], status: str
) -> None:
    """验证器启动失败或超时分别记录，不能伪造修复未通过或成功。"""
    config, suite = fixture(tmp_path)
    provider(monkeypatch, "repair")
    task = suite.tasks[0].model_copy(
        update={"verification_command": command, "verification_timeout": 0.05}
    )
    result = asyncio.run(
        ProductHarness(config, tmp_path / "errors", approve_tools=True).run(
            suite.model_copy(update={"tasks": [task]})
        )
    )
    assert result["counts"]["unevaluable"] == 3
    assert all(r["verification"]["status"] == status for r in result["results"])


def test_user_command_uses_permission_and_facts_without_provider(tmp_path: Path) -> None:
    """用户测试零模型调用、真实权限拒绝和退出码都可独立核验。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = ProductConfig(state_directory=tmp_path / "state")
    denied = asyncio.run(
        run_test_command(
            config,
            workspace,
            [sys.executable, "-c", "open('bad','w').write('bad')"],
            1.0,
            ScriptedApproval([False]),
        )
    )
    assert denied["command_succeeded"] is False and not (workspace / "bad").exists()
    passed = asyncio.run(
        run_test_command(
            config,
            workspace,
            [sys.executable, "-c", "print('tested')"],
            1.0,
            ScriptedApproval([True]),
        )
    )
    assert passed["model_requests"] == 0 and passed["command_succeeded"]
    assert passed["source"] == "user_command" and passed["independent_verdict"] == "not_run"
    with SessionStore(tmp_path / "state", passed["session_id"], create=False) as stored:
        assert stored.get("request_attempts", 0) == 0
        assert stored.get("execution_facts")[-1]["exit_code"] == 0
    failed = asyncio.run(
        run_test_command(
            config,
            workspace,
            [sys.executable, "-c", "raise SystemExit(3)"],
            1.0,
            ScriptedApproval([True]),
        )
    )
    assert not failed["command_succeeded"] and "exit_code=3" in failed["result"]["content"]


def test_resource_prune_defaults_preview_and_refuses_other_identity(tmp_path: Path) -> None:
    """维护不误杀同用户其他进程，不删除工作区，活跃会话不能抢锁清理。"""
    config = ProductConfig(state_directory=tmp_path / "state")
    with SessionStore(tmp_path / "state", "owned") as stored:
        stored.resource("process", {"pid": os.getpid(), "start": process_identity(os.getpid())})
        with pytest.raises(ValueError, match="in use"):
            asyncio.run(prune(config, "owned", apply=True))
    preview = asyncio.run(prune(config, "owned"))
    assert preview["preview"] and not preview["resources"][0]["eligible"]
    assert preview["resources"][0]["reason"] == "identity_mismatch"
    assert not asyncio.run(prune(config, "owned", apply=True))["workspace_files_deleted"]


def test_diff_shows_staged_unstaged_and_non_git(tmp_path: Path) -> None:
    """审查直接读取原生 Git，包括暂存与未跟踪索引，不把全目录变化归属模型。"""
    config, suite = fixture(tmp_path)
    root = Path(suite.tasks[0].repository)
    (root / "calc.py").write_text("first\n")
    subprocess.run(["git", "add", "calc.py"], cwd=root, check=True)
    (root / "calc.py").write_text("second\n")
    (root / "untracked").write_text("user")
    result = asyncio.run(diff(root))
    assert "+first" in result["staged"] and "+second" in result["unstaged"]
    assert "untracked" in result["status"]
    plain = tmp_path / "plain"
    plain.mkdir()
    assert asyncio.run(diff(plain))["git"] is False
    assert permissions(config)["extensions_enabled"] is False


def test_management_cli_is_versioned_and_never_needs_provider_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """配置缺凭据时维护仍可运行，非 TTY 测试审批拒绝不会等待用户输入。"""
    from codepilot.ui.cli import main

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        "provider:\n  kind: openai-compatible\n  model: absent-model\n"
        "  base_url: https://example.invalid\n  api_key_env: B5_MISSING_SECRET\n"
        f"state_directory: {tmp_path / 'state'}\nbackend:\n  kind: local\n  docker_image: null\n"
    )
    for name in ("doctor", "diff", "permissions"):
        assert main([name, "--workspace", str(workspace), "--config", str(config)]) == 0
        assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    assert (
        main(
            [
                "test",
                "--workspace",
                str(workspace),
                "--config",
                str(config),
                "--",
                sys.executable,
                "-c",
                "print('denied')",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert '"model_requests": 0' in output and '"command_succeeded": false' in output


def test_real_tty_approval_cancel_releases_input_and_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 PTY 审批可取消并复用，不遗留抢占后续输入的阻塞线程。"""
    import io

    from codepilot.permissions import ConsoleApproval, PermissionEngine, controlled_edit_policy
    from codepilot.tools import create_basic_registry
    from codepilot.workspace import LocalExecutionBackend

    async def scenario() -> None:
        """三个顺序审批分别批准、取消、拒绝，输入始终归当前等待者。"""
        master, slave = os.openpty()
        stream = os.fdopen(os.dup(slave), "r")
        monkeypatch.setattr(sys, "stdin", stream)
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        spec = next(x for x in create_basic_registry(tmp_path).specs if x.name == "shell")
        request = PermissionEngine(controlled_edit_policy()).request(
            spec,
            ToolCall(call_id="tty", name="shell", arguments={"command": "pwd"}),
            "test",
            backend=LocalExecutionBackend(tmp_path),
        )
        approval = ConsoleApproval()
        try:
            first = asyncio.create_task(approval.approve(request))
            pasted = asyncio.create_task(approval.approve(request))
            await asyncio.sleep(0)
            os.write(master, b"y\ny\n")
            assert await asyncio.wait_for(first, 2)
            assert await asyncio.wait_for(pasted, 2)
            second = asyncio.create_task(approval.approve(request))
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            third = asyncio.create_task(approval.approve(request))
            await asyncio.sleep(0)
            os.write(master, b"n\n")
            assert not await asyncio.wait_for(third, 2)
            long_answer = asyncio.create_task(approval.approve(request))
            await asyncio.sleep(0)
            os.write(master, b"y" + b" " * 63 + b"no\n")
            assert not await asyncio.wait_for(long_answer, 2)
        finally:
            stream.close()
            os.close(slave)
            os.close(master)

    asyncio.run(scenario())


def test_progress_does_not_overwrite_parallel_approval_prompts() -> None:
    """多个审批尚未回答时工具结果只缓冲，全部结束后按原顺序展示。"""
    import io

    from codepilot.contracts.stream import RuntimeEvent
    from codepilot.ui.renderer import EventRenderer

    stream = io.StringIO()
    renderer = EventRenderer(stream)
    for sequence, kind, identity in [
        (1, "approval_requested", "a"),
        (2, "approval_requested", "b"),
        (3, "tool_finished", "done"),
        (4, "approval_resolved", "a"),
    ]:
        renderer(
            RuntimeEvent.model_validate(
                {
                    "run_id": "r",
                    "turn_id": "t",
                    "sequence": sequence,
                    "kind": kind,
                    "call_id": identity,
                    "outcome": "success",
                }
            )
        )
    assert "done" not in stream.getvalue()
    renderer(
        RuntimeEvent(run_id="r", turn_id="t", sequence=5, kind="approval_resolved", call_id="b")
    )
    assert "done" in stream.getvalue()


def test_resource_apply_reaps_only_recorded_process(tmp_path: Path) -> None:
    """预览不终止任务，显式 apply 只回收登记的独立进程组。"""
    from codepilot.tools.process import spawn, stop_process

    async def scenario() -> None:
        """登记真实进程起点，核对预览/执行后的存活状态。"""
        process = await spawn(
            (sys.executable, "-c", "import time;time.sleep(60)"), cwd=str(tmp_path)
        )
        try:
            with SessionStore(tmp_path / "state", "prune") as stored:
                stored.resource(
                    "process", {"pid": process.pid, "start": process_identity(process.pid)}
                )
            config = ProductConfig(state_directory=tmp_path / "state")
            preview = await prune(config, "prune")
            assert preview["resources"][0]["eligible"] and process.returncode is None
            result = await prune(config, "prune", apply=True)
            await asyncio.wait_for(process.wait(), 2)
            assert result["resources"][0]["removed"]
            assert not process_identity(process.pid)
        finally:
            await stop_process(process)

    asyncio.run(scenario())


def test_comparison_rejects_drift_and_file_edit_preserves_executable_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同条件报告才允许描述比较，真实文件修改不能清除脚本执行权限。"""
    from codepilot.evals.product import compare_reports, fingerprint
    from codepilot.tools import create_basic_registry

    config, suite = fixture(tmp_path)
    provider(monkeypatch, "repair")
    output = tmp_path / "compare"
    asyncio.run(ProductHarness(config, output, approve_tools=True).run(suite))
    report = output / "report.json"
    assert compare_reports(report, report)["success_rate_delta"] == 0
    plan = json.loads((output / "plan.json").read_text())
    changed = tmp_path / "changed"
    changed.mkdir()
    second = json.loads(report.read_text())
    plan["config"]["provider"]["model"] = "fake-other"
    plan.pop("fingerprint")
    plan["fingerprint"] = fingerprint(plan)
    second["plan_fingerprint"] = plan["fingerprint"]
    (changed / "plan.json").write_text(json.dumps(plan))
    (changed / "report.json").write_text(json.dumps(second))
    with pytest.raises(ValueError, match="drift"):
        compare_reports(report, changed / "report.json")
    executable = tmp_path / "script.py"
    executable.write_text("print(1)\n")
    executable.chmod(0o755)
    result = asyncio.run(
        create_basic_registry(tmp_path).execute(
            ToolCall(
                call_id="edit-mode",
                name="edit_file",
                arguments={"path": "script.py", "old_text": "print(1)", "new_text": "print(2)"},
            )
        )
    )
    assert not result.is_error and executable.stat().st_mode & 0o777 == 0o755


def test_user_command_trace_contains_tools_but_no_fake_model_requests(tmp_path: Path) -> None:
    """无模型命令必须有实际 Trace，且不靠伪造模型事件拼出执行记录。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = asyncio.run(
        run_test_command(
            ProductConfig(state_directory=tmp_path / "state"),
            workspace,
            [sys.executable, "-c", "print('trace')"],
            1.0,
            ScriptedApproval([True]),
        )
    )
    events = [json.loads(line) for line in Path(result["trace_path"]).read_text().splitlines()]
    assert any(e["kind"] == "tool_started" for e in events)
    assert sum(e["kind"] == "run_finished" for e in events) == 1
    assert not any(e["kind"] == "model_requested" for e in events)


def test_independent_success_does_not_require_model_final_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """步骤耗尽与独立修复成功分开统计，不能用 Runtime 状态取代需求判据。"""
    config, suite = fixture(tmp_path)
    provider(monkeypatch, "repair")
    config = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"max_steps": 1})}
    )
    result = asyncio.run(ProductHarness(config, tmp_path / "steps", approve_tools=True).run(suite))
    assert result["counts"]["success"] == 3
    assert result["runtime_completed_trials"] == 0
    assert all(
        r["runtime_outcome"] == "step_limit" and r["independent_verdict"] == "passed"
        for r in result["results"]
    )


def test_report_storage_failure_keeps_previous_report_and_stops_trials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告写满时保留上一份原子结果，不隐瞒失败或继续启动后续试验。"""
    import errno

    from codepilot.evals import product
    from codepilot.sessions.outputs import atomic_json

    config, suite = fixture(tmp_path)
    provider(monkeypatch, "repair")
    original = atomic_json

    def limited(path: Path, value: object) -> None:
        """仅在第二次报告替换处注入 ENOSPC，其他事实照常持久。"""
        if path.name == "report.json" and path.exists():
            raise OSError(errno.ENOSPC, "synthetic full storage")
        original(path, value)

    monkeypatch.setattr(product, "atomic_json", limited)
    output = tmp_path / "full"
    with pytest.raises(OSError):
        asyncio.run(ProductHarness(config, output, approve_tools=True).run(suite))
    assert json.loads((output / "report.json").read_text())["completed_trials"] == 1
    assert (output / "repair-2/result.json").is_file()
    assert not (output / "repair-3/started.json").exists()


def test_renderer_failure_before_command_does_not_start_process(tmp_path: Path) -> None:
    """输出流故障不能继续执行命令副作用，未知意图保留协调记录。"""
    import io

    from codepilot.ui.renderer import EventRenderer

    class Broken(io.StringIO):
        """真实抛出写故障，而不是忽略进度后假装任务成功。"""

        def write(self, text: str) -> int:
            """在首次实际写入时中断观察者。"""
            raise BrokenPipeError("synthetic closed terminal")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = service.CodePilotApplication(
        ProductConfig(state_directory=tmp_path / "state"),
        workspace,
        approval=ScriptedApproval([True]),
        on_event=EventRenderer(Broken()),
    )
    with pytest.raises(BrokenPipeError):
        asyncio.run(
            app.run(
                "用户命令",
                session_id="broken",
                direct_tool=ToolCall(
                    call_id="write", name="shell", arguments={"command": "printf bad > forbidden"}
                ),
            )
        )
    assert not (workspace / "forbidden").exists()
    with SessionStore(tmp_path / "state", "broken", create=False) as stored:
        assert stored.get("status") == "needs_reconciliation"


def test_wheel_content_check_rejects_build_before_last_edit(tmp_path: Path) -> None:
    """构建版本名相同也不能代表内容最新，真实 zip 必须逐文件匹配源码。"""
    from zipfile import ZipFile

    source = tmp_path / "source"
    source.mkdir()
    code = source / "__init__.py"
    code.write_text('"""合成包源码。"""\n')
    wheel = tmp_path / "package.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("codepilot/__init__.py", code.read_bytes())
    script = Path(__file__).resolve().parents[1] / "scripts/check_wheel.py"
    args = [sys.executable, str(script), str(wheel), "--source", str(source)]
    passed = subprocess.run(args, capture_output=True, text=True)
    assert passed.returncode == 0 and json.loads(passed.stdout)["status"] == "passed"
    code.write_text(code.read_text() + "version = 2\n")
    stale = subprocess.run(args, capture_output=True, text=True)
    assert stale.returncode != 0 and "wheel differs from source" in stale.stderr
