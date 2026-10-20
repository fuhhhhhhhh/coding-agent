"""定义可信用户提供的目的地、秘密引用和运行级联网额度。"""

import re
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, field_validator, model_validator

from codepilot.contracts.models import Contract


def checked_host(value: str) -> str:
    """仅接受规范 ASCII DNS 名，拒绝 IP、通配符和隐式目的地转换。"""
    # 只保留一种域名表示，避免审批范围和实际 TLS/解析目标出现规范化差异。
    if (
        value != value.lower()
        or len(value) > 253
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
        or "." not in value
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in value.split(".")
        )
        or value.replace(".", "").isdigit()
    ):
        raise ValueError("network host requires a canonical DNS name")
    return value


def checked_path(value: str) -> str:
    """限定路径为无歧义 ASCII 形式，避免编码路径绕过凭据作用域。"""
    # 不猜测服务端如何解码斜杠和点段，存在歧义时在发送凭据前拒绝。
    if (
        not value.startswith("/")
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
        or any(c in value for c in "%\\?#")
        or "//" in value
        or any(p in {".", ".."} for p in value.split("/"))
    ):
        raise ValueError("network path must be canonical and unencoded")
    return value


class Destination(Contract):
    """精确 HTTPS 域名及路径；端口固定 443，前缀在路径段边界匹配。"""

    host: str
    path_prefix: str = "/"

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """在配置阶段拒绝模糊主机范围。"""
        return checked_host(value)

    @field_validator("path_prefix")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """路径范围使用与请求完全相同的规范规则。"""
        return checked_path(value)

    def permits(self, host: str, path: str) -> bool:
        """按精确域名和目录边界比较，不让 /api 匹配 /api-evil。"""
        prefix = self.path_prefix.rstrip("/")
        return host == self.host and (not prefix or path == prefix or path.startswith(prefix + "/"))


class SecretReference(Destination):
    """值只从工作区外的私有文件读取，不接受模型提供的凭据正文。"""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    file: Path

    @field_validator("file", mode="before")
    @classmethod
    def parse_secret_path(cls, value: object) -> object:
        """配置边界转换绝对路径，避免跟随运行目录变化。"""
        path = Path(value).expanduser() if isinstance(value, str) else value
        # 相对引用会随用户切换仓库改变秘密来源，因此可信配置也必须使用绝对路径。
        if isinstance(path, Path) and not path.is_absolute():
            raise ValueError("secret reference requires an absolute file path")
        return path


class GitHubRepository(Contract):
    """可信用户声明单个 GitHub App 仓库与最小权限，私钥不进入工具参数。"""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    client_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]
    installation_id: Annotated[int, Field(gt=0)]
    repository_id: Annotated[int, Field(gt=0)]
    owner: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")]
    repository: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,99}$")]
    private_key_file: Path
    contents: Literal["read", "write"] = "read"

    @field_validator("private_key_file", mode="before")
    @classmethod
    def parse_key(cls, value: object, info: ValidationInfo) -> object:
        """私钥必须固定在绝对路径，不能随工作区切换。"""
        path = Path(value).expanduser() if isinstance(value, str) else value
        if isinstance(path, Path) and not path.is_absolute():
            raise ValueError("GitHub key requires absolute path")
        # JSON 模式的 Path 核心校验接收字符串，Python/YAML 模式则接收已转换 Path。
        return str(path) if info.mode == "json" and isinstance(path, Path) else path

    @model_validator(mode="after")
    def canonical_repository(self) -> Self:
        """拒绝具有 URL 或目录歧义的仓库名。"""
        if self.repository.endswith(".git") or ".." in self.repository:
            raise ValueError("GitHub repository requires canonical name without .git")
        return self

    @property
    def scope(self) -> str:
        """审批展示精确仓库和读写，不把名字解释成全安装授权。"""
        return f"https://github.com/{self.owner}/{self.repository}.git:contents={self.contents}"


