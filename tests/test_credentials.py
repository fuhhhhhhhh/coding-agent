"""验证用户凭据文件的来源优先级、文件安全条件和脱敏边界。"""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.config import ProviderConfig
from codepilot.credentials import CredentialFile, resolve_provider_api_key


def _real_provider(path: Path | None) -> ProviderConfig:
    """构造只用于离线测试的真实 Provider 配置，不创建 SDK 或网络请求。"""
    return ProviderConfig(
        kind="openai-compatible",
        model="test-model",
        base_url="https://mock.invalid/v1",
        api_key_env="CODEPILOT_TEST_KEY",
        api_key_file=path,
    )


def _write_credential(path: Path, content: str = "api_key: test-secret\n") -> None:
    """写入测试凭据并收紧权限，使正常路径满足多用户服务器约束。"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def test_credential_file_is_used_when_environment_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量未设置时应读取权限安全的固定 YAML 文件。"""
    path = tmp_path / "credentials.yaml"
    _write_credential(path)
    monkeypatch.delenv("CODEPILOT_TEST_KEY", raising=False)

    assert resolve_provider_api_key(_real_provider(path)) == "test-secret"


def test_provider_config_parses_yaml_credential_path() -> None:
    """严格 Provider 配置应显式把 YAML 路径字符串转换为 Path。"""
    provider = ProviderConfig.model_validate(
        {
            "kind": "openai-compatible",
            "model": "test-model",
            "base_url": "https://mock.invalid/v1",
            "api_key_file": "~/.config/codepilot/credentials.yaml",
        }
    )

    assert provider.api_key_file == Path("~/.config/codepilot/credentials.yaml")


def test_environment_overrides_file_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """临时环境变量应优先，文件即使不存在也不能阻止 CI 或单次覆盖。"""
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv("CODEPILOT_TEST_KEY", "temporary-secret")

    assert resolve_provider_api_key(_real_provider(missing)) == "temporary-secret"


@pytest.mark.skipif(os.name != "posix", reason="文件权限位约束只适用于 POSIX")
def test_credential_file_rejects_group_or_other_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """组或其他用户可访问的文件必须在解析秘密前被拒绝。"""
    path = tmp_path / "credentials.yaml"
    _write_credential(path)
    path.chmod(0o644)
    monkeypatch.delenv("CODEPILOT_TEST_KEY", raising=False)

    with pytest.raises(ValueError, match="0600"):
        resolve_provider_api_key(_real_provider(path))


@pytest.mark.parametrize(
    "content,error_type",
    [
        ("api_key: [", ValueError),
        ("- test-secret\n", ValueError),
        ("api_key: '   '\n", ValidationError),
        ("api_key: test-secret\nextra: forbidden\n", ValidationError),
    ],
)
def test_credential_file_rejects_invalid_yaml_or_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    error_type: type[Exception],
) -> None:
    """损坏结构、空密钥和未知字段都不能进入 Provider。"""
    path = tmp_path / "credentials.yaml"
    _write_credential(path, content)
    monkeypatch.delenv("CODEPILOT_TEST_KEY", raising=False)

    with pytest.raises(error_type):
        resolve_provider_api_key(_real_provider(path))


def test_credential_model_repr_does_not_reveal_secret() -> None:
    """凭据对象的 repr 必须使用 Pydantic 脱敏表示。"""
    credential = CredentialFile.model_validate({"api_key": "test-secret"})

    assert "test-secret" not in repr(credential)
    assert "**********" in repr(credential)
