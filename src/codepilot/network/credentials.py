"""控制器持有 GitHub App 短期凭据；加密租约支持崩溃后的精确撤销，不重放业务。"""

import base64
import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codepilot.network.models import GitHubRepository
from codepilot.network.transport import exchange
from codepilot.sessions.outputs import private_directory
from codepilot.sessions.store import SessionStore, process_identity
from codepilot.tools.base import ToolFailure
from codepilot.tools.file_common import _atomic_write

VERSION = b"2026-03-10"


def file_identity(path: Path, maximum: int = 16384) -> tuple[int, ...]:
    """只读取私有普通文件身份，审批阶段不读取任何私钥正文。"""
    info = path.lstat()
    # 硬链接可能在工作区暴露同一私钥；路径在外还不够，文件与所有父路径都须独立可信。
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or not 1 <= info.st_size <= maximum
        or any(p.is_symlink() for p in path.parents)
    ):
        raise ValueError("credential file must be private, regular and bounded")
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size


def read_private(path: Path, identity: tuple[int, ...], maximum: int) -> bytes:
    """打开后与审批身份再次比较，拒绝链接替换和并发修改。"""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        observed = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
        # 路径检查与打开之间可发生替换，必须同时核对 fd 和当前路径的审批版本。
        if observed != identity or file_identity(path, maximum) != identity:
            raise ValueError("credential identity changed")
        data = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        # 读后再次核验，不能把检查之后并发改写的正文用于签名或解密。
        if (
            (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size)
            != identity
            or file_identity(path, maximum) != identity
            or len(data) > maximum
        ):
            raise ValueError("credential identity changed")
        return data
    finally:
        # 解析或身份核验失败也不得泄漏文件描述符。
        os.close(descriptor)


class CredentialVault:
    """状态根下独立密文库；会话备份只保留不透明 ID，不携带解密钥匙。"""

    def __init__(self, root: Path) -> None:
        """仅绑定目录，读取诊断和预览不能意外生成新主密钥。"""
        self.directory = root / ".credential-leases"
        self.key_path = self.directory / "master.key"

    def path(self, identity: str) -> Path:
        """只接受控制器随机 ID，拒绝资源记录中的路径穿越。"""
        # 资源 ID 不是任意文件路径，不能让损坏记录选择别的宿主文件。
        if re.fullmatch(r"credential-[a-f0-9]{32}", identity) is None:
            raise ValueError("invalid credential lease id")
        return self.directory / (identity + ".json")

    def load(self, identity: str, session_id: str) -> dict[str, Any]:
        """核验精确所属会话、当前 uid 和版本，未解密也可安全诊断阶段。"""
        path = self.path(identity)
        data = read_private(path, file_identity(path, 65536), 65536)
        record: dict[str, Any] = json.loads(data)
        # 相同状态根可以有多个会话，只有精确归属匹配才有资格读取生命周期。
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or record.get("uid") != os.getuid()
            or record.get("id") != identity
            or record.get("session_id") != session_id
            or record.get("phase") not in {"prepared", "issuing", "issued", "revoking", "closed"}
        ):
            raise ValueError("credential lease identity mismatch")
        # 已知令牌的密文丢失不等于从未收到令牌，拒绝把损坏降格为可接受的发行未知。
        if (record["phase"] in {"issued", "revoking"}) != ("ciphertext" in record):
            raise ValueError("credential ciphertext/phase mismatch")
        return record

    def save(self, record: dict[str, Any]) -> None:
        """私有原子文件与目录 fsync 先于外部请求，防止崩溃丢失发送意图。"""
        private_directory(self.directory)
        path = self.path(record["id"])
        if path.exists():
            file_identity(path, 65536)
        _atomic_write(path, json.dumps(record).encode())
        self.sync_directory()

    def sync_directory(self) -> None:
        """目录项也持久化，主密钥与密文不能只依赖文件内容 fsync。"""
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def key(self, *, create: bool) -> bytes:
        """只在全新库创建 AES 密钥；已有租约缺钥时绝不能偷偷替换。"""
        private_directory(self.directory)
        # 新库和损坏旧库必须分开；重新生成密钥不能恢复旧 token 的撤销能力。
        if create and not self.key_path.exists():
            # 存在旧清单却没有钥匙代表损坏，创建新钥匙会永久掩盖可恢复事实。
            if any(self.directory.glob("credential-*.json")):
                raise ValueError("credential master key missing")
            try:
                _atomic_write(self.key_path, os.urandom(32), expected_absent=True)
            except ToolFailure:
                # 多会话并发初始化只允许一个创建者，其他实例核验获胜的密钥。
                if not self.key_path.exists():
                    raise
            self.sync_directory()
        value = read_private(self.key_path, file_identity(self.key_path, 32), 32)
        if len(value) != 32:
            raise ValueError("invalid credential master key")
        return value

    def aad(self, record: dict[str, Any]) -> bytes:
        """密文绑定不透明身份与范围，不能把别的会话密文移接到当前资源。"""
        return json.dumps(
            [record[k] for k in ("version", "uid", "id", "session_id", "run_id", "scope")],
            separators=(",", ":"),
        ).encode()

    def encrypt(self, record: dict[str, Any], token: bytes) -> None:
        """接到令牌后立即提交认证密文，任何明文都不交给会话或 worker。"""
        nonce = os.urandom(12)
        encrypted = AESGCM(self.key(create=False)).encrypt(nonce, token, self.aad(record))
        record.update(ciphertext=base64.b64encode(nonce + encrypted).decode(), phase="issued")
        self.save(record)

    def decrypt(self, record: dict[str, Any]) -> bytes:
        """恢复只解密当前资源，损坏或缺钥时保留证据并失败关闭。"""
        value = base64.b64decode(record["ciphertext"], validate=True)
        return AESGCM(self.key(create=False)).decrypt(value[:12], value[12:], self.aad(record))


