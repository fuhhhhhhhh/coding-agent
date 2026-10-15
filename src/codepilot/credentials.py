"""从环境变量或权限受限的用户 YAML 文件解析 Provider 凭据。"""

import os
import stat
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import Field, SecretStr, field_validator

from codepilot.config import ProviderConfig
from codepilot.contracts.models import Contract

_MAX_CREDENTIAL_BYTES = 16 * 1024


class CredentialFile(Contract):
    """描述凭据文件允许保存的唯一秘密字段，并让对象展示自动脱敏。"""

    api_key: Annotated[SecretStr, Field(min_length=1)]

    @field_validator("api_key")
    @classmethod
    def reject_blank_key(cls, value: SecretStr) -> SecretStr:
        """拒绝只有空白字符的密钥，避免把认证失败误判为网络故障。"""
        if not value.get_secret_value().strip():
            raise ValueError("api_key must not be blank")
        return value


def resolve_provider_api_key(provider: ProviderConfig) -> str | None:
    """按环境变量优先、用户文件回退的顺序解析真实 Provider 密钥。"""
    if provider.kind == "fake":
        return None
    if provider.api_key_env is not None:
        environment_value = os.environ.get(provider.api_key_env)
        # 环境变量适合 CI 或临时覆盖；存在时不接触磁盘凭据文件。
        if environment_value:
            return environment_value
    if provider.api_key_file is not None:
        return _read_credential_file(provider.api_key_file)
    # 错误只列出来源名称，不包含任何可能的秘密值。
    source = provider.api_key_env or "未配置 api_key_env/api_key_file"
    raise ValueError(f"provider API key is unavailable: {source}")


def _read_credential_file(path: Path) -> str:
    """校验文件归属、权限、大小和 YAML Schema 后返回内存中的密钥。"""
    expanded = path.expanduser()
    # 符号链接可能把看似安全的固定路径重定向到权限和归属未知的位置。
    if expanded.is_symlink():
        raise ValueError(f"credential file must not be a symbolic link: {expanded}")
    metadata = expanded.stat()
    if not expanded.is_file():
        raise ValueError(f"credential path must be a regular file: {expanded}")
    if metadata.st_size > _MAX_CREDENTIAL_BYTES:
        raise ValueError(f"credential file exceeds {_MAX_CREDENTIAL_BYTES} bytes: {expanded}")
    if os.name == "posix":
        # 多用户服务器上只接受当前用户拥有且组/其他用户不可读写的文件。
        if metadata.st_uid != os.getuid():
            raise ValueError(f"credential file must be owned by the current user: {expanded}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError(f"credential file permissions must be 0600 or stricter: {expanded}")
    try:
        with expanded.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
    except yaml.YAMLError as error:
        # 不回显 YAML 正文，避免解析错误把秘密带入终端或日志。
        raise ValueError(f"credential YAML could not be parsed: {expanded}") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"credential YAML root must be an object: {expanded}")
    credential = CredentialFile.model_validate(loaded)
    return credential.api_key.get_secret_value()
