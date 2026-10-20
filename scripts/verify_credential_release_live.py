"""在 Rootless 复验受阻时明确单验 Local 固定上游修复与跨进程续问，不替代认证验收。"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from codepilot.config import BackendConfig, load_product_config
from codepilot.evals.product import ProductHarness, ProductTask, Suite, snapshot
from codepilot.network.models import NetworkConfig
from codepilot.sessions.store import SessionStore


async def main(repository: Path, cached_source: Path, output: Path) -> None:
    """预冻修复十二次加续问四次，所有实际请求共享本轮十六次总上限。"""
    if output.exists():
        raise ValueError("evidence exists; never reset the frozen budget")
    base = load_product_config(workspace=repository)
    if (
        base.provider.model != "deepseek-flash"
        or (base.provider.base_url or "").rstrip("/") != "https://api.deepseek.com"
    ):
        raise ValueError("only authorized official DeepSeek Flash")
    root = Path(tempfile.mkdtemp(prefix="codepilot-credential-local-", dir="/dev/shm"))
    original = json.loads((repository / "examples/evals/real-repositories.json").read_bytes())[
        "tasks"
    ][0]
    original.update(
        repository=str(cached_source.resolve(strict=True)),
        max_requests=12,
        validation_material=str(repository / "examples/evals/tomli-hidden"),
    )
    task = ProductTask.model_validate(original)
    suite = Suite(
        name="credential-local-regression",
        repetitions=1,
        request_cap=12,
        required_successes=1,
        tasks=[task],
    )
    config = base.model_copy(
        update={
            "backend": BackendConfig(kind="local"),
            "network": NetworkConfig(),
            "permission": "controlled-edit",
            "extensions": base.extensions.model_copy(update={"enabled": False}),
            "runtime": base.runtime.model_copy(
                update={"max_steps": 12, "max_tool_calls": 32, "timeout_seconds": 300.0}
            ),
            "provider": base.provider.model_copy(update={"max_requests": 12, "max_tokens": 8192}),
        }
    )
    evidence: dict[str, object] = {
        "schema_version": 1,
        "stage_request_cap": 16,
        "repair_cap": 12,
        "resume_cap": 4,
        "workspace_backend": "local-trusted-host",
        "private_git_validation": "not_run",
        "rootless_validation": "separate_failed_evidence",
        "source_commit": task.commit,
        "private_artifacts": str(root),
        "status": "running",
    }
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    report = await ProductHarness(config, root / "eval", approve_tools=True).run(suite)
    trial = report["results"][0]
    evidence.update(
        repair=trial,
        repair_requests=report["request_attempts"],
        task_gate_passed=report["task_gate_passed"],
    )
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    if not report["task_gate_passed"]:
        raise ValueError("repair failed; retain evidence without another attempt")
    directory = root / "eval/tomli-crlf-decimal-1"
    workspace = directory / "workspace"
    before = snapshot(workspace)
    resume_config = config.model_copy(
        update={
            "state_directory": directory / "state",
            "permission": "read-only",
            "provider": config.provider.model_copy(update={"max_requests": 4}),
            "sessions": config.sessions.model_copy(update={"max_requests": 16}),
        }
    )
    configuration = root / "resume.yaml"
    configuration.write_text(resume_config.model_dump_json())
    configuration.chmod(0o600)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from codepilot.ui.cli import main; raise SystemExit(main())",
        "--config",
        str(configuration),
        "--workspace",
        str(workspace),
        "resume",
        "trial",
        "读取刚修改的两个源文件，然后简短解释 CRLF 和 Decimal 回归分别如何修复。"
        "只读，不执行命令、不修改文件、不联网。",
        "--jsonl",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(180):
            stdout, stderr = await process.communicate()
    finally:
        # 超时结束自己的 CLI，不新开会话补请求，不覆盖旧任务结果。
        if process.returncode is None:
            process.kill()
            await process.wait()
    with SessionStore(directory / "state", "trial", create=False) as session:
        budget = session.get("provider_budget", {})
        events = [json.loads(line) for line in stdout.decode().splitlines() if line.startswith("{")]
        evidence.update(
            status="finished",
            model_requests=session.get("request_attempts"),
            provider_budget=budget,
            cross_process_resume={
                "exit_code": process.returncode,
                "workspace_unchanged": before == snapshot(workspace),
                "session_status": session.get("status"),
                "network_pending": session.get("network_pending", {}),
                "jsonl": events,
                "stderr": stderr.decode(),
            },
            release_ready=False,
        )
        passed = (
            process.returncode == 0
            and session.get("status") == "ready"
            and before == snapshot(workspace)
            and session.get("request_attempts") <= 16
            and not budget.get("pending")
        )
    evidence["independent_verdict"] = "passed" if passed else "failed"
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "independent_verdict": evidence["independent_verdict"],
                "model_requests": evidence["model_requests"],
            }
        )
    )
    assert passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.repository, arguments.source, arguments.output))