class GitHubLease:
    """一次命令的单仓库令牌；不续期、不盲目重试签发与撤销。"""

    def __init__(
        self,
        repository: GitHubRepository,
        identity: tuple[int, ...],
        session: SessionStore,
        run_id: str,
    ) -> None:
        """构造只绑定可信配置，真正申请需已批准的网关请求触发。"""
        self.repository, self.identity, self.session = repository, identity, session
        self.vault = CredentialVault(session.directory.parent)
        self.run_id = run_id
        self.token: bytes | None = None
        self.expires = 0.0
        self.record: dict[str, Any] | None = None

    def jwt_authorization(self) -> str:
        """只能在发行方 TLS 可信后读取 PEM 并签短时 RS256 JWT。"""
        key = read_private(self.repository.private_key_file, self.identity, 16384)
        now = int(time.time())
        try:
            private_key = serialization.load_pem_private_key(key, password=None)
            if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
                raise ValueError("GitHub App requires RSA >= 2048 bits")
            value = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.repository.client_id},
                private_key,
                algorithm="RS256",
            )
        except Exception:
            # 加密库异常不能带 PEM 或解析详情进入工具结果。
            raise ToolFailure("github_app_key_invalid") from None
        return "Bearer " + value

    def phase(self, state: str) -> None:
        """发送之前持久化阶段，网络发送失败不能抹掉已经预留的意图。"""
        assert self.record is not None
        self.record["phase"] = state
        self.vault.save(self.record)

    async def acquire(self) -> bytes:
        """显式单仓库与权限签发，先保留可撤销密文再校验服务端范围。"""
        # 同一命令重用已核验的 token，过期只能结束调用，不能悄悄扩大签发次数。
        if self.token is not None:
            if time.time() >= self.expires - 5:
                raise ToolFailure("github_token_expired; no automatic renewal")
            return self.token
        # 有过申请即不能在连接失败后重发，未知发行只允许显式协调。
        if self.record is not None:
            raise ToolFailure("github_issuance_unknown; do not replay")
        self.vault.key(create=True)
        self.record = {
            "version": 1,
            "uid": os.getuid(),
            "id": "credential-" + uuid4().hex,
            "session_id": self.session.session_id,
            "run_id": self.run_id,
            "scope": self.repository.scope,
            "controller_pid": os.getpid(),
            "controller_start": process_identity(os.getpid()),
            "phase": "prepared",
        }
        self.vault.save(self.record)
        self.session.resource("credential_lease", {"lease_id": self.record["id"]})
        result = await exchange(
            "api.github.com",
            f"/app/installations/{self.repository.installation_id}/access_tokens",
            "POST",
            json.dumps(
                {
                    "repository_ids": [self.repository.repository_id],
                    "permissions": {"contents": self.repository.contents},
                }
            ).encode(),
            authorization=self.jwt_authorization,
            sending=lambda: self.phase("issuing"),
            headers=(
                (b"accept", b"application/vnd.github+json"),
                (b"content-type", b"application/json"),
                (b"x-github-api-version", VERSION),
            ),
        )
        # 未知或拒绝正文不回显；只有含合法 token 才能尝试按令牌撤销。
        try:
            data = json.loads(result.body)
            value = data.get("token")
            if not isinstance(value, str) or not re.fullmatch(r"[!-~]{16,4096}", value):
                raise ValueError("invalid token")
            self.token = value.encode()
            self.vault.encrypt(self.record, self.token)
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            repos = data["repositories"]
            permissions = data["permissions"]
            if (
                result.status != 201
                or not time.time() + 10 < expires <= time.time() + 3660
                or len(repos) != 1
                or repos[0]["id"] != self.repository.repository_id
                or repos[0]["full_name"] != f"{self.repository.owner}/{self.repository.repository}"
                or permissions.get("contents") != self.repository.contents
                or any(k not in {"contents", "metadata"} for k in permissions)
                or permissions.get("metadata", "read") != "read"
                or data.get("repository_selection", "selected") != "selected"
            ):
                raise ValueError("issuer scope or expiry mismatch")
            self.expires = expires
            return self.token
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ToolFailure("github_issuer_response_rejected; inspect credential lease") from None

    async def close(self) -> None:
        """先前未发送可本地结算，已知 token 必须取得服务端 204 才删除密文。"""
        # 未触发签发的引用没有外部资源，清理也不能为它发起额外请求。
        if self.record is None:
            return
        if self.token is not None:
            await revoke(self.vault, self.record, self.token)
            self.token = None
        elif self.record["phase"] == "prepared":
            self.phase("closed")
        else:
            raise ToolFailure("github_issuance_unknown; requires explicit network reconciliation")