class NativeNetworkConfig(Contract):
    """声明原生 CLI 的独立 TCP:443 目的地与运行累计额度，不继承 HTTP 路径权限。"""

    enabled: bool = False
    hosts: tuple[str, ...] = ()
    github_repositories: tuple[GitHubRepository, ...] = ()
    max_connections: Annotated[int, Field(ge=1, le=128)] = 32
    max_parallel: Annotated[int, Field(ge=1, le=16)] = 4
    total_bytes: Annotated[int, Field(ge=1024, le=256 * 1024**2)] = 16 * 1024**2
    lease_seconds: Annotated[float, Field(gt=0, le=600)] = 120.0
    idle_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0

    @field_validator("hosts", "github_repositories", mode="before")
    @classmethod
    def parse_hosts(cls, value: object) -> object:
        """配置数组转不可变域名集合，保留严格输入校验。"""
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def check_hosts(self) -> Self:
        """空授权不能变为全网，主机范围必须独立、精确且有限。"""
        if self.enabled and not self.hosts and not self.github_repositories:
            raise ValueError("native network requires explicit hosts")
        if len(self.hosts) > 32 or len(set(self.hosts)) != len(self.hosts):
            raise ValueError("native network hosts must be unique and bounded")
        if len(self.github_repositories) > 8 or len(
            {r.name for r in self.github_repositories}
        ) != len(self.github_repositories):
            raise ValueError("GitHub references must be unique and bounded")
        if len({(r.owner, r.repository) for r in self.github_repositories}) != len(
            self.github_repositories
        ):
            raise ValueError("GitHub repository scopes must be unique")
        for host in self.hosts:
            checked_host(host)
        return self


class NetworkConfig(Contract):
    """默认关闭的请求代理；所有重定向共享一次运行的有限额度。"""

    enabled: bool = False
    native: NativeNetworkConfig = NativeNetworkConfig()
    destinations: tuple[Destination, ...] = ()
    secrets: tuple[SecretReference, ...] = ()
    max_requests: Annotated[int, Field(ge=1, le=64)] = 8
    max_response_bytes: Annotated[int, Field(ge=1024, le=1024**2)] = 256 * 1024
    total_bytes: Annotated[int, Field(ge=1024, le=8 * 1024**2)] = 1024**2
    timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 20.0
    lease_seconds: Annotated[float, Field(gt=0, le=3600)] = 300.0
    max_redirects: Annotated[int, Field(ge=0, le=5)] = 3

    @field_validator("destinations", "secrets", mode="before")
    @classmethod
    def parse_entries(cls, value: object) -> object:
        """将自然 YAML 数组转换成不可变配置序列。"""
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """秘密不能偷偷增加网络目的地，重复命名必须在请求前拒绝。"""
        # 空允许列表和超大策略都不能隐式退化为无限制联网。
        if self.native.enabled and not self.enabled:
            raise ValueError("native network requires network.enabled")
        if self.enabled and not self.destinations and not self.native.enabled:
            raise ValueError("enabled network requires explicit destinations")
        if len(self.destinations) > 32 or len(self.secrets) > 16:
            raise ValueError("network scope exceeds entry limit")
        # 重名引用不能按最后写入者选择，否则审批中的名字无法确定实际秘密。
        if len({s.name for s in self.secrets}) != len(self.secrets):
            raise ValueError("duplicate network secret reference")
        for ref in self.secrets:
            # 凭据声明不是网络授权来源，必须处于已批准的目的地范围内。
            if not any(d.permits(ref.host, ref.path_prefix) for d in self.destinations):
                raise ValueError("secret reference must stay within network destinations")
        return self


def parse_url(url: str) -> tuple[str, str, str]:
    """校验请求入口并返回规范域名、路径和 HTTP 请求目标。"""
    # 在 URL 解析器丢弃换行或做隐式转换前拒绝控制字符和无界输入。
    if len(url) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ValueError("network URL must be bounded ASCII")
    parsed = urlsplit(url)
    host = checked_host(parsed.hostname or "")
    # 用户信息、非标准端口和片段不参与此次固定 GET 能力，禁止模糊批准。
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in (None, 443)
        or parsed.netloc not in (host, host + ":443")
    ):
        raise ValueError("only canonical HTTPS port 443 without userinfo or fragment is supported")
    path = checked_path(parsed.path or "/")
    target = path + ("?" + parsed.query if parsed.query else "")
    return host, path, target
