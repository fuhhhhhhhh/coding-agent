"""为每个产品任务创建固定 Git worktree，并导出可重放补丁。"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Literal

from codepilot.contracts.models import Contract, Identifier
from codepilot.evals import IndependentVerifier, VerificationResult


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
        require_clean_source: bool = True,
        max_patch_bytes: int = 16 * 1024 * 1024,
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
        self.repository = Path()
        self.workspace = Path()
        self.worktree_root = self.run_directory / "worktree"
        self.replay_root = self.run_directory / "replay"
        self.manifest_path = self.run_directory / "workspace.json"
        self.patch_path = self.run_directory / "changes.patch"
        self.base_commit = ""
        self._prepared = False

    def prepare(self) -> Path:
        """核验干净 Git 起点，创建 detached worktree 并返回对应任务目录。"""
        try:
            repository_text = self._source_git("rev-parse", "--show-toplevel").strip()
        except ValueError as error:
            # 默认隔离模式依赖 Git；提示显式降级入口比透传底层 fatal 更可操作。
            raise ValueError(
                "workspace is not a Git repository; initialize Git or use --direct-workspace"
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
        self.run_directory.mkdir(parents=True, exist_ok=False)
        try:
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
    ) -> GitDelivery:
        """导出全部修改、在干净 worktree 重放，并只自动删除无修改任务。"""
        if not self._prepared:
            raise RuntimeError("task worktree is not prepared")
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
        try:
            self._source_git("worktree", "add", "--detach", str(self.replay_root), self.base_commit)
            self._git_at(self.replay_root, "apply", "--check", "--binary", str(self.patch_path))
            self._git_at(self.replay_root, "apply", "--binary", str(self.patch_path))
            replay_status = "passed"
            if verification_command:
                verification = await IndependentVerifier(
                    verification_command, verification_timeout_seconds
                ).verify(self.replay_root)
        except (OSError, RuntimeError, ValueError):
            # 重放失败不能删除原工作树或补丁；最终状态明确要求人工检查。
            replay_status = "failed"
        finally:
            # replay 目录完全由本对象创建且补丁已在外部保存，可强制删除验证副本。
            self._remove_owned_worktree(self.replay_root, force=True)
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

    def changed_files(self) -> tuple[str, ...]:
        """使用 NUL 分隔 porcelain 输出返回新增、删除、重命名及模式变化路径。"""
        output = self._git_bytes_at(
            self.worktree_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        fields = output.split(b"\0")
        paths: list[str] = []
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
        return tuple(sorted(paths))

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
        environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
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
            ("git", "-c", "core.hooksPath=/dev/null", *arguments),
            cwd=cwd,
            env=env,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise ValueError(f"git command failed: {detail or process.returncode}")
        return process.stdout
