"""冻结十二次官方模型请求，验收原生 Git 联网后的实际代码与离线独立判据。"""

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from codepilot.app import CodePilotApplication
from codepilot.config import BackendConfig, load_product_config
from codepilot.contracts.run import RunLimits
from codepilot.network.models import NativeNetworkConfig, NetworkConfig
from codepilot.permissions import ScriptedApproval
from codepilot.sessions.store import SessionStore
from codepilot.tools.process import workspace_environment
from codepilot.workspace.docker import DockerExecutionBackend

URL = "https://gitlab.com/gitlab-org/gitlab-test.git"
COMMIT = "ddd0f15ae83993f5cb66a927a28673882e99100b"
TOTAL_CAP = 12


async def main(repository: Path, output: Path) -> None:
    """固定任务与保护字节；保存全部四层结论后才断言任务验收通过。"""
    base = load_product_config(workspace=repository)
    if (base.provider.base_url or "").rstrip(
        "/"
    ) != "https://api.deepseek.com" or base.provider.model != "deepseek-flash":
        raise ValueError("live acceptance requires official deepseek-flash")
    if output.exists():
        raise ValueError("evidence exists; do not reset the frozen aggregate budget")
    root = Path(tempfile.mkdtemp(prefix="codepilot-native-model-", dir="/dev/shm"))
    source = root / "source"
    source.mkdir()
    protected = b"existing user bytes\r\nnot owned by agent\n"
    (source / "protected.txt").write_bytes(protected)
    # 独立验证器直接用原生 Git 固定内容，不接受模型给的提交或文件数当答案。
    expected = root / "validator"
    subprocess.run(["git", "init", "-q", str(expected)], check=True, timeout=10)
    subprocess.run(
        ["git", "-C", str(expected), "fetch", "-q", "--depth", "1", URL, COMMIT],
        env=workspace_environment(),
        check=True,
        capture_output=True,
        timeout=45,
    )
    subprocess.run(
        ["git", "-C", str(expected), "checkout", "-q", "--detach", "FETCH_HEAD"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    count = len(
        subprocess.check_output(
            ["git", "-C", str(expected), "ls-tree", "-r", "--name-only", COMMIT], timeout=10
        ).splitlines()
    )
    # 第二个隐藏仓库使用不同提交和文件数，防止只对公开样例硬编码答案。
    second = expected / "second"
    environment = workspace_environment()
    environment.update(
        GIT_AUTHOR_DATE="2026-10-20T00:00:00Z", GIT_COMMITTER_DATE="2026-10-20T00:00:00Z"
    )
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(expected), str(second)],
        check=True,
        env=environment,
        capture_output=True,
        timeout=15,
    )
    (second / "independent-added.txt").write_text("fixed independent verification\n")
    subprocess.run(
        ["git", "-C", str(second), "add", "independent-added.txt"],
        check=True,
        env=environment,
        timeout=10,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(second),
            "-c",
            "user.name=Validator",
            "-c",
            "user.email=validator@example.invalid",
            "commit",
            "-qm",
            "hidden fixture",
        ],
        check=True,
        env=environment,
        timeout=10,
    )
    second_commit = subprocess.check_output(
        ["git", "-C", str(second), "rev-parse", "HEAD"], text=True, env=environment, timeout=10
    ).strip()
    config = base.model_copy(
        update={
            "backend": BackendConfig(kind="docker", docker_image="codepilot-sandbox:0.1.0"),
            "network": NetworkConfig(
                enabled=True,
                native=NativeNetworkConfig(
                    enabled=True, hosts=("gitlab.com",), lease_seconds=300, total_bytes=16 * 1024**2
                ),
            ),
            "provider": base.provider.model_copy(
                update={
                    "protocol": "chat-completions",
                    "max_requests": TOTAL_CAP,
                    "max_tokens": 8192,
                }
            ),
            "runtime": RunLimits(max_steps=TOTAL_CAP, max_tool_calls=24, timeout_seconds=360.0),
            "sessions": base.sessions.model_copy(update={"max_requests": TOTAL_CAP}),
            "git_workspace": base.git_workspace.model_copy(update={"enabled": False}),
            "permission": "controlled-edit",
            "state_directory": root / "state",
        }
    )
    # 从第一次请求前固定任务、版本、预算和独立判据，失败也保留，禁止静默另开预算重试。
    frozen = {
        "schema_version": 1,
        "frozen_request_cap": TOTAL_CAP,
        "source_url": URL,
        "expected_commit": COMMIT,
        "expected_tracked_files": count,
        "hidden_commit": second_commit,
        "hidden_tracked_files": count + 1,
        "workspace": str(source),
        "status": "running",
    }
    output.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")
    result = await CodePilotApplication(config, source, approval=ScriptedApproval([True] * 24)).run(
        f"实现一个小型仓库检查脚本。先使用 shell 的 network=true 原生 Git，"
        f"在新目录 upstream 初始化仓库，git fetch --depth 1 {URL} {COMMIT}，"
        "checkout FETCH_HEAD；仅这个步骤申请联网。"
        "然后创建 inspect_repo.py（仅标准库，函数写中文 docstring），支持 --repo 目录，"
        "输出一行 JSON，恰好两个字段 commit（HEAD 40位哈希）、tracked_files"
        "（git ls-tree -r --name-only HEAD 的文件数）。"
        "无效或非 Git 目录退出非零。使用 shell network=false 实际运行脚本和无效目录测试。"
        "不要修改 protected.txt，不要设置凭据或其他远端，不要安装依赖。",
        session_id="native-model",
    )
    # 生成代码只在新的断网容器验收，不让独立验证器反过来暴露控制器凭据。
    candidate = source / "inspect_repo.py"
    actual = None
    hidden_actual = None
    hidden_exit: int | None = None
    probe_exit: int | None = None
    invalid_nonzero = False
    verification_closed = False
    if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 65536:
        assert not (expected / "inspect_repo.py").exists()
        (expected / "inspect_repo.py").write_bytes(candidate.read_bytes())
        verifier = DockerExecutionBackend(expected, "codepilot-sandbox:0.1.0")
        try:
            await verifier.start()
            probe = await verifier.run_argv(
                ("python3", "/workspace/inspect_repo.py", "--repo", "/workspace"),
                timeout_seconds=15,
            )
            probe_exit = probe.exit_code
            try:
                actual = json.loads(probe.stdout)
            except ValueError:
                actual = None
            hidden = await verifier.run_argv(
                ("python3", "/workspace/inspect_repo.py", "--repo", "/workspace/second"),
                timeout_seconds=15,
            )
            hidden_exit = hidden.exit_code
            try:
                hidden_actual = json.loads(hidden.stdout)
            except ValueError:
                hidden_actual = None
            invalid = await verifier.run_argv(
                ("python3", "/workspace/inspect_repo.py", "--repo", "/workspace/absent"),
                timeout_seconds=15,
            )
            invalid_nonzero = invalid.exit_code != 0 and not invalid.timed_out
        finally:
            await verifier.close()
            verification_closed = any(e.kind == "backend_closed" for e in verifier.events)
    with SessionStore(root / "state", "native-model", create=False) as session:
        requests = session.get("request_attempts")
        budget = session.get("provider_budget")
        pending = session.get("network_pending", {})
    independent = (
        probe_exit == 0
        and actual == {"commit": COMMIT, "tracked_files": count}
        and hidden_exit == 0
        and hidden_actual == {"commit": second_commit, "tracked_files": count + 1}
        and invalid_nonzero
        and verification_closed
        and (source / "protected.txt").read_bytes() == protected
    )
    frozen.update(
        {
            "status": "finished",
            "model_requests": requests,
            "provider_budget": budget,
            "model_claim": next(
                (
                    m.content
                    for m in reversed(result.report.messages)
                    if m.role == "assistant" and m.content
                ),
                "",
            ),
            "tool_results": [o.result.model_dump() for o in result.report.observations if o.result],
            "runtime_outcome": result.report.reason,
            "independent_verdict": "passed" if independent else "failed",
            "independent_actual": actual,
            "independent_hidden_actual": hidden_actual,
            "invalid_directory_nonzero": invalid_nonzero,
            "verification_backend_closed": verification_closed,
            "protected_unchanged": (source / "protected.txt").read_bytes() == protected,
            "network_pending": pending,
            "needs_reconciliation": result.needs_reconciliation,
            "backend_closed": result.resources.backend_closed,
            "image_id": subprocess.check_output(
                ["docker", "image", "inspect", "codepilot-sandbox:0.1.0", "--format", "{{.Id}}"],
                text=True,
            ).strip(),
            "base_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip(),
            "worktree_changes": True,
            "controller_uid": os.getuid(),
        }
    )
    output.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: frozen[k]
                for k in [
                    "model_requests",
                    "runtime_outcome",
                    "independent_verdict",
                    "backend_closed",
                ]
            }
        )
    )
    assert independent and not result.needs_reconciliation and result.resources.backend_closed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.repository, arguments.output))