async def revoke(vault: CredentialVault, record: dict[str, Any], token: bytes) -> None:
    """撤销控制面独立于已耗尽的业务流量预算；失败保留密文和未知事实。"""

    def sending() -> None:
        """请求可被发行方接受之前保存撤销意图。"""
        record["phase"] = "revoking"
        vault.save(record)

    result = await exchange(
        "api.github.com",
        "/installation/token",
        "DELETE",
        authorization=lambda: "Bearer " + token.decode("ascii"),
        sending=sending,
        headers=((b"accept", b"application/vnd.github+json"), (b"x-github-api-version", VERSION)),
    )
    # 401 或超时不能证明本次撤销已完成；保留密文供明确的维护操作再核验。
    if result.status != 204:
        raise ToolFailure("github_revocation_unconfirmed; resources prune required")
    record.pop("ciphertext", None)
    record["phase"] = "closed"
    vault.save(record)


async def recover(
    session: SessionStore, identity: str, *, apply: bool, accept_unknown: bool = False
) -> dict[str, Any]:
    """持会话锁回收凭据，不重放 Git；无 token 的发行未知需单独明确接受。"""
    vault = CredentialVault(session.directory.parent)
    record = vault.load(identity, session.session_id)
    result: dict[str, Any] = {"eligible": False, "removed": False, "phase": record["phase"]}
    # 幂等维护只读取已闭合事实，不再次向发行方发送认证请求。
    if record["phase"] == "closed":
        return {**result, "removed": True, "reason": "already_closed"}
    current = process_identity(record["controller_pid"])
    # 会话锁之外再核对进程起点，拒绝接管仍在运行的原控制器资源。
    if current and current == record["controller_start"] and session.get("status") == "running":
        return {**result, "reason": "credential_controller_still_active"}
    result["eligible"] = True
    if apply:
        if "ciphertext" in record:
            await revoke(vault, record, vault.decrypt(record))
        elif record["phase"] == "prepared" or (accept_unknown and record["phase"] == "issuing"):
            # 接受未知只记录人为结论；不声称发行方已撤销没有收到的 token。
            record.update(phase="closed", issuance_unknown_accepted=record["phase"] != "prepared")
            vault.save(record)
        else:
            return {**result, "reason": "issuance_unknown_requires_accept_network_unknown"}
        result.update(removed=True, phase="closed")
    return result
