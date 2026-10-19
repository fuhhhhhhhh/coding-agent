"""固定版本、独立副本和隐藏验收的产品评测；只调用正式应用，不另写 Agent 循环。"""

import asyncio
import hashlib
import json
import os
import resource
import shutil
import stat
import statistics
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, model_validator

from codepilot.app.operations import command, doctor
from codepilot.app.service import CodePilotApplication, _create_backend
from codepilot.config import ProductConfig
from codepilot.contracts.models import Contract
from codepilot.contracts.stream import RuntimeEvent
from codepilot.permissions import ScriptedApproval
from codepilot.sessions.outputs import atomic_json, private_directory
from codepilot.sessions.store import SessionStore


class Seed(Contract):
    """只在固定版本的精确单匹配处注入回归，版本不符先停止。"""

    path: str
    before: str
    after: str


class ProductTask(Contract):
    """可信评测任务固定来源、允许修改范围及模型不可见验证材料。"""

    task_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]
    repository: str
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    description: Annotated[str, Field(min_length=1, max_length=16384)]
    seeds: list[Seed] = []
    dirty_files: dict[str, str] = {}
    allowed_paths: Annotated[list[str], Field(max_length=32)] = []
    expect_changes: bool = True
    validation_material: str
    verification_command: Annotated[list[str], Field(min_length=1, max_length=32)]
    verification_timeout: Annotated[float, Field(gt=0, le=300)] = 60.0
    max_requests: Annotated[int, Field(ge=1, le=32)] = 16

    @model_validator(mode="after")
    def paths(self) -> "ProductTask":
        """拒绝元数据目录和穿越，不能把允许路径当作任意宿主写能力。"""
        for value in [*self.allowed_paths, *self.dirty_files, *(x.path for x in self.seeds)]:
            relative_path(value)
        if self.expect_changes and not self.allowed_paths:
            raise ValueError("repair task needs allowed_paths")
        return self


