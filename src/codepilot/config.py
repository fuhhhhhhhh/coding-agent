"""加载分层 YAML/TOML 产品配置；配置文件只保存密钥环境变量名。"""

import tomllib
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import Field, field_validator, model_validator

from codepilot.context import ContextLimits
from codepilot.contracts.models import Contract
from codepilot.contracts.run import RunLimits
from codepilot.workspace import BackendLimits

_MAX_CONFIG_BYTES = 256 * 1024


class Config(Contract):
    """保存 M01 仅支持 fake Provider 的最小离线配置。"""

    provider: Literal["fake"] = "fake"
    model: Literal["fake-m01"] = "fake-m01"


class ProviderConfig(Contract):
    """保存 Provider 选择、模型、网络入口和有限请求参数。"""

    kind: Literal["fake", "openai-compatible"] = "fake"
    model: Annotated[str, Field(min_length=1)] = "fake-codepilot"
    base_url: str | None = None
    api_key_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")] | None = "CODEPILOT_API_KEY"
    api_key_file: Path | None = None
    max_tokens: Annotated[int, Field(ge=1, le=65_536)] = 2_048
    max_requests: Annotated[int, Field(ge=1, le=100)] = 8
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 60.0

    @field_validator("api_key_file", mode="before")
    @classmethod
    def parse_api_key_file(cls, value: object) -> object:
        """在严格模型边界把 YAML/TOML 路径字符串显式转换为 Path。"""
        return Path(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def check_provider_fields(self) -> Self:
        """拒绝缺少网关的真实 Provider，并避免 fake 配置伪装成联网模式。"""
        if self.kind == "openai-compatible":
            if self.base_url is None or not self.base_url.startswith(("https://", "http://")):
                raise ValueError("openai-compatible provider requires an HTTP base_url")
        elif self.base_url is not None:
            raise ValueError("fake provider does not accept base_url")
        elif not self.model.startswith("fake-"):
            raise ValueError("fake provider requires a fake model name")
        elif self.api_key_file is not None:
            raise ValueError("fake provider does not accept api_key_file")
        return self


class BackendConfig(Contract):
    """保存 Local 或 Docker 工作区后端的启动参数。"""

    kind: Literal["local", "docker"] = "local"
    docker_image: str | None = None
    limits: BackendLimits = BackendLimits()

    @model_validator(mode="after")
    def check_docker_image(self) -> Self:
        """只在选择 Docker 时要求显式镜像，避免悄悄使用不受控标签。"""
        if self.kind == "docker" and not (self.docker_image and self.docker_image.strip()):
            raise ValueError("docker backend requires docker_image")
        if self.kind == "local" and self.docker_image is not None:
            raise ValueError("local backend does not accept docker_image")
        return self


class GitWorkspaceConfig(Contract):
    """保存任务工作树、固定起点和独立复验所需的产品配置。"""

    enabled: bool = True
    base_commit: Annotated[str, Field(min_length=1)] = "HEAD"
    require_clean_source: bool = True
    verification_command: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    verification_timeout_seconds: Annotated[float, Field(gt=0, le=900)] = 120.0
    max_patch_bytes: Annotated[int, Field(ge=1024, le=64 * 1024 * 1024)] = 16 * 1024 * 1024

    @field_validator("verification_command", mode="before")
    @classmethod
    def parse_verification_command(cls, value: object) -> object:
        """在 YAML 边界把自然数组显式转换为内部不可变 argv。"""
        return tuple(value) if isinstance(value, list) else value


class ProductConfig(Contract):
    """描述统一产品入口装配一次 Agent 运行所需的全部非秘密参数。"""

    provider: ProviderConfig = ProviderConfig()
    backend: BackendConfig = BackendConfig()
    git_workspace: GitWorkspaceConfig = GitWorkspaceConfig()
    permission: Literal["read-only", "ask-edits", "controlled-edit"] = "ask-edits"
    runtime: RunLimits = RunLimits()
    context: ContextLimits = ContextLimits()
    scheduler_concurrency: Annotated[int, Field(ge=1, le=32)] = 4
    state_directory: Path | None = None

    @field_validator("state_directory", mode="before")
    @classmethod
    def parse_state_directory(cls, value: object) -> object:
        """只在配置输入边界把配置文件中的路径字符串转换为 Path。"""
        return Path(value) if isinstance(value, str) else value


def load_config(path: Path | None = None) -> Config:
    """读取并校验 ``path`` 指向的 TOML；未提供路径时返回离线默认配置。"""
    # 未传路径表示使用默认值；显式传入的错误路径必须原样暴露给调用方。
    if path is None:
        return Config()
    with path.open("rb") as file:
        return Config.model_validate(tomllib.load(file))


def load_product_config(
    *,
    workspace: Path,
    explicit_path: Path | None = None,
    user_path: Path | None = None,
    overrides: Mapping[str, object] | None = None,
) -> ProductConfig:
    """按用户、项目、显式文件和命令行覆盖的顺序合并严格配置。

    缺少默认用户配置或项目配置属于正常情况；用户显式传入的文件不存在时保留
    ``FileNotFoundError``，让 CLI 能区分“没有默认配置”和“路径写错”。
    """
    merged: dict[str, Any] = {}
    default_user = (
        _select_optional_config(_default_user_config_paths(), "用户级")
        if user_path is None
        else user_path
    )
    project_path = _select_optional_config(
        (
            workspace / ".codepilot" / "config.yaml",
            workspace / ".codepilot" / "config.yml",
            workspace / ".codepilot.yaml",
            workspace / ".codepilot.yml",
            workspace / ".codepilot.toml",
        ),
        "工作区",
    )
    for path, required in (
        (default_user, False),
        (project_path, False),
        (explicit_path, explicit_path is not None),
    ):
        if path is None:
            continue
        if not path.exists():
            if required:
                raise FileNotFoundError(path)
            continue
        _deep_merge(merged, _read_product_config(path))
    if overrides:
        _deep_merge(merged, dict(overrides))
    return ProductConfig.model_validate(merged)


def _default_user_config_paths() -> tuple[Path, ...]:
    """根据 XDG 约定返回支持的用户配置候选，不读取任何凭据。"""
    import os

    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    directory = root / "codepilot"
    return (
        directory / "config.yaml",
        directory / "config.yml",
        directory / "config.toml",
    )


def _select_optional_config(paths: tuple[Path, ...], layer: str) -> Path | None:
    """选择一个配置文件；同层出现多个候选时拒绝猜测优先级。"""
    existing = tuple(path for path in paths if path.exists())
    # 同层文件没有合理的隐式优先级，静默选择会让用户编辑错误的配置副本。
    if len(existing) > 1:
        names = ", ".join(str(path) for path in existing)
        raise ValueError(f"{layer}存在多个 CodePilot 配置文件：{names}")
    return existing[0] if existing else None


def _read_product_config(path: PathLike[str] | str) -> dict[str, Any]:
    """按扩展名安全读取 YAML 或 TOML，并要求顶层必须是映射。"""
    resolved = Path(path)
    if resolved.stat().st_size > _MAX_CONFIG_BYTES:
        raise ValueError(f"配置文件超过 {_MAX_CONFIG_BYTES} 字节：{resolved}")
    suffix = resolved.suffix.lower()
    if suffix == ".toml":
        return _read_toml(resolved)
    if suffix in {".yaml", ".yml"}:
        # safe_load 禁止 Python 对象标签，避免把普通配置文件变成代码执行入口。
        try:
            with resolved.open("r", encoding="utf-8") as stream:
                loaded = yaml.safe_load(stream)
        except yaml.YAMLError as error:
            # 统一转换为配置边界的 ValueError，让 CLI 给出稳定退出码而不泄露正文。
            raise ValueError(f"YAML 配置解析失败：{resolved}") from error
        # 空 YAML 等价于没有覆盖；列表等其他顶层则无法与分层配置递归合并。
        if loaded is None:
            return {}
        if not isinstance(loaded, Mapping):
            raise ValueError(f"YAML 配置顶层必须是对象：{resolved}")
        return dict(loaded)
    raise ValueError(f"不支持的配置文件扩展名：{resolved.suffix or '<none>'}")


def _read_toml(path: PathLike[str] | str) -> dict[str, Any]:
    """读取一个 TOML 对象，并让解析错误保留原始异常类型。"""
    with Path(path).open("rb") as stream:
        return tomllib.load(stream)


def _deep_merge(target: dict[str, Any], source: Mapping[str, object]) -> None:
    """递归覆盖嵌套表，使高优先级配置只需声明变化字段。"""
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            # 复制映射，避免调用方后续修改命令行覆盖对象影响已加载配置。
            target[key] = dict(value) if isinstance(value, Mapping) else value
