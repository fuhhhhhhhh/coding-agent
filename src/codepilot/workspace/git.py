"""为每个产品任务创建固定 Git worktree，并导出可重放补丁。"""

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from codepilot.contracts.models import Contract, Identifier
from codepilot.evals import IndependentVerifier, VerificationResult
from codepilot.tools.file_common import _atomic_write
from codepilot.tools.process import workspace_environment
from codepilot.workspace.base import ExecutionBackend


class GitDelivery(Contract):
    """描述任务工作树、补丁及独立重放验证的最终状态。"""

    source_repository: Path
    workspace: Path
    base_commit: str
    state: Literal["cleaned", "changes_retained", "failed_retained"]
    changed_files: tuple[str, ...] = ()
    patch_path: Path | None = None
    patch_sha256: str | None = None
    replay_status: Literal["not_needed", "passed", "failed"] = "not_needed"
    verification: VerificationResult | None = None


class GitTaskWorkspace:
    """拥有一个任务 worktree，并在结束时保留修改或安全清理空工作树。"""

    def __init__(
        self,
        source_workspace: Path,
        run_directory: Path,
        *,
        session_id: Identifier,
        task_id: Identifier,
        run_id: Identifier,
        base_commit: str = "HEAD",
        require_clean_source: bool = False,
        max_patch_bytes: int = 16 * 1024 * 1024,
        independent_repository: bool = False,
    ) -> None:
        """保存任务身份和边界；真正的 Git 副作用延迟到 ``prepare``。"""
        self.source_workspace = source_workspace.resolve(strict=True)
        self.run_directory = run_directory.resolve()
        self.session_id = session_id
        self.task_id = task_id
        self.run_id = run_id
        self.requested_base = base_commit
        self.require_clean_source = require_clean_source
        self.max_patch_bytes = max_patch_bytes
        self.independent_repository = independent_repository
        self.repository = Path()
        self.workspace = Path()
        self.worktree_root = self.run_directory / "worktree"
        self.replay_root = self.run_directory / "replay"
        self.manifest_path = self.run_directory / "workspace.json"
        self.patch_path = self.run_directory / "changes.patch"
        self.base_commit = ""
        self._prepared = False
        self._delivery_git_config = b""

    def prepare(self) -> Path:
        """核验干净 Git 起点，创建 detached worktree 并返回对应任务目录。"""
        try:
            repository_text = self._source_git("rev-parse", "--show-toplevel").strip()
        except ValueError as error:
            # 隔离模式依赖已有 commit；普通直写模式并没有这个前置条件。
            raise ValueError(
                "worktree mode requires a Git repository with at least one commit"
            ) from error
        self.repository = Path(repository_text).resolve(strict=True)
        if not self.source_workspace.is_relative_to(self.repository):
            raise ValueError("workspace is outside discovered Git repository")
        if self.run_directory.is_relative_to(self.repository):
            raise ValueError("task worktree state must be outside source repository")
        if self.require_clean_source:
            status = self._source_git("status", "--porcelain=v1", "--untracked-files=all")
            # 源工作区的未提交内容不会进入固定 commit，继续会让用户误以为任务看到了它们。
            if status:
                raise ValueError("source Git workspace must be clean before task isolation")
        self.base_commit = self._source_git(
            "rev-parse", "--verify", f"{self.requested_base}^{{commit}}"
        ).strip()
        relative = self.source_workspace.relative_to(self.repository)
        # 容器只挂载所选工作目录；子目录看不到祖先 .git，不能悄悄回退宿主。
        if self.independent_repository and relative.parts:
            raise ValueError("Docker isolated Git currently requires the repository root")
        self.run_directory.mkdir(parents=True, exist_ok=False)
        try:
            # Docker 只挂载完整任务副本，不暴露宿主源仓库元数据或共享 objects。
            if self.independent_repository:
                self._source_git(
                    "clone",
                    "--no-local",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--",
                    str(self.repository),
                    str(self.worktree_root),
                )
                self._git_at(self.worktree_root, "remote", "remove", "origin")
                self._git_at(self.worktree_root, "checkout", "--detach", self.base_commit)
                self._delivery_git_config = (self.worktree_root / ".git/config").read_bytes()
            else:
                self._source_git(
                    "worktree", "add", "--detach", str(self.worktree_root), self.base_commit
                )
        except BaseException:
            # worktree add 可能在失败前创建目录或登记元数据，统一做保守清理。
            self._remove_owned_worktree(self.worktree_root, force=True)
            raise
        self.workspace = (self.worktree_root / relative).resolve(strict=True)
        self._prepared = True
        self._write_manifest("active")
        return self.workspace

    async def finish(
        self,
        *,
        verification_command: tuple[str, ...] = (),
        verification_timeout_seconds: float = 120.0,
        failed: bool = False,
        verification_backend_factory: Callable[[Path], ExecutionBackend] | None = None,
        verification_started: Callable[[ExecutionBackend], None] | None = None,
    ) -> GitDelivery:
        """导出全部修改、在干净 worktree 重放，并只自动删除无修改任务。"""
        if not self._prepared:
            raise RuntimeError("task worktree is not prepared")
        if self.independent_repository:
            self._prepare_delivery_metadata()
        changed = self.changed_files()
        if not changed:
            self._remove_owned_worktree(self.worktree_root)
            self._write_manifest("cleaned", changed_files=())
            return GitDelivery(
                source_repository=self.repository,
                workspace=self.workspace,
                base_commit=self.base_commit,
                state="cleaned",
            )

        patch = self._build_patch()
        if len(patch) > self.max_patch_bytes:
            self._write_manifest("failed_retained", changed_files=changed)
            raise ValueError("task patch exceeds configured size limit; worktree retained")
        self._write_private(self.patch_path, patch)
        digest = hashlib.sha256(patch).hexdigest()
        replay_status: Literal["passed", "failed"] = "failed"
        verification: VerificationResult | None = None
        verifier: IndependentVerifier | None = None
        replay_retained = False
        try:
            # Docker 验证副本也必须自带 Git 元数据；测试可能依赖 Git 版本信息。
            if self.independent_repository:
                self._source_git(
                    "clone",
                    "--no-local",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--",
                    str(self.repository),
                    str(self.replay_root),
                )
                self._git_at(self.replay_root, "remote", "remove", "origin")
                self._git_at(self.replay_root, "checkout", "--detach", self.base_commit)
            else:
                self._source_git(
                    "worktree", "add", "--detach", str(self.replay_root), self.base_commit
                )
            self._git_at(self.replay_root, "apply", "--check", "--binary", str(self.patch_path))
            self._git_at(self.replay_root, "apply", "--binary", str(self.patch_path))
            replay_status = "passed"
            if verification_command:
                verifier = IndependentVerifier(
                    verification_command,
                    verification_timeout_seconds,
                    backend_factory=verification_backend_factory,
                    on_started=verification_started,
                )
                verification = await verifier.verify(self.replay_root)
        except (OSError, RuntimeError, ValueError):
            # 重放失败不能删除原工作树或补丁；最终状态明确要求人工检查。
            replay_status = "failed"
        finally:
            # 仅在验证后端确定关闭后删除自有副本；未知资源仍保留挂载目录。
            verification_backend = verifier.backend if verifier is not None else None
            if verification_backend is None or any(
                event.kind == "backend_closed" for event in verification_backend.events
            ):
                self._remove_owned_worktree(self.replay_root, force=True)
            else:
                # 验证进程或容器未确定关闭时保留挂载目录，不删除仍可能被使用的代码。
                replay_retained = True
                self._write_manifest("failed_retained", replay_retained=str(self.replay_root))
        state: Literal["changes_retained", "failed_retained"] = (
            "failed_retained"
            if failed
            or replay_status != "passed"
            or (verification is not None and not verification.passed)
            else "changes_retained"
        )
        self._write_manifest(
            state,
            changed_files=changed,
            patch_sha256=digest,
            replay_status=replay_status,
            verification=verification,
            replay_retained=str(self.replay_root) if replay_retained else None,
        )
        return GitDelivery(
            source_repository=self.repository,
            workspace=self.workspace,
            base_commit=self.base_commit,
            state=state,
            changed_files=changed,
            patch_path=self.patch_path,
            patch_sha256=digest,
            replay_status=replay_status,
            verification=verification,
        )

    def _prepare_delivery_metadata(self) -> None:
        """后端关闭后核验副本元数据，并恢复控制器配置，防止候选 filter 在宿主执行。"""
        metadata = self.worktree_root / ".git"
        # 元数据重定向不是可交付代码；未知路径必须保留，不能回退读取宿主仓库。
        if metadata.is_symlink() or not metadata.is_dir() or (metadata / "commondir").exists():
            raise ValueError("isolated Git metadata was redirected; workspace retained")
        for index, path in enumerate(metadata.rglob("*")):
            if index >= 100000 or path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("unsafe isolated Git metadata; workspace retained")
        config = metadata / "config"
        if config.stat().st_size > 262144 or (metadata / "objects/info/alternates").exists():
            raise ValueError("unsafe isolated Git configuration; workspace retained")
        # git status/add 等也可能调用候选配置的 filter；候选结束后不在宿主信任它。
        actual = config.read_bytes()
        if actual != self._delivery_git_config:
            self._write_private(self.run_directory / "task-git-config.before-delivery", actual)
            _atomic_write(config, self._delivery_git_config)

    def changed_files(self) -> tuple[str, ...]:
        """使用 NUL 分隔 porcelain 输出返回新增、删除、重命名及模式变化路径。"""
        # 模型可在副本里运行 Git；交付前不能相信被重定向的元数据与工作区配置。
        if self.independent_repository:
            metadata = self.worktree_root / ".git"
            if not metadata.is_dir() or metadata.is_symlink():
                raise ValueError("isolated Git metadata was replaced; workspace retained")
            discovered = self._git_at(self.worktree_root, "rev-parse", "--show-toplevel").strip()
            common = self._git_at(
                self.worktree_root, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).strip()
            if (
                Path(discovered).resolve() != self.worktree_root
                or Path(common).resolve() != metadata
            ):
                raise ValueError("isolated Git metadata identity changed; workspace retained")
        output = self._git_bytes_at(
            self.worktree_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        fields = output.split(b"\0")
        # status 只看未提交内容；交付还必须包括任务在副本内已提交的改动。
        committed = self._git_bytes_at(
            self.worktree_root, "diff", "--name-only", "-z", self.base_commit
        )
        paths = [
            part.decode("utf-8", errors="surrogateescape")
            for part in committed.split(b"\0")
            if part
        ]
        index = 0
        while index < len(fields) and fields[index]:
            record = fields[index]
            if len(record) < 4:
                raise RuntimeError("invalid Git status record")
            status = record[:2]
            paths.append(record[3:].decode("utf-8", errors="surrogateescape"))
            index += 1
            # 重命名和复制记录还包含一个原路径；交付列表保留最终路径即可。
            if b"R" in status or b"C" in status:
                index += 1
        return tuple(sorted(set(paths)))

    def cleanup(self) -> None:
        """仅在任务 worktree 已干净且归属清单匹配时执行显式清理。"""
        if not self._prepared or not self.manifest_path.is_file():
            raise RuntimeError("task worktree ownership record is unavailable")
        record = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if (
            record.get("creator_uid") != os.getuid()
            or record.get("task_id") != self.task_id
            or record.get("worktree") != str(self.worktree_root)
        ):
            raise ValueError("task worktree ownership does not match cleanup request")
        # 任何未交付或事后新增内容都必须由用户先保存或清除，不能用 force 静默删除。
        if self.changed_files():
            raise ValueError("task worktree still has changes; refusing automatic cleanup")
        self._remove_owned_worktree(self.worktree_root)
        self._write_manifest("cleaned", changed_files=())

    def _build_patch(self) -> bytes:
        """通过临时 index 把未跟踪文件也纳入 Git 原生二进制补丁。"""
        index_path = self.run_directory / "delivery.index"
        environment = {**workspace_environment(), "GIT_INDEX_FILE": str(index_path)}
        try:
            self._git_at(self.worktree_root, "read-tree", self.base_commit, env=environment)
            # intent-to-add 不读取 clean filter 产物，但让 git diff 能看见未跟踪文件。
            self._git_at(self.worktree_root, "add", "-N", "-A", env=environment)
            patch = self._git_bytes_at(
                self.worktree_root,
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-textconv",
                "--find-renames",
                self.base_commit,
                env=environment,
            )
        finally:
            # 临时 index 只用于构建交付物，成功和失败都不应留给后续恢复误读。
            index_path.unlink(missing_ok=True)
        if not patch:
            raise RuntimeError("Git reported changes but produced an empty patch")
        return patch

    def _write_manifest(self, state: str, **details: object) -> None:
        """以仅所有者权限原子记录资源归属、状态和清理条件。"""
        payload = {
            "version": 1,
            "independent_repository": self.independent_repository,
            "creator_uid": os.getuid(),
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "source_repository": str(self.repository),
            "worktree": str(self.worktree_root),
            "base_commit": self.base_commit,
            "state": state,
            **details,
        }
        temporary = self.manifest_path.with_suffix(".tmp")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=lambda value: (
                value.model_dump(mode="json") if isinstance(value, Contract) else str(value)
            ),
        ).encode()
        self._write_private(temporary, encoded)
        os.replace(temporary, self.manifest_path)

    @staticmethod
    def _write_private(path: Path, content: bytes) -> None:
        """创建或截断仅当前用户可读写的控制器文件。"""
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("failed to write controller-owned file")
                remaining = remaining[written:]
        finally:
            os.close(descriptor)

    def _remove_owned_worktree(self, path: Path, *, force: bool = False) -> None:
        """只删除本次运行目录下的已知 worktree，路径或所有权异常时拒绝。"""
        resolved = path.resolve()
        if resolved.parent != self.run_directory or resolved.name not in {"worktree", "replay"}:
            raise ValueError("refusing to remove worktree outside owned run directory")
        if not resolved.exists():
            return
        # 完整副本只删除本对象的固定目录；调用者已经核验变更与归属。
        if self.independent_repository:
            shutil.rmtree(resolved)
            return
        arguments = ["worktree", "remove"]
        if force:
            arguments.append("--force")
        arguments.append(str(resolved))
        self._source_git(*arguments)

    def _source_git(self, *arguments: str) -> str:
        """在用户选择的源目录运行固定 Git argv 并返回文本。"""
        return self._run_git(self.source_workspace, arguments).decode(
            "utf-8", errors="surrogateescape"
        )

    def _git_at(self, cwd: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
        """在指定自有目录运行固定 Git argv 并返回文本。"""
        return self._run_git(cwd, arguments, env=env).decode("utf-8", errors="surrogateescape")

    def _git_bytes_at(self, cwd: Path, *arguments: str, env: dict[str, str] | None = None) -> bytes:
        """在指定自有目录运行 Git，并为补丁和 NUL 输出保留原始字节。"""
        return self._run_git(cwd, arguments, env=env)

    @staticmethod
    def _run_git(
        cwd: Path, arguments: tuple[str, ...], *, env: dict[str, str] | None = None
    ) -> bytes:
        """不经 Shell 执行 Git，失败时只暴露有界诊断。"""
        process = subprocess.run(
            ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *arguments),
            cwd=cwd,
            env=env if env is not None else workspace_environment(),
            timeout=120,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise ValueError(f"git command failed: {detail or process.returncode}")
        return process.stdout