class Suite(Contract):
    """真实试验前冻结重复次数、总体请求上限和目标，不事后择优筛选。"""

    schema_version: Literal[1] = 1
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]
    repetitions: Annotated[int, Field(ge=1, le=10)] = 3
    request_cap: Annotated[int, Field(ge=1, le=1000)] = 96
    required_successes: Annotated[int, Field(ge=0)]
    tasks: Annotated[list[ProductTask], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def bounds(self) -> "Suite":
        """清单不得预先超售请求预算或混用重复任务身份。"""
        if sum(t.max_requests for t in self.tasks) * self.repetitions > self.request_cap:
            raise ValueError("suite trials exceed total request cap")
        if self.required_successes > len(self.tasks) * self.repetitions:
            raise ValueError("success target exceeds trials")
        if len({t.task_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate task ID")
        return self


def relative_path(value: str) -> Path:
    """限制任务文件位于工作区且不指向 Git/隐藏验证注入路径。"""
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or any(x in {".git", ".codepilot-validation"} for x in path.parts)
    ):
        raise ValueError("unsafe task path")
    return path


def snapshot(root: Path) -> dict[str, dict[str, Any]]:
    """按字节和模式采样普通文件，限制遍历规模且拒绝链接/特殊文件。"""
    result: dict[str, dict[str, Any]] = {}
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(x for x in names if x not in {".git", "__pycache__", ".pytest_cache"})
        for name in [*names, *files]:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("candidate symlink refused")
        for name in sorted(files):
            path = Path(directory) / name
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("special candidate file refused")
            total += info.st_size
            if len(result) >= 20000 or total > 256 * 1024**2:
                raise ValueError("repository exceeds measured snapshot bound")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
            result[path.relative_to(root).as_posix()] = {
                "sha256": digest.hexdigest(),
                "mode": stat.S_IMODE(info.st_mode),
                "bytes": info.st_size,
            }
    return result


def fingerprint(value: Any) -> str:
    """对非秘密配置或清单生成稳定来源指纹。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class ProductHarness:
    """持有不可覆盖的评测目录，每个 trial 独立状态/后端/工作区。"""

    def __init__(
        self,
        config: ProductConfig,
        output: Path,
        *,
        approve_tools: bool = False,
        allow_download: bool = False,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """只接可信清单；批量进程授权必须由 CLI 明确选择。"""
        self.config, self.output, self.approve_tools = config, output.absolute(), approve_tools
        self.allow_download, self.on_progress = allow_download, on_progress
        self.materials: dict[str, Path] = {}

    async def run(self, suite: Suite) -> dict[str, Any]:
        """拥有评测镜像引用直到所有 trial 与验证器结束，取消同样释放。"""
        async with AsyncExitStack() as resources:
            result = await self._run(suite, resources)
        result["owned_resources_released"] = True
        atomic_json(self.output / "report.json", result)
        return result

    async def _run(self, suite: Suite, resources: AsyncExitStack) -> dict[str, Any]:
        """先写冻结计划再运行，所有失败计入结果，旧目录不能重跑未知 trial。"""
        if self.output.exists():
            raise ValueError("evaluation output already exists; inspect it, do not replay")
        private_directory(self.output)
        config = self.config.model_dump(mode="json")
        # 凭据路径不作为可公开配置值，实际秘密从不进入冻结清单。
        config["provider"]["api_key_file"] = (
            "<configured>" if self.config.provider.api_key_file else None
        )
        hidden: dict[str, str] = {}
        for task in suite.tasks:
            material = Path(task.validation_material).resolve(strict=True)
            hidden[task.task_id] = fingerprint(snapshot(material))
            frozen_material = self.output / "materials" / task.task_id
            shutil.copytree(material, frozen_material)
            if fingerprint(snapshot(frozen_material)) != hidden[task.task_id]:
                raise ValueError("verifier material changed while freezing")
            self.materials[task.task_id] = frozen_material
        environment = await doctor(self.config, Path.cwd())
        # 一次 suite 的所有后端固定到同一镜像 ID，标签中途改变也不混合比较。
        if self.config.backend.kind == "docker":
            image_id = environment["checks"].get("image", {}).get("id")
            if image_id:
                lease = f"codepilot-eval-{os.getuid()}-{uuid4().hex}:pinned"
                retained = await command(("docker", "image", "tag", image_id, lease), self.output)
                if retained["exit_code"] != 0:
                    raise ValueError("cannot retain evaluation image")

                async def release_image() -> None:
                    """只移除本 suite 新建的唯一别名，不删除原镜像标签或其他资源。"""
                    result = await command(("docker", "image", "rm", lease), self.output)
                    if result["exit_code"] != 0:
                        raise RuntimeError("evaluation image alias cleanup failed")

                resources.push_async_callback(release_image)
                environment["owned_image_tag"] = lease
                self.config = self.config.model_copy(
                    update={
                        "backend": self.config.backend.model_copy(update={"docker_image": image_id})
                    }
                )
        frozen = {
            "schema_version": 1,
            "suite": suite.model_dump(mode="json"),
            "config": config,
            "material_hashes": hidden,
            "environment": environment,
            "approve_tools": self.approve_tools,
            "allow_download": self.allow_download,
            "source_tree_hash": fingerprint(snapshot(Path(__file__).parents[1])),
        }
        plan_id = fingerprint(frozen)
        frozen["fingerprint"] = plan_id
        atomic_json(self.output / "plan.json", frozen)
        results: list[dict[str, Any]] = []
        for task in suite.tasks:
            for repetition in range(1, suite.repetitions + 1):
                identity = f"{task.task_id}-{repetition}"
                directory = self.output / identity
                private_directory(directory)
                atomic_json(
                    directory / "started.json", {"trial": identity, "plan": frozen["fingerprint"]}
                )
                if self.on_progress is not None:
                    self.on_progress(
                        {"schema_version": 1, "kind": "trial_started", "trial": identity}
                    )
                try:
                    if (
                        fingerprint(snapshot(Path(task.validation_material)))
                        != hidden[task.task_id]
                    ):
                        raise ValueError("hidden verifier material drift")
                    result = await self.trial(task, directory)
                except asyncio.CancelledError:
                    # 退出不补造失败/成功；started 无 result 明确表示未知，中断后不重放。
                    raise
                except Exception as error:
                    state_path = directory / "state"
                    attempts = 0
                    if (state_path / "trial/session.db").exists():
                        attempts = SessionStore.inspect(state_path, "trial").get(
                            "request_attempts", 0
                        )
                    result = {
                        "task_id": task.task_id,
                        "classification": "infrastructure_error",
                        "error_type": type(error).__name__,
                        "independent_verdict": "not_run",
                        "request_attempts": attempts,
                    }
                result["trial"] = identity
                atomic_json(directory / "result.json", result)
                results.append(result)
                summary = summarize(suite, plan_id, results)
                atomic_json(self.output / "report.json", summary)
                if self.on_progress is not None:
                    self.on_progress(
                        {
                            "schema_version": 1,
                            "kind": "trial_finished",
                            "trial": identity,
                            "classification": result["classification"],
                        }
                    )
        return summary

    async def trial(self, task: ProductTask, directory: Path) -> dict[str, Any]:
        """checkout 固定版本、应用精确回归与用户改动，再通过正式产品修复。"""
        started = time.monotonic()
        url = urlsplit(task.repository)
        if url.scheme:
            # 网络获取只在控制器显式允许后发生，不改变工具容器断网策略。
            if (
                not self.allow_download
                or url.scheme != "https"
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("repository download requires explicit HTTPS permission")
            source = task.repository
        else:
            source = str(Path(task.repository).resolve(strict=True))
        workspace = directory / "workspace"
        clone = await command(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                source,
                str(workspace),
            ),
            directory,
            60,
        )
        if clone["exit_code"] != 0:
            raise ValueError("repository clone failed")
        checkout = await command(
            ("git", "-c", "core.hooksPath=/dev/null", "checkout", "--detach", task.commit),
            workspace,
            60,
        )
        actual = await command(("git", "rev-parse", "HEAD"), workspace)
        if checkout["exit_code"] != 0 or actual["stdout"].strip() != task.commit:
            raise ValueError("repository commit mismatch")
        # 初始快照先拒绝符号链接，再允许可信清单写入，避免穿越源中的链接目录。
        snapshot(workspace)
        for seed in task.seeds:
            path = workspace / relative_path(seed.path)
            text = path.read_text()
            if text.count(seed.before) != 1:
                raise ValueError("seed does not match pinned source exactly once")
            path.write_text(text.replace(seed.before, seed.after, 1))
        for name, text in task.dirty_files.items():
            path = workspace / relative_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        before = snapshot(workspace)
        git_config = (workspace / ".git/config").read_bytes()
        atomic_json(directory / "baseline.json", before)
        baseline = directory / "baseline"
        shutil.copytree(
            workspace,
            baseline,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
        )
        # 先证明注入缺陷确实使独立判据失败，避免把无效任务或坏验证器拿去消耗模型。
        preflight = await self.verify(task, baseline, workspace, directory / "preflight")
        atomic_json(directory / "baseline-verification.json", preflight)
        expected = "failed" if task.expect_changes else "passed"
        if preflight["status"] != expected:
            return {
                "task_id": task.task_id,
                "classification": "unevaluable",
                "verification": preflight,
                "runtime_outcome": "not_run",
                "independent_verdict": "not_run",
                "request_attempts": 0,
                "detail": "baseline verifier did not show expected task condition",
            }
        initial = await command(
            ("git", "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"), workspace
        )
        atomic_json(directory / "initial-diff.json", initial)
        index = await command(
            ("git", "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"), workspace
        )
        config = self.config.model_copy(
            update={
                "state_directory": directory / "state",
                "provider": self.config.provider.model_copy(
                    update={"max_requests": task.max_requests}
                ),
                "sessions": self.config.sessions.model_copy(
                    update={"max_requests": task.max_requests}
                ),
                "git_workspace": self.config.git_workspace.model_copy(update={"enabled": False}),
            }
        )
        first_text: float | None = None
        first_request: float | None = None

        def event(event: RuntimeEvent) -> None:
            """只采计量，不导出模型流正文；首字时延含真实请求和工具前置等待。"""
            nonlocal first_text, first_request
            if event.kind == "model_requested" and first_request is None:
                first_request = time.monotonic()
            if event.kind == "text_delta" and first_text is None:
                first_text = time.monotonic() - (first_request or started)

        approval = (
            ScriptedApproval([True] * (config.runtime.max_tool_calls * 2 + 8))
            if self.approve_tools
            else ScriptedApproval(())
        )
        prompt = (
            task.description
            + "\n只允许修改："
            + json.dumps(task.allowed_paths, ensure_ascii=False)
            + "。保留其他文件和用户改动；无需提交、安装或联网。"
        )
        outcome = await CodePilotApplication(
            config, workspace, approval=approval, on_event=event
        ).run(prompt, session_id="trial")
        with SessionStore(directory / "state", "trial", create=False) as stored:
            attempts = stored.get("request_attempts", 0)
            budget = stored.get("provider_budget", {})
            resources = stored.get("resources", [])
            facts = stored.get("execution_facts", [])
        after = snapshot(workspace)
        metadata = workspace / ".git"
        if not metadata.is_dir() or metadata.is_symlink() or (metadata / "commondir").exists():
            raise ValueError("candidate redirected Git metadata")
        snapshot(metadata)
        if (metadata / "config").read_bytes() != git_config:
            raise ValueError("candidate changed Git execution configuration")
        changes = sorted(
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        )
        forbidden = sorted(set(changes) - set(task.allowed_paths))
        head = await command(("git", "rev-parse", "HEAD"), workspace)
        final_index = await command(
            ("git", "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"), workspace
        )
        patch = await command(
            ("git", "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"), workspace
        )
        atomic_json(directory / "candidate-diff.json", patch)
        integrity = (
            not forbidden
            and head["stdout"].strip() == task.commit
            and final_index == index
            and not patch["truncated"]
        )
        verification: dict[str, Any] = {"status": "not_run"}
        if integrity and outcome.resources.backend_closed and (changes or not task.expect_changes):
            verification = await self.verify(task, baseline, workspace, directory)
        passed = verification["status"] == "passed" and integrity and not resources
        # 独立通过与 Runtime 完成分开记录；基础设施失败不能因为恰巧有补丁被隐藏。
        classification = "success" if passed else "unresolved"
        if not passed and outcome.report.reason in {
            "provider_error",
            "contract_error",
            "persistence_error",
        }:
            classification = "infrastructure_error"
        if verification["status"] in {"error", "timeout"}:
            classification = "unevaluable"
        if not passed and outcome.report.reason in {"time_limit", "cancelled"}:
            classification = "timeout"
        claims = [m.content for m in outcome.report.messages if m.role == "assistant" and m.content]
        artifact_bytes = sum(
            p.stat().st_size for p in (directory / "state").rglob("*") if p.is_file()
        )
        waits = []
        if outcome.resources.trace_path is not None:
            for line in outcome.resources.trace_path.read_text().splitlines():
                event_record = json.loads(line)
                if event_record["kind"] == "tool_scheduled":
                    waits.append(event_record["duration_seconds"] or 0.0)
        return {
            "task_id": task.task_id,
            "commit": task.commit,
            "classification": classification,
            "model_claim": claims[-1] if claims else "",
            "tool_facts": facts,
            "runtime_outcome": outcome.report.reason,
            "runtime_completed": outcome.report.reason == "completed",
            "runtime_error_type": outcome.report.detail,
            "independent_verdict": "passed"
            if passed
            else "failed"
            if verification["status"] == "failed" or not integrity or not changes
            else "not_run",
            "verification": verification,
            "integrity": integrity,
            "forbidden_changes": forbidden,
            "changed_files": changes,
            # 完整基线单独持久保存，汇总只嵌入变更文件，避免每次重写整个真实仓库索引。
            "before": {name: before.get(name) for name in changes[:100]},
            "baseline_manifest_sha256": fingerprint(before),
            "baseline_file_count": len(before),
            "after_changes": {p: after.get(p) for p in changes},
            "request_attempts": attempts,
            "budget": budget,
            "elapsed_seconds": time.monotonic() - started,
            "first_text_seconds": first_text,
            "controller_process_highwater_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * 1024,
            "controller_memory_scope": (
                "process cumulative high-water; includes earlier trials, not per-trial peak"
            ),
            "container_peak_sampled_memory_bytes": max(
                (e.memory_bytes for e in outcome.backend_events if e.memory_bytes is not None),
                default=None,
            ),
            "artifact_bytes": artifact_bytes,
            "tool_wait_seconds": {
                "count": len(waits),
                "sum": sum(waits),
                "max": max(waits, default=0),
            },
            "backend_closed": outcome.resources.backend_closed,
            "remaining_resources": resources,
            "steps": outcome.report.steps,
            "tool_calls": outcome.report.tool_calls,
        }

    async def verify(
        self, task: ProductTask, baseline: Path, candidate: Path, directory: Path
    ) -> dict[str, Any]:
        """重建可信基线，仅覆盖允许的候选文件，最后注入模型不可见材料。"""
        root = directory / "verification"
        shutil.copytree(baseline, root)
        for name in task.allowed_paths:
            source, target = candidate / name, root / name
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                target.unlink(missing_ok=True)
        material = self.materials.get(task.task_id, Path(task.validation_material)).resolve(
            strict=True
        )
        snapshot(material)
        hidden = root / ".codepilot-validation"
        if hidden.exists():
            raise ValueError("reserved verification path occupied")
        shutil.copytree(material, hidden)
        backend = _create_backend(self.config, root, directory / "verifier-backend")
        phase = "backend_start"
        try:
            async with backend:
                phase = "verifier_command"
                result = await backend.run_argv(
                    tuple(task.verification_command), timeout_seconds=task.verification_timeout
                )
                phase = "backend_close"
            status = (
                "timeout"
                if result.timed_out
                else "error"
                if result.oom_killed
                else "passed"
                if result.exit_code == 0
                else "failed"
            )
            return {
                "status": status,
                "exit_code": result.exit_code,
                "detail": (result.stdout + result.stderr)[-4000:],
                "source": "controller_hidden_verifier",
            }
        except (OSError, RuntimeError, ValueError) as error:
            # 只保存异常类别，验证器错误不等于代码测试失败。
            return {"status": "error", "error_type": type(error).__name__, "phase": phase}


def summarize(suite: Suite, plan: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """全部试验进入分母，基础设施错误单列，发布限制独立于任务成功率。"""
    successes = sum(r["classification"] == "success" for r in results)
    classifications = {
        name: sum(r["classification"] == name for r in results)
        for name in ("success", "unresolved", "timeout", "unevaluable", "infrastructure_error")
    }
    complete = len(results) == len(suite.tasks) * suite.repetitions
    return {
        "schema_version": 1,
        "suite": suite.name,
        "plan_fingerprint": plan,
        "completed_trials": len(results),
        "expected_trials": len(suite.tasks) * suite.repetitions,
        "counts": classifications,
        "runtime_completed_trials": sum(r.get("runtime_outcome") == "completed" for r in results),
        "success_rate": successes / len(results) if results else None,
        "request_attempts": sum(r["request_attempts"] for r in results),
        "task_gate_passed": complete and successes >= suite.required_successes,
        "release_ready": False,
        "release_blockers": [
            "B2 network/secrets boundary incomplete",
            "no hard workspace disk quota",
            "Docker isolated Git supports repository roots only",
            "undo excludes unowned Shell/extension changes and legacy runs",
        ],
        "results": results,
    }


def compare_reports(first: Path, second: Path) -> dict[str, Any]:
    """仅比较相同任务/预算/模型/平台/镜像的完整报告，不把漂移或小样本当优化。"""
    reports = [json.loads(path.read_bytes()) for path in (first, second)]
    plans = [json.loads(path.with_name("plan.json").read_bytes()) for path in (first, second)]
    identities = []
    for plan, report in zip(plans, reports, strict=True):
        frozen = {key: value for key, value in plan.items() if key != "fingerprint"}
        if (
            fingerprint(frozen) != plan["fingerprint"]
            or report["plan_fingerprint"] != plan["fingerprint"]
        ):
            raise ValueError("report plan fingerprint invalid")
        if report["completed_trials"] != report["expected_trials"]:
            raise ValueError("cannot compare incomplete reports")
        tasks = [
            {
                key: value
                for key, value in task.items()
                if key not in {"repository", "validation_material"}
            }
            for task in plan["suite"]["tasks"]
        ]
        config = {key: value for key, value in plan["config"].items() if key != "state_directory"}
        for field in ("api_key_env", "api_key_file"):
            config["provider"].pop(field, None)
        environment = plan["environment"]
        identities.append(
            fingerprint(
                {
                    "tasks": tasks,
                    "materials": plan["material_hashes"],
                    "config": config,
                    "repetitions": plan["suite"]["repetitions"],
                    "request_cap": plan["suite"]["request_cap"],
                    "python": environment["python"],
                    "platform": environment["platform"],
                    "image": environment["checks"].get("image"),
                }
            )
        )
    if identities[0] != identities[1]:
        raise ValueError("task/model/budget/environment drift; comparison refused")
    return {
        "schema_version": 1,
        "comparison_identity": identities[0],
        "success_rate_delta": reports[1]["success_rate"] - reports[0]["success_rate"],
        "median_elapsed_seconds": [
            statistics.median(
                r["elapsed_seconds"] for r in report["results"] if "elapsed_seconds" in r
            )
            if any("elapsed_seconds" in r for r in report["results"])
            else None
            for report in reports
        ],
        "request_attempts": [report["request_attempts"] for report in reports],
        "statistical_advantage": "not_established; descriptive comparison only",
    }
